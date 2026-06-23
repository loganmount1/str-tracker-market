#!/usr/bin/env python3
"""Collect market-wide pricing data from Airbnb search results for Mt Hood."""

import sys
import re
import json
import logging
import statistics
from datetime import date, timedelta

sys.path.insert(0, ".")

from src.models.database import init_database
from src.utils.http import create_session, make_request, RateLimiter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

MAX_PAGES = 50  # per search query — 881 homes ÷ ~20/page = ~44 pages

# Multiple search queries to cover the full Mt Hood corridor
SEARCH_QUERIES = [
    "https://www.airbnb.com/s/Mt-Hood--Oregon/homes",
    "https://www.airbnb.com/s/Welches--Oregon/homes",
    "https://www.airbnb.com/s/Brightwood--Oregon/homes",
    "https://www.airbnb.com/s/Zigzag--Oregon/homes",
    "https://www.airbnb.com/s/Government-Camp--Oregon/homes",
    "https://www.airbnb.com/s/Rhododendron--Oregon/homes",
]

# Search multiple date windows to find properties blocked near-term
# but available in peak season (captures more of the 881 total homes)
SEARCH_DATE_OFFSETS = [
    (14, 2),    # 2 weeks out, 2-night stay
    (120, 2),   # ~4 months out (summer), 2-night stay
    (210, 2),   # ~7 months out (fall), 2-night stay
]

# Airbnb caps each search at ~300 results. Slicing by nightly price band makes
# each (town x band) query return a DIFFERENT set under that cap, so we surface
# listings the plain search misses (verified: the unfiltered search skips the
# cheap end entirely). None = the original unfiltered pass. Bands overlap the
# cap boundaries slightly on purpose; the global dedup handles repeats.
# To bound runtime, price bands are only applied on the peak date window
# (PRICE_BAND_WINDOW_DAYS); other windows use a single unfiltered pass.
SEARCH_PRICE_BANDS = [
    None,            # unfiltered (original behavior)
    (0, 200),        # budget
    (200, 350),
    (350, 500),
    (500, None),     # premium lodges (Mt Hood has many large high-end homes)
]
PRICE_BAND_WINDOW_DAYS = 120  # apply the band sweep only on this date offset

# Only include listings from actual Mt Hood communities
MT_HOOD_LOCATIONS = [
    "Rhododendron", "Mount Hood Village", "Government Camp",
    "Welches", "Brightwood", "Sandy", "Zigzag", "Wemme",
]

# Exclude non-whole-home listing types (rooms, tents, etc.)
EXCLUDED_PREFIXES = [
    "Room in ", "Rooms in ", "Tent in ", "Yurt in ",
    "Guest suite in ", "Treehouse in ",
]


def extract_search_data(html):
    """Extract search results from the deferred state JSON in the HTML."""
    match = re.search(r'id="data-deferred-state-0"[^>]*>([^<]+)', html)
    if not match:
        return None, None

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        logger.error("Failed to parse deferred state JSON")
        return None, None

    # Navigate to search results — the structure varies but follows a pattern
    niobe = data.get("niobeClientData", [])
    if not niobe:
        return None, None

    # Search through niobe data for StaysSearch results
    results = []
    pagination_cursors = None

    def find_search_results(obj, depth=0):
        """Recursively find search result items in the nested JSON."""
        nonlocal pagination_cursors
        if depth > 15:
            return
        if isinstance(obj, dict):
            # Check for search result item
            if obj.get("__typename") == "StaySearchResult":
                results.append(obj)
                return
            # Check for pagination
            if "paginationInfo" in obj:
                pagination_cursors = obj["paginationInfo"]
            if "pageCursors" in obj:
                pagination_cursors = obj
            for v in obj.values():
                find_search_results(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                find_search_results(item, depth + 1)

    find_search_results(data)
    return results, pagination_cursors


def parse_amenity_flags(text):
    """Detect amenity keywords in listing text."""
    if not text:
        return False, False
    t = text.lower()
    hot_tub = bool(re.search(r'hot\s*tub|jacuzzi', t))
    pet_friendly = bool(re.search(r'pet[\s-]*friend|dog[\s-]*friend|dogs?\s*ok|pets?\s*(?:ok|welcome|allowed)', t))
    return hot_tub, pet_friendly


def parse_listing(result):
    """Parse a StaySearchResult into a listing dict."""
    listing = {
        "listing_id": None,
        "airbnb_url": None,
        "cover_photo": None,
        "host_name": None,
        "title": None,
        "subtitle": None,
        "location": None,
        "price_per_night": None,
        "total_price": None,
        "nights": None,
        "rating": None,
        "review_count": None,
        "latitude": None,
        "longitude": None,
        "bedrooms": None,
        "beds": None,
        "est_guests": None,
        "has_hot_tub": 0,
        "has_pet_friendly": 0,
    }

    # Extract listing ID from demandStayListing (base64 encoded)
    dsl = result.get("demandStayListing", {})
    encoded_id = dsl.get("id", "")
    if encoded_id:
        try:
            import base64
            decoded = base64.b64decode(encoded_id).decode()
            # Format: "DemandStayListing:12345"
            numeric_id = decoded.split(":")[-1]
            listing["listing_id"] = numeric_id
            listing["airbnb_url"] = f"https://www.airbnb.com/rooms/{numeric_id}"
        except Exception:
            pass

    # Extract cover photo
    pics = result.get("contextualPictures", [])
    if pics:
        listing["cover_photo"] = pics[0].get("picture", "")

    # Title is type+location format: "Cabin in Rhododendron"
    listing["title"] = result.get("title")

    # Subtitle is the descriptive listing name with amenity info
    listing["subtitle"] = result.get("subtitle")

    # Fallback: if no title, try other sources
    if not listing["title"]:
        listing["title"] = result.get("listing", {}).get("name") or result.get("listingTitle")

    # Parse location from title: "Cabin in Rhododendron" → "Rhododendron"
    title = listing.get("title") or ""
    loc_match = re.search(r'\b(?:in|at)\s+(.+)$', title)
    if loc_match:
        listing["location"] = loc_match.group(1).strip()

    # Parse amenity flags from subtitle (descriptive name with amenities)
    combined = f"{listing.get('subtitle', '')} {listing.get('title', '')}"
    hot_tub, pet_friendly = parse_amenity_flags(combined)
    listing["has_hot_tub"] = 1 if hot_tub else 0
    listing["has_pet_friendly"] = 1 if pet_friendly else 0

    # Price extraction
    price_data = result.get("pricingQuote") or result.get("structuredDisplayPrice") or {}

    # Try structuredDisplayPrice path
    primary = price_data.get("primaryLine", {})
    if primary.get("price"):
        price_str = primary["price"]
        # Parse "$1,165" format
        price_match = re.search(r'\$?([\d,]+)', price_str)
        if price_match:
            listing["total_price"] = float(price_match.group(1).replace(",", ""))

    qualifier = primary.get("qualifier", "")
    nights_match = re.search(r'(\d+)\s*night', qualifier)
    if nights_match:
        listing["nights"] = int(nights_match.group(1))

    # Try to get per-night rate from explanation data
    explanation = price_data.get("explanationData", {})
    price_details = explanation.get("priceDetails", [])
    for detail in price_details:
        desc = detail.get("description", "")
        # Parse "5 nights x $233.00" format
        rate_match = re.search(r'(\d+)\s*nights?\s*x\s*\$?([\d,.]+)', desc)
        if rate_match:
            listing["nights"] = int(rate_match.group(1))
            listing["price_per_night"] = float(rate_match.group(2).replace(",", ""))
            break
        # Parse "$233 x 5 nights" format
        rate_match2 = re.search(r'\$?([\d,.]+)\s*x\s*(\d+)\s*night', desc)
        if rate_match2:
            listing["price_per_night"] = float(rate_match2.group(1).replace(",", ""))
            listing["nights"] = int(rate_match2.group(2))
            break

    # Compute per-night if we have total and nights but no per-night
    if not listing["price_per_night"] and listing["total_price"] and listing["nights"]:
        listing["price_per_night"] = listing["total_price"] / listing["nights"]

    # Try pricingQuote.rate path
    if not listing["price_per_night"]:
        rate = price_data.get("rate", {})
        amount = rate.get("amount")
        if amount:
            listing["price_per_night"] = float(amount)

    # Try priceString directly
    if not listing["price_per_night"]:
        price_str = price_data.get("priceString") or price_data.get("price")
        if price_str and isinstance(price_str, str):
            match = re.search(r'\$?([\d,]+)', price_str)
            if match:
                listing["price_per_night"] = float(match.group(1).replace(",", ""))

    # Rating
    rating_str = result.get("avgRatingLocalized") or ""
    if rating_str and rating_str != "New":
        try:
            listing["rating"] = float(rating_str)
        except ValueError:
            pass

    # Rating from a11y label: "4.98 out of 5 average rating, 54 reviews"
    a11y = result.get("avgRatingA11yLabel", "")
    if a11y:
        r_match = re.search(r'([\d.]+)\s*out\s*of\s*5', a11y)
        if r_match:
            listing["rating"] = float(r_match.group(1))
        c_match = re.search(r'(\d+)\s*review', a11y)
        if c_match:
            listing["review_count"] = int(c_match.group(1))

    # Bedrooms and beds from structuredContent.primaryLine
    sc = result.get("structuredContent", {})
    for item in sc.get("primaryLine", []):
        body = item.get("body", "")
        br_match = re.match(r"(\d+)\s*bedroom", body)
        if br_match:
            listing["bedrooms"] = int(br_match.group(1))
        # Match "X beds" or "X king bed" / "X queen beds" etc.
        bed_match = re.match(r"(\d+)\s*(?:king\s*|queen\s*|twin\s*|double\s*|bunk\s*|sofa\s*|single\s*)?beds?$", body)
        if bed_match:
            listing["beds"] = int(bed_match.group(1))
    # Studio = 0 bedrooms
    if listing["bedrooms"] is None:
        for item in sc.get("primaryLine", []):
            if "studio" in item.get("body", "").lower():
                listing["bedrooms"] = 0
                break
    # Estimate guest capacity: beds × 2 (reasonable average)
    if listing["beds"]:
        listing["est_guests"] = listing["beds"] * 2

    # Coordinates
    loc = result.get("listing", {}).get("coordinate") or {}
    if not loc:
        # Try nested path
        stay = result.get("demandStayListing", {})
        loc = stay.get("location", {}).get("coordinate", {})
    listing["latitude"] = loc.get("latitude")
    listing["longitude"] = loc.get("longitude")

    return listing


def extract_next_cursor(pagination):
    """Extract the next page cursor from pagination data."""
    if not pagination:
        return None

    # Try nextPageCursor directly
    if isinstance(pagination, dict):
        cursor = pagination.get("nextPageCursor")
        if cursor:
            return cursor
        # Try pageCursors array
        cursors = pagination.get("pageCursors", [])
        if cursors and len(cursors) > 1:
            return cursors[-1]
        # Try hasNextPage + nextCursor
        if pagination.get("hasNextPage"):
            return pagination.get("nextCursor") or pagination.get("endCursor")

    return None


def fetch_search_page(session, rate_limiter, url, params=None):
    """Fetch a search page and extract listings."""
    rate_limiter.wait("airbnb.com")

    response = make_request(session, "GET", url, params=params)
    html = response.text

    results, pagination = extract_search_data(html)
    if not results:
        logger.warning("No search results found in page")
        return [], None

    listings = []
    for result in results:
        listing = parse_listing(result)
        if listing["price_per_night"] and listing["price_per_night"] > 0:
            listings.append(listing)

    next_cursor = extract_next_cursor(pagination)
    return listings, next_cursor


def main():
    db = init_database("data/tracker.db")
    today = date.today().isoformat()

    session = create_session()
    rate_limiter = RateLimiter(delays={"airbnb.com": 3.0}, jitter=1.5)

    logger.info("Collecting Mt Hood market data from Airbnb search...")
    logger.info(f"  Running {len(SEARCH_QUERIES)} queries × {len(SEARCH_DATE_OFFSETS)} date windows, up to {MAX_PAGES} pages each")

    all_listings = []
    # Track seen listings globally across all queries to deduplicate
    # Use subtitle (actual listing name) since title is just "Cabin in Rhododendron" format
    # Fall back to coordinates for listings without subtitles
    seen_keys = set()

    for days_out, nights in SEARCH_DATE_OFFSETS:
        checkin = date.today() + timedelta(days=days_out)
        checkout = checkin + timedelta(days=nights)
        window_params = {
            "checkin": checkin.isoformat(),
            "checkout": checkout.isoformat(),
        }
        logger.info(f"\n--- Date window: {checkin} to {checkout} ({days_out} days out) ---")

        # Apply the price-band sweep only on the peak window; other windows
        # do a single unfiltered pass (keeps total request volume bounded).
        price_bands = SEARCH_PRICE_BANDS if days_out == PRICE_BAND_WINDOW_DAYS else [None]

        for band in price_bands:
            base_params = dict(window_params)
            if band is not None:
                lo, hi = band
                if lo is not None:
                    base_params["price_min"] = lo
                if hi is not None:
                    base_params["price_max"] = hi
                band_label = f"${lo or 0}-{hi or '+'}"
                logger.info(f"\n  Price band: {band_label}")

            for search_url in SEARCH_QUERIES:
                query_name = search_url.split("/s/")[1].split("/")[0]
                logger.info(f"\n  Search: {query_name}")
                page = 1
                next_cursor = None
                consecutive_dupes = 0

                while page <= MAX_PAGES:
                    logger.info(f"    Page {page}...")

                    params = dict(base_params)
                    if next_cursor:
                        params["cursor"] = next_cursor

                    try:
                        listings, next_cursor = fetch_search_page(session, rate_limiter, search_url, params)

                        if not listings:
                            logger.info(f"    No listings on page {page}, stopping")
                            break

                        # Deduplicate using subtitle (actual listing name) or coordinates
                        # "title" is just type+location like "Cabin in Rhododendron" — not unique
                        new_listings = []
                        for l in listings:
                            # Build a unique key: prefer subtitle, fall back to rounded coords
                            key = l.get("subtitle") or ""
                            if not key and l.get("latitude") and l.get("longitude"):
                                key = f"{round(l['latitude'],4)},{round(l['longitude'],4)}"
                            if not key:
                                key = f"{l['title']}_{l['price_per_night']}"
                            if key not in seen_keys:
                                seen_keys.add(key)
                                new_listings.append(l)

                        if not new_listings:
                            consecutive_dupes += 1
                            if consecutive_dupes >= 3:
                                logger.info(f"    3 consecutive dupe pages, stopping")
                                break
                            logger.info(f"    All duplicates on page {page}, continuing...")
                            if not next_cursor:
                                break
                            page += 1
                            continue

                        consecutive_dupes = 0
                        all_listings.extend(new_listings)
                        logger.info(f"    Got {len(new_listings)} new listings ({len(all_listings)} total)")

                        if not next_cursor:
                            logger.info("    No more pages")
                            break

                        page += 1

                    except Exception as e:
                        logger.error(f"    Error on page {page}: {type(e).__name__}: {e}")
                        break

    if not all_listings:
        logger.error("No listings collected. Check if Airbnb is blocking requests.")
        db.close()
        return

    # Store ALL individual listings (unfiltered, for reference)
    logger.info(f"Storing {len(all_listings)} raw listings...")
    for listing in all_listings:
        try:
            db.execute("""
                INSERT OR REPLACE INTO market_listings
                    (snapshot_date, title, price_per_night, total_price, nights,
                     rating, review_count, latitude, longitude, bedrooms,
                     beds, est_guests, location, subtitle,
                     has_hot_tub, has_pet_friendly, is_tracked,
                     listing_id, airbnb_url, cover_photo, host_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            """, (
                today, listing["title"], listing["price_per_night"],
                listing["total_price"], listing["nights"],
                listing["rating"], listing["review_count"],
                listing["latitude"], listing["longitude"],
                listing["bedrooms"],
                listing["beds"], listing["est_guests"],
                listing["location"], listing["subtitle"],
                listing["has_hot_tub"], listing["has_pet_friendly"],
                listing.get("listing_id"), listing.get("airbnb_url"),
                listing.get("cover_photo"), listing.get("host_name"),
            ))
        except Exception as e:
            logger.debug(f"  Skip duplicate: {listing['title'][:40]} - {e}")
    db.commit()

    # Filter to Mt Hood area whole-home listings only
    def is_mt_hood_whole_home(listing):
        title = listing.get("title") or ""
        # Exclude non-whole-home types
        for prefix in EXCLUDED_PREFIXES:
            if title.startswith(prefix):
                return False
        # Must be in a Mt Hood community
        for loc in MT_HOOD_LOCATIONS:
            if f" in {loc}" in title:
                return True
        return False

    filtered = [l for l in all_listings if is_mt_hood_whole_home(l)]
    excluded_count = len(all_listings) - len(filtered)
    logger.info(f"Filtered to {len(filtered)} Mt Hood whole-home listings "
                f"(excluded {excluded_count} outside area or non-whole-home)")

    if not filtered:
        logger.error("No Mt Hood listings after filtering!")
        db.close()
        return

    # Compute aggregates from filtered listings
    prices = [l["price_per_night"] for l in filtered]
    prices.sort()

    avg_price = statistics.mean(prices)
    median_price = statistics.median(prices)
    min_price = min(prices)
    max_price = max(prices)
    p25 = prices[len(prices) // 4] if len(prices) >= 4 else min_price
    p75 = prices[3 * len(prices) // 4] if len(prices) >= 4 else max_price

    logger.info(f"\nMarket Summary ({len(prices)} whole-home listings in Mt Hood area):")
    logger.info(f"  Avg Price/Night: ${avg_price:,.0f}")
    logger.info(f"  Median Price:    ${median_price:,.0f}")
    logger.info(f"  Range:           ${min_price:,.0f} - ${max_price:,.0f}")
    logger.info(f"  25th/75th:       ${p25:,.0f} / ${p75:,.0f}")

    # Store snapshot (filtered data only)
    db.execute("""
        INSERT OR REPLACE INTO market_snapshots
            (snapshot_date, total_listings, avg_price, median_price,
             min_price, max_price, p25_price, p75_price)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (today, len(prices), avg_price, median_price,
          min_price, max_price, p25, p75))
    db.commit()

    logger.info(f"\nComplete: {len(all_listings)} total scraped, "
                f"{len(filtered)} Mt Hood homes stored in snapshot")
    db.close()


if __name__ == "__main__":
    main()

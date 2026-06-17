"""Airbnb data collector using the homes_pdp_availability_calendar API,
StaysPdpSections GraphQL API for pricing, and listing page scraping for details."""

import base64
import re
import json
import logging
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional

from .base import BaseCollector, CalendarDay, PropertyDetails, ReviewData
from ..utils.http import make_request, create_session, RateLimiter, StructureChangedError

logger = logging.getLogger(__name__)

# Public client-side API key used by Airbnb's own web client.
# Find the current key via browser DevTools on any Airbnb page.
AIRBNB_API_KEY = "d306zoyjsyarp7ifhu67rjxn52tv0t20"
AIRBNB_API_V2 = "https://www.airbnb.com/api/v2"
AIRBNB_API_V3 = "https://www.airbnb.com/api/v3"


class AirbnbCollector(BaseCollector):
    platform = "airbnb"

    def __init__(self, rate_limiter: RateLimiter):
        self.rate_limiter = rate_limiter
        self.session = create_session()
        self._pdp_sections_hash = None  # Cached GraphQL hash

    def extract_id(self, url: str) -> str:
        """Extract listing ID from Airbnb URL."""
        match = re.search(r'/rooms/(?:plus/)?(\d+)', url)
        if match:
            return match.group(1)
        raise ValueError(f"Cannot extract Airbnb listing ID from: {url}")

    def collect_calendar(self, property_id: str, months: int = 12) -> list[CalendarDay]:
        """Fetch calendar availability via /api/v2/homes_pdp_availability_calendar.

        Returns day-by-day availability for up to 12 months.
        Note: Pricing data is not available from this endpoint.
        """
        self.rate_limiter.wait("airbnb.com")

        now = datetime.now()
        params = {
            "key": AIRBNB_API_KEY,
            "listing_id": property_id,
            "month": now.month,
            "year": now.year,
            "count": months,
            "currency": "USD",
        }

        logger.debug(f"Fetching calendar for Airbnb listing {property_id}")
        response = make_request(
            self.session, "GET",
            f"{AIRBNB_API_V2}/homes_pdp_availability_calendar",
            params=params,
            headers={"X-Airbnb-Api-Key": AIRBNB_API_KEY},
        )
        data = response.json()

        calendar_months = data.get("calendar_months", [])
        if not calendar_months:
            raise StructureChangedError(
                "No 'calendar_months' in Airbnb calendar response"
            )

        days = []
        for month_data in calendar_months:
            for day_data in month_data.get("days", []):
                # Extract price if available (may be empty dict)
                price_obj = day_data.get("price", {})
                price = None
                if isinstance(price_obj, dict) and price_obj:
                    price = (price_obj.get("local_price")
                             or price_obj.get("native_price")
                             or price_obj.get("amount"))
                elif isinstance(price_obj, (int, float)):
                    price = float(price_obj)

                # A date is truly available only if both available AND bookable.
                # bookable=False means gap nights that can't be reserved due to
                # min-night requirements between existing bookings.
                is_available = day_data.get("available", False)
                is_bookable = day_data.get("bookable", True)

                days.append(CalendarDay(
                    date=day_data["date"],
                    available=is_available and is_bookable,
                    price=float(price) if price else None,
                    min_nights=day_data.get("min_nights"),
                ))

        return days

    def collect_details(self, property_id: str) -> PropertyDetails:
        """Collect property details by scraping the listing page.

        Extracts JSON-LD structured data and SSR data from the HTML.
        """
        self.rate_limiter.wait("airbnb.com")

        url = f"https://www.airbnb.com/rooms/{property_id}"
        logger.debug(f"Fetching details for Airbnb listing {property_id}")
        response = make_request(self.session, "GET", url)
        html = response.text

        # Extract JSON-LD structured data (VacationRental)
        json_ld = self._extract_json_ld(html)

        # Extract SSR data for reviews and host info
        ssr_data = self._extract_ssr_data(html)

        return self._build_details(json_ld, ssr_data)

    def _extract_json_ld(self, html: str) -> dict:
        """Extract VacationRental JSON-LD from the page."""
        for match in re.finditer(
            r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL
        ):
            try:
                data = json.loads(match.group(1))
                if isinstance(data, dict) and data.get("@type") == "VacationRental":
                    return data
            except (json.JSONDecodeError, TypeError):
                continue
        return {}

    def _extract_ssr_data(self, html: str) -> dict:
        """Extract server-side rendered data from deferred state or scripts."""
        # Try deferred state first
        deferred_match = re.search(
            r'id="data-deferred-state-0"[^>]*>([^<]+)', html
        )
        if deferred_match:
            try:
                data = json.loads(deferred_match.group(1))
                niobe = data.get("niobeClientData", [])
                if niobe and isinstance(niobe[0], list) and len(niobe[0]) >= 2:
                    return niobe[0][1].get("data", {})
            except (json.JSONDecodeError, TypeError, IndexError):
                pass

        # Fallback: try script tags with niobeClientData
        for match in re.finditer(r'<script[^>]*>(.*?)</script>', html, re.DOTALL):
            script = match.group(1).strip()
            if '"niobeClientData"' in script and len(script) > 10000:
                try:
                    data = json.loads(script)
                    niobe = data.get("niobeClientData", [])
                    if niobe and isinstance(niobe[0], list) and len(niobe[0]) >= 2:
                        return niobe[0][1].get("data", {})
                except (json.JSONDecodeError, TypeError, IndexError):
                    continue
        return {}

    def _build_details(self, json_ld: dict, ssr_data: dict) -> PropertyDetails:
        """Build PropertyDetails from JSON-LD and SSR data."""
        # From JSON-LD
        name = json_ld.get("name")
        agg_rating = json_ld.get("aggregateRating", {})
        overall_rating = agg_rating.get("ratingValue")
        review_count = agg_rating.get("ratingCount")
        latitude = json_ld.get("latitude")
        longitude = json_ld.get("longitude")
        address = json_ld.get("address", {})
        occupancy = json_ld.get("containsPlace", {}).get(
            "occupancy", {}
        ).get("value")

        # From SSR data - get review details
        reviews_section = self._find_section(ssr_data, "StayPdpReviewsSection")
        if reviews_section:
            overall_rating = reviews_section.get("overallRating", overall_rating)
            review_count = reviews_section.get("overallCount", review_count)

        # Extract host name and superhost status
        host_name = None
        superhost = None
        ssr_str = json.dumps(ssr_data) if ssr_data else ""

        host_section = self._find_section(ssr_data, "MeetYourHostSection")
        if host_section:
            sh_text = host_section.get("superhostTitleText") or ""
            name_match = re.match(r'(\w+(?:\s+\w+)*?)\s+is\s+a\s+Superhost', sh_text)
            if name_match:
                host_name = name_match.group(1)
                superhost = True
        # Fallback: "Hosted by X" in SSR
        if not host_name:
            hosted_match = re.search(r'"Hosted by ([^"]+)"', ssr_str)
            if hosted_match:
                host_name = hosted_match.group(1)
        # Fallback: JSON-LD author
        if not host_name:
            author = json_ld.get("author", {})
            if isinstance(author, dict):
                host_name = author.get("name")

        # Extract thumbnail from JSON-LD image array
        thumbnail_url = None
        images = json_ld.get("image", [])
        if isinstance(images, list) and images:
            thumbnail_url = images[0]
        elif isinstance(images, str):
            thumbnail_url = images

        # Extract bedrooms from SSR text (look for "X bedrooms" or "X bedroom")
        bedrooms = None
        br_matches = re.findall(r'(\d+)\s+bedroom', ssr_str)
        if br_matches:
            # Take the most common value > 0, or the first > 0
            br_vals = [int(v) for v in br_matches if int(v) > 0]
            if br_vals:
                bedrooms = max(set(br_vals), key=br_vals.count)

        return PropertyDetails(
            name=name,
            bedrooms=bedrooms,
            max_guests=int(occupancy) if occupancy else None,
            latitude=float(latitude) if latitude else None,
            longitude=float(longitude) if longitude else None,
            overall_rating=float(overall_rating) if overall_rating else None,
            review_count=int(review_count) if review_count else None,
            superhost=superhost,
            host_name=host_name,
            thumbnail_url=thumbnail_url,
        )

    def _find_section(self, ssr_data: dict, typename: str) -> dict:
        """Find a section by __typename in SSR data."""
        try:
            sections = (ssr_data.get("presentation", {})
                        .get("stayProductDetailPage", {})
                        .get("sections", {})
                        .get("sections", []))
            for sw in sections:
                s = sw.get("section", {})
                if s and s.get("__typename") == typename:
                    return s
        except (AttributeError, TypeError):
            pass
        return {}

    def collect_reviews(self, property_id: str) -> ReviewData:
        """Collect review data from the listing page."""
        # Reviews are collected as part of collect_details via SSR data.
        # Do a lightweight page fetch to get review info.
        self.rate_limiter.wait("airbnb.com")

        url = f"https://www.airbnb.com/rooms/{property_id}"
        logger.debug(f"Fetching reviews for Airbnb listing {property_id}")

        try:
            response = make_request(self.session, "GET", url)
            html = response.text

            json_ld = self._extract_json_ld(html)
            ssr_data = self._extract_ssr_data(html)

            agg = json_ld.get("aggregateRating", {})
            reviews_section = self._find_section(ssr_data, "StayPdpReviewsSection")

            overall_rating = (reviews_section.get("overallRating")
                              or agg.get("ratingValue"))
            review_count = (reviews_section.get("overallCount")
                            or agg.get("ratingCount"))

            return ReviewData(
                overall_rating=float(overall_rating) if overall_rating else None,
                review_count=int(review_count) if review_count else None,
            )
        except Exception as e:
            logger.warning(f"Failed to fetch reviews for {property_id}: {e}")
            return ReviewData()

    # ── Pricing via StaysPdpSections GraphQL API ──

    # Known-working hash for the old-style query variables
    _FALLBACK_PDP_HASH = "817db68da8bfce0eeea799a4531a191ea2aa0238830f398b9c16e6c98d3249fa"

    def _discover_pdp_hash(self) -> str:
        """Return the StaysPdpSections persisted query hash.
        Uses a known-working hash first; falls back to auto-discovery if it stops working."""
        if self._pdp_sections_hash:
            return self._pdp_sections_hash

        # Use the known-working hash that matches our old-style query variables.
        # The auto-discovered hash from JS bundles may use a newer query schema
        # with different variable requirements.
        self._pdp_sections_hash = self._FALLBACK_PDP_HASH
        logger.info(f"Using known StaysPdpSections hash: {self._pdp_sections_hash[:16]}...")
        return self._pdp_sections_hash

    def _discover_pdp_hash_from_bundle(self) -> str:
        """Auto-discover the StaysPdpSections persisted query hash from Airbnb's JS bundles.
        Only used if the known hash stops working."""
        self.rate_limiter.wait("airbnb.com")

        # Fetch a listing page to find the JS bundle URL
        response = make_request(
            self.session, "GET", "https://www.airbnb.com/rooms/20477707"
        )
        html = response.text

        # Find PdpPlatformRoute bundle URL
        bundle_match = re.search(
            r'(https://a0\.muscache\.com/airbnb/static/packages/web/[^"]*PdpPlatformRoute[^"]*\.js)',
            html
        )
        if not bundle_match:
            # Try alternative pattern
            bundle_match = re.search(
                r'"(/airbnb/static/packages/web/[^"]*PdpPlatformRoute[^"]*\.js)"',
                html
            )
            if bundle_match:
                bundle_url = "https://a0.muscache.com" + bundle_match.group(1)
            else:
                raise StructureChangedError("Cannot find PdpPlatformRoute JS bundle")
        else:
            bundle_url = bundle_match.group(1)

        logger.debug(f"Found bundle: {bundle_url}")
        self.rate_limiter.wait("airbnb.com")
        bundle_resp = make_request(self.session, "GET", bundle_url)
        bundle_js = bundle_resp.text

        # Extract the StaysPdpSections operation hash
        hash_match = re.search(
            r"""['"]StaysPdpSections['"][^}]*operationId['":\s]*['"]([a-f0-9]{64})['"]""",
            bundle_js
        )
        if not hash_match:
            # Try alternative patterns
            hash_match = re.search(
                r'name\s*:\s*["\']StaysPdpSections["\'].*?operationId\s*:\s*["\']([a-f0-9]{64})["\']',
                bundle_js, re.DOTALL
            )
        if not hash_match:
            # Try reversed order: operationId before name
            hash_match = re.search(
                r'operationId\s*:\s*["\']([a-f0-9]{64})["\'].*?name\s*:\s*["\']StaysPdpSections["\']',
                bundle_js, re.DOTALL
            )

        if not hash_match:
            raise StructureChangedError(
                "Cannot find StaysPdpSections hash in JS bundle"
            )

        self._pdp_sections_hash = hash_match.group(1)
        logger.info(f"Discovered StaysPdpSections hash: {self._pdp_sections_hash[:16]}...")
        return self._pdp_sections_hash

    def collect_price_for_date(self, property_id: str, check_in: date,
                               min_nights: int = 1) -> Optional[float]:
        """Get the nightly rate for a specific stay via GraphQL.

        Uses min_nights to satisfy minimum stay requirements.
        Returns the per-night price in USD, or None if unavailable.
        """
        op_hash = self._discover_pdp_hash()
        self.rate_limiter.wait("airbnb.com")

        stay_nights = max(min_nights, 1)
        check_out = check_in + timedelta(days=stay_nights)
        b64_stay = base64.b64encode(
            f"StayListing:{property_id}".encode()
        ).decode()
        b64_demand = base64.b64encode(
            f"DemandStayListing:{property_id}".encode()
        ).decode()

        variables = {
            "categoryTag": None,
            "demandStayListingId": b64_demand,
            "federatedSearchId": None,
            "id": b64_stay,
            "includeGpDescriptionFragment": True,
            "includeGpHighlightsFragment": True,
            "includeGpNavFragment": True,
            "includeGpNavMobileFragment": True,
            "includeGpReportToAirbnbFragment": False,
            "includeGpReviewsEmptyFragment": True,
            "includeGpReviewsFragment": True,
            "includeGpReviewsHighlightBannerFragment": True,
            "includeGpTitleFragment": True,
            "includeHotelFragments": True,
            "includePdpMigrationDescriptionFragment": False,
            "includePdpMigrationHighlightsFragment": False,
            "includePdpMigrationNavFragment": False,
            "includePdpMigrationNavMobileFragment": False,
            "includePdpMigrationReportToAirbnbFragment": True,
            "includePdpMigrationReviewsEmptyFragment": False,
            "includePdpMigrationReviewsFragment": False,
            "includePdpMigrationReviewsHighlightBannerFragment": False,
            "includePdpMigrationTitleFragment": False,
            "p3ImpressionId": f"p3_{int(datetime.now().timestamp())}_strtracker",
            "pdpSectionsRequest": {
                "adults": "2",
                "amenityFilters": None,
                "bypassTargetings": False,
                "categoryTag": None,
                "causeId": None,
                "checkIn": check_in.isoformat(),
                "checkOut": check_out.isoformat(),
                "children": None,
                "disasterId": None,
                "discountedGuestFeeVersion": None,
                "federatedSearchId": None,
                "forceBoostPriorityMessageType": None,
                "hostPreview": False,
                "infants": None,
                "interactionType": None,
                "layouts": ["SIDEBAR", "SINGLE_COLUMN"],
                "p3ImpressionId": f"p3_{int(datetime.now().timestamp())}_strtracker",
                "pdpTypeOverride": None,
                "pets": 0,
                "photoId": None,
                "preview": False,
                "previousStateCheckIn": None,
                "previousStateCheckOut": None,
                "priceDropSource": None,
                "privateBooking": False,
                "promotionUuid": None,
                "relaxedAmenityIds": None,
                "searchId": None,
                "sectionIds": ["BOOK_IT_SIDEBAR"],
                "selectedCancellationPolicyId": None,
                "selectedRatePlanId": None,
                "splitStays": None,
                "staysBookingMigrationEnabled": False,
                "translateUgc": None,
                "useNewSectionWrapperApi": False,
            },
            "photoId": None,
        }

        extensions = {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": op_hash,
            }
        }

        params = {
            "operationName": "StaysPdpSections",
            "locale": "en",
            "currency": "USD",
            "variables": json.dumps(variables),
            "extensions": json.dumps(extensions),
        }

        headers = {
            "X-Airbnb-Api-Key": AIRBNB_API_KEY,
            "Origin": "https://www.airbnb.com",
            "Referer": f"https://www.airbnb.com/rooms/{property_id}",
        }

        try:
            response = make_request(
                self.session, "GET",
                f"{AIRBNB_API_V3}/StaysPdpSections/{op_hash}",
                params=params,
                headers=headers,
                timeout=15,
            )
            data = response.json()
            # If the discovered hash returns errors, fall back to known-working hash
            if data.get("errors") and op_hash != self._FALLBACK_PDP_HASH:
                logger.debug(f"Discovered hash failed, falling back to known hash")
                self._pdp_sections_hash = self._FALLBACK_PDP_HASH
                params["extensions"] = json.dumps({
                    "persistedQuery": {"version": 1, "sha256Hash": self._FALLBACK_PDP_HASH}
                })
                self.rate_limiter.wait("airbnb.com")
                response = make_request(
                    self.session, "GET",
                    f"{AIRBNB_API_V3}/StaysPdpSections/{self._FALLBACK_PDP_HASH}",
                    params=params,
                    headers=headers,
                    timeout=15,
                )
                data = response.json()
            return self._extract_price_from_pdp(data, nights=stay_nights)
        except Exception as e:
            logger.debug(f"Price query failed for {property_id} on {check_in}: {e}")
            return None

    def _extract_price_from_pdp(self, data: dict,
                               nights: int = 1) -> Optional[float]:
        """Extract nightly price from StaysPdpSections response.

        If the stay was for multiple nights, divides total by nights
        to get per-night rate.
        """
        try:
            sections = (data.get("data", {})
                        .get("presentation", {})
                        .get("stayProductDetailPage", {})
                        .get("sections", {})
                        .get("sections", []))

            for section_wrapper in sections:
                section = section_wrapper.get("section", {})
                if not section:
                    continue

                typename = section.get("__typename", "")
                if typename != "BookItSection":
                    continue

                if not section.get("available", False):
                    return None

                # Try to get exact per-night price from breakdown
                display_price = section.get("structuredDisplayPrice", {})
                explanation = display_price.get("explanationData", {})
                details = explanation.get("priceDetails", [])

                for group in details:
                    for item in group.get("items", []):
                        desc = item.get("description", "")
                        price_str = item.get("priceString", "")
                        # Format: description="2 nights x $201.50", priceString="$403.00"
                        # Extract per-night rate from description (after "x")
                        if desc and "night" in desc.lower():
                            per_night = re.search(r'x\s*\$?([\d,]+\.?\d*)', desc)
                            if per_night:
                                return float(per_night.group(1).replace(",", ""))
                            # Fallback: divide priceString total by nights
                            if price_str:
                                total_val = re.search(r'\$?([\d,]+\.?\d*)', price_str)
                                if total_val:
                                    return float(total_val.group(1).replace(",", "")) / max(nights, 1)

                # Fallback: parse the primary display price and divide by nights
                primary = display_price.get("primaryLine", {})
                price_text = primary.get("price", "")
                if price_text:
                    price_val = re.search(r'\$?([\d,]+\.?\d*)', price_text)
                    if price_val:
                        total = float(price_val.group(1).replace(",", ""))
                        return total / max(nights, 1)

        except (KeyError, TypeError, IndexError, ValueError) as e:
            logger.debug(f"Failed to parse pricing response: {e}")

        return None

    def collect_pricing(self, property_id: str,
                        available_dates: List[str],
                        max_samples: int = 6,
                        min_nights_map: Optional[Dict[str, int]] = None
                        ) -> Dict[str, float]:
        """Collect nightly rates for a sample of available dates.

        Prioritizes dates within the next 30 days so that adr_30d and
        est_revenue_30d are non-zero. Falls back to further-out dates
        when the near-term is fully booked.

        Args:
            property_id: Airbnb listing ID
            available_dates: List of available date strings (YYYY-MM-DD)
            max_samples: Max number of dates to query pricing for
            min_nights_map: Optional dict mapping date -> min_nights

        Returns:
            Dict mapping date string -> nightly price
        """
        if not available_dates:
            return {}

        today = date.today()
        cutoff_30d = today + timedelta(days=30)
        cutoff_90d = today + timedelta(days=90)
        cutoff_365d = today + timedelta(days=365)

        all_dates = [
            d for d in available_dates
            if today <= date.fromisoformat(d) <= cutoff_365d
        ]

        if not all_dates:
            return {}

        # Split into near (0-30d), mid (31-90d), far (91-365d)
        near_dates = [d for d in all_dates if date.fromisoformat(d) <= cutoff_30d]
        mid_dates = [d for d in all_dates if cutoff_30d < date.fromisoformat(d) <= cutoff_90d]
        far_dates = [d for d in all_dates if date.fromisoformat(d) > cutoff_90d]

        sampled = []

        # Priority 1: Up to 4 samples from the 30-day window (weekday + weekend mix)
        for pool in [
            [d for d in near_dates if date.fromisoformat(d).weekday() < 4],
            [d for d in near_dates if date.fromisoformat(d).weekday() >= 4],
        ]:
            if not pool:
                continue
            sampled.append(pool[0])
            if len(pool) > 2:
                sampled.append(pool[len(pool) // 2])

        # Priority 2: If near-term is sparse, fill from 31-90d window
        if len(sampled) < 4:
            for pool in [
                [d for d in mid_dates if date.fromisoformat(d).weekday() < 4],
                [d for d in mid_dates if date.fromisoformat(d).weekday() >= 4],
            ]:
                if not pool:
                    continue
                sampled.append(pool[0])

        # Priority 3: If still sparse, take from far-out dates
        if len(sampled) < 2 and far_dates:
            sampled.append(far_dates[0])
            if len(far_dates) > 2:
                sampled.append(far_dates[len(far_dates) // 2])

        # Limit to max_samples
        sampled = sorted(set(sampled))[:max_samples]

        prices = {}
        mn_map = min_nights_map or {}

        logger.info(f"  Pricing: querying {len(sampled)} sample dates")
        for d in sampled:
            mn = mn_map.get(d, 1)
            price = self.collect_price_for_date(
                property_id, date.fromisoformat(d), min_nights=mn
            )
            if price is not None:
                prices[d] = price
                logger.debug(f"    {d}: ${price:.2f}")

        return prices

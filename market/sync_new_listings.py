#!/usr/bin/env python3
"""Sync newly discovered market listings into the market tracker DB."""

import sys
import sqlite3
import logging

sys.path.insert(0, ".")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

MARKET_DB = "market/market_tracker.db"

LOCATION_TO_MARKET = {
    "Rhododendron": "rhododendron",
    "Mount Hood Village": "mt_hood_other",
    "Government Camp": "government_camp",
    "Welches": "welches",
    "Brightwood": "brightwood",
    "Sandy": "sandy",
    "Zigzag": "rhododendron",
    "Wemme": "welches",
    "Mount Hood": "mt_hood_other",
    "Clackamas County": "mt_hood_other",
}


def bedrooms_to_comp_set(bedrooms):
    if bedrooms is None:
        return "unknown"
    if bedrooms <= 1:
        return "1br"
    if bedrooms == 2:
        return "2br"
    if bedrooms == 3:
        return "3br"
    if bedrooms == 4:
        return "4br"
    return "5br_plus"


def main():
    main_db = sqlite3.connect("data/tracker.db")
    main_db.row_factory = sqlite3.Row
    mdb = sqlite3.connect(MARKET_DB)

    latest = main_db.execute(
        "SELECT MAX(snapshot_date) FROM market_listings WHERE listing_id IS NOT NULL"
    ).fetchone()[0]

    if not latest:
        logger.info("No new market data found.")
        return

    # Get existing IDs in market DB
    existing = set(r[0] for r in mdb.execute("SELECT id FROM properties").fetchall())

    listings = main_db.execute("""
        SELECT listing_id, airbnb_url, subtitle, title, location,
               bedrooms, rating, review_count, cover_photo,
               latitude, longitude
        FROM market_listings
        WHERE snapshot_date = ? AND listing_id IS NOT NULL
    """, (latest,)).fetchall()

    added = 0
    for listing in listings:
        prop_id = f"airbnb_{listing['listing_id']}"
        if prop_id in existing:
            continue

        location = listing["location"] or "Unknown"
        market = LOCATION_TO_MARKET.get(location, "mt_hood_other")
        comp_set = bedrooms_to_comp_set(listing["bedrooms"])
        name = listing["subtitle"] or listing["title"] or "Unknown"

        try:
            mdb.execute("""
                INSERT OR IGNORE INTO properties (
                    id, platform, platform_id, url, name,
                    market, comp_set, bedrooms, overall_rating, review_count,
                    latitude, longitude, thumbnail_url
                ) VALUES (?, 'airbnb', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                prop_id, listing["listing_id"], listing["airbnb_url"],
                name[:200], market, comp_set, listing["bedrooms"],
                listing["rating"], listing["review_count"],
                listing["latitude"], listing["longitude"],
                listing["cover_photo"] or "",
            ))
            added += 1
        except Exception as e:
            logger.error(f"Error: {e}")

    mdb.commit()
    total = mdb.execute("SELECT COUNT(*) FROM properties").fetchone()[0]
    logger.info(f"Synced {added} new properties. Total in market DB: {total}")

    main_db.close()
    mdb.close()


if __name__ == "__main__":
    main()

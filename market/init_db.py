#!/usr/bin/env python3
"""Initialize the market tracker database with properties from market_listings."""

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

# Map search result locations to market names
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
    # Create fresh market DB
    mdb = sqlite3.connect(MARKET_DB)
    mdb.execute("PRAGMA journal_mode=WAL")

    mdb.executescript("""
        CREATE TABLE IF NOT EXISTS properties (
            id TEXT PRIMARY KEY,
            platform TEXT NOT NULL DEFAULT 'airbnb',
            platform_id TEXT NOT NULL,
            url TEXT NOT NULL,
            name TEXT,
            host_name TEXT,
            market TEXT,
            comp_set TEXT,
            bedrooms INTEGER,
            bathrooms REAL,
            max_guests INTEGER,
            property_type TEXT,
            overall_rating REAL,
            review_count INTEGER,
            superhost INTEGER DEFAULT 0,
            latitude REAL,
            longitude REAL,
            thumbnail_url TEXT,
            amenities TEXT,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS calendar_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            property_id TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            calendar_date TEXT NOT NULL,
            available INTEGER NOT NULL,
            price REAL,
            price_currency TEXT DEFAULT 'USD',
            min_nights INTEGER,
            UNIQUE(property_id, snapshot_date, calendar_date)
        );

        CREATE TABLE IF NOT EXISTS daily_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            property_id TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            occupancy_30d REAL,
            adr_30d REAL,
            revpar_30d REAL,
            est_revenue_30d REAL,
            occupancy_90d REAL,
            adr_90d REAL,
            revpar_90d REAL,
            est_revenue_90d REAL,
            new_bookings_since_last INTEGER DEFAULT 0,
            cancellations_since_last INTEGER DEFAULT 0,
            min_price REAL,
            max_price REAL,
            weekend_avg_price REAL,
            weekday_avg_price REAL,
            UNIQUE(property_id, snapshot_date)
        );

        CREATE TABLE IF NOT EXISTS review_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            property_id TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            overall_rating REAL,
            review_count INTEGER,
            cleanliness_rating REAL,
            accuracy_rating REAL,
            checkin_rating REAL,
            communication_rating REAL,
            location_rating REAL,
            value_rating REAL,
            UNIQUE(property_id, snapshot_date)
        );

        CREATE TABLE IF NOT EXISTS collection_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            phase TEXT,
            properties_collected INTEGER DEFAULT 0,
            errors INTEGER DEFAULT 0,
            duration_seconds REAL,
            notes TEXT
        );
    """)

    # Seed from main tracker's market_listings
    main_db = sqlite3.connect("data/tracker.db")
    main_db.row_factory = sqlite3.Row

    latest = main_db.execute(
        "SELECT MAX(snapshot_date) FROM market_listings WHERE listing_id IS NOT NULL"
    ).fetchone()[0]

    if not latest:
        logger.error("No market listings found. Run collect_market_data.py first.")
        return

    listings = main_db.execute("""
        SELECT listing_id, airbnb_url, subtitle, title, location,
               bedrooms, rating, review_count, cover_photo,
               latitude, longitude
        FROM market_listings
        WHERE snapshot_date = ? AND listing_id IS NOT NULL
    """, (latest,)).fetchall()

    logger.info(f"Seeding market DB from {len(listings)} listings (snapshot {latest})")

    added = 0
    for listing in listings:
        prop_id = f"airbnb_{listing['listing_id']}"
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
    logger.info(f"Added {added} properties. Total in market DB: {total}")

    main_db.close()
    mdb.close()


if __name__ == "__main__":
    main()

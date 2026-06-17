"""SQLite database schema and helper functions for STR Tracker."""

import sqlite3
import json
from datetime import date, datetime
from pathlib import Path


SCHEMA_SQL = """
-- Properties being tracked
CREATE TABLE IF NOT EXISTS properties (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    platform_id TEXT NOT NULL,
    url TEXT NOT NULL,
    name TEXT,
    nickname TEXT,
    bedrooms INTEGER,
    bathrooms REAL,
    max_guests INTEGER,
    property_type TEXT,
    latitude REAL,
    longitude REAL,
    amenities TEXT,
    market TEXT,
    comp_set TEXT,
    overall_rating REAL,
    review_count INTEGER,
    superhost INTEGER,
    host_name TEXT,
    active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Daily calendar snapshots
CREATE TABLE IF NOT EXISTS calendar_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    calendar_date TEXT NOT NULL,
    available INTEGER NOT NULL,
    price REAL,
    price_currency TEXT DEFAULT 'USD',
    min_nights INTEGER,
    FOREIGN KEY (property_id) REFERENCES properties(id),
    UNIQUE(property_id, snapshot_date, calendar_date)
);

-- Pre-computed daily metrics
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
    new_bookings_since_last INTEGER,
    cancellations_since_last INTEGER,
    min_price REAL,
    max_price REAL,
    weekend_avg_price REAL,
    weekday_avg_price REAL,
    FOREIGN KEY (property_id) REFERENCES properties(id),
    UNIQUE(property_id, snapshot_date)
);

-- Review count snapshots
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
    FOREIGN KEY (property_id) REFERENCES properties(id),
    UNIQUE(property_id, snapshot_date)
);

-- Individual reviews
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id TEXT NOT NULL,
    platform_review_id TEXT,
    reviewer_name TEXT,
    review_date TEXT,
    rating REAL,
    review_text TEXT,
    response_text TEXT,
    collected_date TEXT NOT NULL,
    FOREIGN KEY (property_id) REFERENCES properties(id),
    UNIQUE(property_id, platform_review_id)
);

-- Collection run logs
CREATE TABLE IF NOT EXISTS collection_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT,
    properties_attempted INTEGER DEFAULT 0,
    properties_succeeded INTEGER DEFAULT 0,
    properties_failed INTEGER DEFAULT 0,
    errors TEXT,
    status TEXT DEFAULT 'running'
);

-- Market-wide snapshots (from Airbnb search results)
CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date TEXT NOT NULL,
    total_listings INTEGER,
    avg_price REAL,
    median_price REAL,
    min_price REAL,
    max_price REAL,
    p25_price REAL,
    p75_price REAL,
    sample_dates TEXT,
    UNIQUE(snapshot_date)
);

-- Individual listings from Airbnb search results
CREATE TABLE IF NOT EXISTS market_listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date TEXT NOT NULL,
    title TEXT,
    price_per_night REAL,
    total_price REAL,
    nights INTEGER,
    rating REAL,
    review_count INTEGER,
    latitude REAL,
    longitude REAL,
    bedrooms INTEGER,
    beds INTEGER,
    est_guests INTEGER,
    location TEXT,
    subtitle TEXT,
    has_hot_tub INTEGER DEFAULT 0,
    has_pet_friendly INTEGER DEFAULT 0,
    is_tracked INTEGER DEFAULT 0,
    UNIQUE(snapshot_date, title, price_per_night)
);

-- Mt Hood for-sale market tracker (Redfin)
CREATE TABLE IF NOT EXISTS for_sale_listings (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'redfin',
    redfin_property_id INTEGER,
    redfin_listing_id INTEGER,
    mls_id TEXT,
    status TEXT,
    street_address TEXT,
    city TEXT,
    state TEXT,
    zip TEXT,
    latitude REAL,
    longitude REAL,
    price REAL,
    original_price REAL,
    beds INTEGER,
    baths REAL,
    sqft INTEGER,
    lot_sqft INTEGER,
    year_built INTEGER,
    hoa REAL,
    listing_remarks TEXT,
    days_on_market INTEGER,
    sold_date TEXT,
    sold_price REAL,
    photo_url TEXT,
    url TEXT,
    -- Buyer enrichment for closed sales (filled by AscendWeb lookup)
    parcel_id TEXT,
    new_owner_name TEXT,
    new_owner_mailing_address TEXT,
    new_owner_mailing_city TEXT,
    new_owner_mailing_state TEXT,
    new_owner_mailing_zip TEXT,
    -- Tracking timestamps
    first_seen TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
    buyer_enriched_at TEXT,
    synced_to_crm_at TEXT
);

CREATE TABLE IF NOT EXISTS listing_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL,
    changed_at TEXT NOT NULL DEFAULT (datetime('now')),
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    FOREIGN KEY (listing_id) REFERENCES for_sale_listings(id)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_for_sale_status
    ON for_sale_listings(status);
CREATE INDEX IF NOT EXISTS idx_for_sale_zip
    ON for_sale_listings(zip);
CREATE INDEX IF NOT EXISTS idx_for_sale_sold_date
    ON for_sale_listings(sold_date);
CREATE INDEX IF NOT EXISTS idx_listing_changes_listing
    ON listing_changes(listing_id);
CREATE INDEX IF NOT EXISTS idx_calendar_property_snapshot
    ON calendar_snapshots(property_id, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_calendar_property_caldate
    ON calendar_snapshots(property_id, calendar_date);
CREATE INDEX IF NOT EXISTS idx_metrics_property_date
    ON daily_metrics(property_id, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_reviews_property
    ON review_snapshots(property_id, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_market_listings_date
    ON market_listings(snapshot_date);
"""


def init_database(db_path: str) -> sqlite3.Connection:
    """Initialize the SQLite database and create tables if needed."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)

    # Schema migrations — add columns idempotently
    for sql in [
        "ALTER TABLE market_listings ADD COLUMN beds INTEGER",
        "ALTER TABLE market_listings ADD COLUMN est_guests INTEGER",
        "ALTER TABLE market_listings ADD COLUMN location TEXT",
        "ALTER TABLE market_listings ADD COLUMN subtitle TEXT",
        "ALTER TABLE market_listings ADD COLUMN has_hot_tub INTEGER DEFAULT 0",
        "ALTER TABLE market_listings ADD COLUMN has_pet_friendly INTEGER DEFAULT 0",
        "ALTER TABLE properties ADD COLUMN outreach_status TEXT DEFAULT 'not_contacted'",
        "ALTER TABLE properties ADD COLUMN thumbnail_url TEXT",
        # Owner enrichment columns (free public records lookup)
        "ALTER TABLE properties ADD COLUMN street_address TEXT",
        "ALTER TABLE properties ADD COLUMN site_city TEXT",
        "ALTER TABLE properties ADD COLUMN site_state TEXT",
        "ALTER TABLE properties ADD COLUMN site_zip TEXT",
        "ALTER TABLE properties ADD COLUMN county TEXT",
        "ALTER TABLE properties ADD COLUMN parcel_id TEXT",
        "ALTER TABLE properties ADD COLUMN owner_name TEXT",
        "ALTER TABLE properties ADD COLUMN owner_names_all TEXT",
        "ALTER TABLE properties ADD COLUMN owner_mailing_address TEXT",
        "ALTER TABLE properties ADD COLUMN owner_mailing_city TEXT",
        "ALTER TABLE properties ADD COLUMN owner_mailing_state TEXT",
        "ALTER TABLE properties ADD COLUMN owner_mailing_zip TEXT",
        "ALTER TABLE properties ADD COLUMN owner_is_absentee INTEGER",
        "ALTER TABLE properties ADD COLUMN last_sale_date TEXT",
        "ALTER TABLE properties ADD COLUMN last_sale_price REAL",
        "ALTER TABLE properties ADD COLUMN year_built INTEGER",
        "ALTER TABLE properties ADD COLUMN building_sqft INTEGER",
        "ALTER TABLE properties ADD COLUMN assessed_value REAL",
        "ALTER TABLE properties ADD COLUMN market_value REAL",
        "ALTER TABLE properties ADD COLUMN enrichment_source TEXT",
        "ALTER TABLE properties ADD COLUMN enriched_at TEXT",
    ]:
        try:
            db.execute(sql)
        except sqlite3.OperationalError:
            pass  # column already exists

    # Indexes that depend on migrated columns
    db.executescript("""
        CREATE INDEX IF NOT EXISTS idx_market_listings_location
            ON market_listings(snapshot_date, location);
    """)

    db.commit()
    return db


def save_property(db: sqlite3.Connection, internal_id: str, platform: str,
                  platform_id: str, url: str, nickname: str, market: str,
                  comp_set: str, details: dict = None):
    """Insert or update a property record."""
    details = details or {}
    db.execute("""
        INSERT INTO properties (id, platform, platform_id, url, nickname, market, comp_set,
                                name, bedrooms, bathrooms, max_guests, property_type,
                                latitude, longitude, amenities, overall_rating, review_count, superhost, host_name, thumbnail_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = COALESCE(excluded.name, properties.name),
            nickname = COALESCE(excluded.nickname, properties.nickname),
            bedrooms = COALESCE(excluded.bedrooms, properties.bedrooms),
            bathrooms = COALESCE(excluded.bathrooms, properties.bathrooms),
            max_guests = COALESCE(excluded.max_guests, properties.max_guests),
            property_type = COALESCE(excluded.property_type, properties.property_type),
            latitude = COALESCE(excluded.latitude, properties.latitude),
            longitude = COALESCE(excluded.longitude, properties.longitude),
            amenities = COALESCE(excluded.amenities, properties.amenities),
            overall_rating = COALESCE(excluded.overall_rating, properties.overall_rating),
            review_count = COALESCE(excluded.review_count, properties.review_count),
            superhost = COALESCE(excluded.superhost, properties.superhost),
            host_name = COALESCE(excluded.host_name, properties.host_name),
            thumbnail_url = COALESCE(excluded.thumbnail_url, properties.thumbnail_url),
            market = excluded.market,
            comp_set = excluded.comp_set,
            updated_at = datetime('now')
    """, (
        internal_id, platform, platform_id, url, nickname, market, comp_set,
        details.get("name"), details.get("bedrooms"), details.get("bathrooms"),
        details.get("max_guests"), details.get("property_type"),
        details.get("latitude"), details.get("longitude"),
        json.dumps(details.get("amenities", [])) if details.get("amenities") else None,
        details.get("overall_rating"), details.get("review_count"),
        details.get("superhost"), details.get("host_name"),
        details.get("thumbnail_url"),
    ))
    db.commit()


def save_calendar_snapshot(db: sqlite3.Connection, property_id: str,
                           snapshot_date: date, days: list):
    """Save calendar snapshot data. Preserves existing prices when new data has no price.
    Skips same-day and next-day dates — Airbnb marks these as unavailable
    after check-in cutoff, producing false 'booked' signals."""
    from datetime import timedelta
    cutoff = snapshot_date + timedelta(days=2)
    rows = [
        (property_id, snapshot_date.isoformat(), d["date"],
         1 if d["available"] else 0,
         d.get("price"), d.get("price_currency", "USD"), d.get("min_nights"))
        for d in days
        if d["date"] >= cutoff.isoformat()  # Skip same-day and next-day
    ]
    db.executemany("""
        INSERT INTO calendar_snapshots
            (property_id, snapshot_date, calendar_date, available, price, price_currency, min_nights)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(property_id, snapshot_date, calendar_date) DO UPDATE SET
            available = excluded.available,
            price = COALESCE(excluded.price, calendar_snapshots.price),
            price_currency = COALESCE(excluded.price_currency, calendar_snapshots.price_currency),
            min_nights = COALESCE(excluded.min_nights, calendar_snapshots.min_nights)
    """, rows)
    db.commit()


def save_daily_metrics(db: sqlite3.Connection, property_id: str,
                       snapshot_date: date, metrics: dict):
    """Save computed daily metrics."""
    db.execute("""
        INSERT OR REPLACE INTO daily_metrics
            (property_id, snapshot_date, occupancy_30d, adr_30d, revpar_30d, est_revenue_30d,
             occupancy_90d, adr_90d, revpar_90d, est_revenue_90d,
             new_bookings_since_last, cancellations_since_last,
             min_price, max_price, weekend_avg_price, weekday_avg_price)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        property_id, snapshot_date.isoformat(),
        metrics.get("occupancy_30d"), metrics.get("adr_30d"),
        metrics.get("revpar_30d"), metrics.get("est_revenue_30d"),
        metrics.get("occupancy_90d"), metrics.get("adr_90d"),
        metrics.get("revpar_90d"), metrics.get("est_revenue_90d"),
        metrics.get("new_bookings_since_last"), metrics.get("cancellations_since_last"),
        metrics.get("min_price"), metrics.get("max_price"),
        metrics.get("weekend_avg_price"), metrics.get("weekday_avg_price"),
    ))
    db.commit()


def save_review_snapshot(db: sqlite3.Connection, property_id: str,
                         snapshot_date: date, review_data: dict):
    """Save review count/rating snapshot."""
    db.execute("""
        INSERT OR REPLACE INTO review_snapshots
            (property_id, snapshot_date, overall_rating, review_count,
             cleanliness_rating, accuracy_rating, checkin_rating,
             communication_rating, location_rating, value_rating)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        property_id, snapshot_date.isoformat(),
        review_data.get("overall_rating"), review_data.get("review_count"),
        review_data.get("cleanliness_rating"), review_data.get("accuracy_rating"),
        review_data.get("checkin_rating"), review_data.get("communication_rating"),
        review_data.get("location_rating"), review_data.get("value_rating"),
    ))
    db.commit()


def start_collection_run(db: sqlite3.Connection, run_date: date) -> int:
    """Start a new collection run and return its ID."""
    cursor = db.execute("""
        INSERT INTO collection_runs (run_date, start_time, status)
        VALUES (?, ?, 'running')
    """, (run_date.isoformat(), datetime.now().isoformat()))
    db.commit()
    return cursor.lastrowid


def update_run_success(db: sqlite3.Connection, run_id: int):
    """Increment success counter for a collection run."""
    db.execute("""
        UPDATE collection_runs
        SET properties_attempted = properties_attempted + 1,
            properties_succeeded = properties_succeeded + 1
        WHERE id = ?
    """, (run_id,))
    db.commit()


def update_run_failure(db: sqlite3.Connection, run_id: int, error_msg: str):
    """Increment failure counter and log error."""
    run = db.execute("SELECT errors FROM collection_runs WHERE id = ?", (run_id,)).fetchone()
    errors = json.loads(run["errors"]) if run["errors"] else []
    errors.append(error_msg)

    db.execute("""
        UPDATE collection_runs
        SET properties_attempted = properties_attempted + 1,
            properties_failed = properties_failed + 1,
            errors = ?
        WHERE id = ?
    """, (json.dumps(errors), run_id))
    db.commit()


def complete_collection_run(db: sqlite3.Connection, run_id: int):
    """Mark a collection run as complete."""
    db.execute("""
        UPDATE collection_runs
        SET end_time = ?, status = 'completed'
        WHERE id = ?
    """, (datetime.now().isoformat(), run_id))
    db.commit()


def should_refresh_details(db: sqlite3.Connection, property_id: str, days: int) -> bool:
    """Check if property details need refreshing."""
    row = db.execute("""
        SELECT name, updated_at FROM properties WHERE id = ?
    """, (property_id,)).fetchone()
    if not row or not row["updated_at"]:
        return True
    # Always refresh if we don't have a name yet (first run)
    if not row["name"]:
        return True
    last_update = datetime.fromisoformat(row["updated_at"])
    return (datetime.now() - last_update).days >= days


def load_all_current_metrics(db: sqlite3.Connection, snapshot_date: date) -> list:
    """Load current metrics for all properties, joined with property details."""
    rows = db.execute("""
        SELECT p.id, p.platform, p.nickname, p.name, p.bedrooms, p.market, p.comp_set,
               p.overall_rating, p.review_count, p.url,
               m.occupancy_30d, m.adr_30d, m.revpar_30d, m.est_revenue_30d,
               m.occupancy_90d, m.adr_90d, m.revpar_90d, m.est_revenue_90d,
               m.new_bookings_since_last, m.cancellations_since_last,
               m.min_price, m.max_price, m.weekend_avg_price, m.weekday_avg_price,
               m.snapshot_date
        FROM properties p
        LEFT JOIN daily_metrics m ON p.id = m.property_id AND m.snapshot_date = ?
        WHERE p.active = 1
        ORDER BY p.market, p.comp_set, p.nickname
    """, (snapshot_date.isoformat(),)).fetchall()
    return [dict(r) for r in rows]

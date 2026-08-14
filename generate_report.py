#!/usr/bin/env python3
"""Generate the HTML report from current database data."""

import os
import shutil
import sqlite3
import csv
from collections import Counter
from datetime import date, timedelta

import sys
import argparse

# Parse CLI args
_parser = argparse.ArgumentParser()
_parser.add_argument("--date", help="Report date (YYYY-MM-DD)")
_parser.add_argument("--db", default="data/tracker.db", help="Path to SQLite database")
_parser.add_argument("--output", help="Output HTML path (default: data/report.html)")
_parser.add_argument("--csv-output", help="Output CSV path")
_args, _ = _parser.parse_known_args()

DB_PATH = _args.db
_is_market = "market" in DB_PATH
_report_title = "Market Performance Data" if _is_market else "STR Prospects"
_route_prefix = "/market/" if _is_market else "/"
_dates_json_url = "/market/dates.json" if _is_market else "/dates.json"

# Set default output paths based on which DB we're using
if _args.output:
    report_path = _args.output
elif _is_market:
    report_path = "market/report.html"
else:
    report_path = "data/report.html"

if _args.csv_output:
    csv_path = _args.csv_output
elif _is_market:
    csv_path = "market/property_report.csv"
else:
    csv_path = "data/property_report.csv"

if _args.date:
    today = date.fromisoformat(_args.date)
else:
    today = date.today()
    # Auto-fallback: if no data for today, use the latest date with pricing
    _db_check = sqlite3.connect(DB_PATH)
    _has_today = _db_check.execute(
        "SELECT COUNT(DISTINCT property_id) FROM calendar_snapshots WHERE snapshot_date = ? AND price IS NOT NULL AND price > 0",
        (today.isoformat(),)
    ).fetchone()[0]
    if _has_today < 20:
        # Prefer latest date with good pricing data (at least 20 properties)
        _latest = _db_check.execute(
            "SELECT snapshot_date FROM calendar_snapshots WHERE price IS NOT NULL AND price > 0 GROUP BY snapshot_date HAVING COUNT(DISTINCT property_id) >= 20 ORDER BY snapshot_date DESC LIMIT 1"
        ).fetchone()
        _latest = _latest[0] if _latest else None
        if not _latest:
            # Fall back to latest date with any metrics
            _latest = _db_check.execute(
                "SELECT MAX(snapshot_date) FROM daily_metrics"
            ).fetchone()[0]
        if _latest:
            today = date.fromisoformat(_latest)
            print(f"No data for today, using latest available: {today.isoformat()}")
    _db_check.close()

# Archive previous report before overwriting (only during normal runs, not --date overrides)
_explicit_date = bool(_args.date)
if not _explicit_date and os.path.exists(report_path):
    if _is_market:
        archive_dir = f"market/reports/{today.isoformat()}"
    else:
        archive_dir = f"data/reports/{today.isoformat()}"
    os.makedirs(archive_dir, exist_ok=True)
    shutil.copy2(report_path, os.path.join(archive_dir, "report.html"))
    if os.path.exists(csv_path):
        shutil.copy2(csv_path, os.path.join(archive_dir, "property_report.csv"))
    print(f"Archived previous report to {archive_dir}")

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row

# Get available report dates for the date navigator.
# Only include dates that have an actual archived report on disk — otherwise the static
# dropdown lists "phantom dates" (DB had a snapshot but no report.html was archived),
# and a user clicking one before the dynamic /dates.json loader fires gets a 404.
_archive_root = "market/reports" if _is_market else "data/reports"
_archived_dates = set()
if os.path.isdir(_archive_root):
    _archived_dates = {d for d in os.listdir(_archive_root)
                       if d.startswith("20") and os.path.isfile(f"{_archive_root}/{d}/report.html")}
# Always include today if we're generating now (it gets archived this run)
_archived_dates.add(today.isoformat())
available_dates = sorted(
    {r[0] for r in db.execute(
        "SELECT DISTINCT snapshot_date FROM daily_metrics ORDER BY snapshot_date DESC"
    ).fetchall()} & _archived_dates,
    reverse=True
)

# Ensure outreach_status column exists
try:
    db.execute("ALTER TABLE properties ADD COLUMN outreach_status TEXT DEFAULT 'not_contacted'")
    db.commit()
except sqlite3.OperationalError:
    pass

# Load all properties with metrics
rows = db.execute("""
    SELECT p.id, p.platform, p.nickname, p.name, p.bedrooms, p.market, p.comp_set,
           p.overall_rating, p.review_count, p.url, p.max_guests,
           p.latitude, p.longitude, p.host_name, p.superhost,
           p.outreach_status, p.thumbnail_url,
           COALESCE(m.occupancy_30d, mf.occupancy_30d) as occupancy_30d,
           COALESCE(m.occupancy_90d, mf.occupancy_90d) as occupancy_90d,
           COALESCE(m.adr_30d, mf.adr_30d) as adr_30d,
           COALESCE(m.revpar_30d, mf.revpar_30d) as revpar_30d,
           COALESCE(m.est_revenue_30d, mf.est_revenue_30d) as est_revenue_30d,
           COALESCE(m.adr_90d, mf.adr_90d) as adr_90d,
           COALESCE(m.est_revenue_90d, mf.est_revenue_90d) as est_revenue_90d,
           COALESCE(m.new_bookings_since_last, mf.new_bookings_since_last) as new_bookings_since_last,
           COALESCE(m.cancellations_since_last, mf.cancellations_since_last) as cancellations_since_last,
           COALESCE(m.min_price, mf.min_price) as min_price,
           COALESCE(m.max_price, mf.max_price) as max_price,
           COALESCE(m.weekend_avg_price, mf.weekend_avg_price) as weekend_avg_price,
           COALESCE(m.weekday_avg_price, mf.weekday_avg_price) as weekday_avg_price,
           COALESCE(m.snapshot_date, mf.snapshot_date) as snapshot_date
    FROM properties p
    LEFT JOIN daily_metrics m ON p.id = m.property_id AND m.snapshot_date = ?
    LEFT JOIN daily_metrics mf ON p.id = mf.property_id AND m.property_id IS NULL
        AND mf.snapshot_date = (
            SELECT MAX(m2.snapshot_date) FROM daily_metrics m2
            WHERE m2.property_id = p.id AND m2.snapshot_date < ?
        )
    WHERE p.active = 1
    ORDER BY COALESCE(m.occupancy_30d, mf.occupancy_30d) DESC NULLS LAST
""", (today.isoformat(), today.isoformat())).fetchall()
props = [dict(r) for r in rows]

# Cross-link tracked rentals with for-sale listings (matched by street_address)
# so the dashboard can flag "FOR SALE / PENDING / SOLD" + show the listing agent.
# Market DB doesn't have for_sale_listings table — only the main prospect tracker does.
if not _is_market:
    for_sale_rows = db.execute("""
        SELECT p.id AS property_id,
               f.status, f.price, f.sold_price, f.sold_date, f.days_on_market,
               f.listing_agent_name, f.listing_agent_brokerage,
               f.url AS for_sale_url
        FROM properties p
        INNER JOIN for_sale_listings f
            ON LOWER(TRIM(p.street_address)) = LOWER(TRIM(f.street_address))
        WHERE p.active = 1 AND p.street_address IS NOT NULL
    """).fetchall()
    for_sale_by_pid = {r["property_id"]: dict(r) for r in for_sale_rows}
else:
    for_sale_by_pid = {}
for p in props:
    fs = for_sale_by_pid.get(p["id"])
    if fs:
        p["for_sale_status"] = fs["status"]
        p["for_sale_price"] = fs["price"] or fs["sold_price"]
        p["for_sale_dom"] = fs["days_on_market"]
        p["for_sale_sold_date"] = fs["sold_date"]
        p["for_sale_url"] = fs["for_sale_url"]
        p["for_sale_agent"] = fs["listing_agent_name"]
        p["for_sale_brokerage"] = fs["listing_agent_brokerage"]

# Compute ADR from raw pricing data (all sampled dates, not just 30d)
# If today's snapshot has no pricing for a property, fall back to the most recent day that does
from datetime import date as d
for p in props:
    price_rows = db.execute("""
        SELECT price, calendar_date FROM calendar_snapshots
        WHERE property_id = ? AND snapshot_date = ? AND price IS NOT NULL AND price > 0
    """, (p["id"], today.isoformat())).fetchall()
    if not price_rows:
        # Fall back to the most recent snapshot with pricing for this property
        fallback = db.execute("""
            SELECT snapshot_date FROM calendar_snapshots
            WHERE property_id = ? AND price IS NOT NULL AND price > 0
            ORDER BY snapshot_date DESC LIMIT 1
        """, (p["id"],)).fetchone()
        if fallback:
            price_rows = db.execute("""
                SELECT price, calendar_date FROM calendar_snapshots
                WHERE property_id = ? AND snapshot_date = ? AND price IS NOT NULL AND price > 0
            """, (p["id"], fallback["snapshot_date"])).fetchall()
    if price_rows:
        prices = [r["price"] for r in price_rows]
        p["raw_adr"] = sum(prices) / len(prices)
        # Weekday vs weekend from sampled prices
        wd_prices = [r["price"] for r in price_rows if d.fromisoformat(r["calendar_date"]).weekday() < 4]
        we_prices = [r["price"] for r in price_rows if d.fromisoformat(r["calendar_date"]).weekday() >= 4]
        p["raw_weekday"] = sum(wd_prices) / len(wd_prices) if wd_prices else None
        p["raw_weekend"] = sum(we_prices) / len(we_prices) if we_prices else None
    else:
        p["raw_adr"] = None
        p["raw_weekday"] = None
        p["raw_weekend"] = None

# Normalize host names and compute portfolio counts
VACASA_ALIASES = {"vacasa", "vacasa western", "vacasa llc", "vacasa inc", "vacasa oregon"}
for p in props:
    host = (p.get("host_name") or "").strip()
    if any(alias in host.lower() for alias in VACASA_ALIASES):
        p["host_normalized"] = "Vacasa"
    elif host.lower().startswith("mt hood") or host.lower().startswith("mount hood"):
        p["host_normalized"] = None
    else:
        p["host_normalized"] = host if host and host != "?" else None
    p["outreach_status"] = p.get("outreach_status") or "not_contacted"

host_counts = Counter()
for p in props:
    h = p["host_normalized"]
    if h:
        host_counts[h] += 1
for p in props:
    h = p["host_normalized"]
    p["portfolio_count"] = host_counts.get(h, 1) if h else 1

# Load calendar data for heatmap (next 90 days)
# Falls back to most recent snapshot if today's is missing
cal_days_count = 90
cal_data = {}
for p in props:
    days = db.execute("""
        SELECT calendar_date, available FROM calendar_snapshots
        WHERE property_id = ? AND snapshot_date = ?
        AND calendar_date >= ? AND calendar_date <= ?
        ORDER BY calendar_date
    """, (p["id"], today.isoformat(), today.isoformat(),
          (today + timedelta(days=cal_days_count - 1)).isoformat())).fetchall()
    if not days:
        # Fall back to the most recent snapshot for this property
        fallback_sd = db.execute("""
            SELECT MAX(snapshot_date) FROM calendar_snapshots
            WHERE property_id = ? AND snapshot_date < ?
        """, (p["id"], today.isoformat())).fetchone()
        if fallback_sd and fallback_sd[0]:
            days = db.execute("""
                SELECT calendar_date, available FROM calendar_snapshots
                WHERE property_id = ? AND snapshot_date = ?
                AND calendar_date >= ? AND calendar_date <= ?
                ORDER BY calendar_date
            """, (p["id"], fallback_sd[0], today.isoformat(),
                  (today + timedelta(days=cal_days_count - 1)).isoformat())).fetchall()
    cal_data[p["id"]] = [(d["calendar_date"], d["available"]) for d in days]

# Build full historical calendar per property
# For past dates: check the snapshot from 2 days BEFORE the calendar date.
# Checking the day-of snapshot is wrong — Airbnb marks dates as unavailable once
# check-in time passes, so every past date would look "booked".
# By checking 2 days prior, we see if it was genuinely booked in advance.
# For future dates, use today's snapshot.
_all_snapshots = sorted(set(r[0] for r in db.execute(
    "SELECT DISTINCT snapshot_date FROM calendar_snapshots ORDER BY snapshot_date"
).fetchall()))
_first_date = date.fromisoformat(_all_snapshots[0]) if _all_snapshots else today
_last_date = today + timedelta(days=cal_days_count)

# For each past calendar date, find availability from the snapshot 2 days before.
# This avoids false "booked" from same-day/next-day check-in cutoffs.
_LEAD_DAYS = 2
_hist_rows = db.execute("""
    SELECT c.property_id, c.calendar_date, c.available
    FROM calendar_snapshots c
    WHERE c.calendar_date <= ?
      AND c.snapshot_date = date(c.calendar_date, '-' || ? || ' days')
    ORDER BY c.property_id, c.calendar_date
""", (today.isoformat(), str(_LEAD_DAYS))).fetchall()

_hist_data = {}  # {property_id: {date_str: available}}
for r in _hist_rows:
    pid = r["property_id"]
    if pid not in _hist_data:
        _hist_data[pid] = {}
    _hist_data[pid][r["calendar_date"]] = r["available"]

# Fill gaps: if no snapshot exists exactly 2 days before, try 3 days, then 1 day
# OPTIMIZED 2026-06-30: the original ran two correlated subqueries per row over
# ~3M rows (~105 min/report). Equivalent result via a single windowed pass — for
# each (property_id, calendar_date) pick the latest snapshot taken before
# calendar_date-1. The "only fill gaps" behavior (skip dates that already have the
# exact 2-day-prior snapshot) is preserved by the Python guard below, so the
# original NOT IN clause is redundant and dropped.
_gap_rows = db.execute("""
    SELECT property_id, calendar_date, available, snapshot_date FROM (
        SELECT c.property_id, c.calendar_date, c.available, c.snapshot_date,
               ROW_NUMBER() OVER (
                   PARTITION BY c.property_id, c.calendar_date
                   ORDER BY c.snapshot_date DESC
               ) AS rn
        FROM calendar_snapshots c
        WHERE c.calendar_date <= ?
          AND c.snapshot_date < date(c.calendar_date, '-1 days')
    )
    WHERE rn = 1
    ORDER BY property_id, calendar_date
""", (today.isoformat(),)).fetchall()

for r in _gap_rows:
    pid = r["property_id"]
    if pid not in _hist_data:
        _hist_data[pid] = {}
    if r["calendar_date"] not in _hist_data[pid]:
        _hist_data[pid][r["calendar_date"]] = r["available"]

# For future dates, merge from today's snapshot
_future_rows = db.execute("""
    SELECT property_id, calendar_date, available
    FROM calendar_snapshots
    WHERE snapshot_date = ? AND calendar_date > ?
    ORDER BY property_id, calendar_date
""", (today.isoformat(), today.isoformat())).fetchall()

for r in _future_rows:
    pid = r["property_id"]
    if pid not in _hist_data:
        _hist_data[pid] = {}
    _hist_data[pid][r["calendar_date"]] = r["available"]

# Build compact JSON: {property_id: "BBAABBA..."} where B=booked, A=available, ?=unknown
# Along with the start date and date count
import json as _json
_cal_start = _first_date
_cal_end = _last_date
_cal_total_days = (_cal_end - _cal_start).days

full_cal_json = {}
for p in props:
    pid = p["id"]
    hist = _hist_data.get(pid, {})
    bits = []
    for i in range(_cal_total_days):
        d_str = (_cal_start + timedelta(days=i)).isoformat()
        if d_str in hist:
            bits.append("A" if hist[d_str] else "B")
        else:
            bits.append("U")  # unknown/no data
    full_cal_json[pid] = "".join(bits)

# Market stats
market_labels = {
    "government_camp": "Government Camp",
    "rhododendron": "Rhododendron",
    "welches": "Welches",
    "brightwood": "Brightwood",
    "sandy": "Sandy",
    "mt_hood_other": "Mt Hood Other",
    "columbia_gorge": "Columbia Gorge",
    "unknown_area": "Unknown Area",
}
market_stats = {}
for p in props:
    m = p.get("market", "unknown")
    if m not in market_stats:
        market_stats[m] = {"count": 0, "occ_sum": 0, "occ_n": 0, "adr_sum": 0, "adr_n": 0}
    market_stats[m]["count"] += 1
    is_fully_blocked = p.get("occupancy_30d") is not None and p["occupancy_30d"] >= 1.0
    if p.get("occupancy_30d") is not None and not is_fully_blocked:
        market_stats[m]["occ_sum"] += p["occupancy_30d"]
        market_stats[m]["occ_n"] += 1
    p_adr = p.get("raw_adr") or p.get("adr_30d") or 0
    if p_adr > 0 and not is_fully_blocked:
        market_stats[m]["adr_sum"] += p_adr
        market_stats[m]["adr_n"] += 1

# Pricing sophistication classification
def classify_pricing(p):
    wk = p.get("weekday_avg_price") or p.get("raw_weekday")
    we = p.get("weekend_avg_price") or p.get("raw_weekend")
    min_p = p.get("min_price")
    max_p = p.get("max_price")
    if not wk or not we or not min_p or not max_p:
        return None
    avg_price = (wk + we) / 2
    if avg_price == 0:
        return None
    wk_we_var = abs(we - wk) / avg_price
    mid_price = (min_p + max_p) / 2
    if mid_price == 0:
        return None
    range_var = (max_p - min_p) / mid_price
    flat_wk_we = wk_we_var <= 0.05
    flat_range = range_var <= 0.10
    if flat_wk_we and flat_range:
        return "Flat"
    elif flat_wk_we or flat_range:
        return "Basic"
    else:
        return "Dynamic"

for p in props:
    p["pricing_type"] = classify_pricing(p)

# Market averages for scoring
market_avg = {}
for mkey, ms in market_stats.items():
    avg_occ_m = ms["occ_sum"] / ms["occ_n"] if ms["occ_n"] else 0
    avg_adr_m = ms["adr_sum"] / ms["adr_n"] if ms["adr_n"] else 0
    ratings_m = [p["overall_rating"] for p in props if p.get("market") == mkey and p.get("overall_rating")]
    avg_rating_m = sum(ratings_m) / len(ratings_m) if ratings_m else 0
    market_avg[mkey] = {"occ": avg_occ_m, "adr": avg_adr_m, "rating": avg_rating_m}

# Revenue gap computation
def compute_revenue_gap(p):
    mkt = market_avg.get(p.get("market", ""), {})
    mkt_occ = mkt.get("occ", 0)
    mkt_adr = mkt.get("adr", 0)
    if mkt_occ <= 0 or mkt_adr <= 0:
        return None
    mkt_potential = mkt_occ * mkt_adr * 30
    prop_rev = p.get("est_revenue_30d") or 0
    if prop_rev == 0:
        prop_occ = p.get("occupancy_30d") or 0
        prop_adr = p.get("raw_adr") or p.get("adr_30d") or 0
        if prop_occ > 0 and prop_adr > 0:
            prop_rev = prop_occ * prop_adr * 30
    return mkt_potential - prop_rev

for p in props:
    p["revenue_gap"] = compute_revenue_gap(p)

# Prospect score computation (0-100)
def compute_prospect_score(p):
    mkt = market_avg.get(p.get("market", ""), {})
    mkt_occ = mkt.get("occ", 0)
    mkt_adr = mkt.get("adr", 0)
    mkt_rating = mkt.get("rating", 0)

    components = {}
    weights = {}

    # Occupancy gap vs market (30%)
    prop_occ = p.get("occupancy_30d")
    if prop_occ is not None and mkt_occ > 0:
        occ_gap = max(0, mkt_occ - prop_occ) / mkt_occ
        components["occ_gap"] = min(occ_gap * 100, 100)
        weights["occ_gap"] = 30

    # Revenue gap vs market potential (25%)
    mkt_potential = mkt_occ * mkt_adr * 30 if mkt_occ > 0 and mkt_adr > 0 else 0
    prop_rev = p.get("est_revenue_30d") or 0
    if prop_rev == 0:
        po = p.get("occupancy_30d") or 0
        pa = p.get("raw_adr") or p.get("adr_30d") or 0
        if po > 0 and pa > 0:
            prop_rev = po * pa * 30
    if mkt_potential > 0:
        rev_gap_pct = max(0, mkt_potential - prop_rev) / mkt_potential
        components["rev_gap"] = min(rev_gap_pct * 100, 100)
        weights["rev_gap"] = 25

    # Pricing sophistication (15%)
    pricing = p.get("pricing_type")
    if pricing:
        components["pricing"] = {"Flat": 100, "Basic": 50, "Dynamic": 0}[pricing]
        weights["pricing"] = 15

    # Rating below market avg (15%)
    prop_rating = p.get("overall_rating")
    if prop_rating and mkt_rating > 0:
        rating_gap = max(0, mkt_rating - prop_rating)
        components["rating"] = min(rating_gap / 1.0 * 100, 100)
        weights["rating"] = 15

    # Portfolio size (15%) — single-property hosts easier to convert
    portfolio = p.get("portfolio_count", 1)
    if portfolio == 1:
        components["portfolio"] = 100
    elif portfolio <= 3:
        components["portfolio"] = 60
    elif portfolio <= 5:
        components["portfolio"] = 30
    else:
        components["portfolio"] = 0
    weights["portfolio"] = 15

    if not weights:
        return 0
    total_weight = sum(weights.values())
    return round(sum(components[k] * weights[k] for k in components) / total_weight)

for p in props:
    p["prospect_score"] = compute_prospect_score(p)

# Overall stats
total = len(props)
with_data = [p for p in props if p.get("occupancy_30d") is not None and p["occupancy_30d"] < 1.0]
avg_occ = sum(p["occupancy_30d"] for p in with_data) / len(with_data) if with_data else 0
simply_data = [p for p in with_data if p.get("outreach_status") == "client"]
market_data = [p for p in with_data if p.get("outreach_status") != "client"]
simply_occ = sum(p["occupancy_30d"] for p in simply_data) / len(simply_data) if simply_data else 0
market_occ = sum(p["occupancy_30d"] for p in market_data) / len(market_data) if market_data else 0
ratings = [p["overall_rating"] for p in props if p.get("overall_rating")]
avg_rating = sum(ratings) / len(ratings) if ratings else 0
total_reviews = sum(p["review_count"] or 0 for p in props)
with_pricing = [p for p in props if ((p.get("adr_30d") and p["adr_30d"] > 0) or (p.get("raw_adr") and p["raw_adr"] > 0)) and (p.get("occupancy_30d") is None or p["occupancy_30d"] < 1.0)]
avg_adr = sum((p.get("raw_adr") or p.get("adr_30d") or 0) for p in with_pricing) / len(with_pricing) if with_pricing else 0

# Simply VRM stats
simply_props = [p for p in props if p.get("outreach_status") == "client"]
simply_total = len(simply_props)
simply_ratings = [p["overall_rating"] for p in simply_props if p.get("overall_rating")]
simply_avg_rating = sum(simply_ratings) / len(simply_ratings) if simply_ratings else 0
simply_reviews = sum(p["review_count"] or 0 for p in simply_props)
simply_pricing = [p for p in simply_props if ((p.get("adr_30d") and p["adr_30d"] > 0) or (p.get("raw_adr") and p["raw_adr"] > 0)) and (p.get("occupancy_30d") is None or p["occupancy_30d"] < 1.0)]
simply_avg_adr = sum((p.get("raw_adr") or p.get("adr_30d") or 0) for p in simply_pricing) / len(simply_pricing) if simply_pricing else 0
# Compute est revenue using occupancy * best ADR * 30 (fallback-aware)
def _est_rev(p):
    occ = p.get("occupancy_30d") or 0
    adr = p.get("raw_adr") or p.get("adr_30d") or 0
    return occ * adr * 30 if adr > 0 and occ > 0 else 0

simply_revs = [_est_rev(p) for p in simply_props if _est_rev(p) > 0 and (p.get("occupancy_30d") is None or p["occupancy_30d"] < 1.0)]
simply_avg_rev = sum(simply_revs) / len(simply_revs) if simply_revs else 0

# Market (non-Simply) stats
other_props = [p for p in props if p.get("outreach_status") != "client"]
other_total = len(other_props)
other_ratings = [p["overall_rating"] for p in other_props if p.get("overall_rating")]
other_avg_rating = sum(other_ratings) / len(other_ratings) if other_ratings else 0
other_reviews = sum(p["review_count"] or 0 for p in other_props)
other_pricing = [p for p in other_props if ((p.get("adr_30d") and p["adr_30d"] > 0) or (p.get("raw_adr") and p["raw_adr"] > 0)) and (p.get("occupancy_30d") is None or p["occupancy_30d"] < 1.0)]
other_avg_adr = sum((p.get("raw_adr") or p.get("adr_30d") or 0) for p in other_pricing) / len(other_pricing) if other_pricing else 0
other_revs = [_est_rev(p) for p in other_props if _est_rev(p) > 0 and (p.get("occupancy_30d") is None or p["occupancy_30d"] < 1.0)]
other_avg_rev = sum(other_revs) / len(other_revs) if other_revs else 0

# Price tier breakdown
price_tiers = [
    (0, 300, "Budget ($0-$300)"),
    (300, 500, "Mid-Range ($300-$500)"),
    (500, 750, "Premium ($500-$750)"),
    (750, 99999, "Luxury ($750+)"),
]
tier_stats = []
for lo, hi, label in price_tiers:
    def _in_tier(p, lo=lo, hi=hi):
        adr = p.get("raw_adr") or p.get("adr_30d") or 0
        return lo <= adr < hi
    t_simply = [p for p in simply_props if _in_tier(p)]
    t_market = [p for p in other_props if _in_tier(p)]
    t_all = t_simply + t_market
    # Simply stats for this tier
    s_occ_data = [p for p in t_simply if p.get("occupancy_30d") is not None and p["occupancy_30d"] < 1.0]
    s_occ = sum(p["occupancy_30d"] for p in s_occ_data) / len(s_occ_data) if s_occ_data else None
    s_adr_data = [p for p in t_simply if (p.get("raw_adr") or p.get("adr_30d") or 0) > 0]
    s_adr = sum((p.get("raw_adr") or p.get("adr_30d") or 0) for p in s_adr_data) / len(s_adr_data) if s_adr_data else None
    s_revs = [_est_rev(p) for p in t_simply if _est_rev(p) > 0]
    s_rev = sum(s_revs) / len(s_revs) if s_revs else None
    # Market stats for this tier
    m_occ_data = [p for p in t_market if p.get("occupancy_30d") is not None and p["occupancy_30d"] < 1.0]
    m_occ = sum(p["occupancy_30d"] for p in m_occ_data) / len(m_occ_data) if m_occ_data else None
    m_adr_data = [p for p in t_market if (p.get("raw_adr") or p.get("adr_30d") or 0) > 0]
    m_adr = sum((p.get("raw_adr") or p.get("adr_30d") or 0) for p in m_adr_data) / len(m_adr_data) if m_adr_data else None
    m_revs = [_est_rev(p) for p in t_market if _est_rev(p) > 0]
    m_rev = sum(m_revs) / len(m_revs) if m_revs else None
    def _prop_summary(p):
        adr = p.get("raw_adr") or p.get("adr_30d") or 0
        occ = p.get("occupancy_30d") or 0
        return {"name": (p.get("name") or p.get("nickname") or "?")[:50],
                "url": p.get("url", ""), "adr": round(adr), "occ": round(occ * 100),
                "rev": round(occ * adr * 30) if adr > 0 and occ > 0 else 0}
    tier_stats.append({
        "label": label, "simply_n": len(t_simply), "market_n": len(t_market),
        "simply_occ": s_occ, "simply_adr": s_adr, "simply_rev": s_rev,
        "market_occ": m_occ, "market_adr": m_adr, "market_rev": m_rev,
        "simply_props": [_prop_summary(p) for p in t_simply],
        "market_props": [_prop_summary(p) for p in t_market],
    })

hot_prospects = len([p for p in props if p.get("prospect_score", 0) >= 70])
warm_prospects = len([p for p in props if 40 <= p.get("prospect_score", 0) < 70])
pipeline_counts = Counter(p.get("outreach_status", "not_contacted") for p in props)

def occ_class(occ):
    if occ is None: return "occ-low"
    if occ >= 0.7: return "occ-high"
    if occ >= 0.4: return "occ-med"
    return "occ-low"

# Build date navigator options
latest_date = available_dates[0] if available_dates else today.isoformat()
date_options_html = ""
for d in available_dates:
    sel = ' selected' if d == latest_date else ''
    label = date.fromisoformat(d).strftime("%b %d, %Y")
    date_options_html += f'<option value="{d}"{sel}>{label}</option>\n'

html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_report_title}</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='6' fill='%23d4a843'/%3E%3Ctext x='16' y='22' font-family='Arial,sans-serif' font-weight='bold' font-size='16' fill='%231a1a1a' text-anchor='middle'%3Etr%3C/text%3E%3C/svg%3E">
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html {{ overflow-x: hidden; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #e2e8f0; padding: 20px; overflow-x: hidden; width: 100%; }}
.container {{ max-width: 1800px; margin: 0 auto; overflow-x: hidden; }}
h1 {{ font-size: 28px; font-weight: 700; margin-bottom: 5px; color: #f8fafc; }}
.subtitle {{ color: #94a3b8; margin-bottom: 30px; font-size: 14px; }}
.stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 30px; }}
.stat-card {{ background: #1e293b; border-radius: 12px; padding: 20px; border: 1px solid #334155; }}
.stat-card .label {{ color: #94a3b8; font-size: 13px; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.5px; }}
.stat-card .value {{ font-size: 32px; font-weight: 700; color: #f8fafc; }}
.stat-card .value.green {{ color: #4ade80; }}
.stat-card .value.blue {{ color: #60a5fa; }}
.stat-card .value.yellow {{ color: #fbbf24; }}
.stat-card .value.purple {{ color: #c084fc; }}
.stat-card .value.orange {{ color: #fb923c; }}
.stat-card .sub-label {{ color: #64748b; font-size: 11px; margin-top: 4px; }}
.stats-section {{ margin-bottom: 24px; }}
.tier-table {{ width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 14px; }}
.tier-table th, .tier-table td {{ padding: 10px 14px; text-align: center; border-bottom: 1px solid #334155; }}
.tier-table thead th {{ color: #94a3b8; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }}
.tier-group.simply-group {{ color: #4ade80; border-bottom: 2px solid #166534; }}
.tier-group.market-group {{ color: #fb923c; border-bottom: 2px solid #9a3412; }}
.tier-sub {{ font-size: 11px; color: #64748b; }}
.tier-label {{ text-align: left; font-weight: 600; color: #e2e8f0; }}
.tier-n {{ color: #64748b; }}
.tier-win {{ color: #4ade80; font-weight: 600; }}
.tier-click {{ cursor: pointer; color: #60a5fa; text-decoration: underline; }}
.tier-click:hover {{ color: #93c5fd; }}
.tier-detail td {{ padding: 0; }}
.tier-props {{ padding: 8px 14px 12px; }}
.tier-prop {{ display: flex; justify-content: space-between; align-items: center; padding: 6px 12px; border-bottom: 1px solid #1e293b; font-size: 13px; }}
.tier-prop:last-child {{ border-bottom: none; }}
.tier-prop a {{ color: #60a5fa; text-decoration: none; }}
.tier-prop a:hover {{ text-decoration: underline; }}
.tier-prop span {{ color: #94a3b8; font-size: 12px; }}
.stats-header {{ font-size: 16px; font-weight: 700; margin-bottom: 12px; padding-bottom: 8px; border-bottom: 2px solid #334155; }}
.stats-header.simply {{ color: #4ade80; border-bottom-color: #166534; }}
.stats-header.market {{ color: #fb923c; border-bottom-color: #9a3412; }}
.stats-count {{ font-size: 13px; font-weight: 400; color: #64748b; margin-left: 8px; }}
.stat-card.simply-card {{ border-color: #166534; }}
h2 {{ font-size: 20px; font-weight: 600; margin: 30px 0 15px; color: #f8fafc; }}
.table-wrap {{ overflow-x: auto; margin-bottom: 30px; border-radius: 12px; max-width: 100%; -webkit-overflow-scrolling: touch; }}
table {{ width: 100%; border-collapse: collapse; background: #1e293b; }}
th {{ background: #334155; padding: 12px 10px; text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; color: #94a3b8; white-space: nowrap; cursor: help; }}
td {{ padding: 8px 10px; border-top: 1px solid #334155; font-size: 13px; white-space: nowrap; }}
tr:hover td {{ background: #253347; }}
.occ-bar {{ display: inline-block; height: 8px; border-radius: 4px; min-width: 4px; }}
.occ-high {{ background: #ef4444; }}
.occ-med {{ background: #fbbf24; }}
.occ-low {{ background: #4ade80; }}
.rating {{ color: #fbbf24; }}
a {{ color: #60a5fa; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.market-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 30px; }}
.market-card {{ background: #1e293b; border-radius: 10px; padding: 16px; border: 1px solid #334155; }}
.market-card .name {{ font-size: 14px; font-weight: 600; margin-bottom: 8px; }}
.market-card .detail {{ font-size: 13px; color: #94a3b8; }}
.cal-grid {{ display: flex; gap: 1px; flex-wrap: wrap; margin-top: 4px; }}
.cal-clickable {{ cursor: pointer; opacity: 0.9; transition: opacity 0.15s; }}
.cal-clickable:hover {{ opacity: 1; }}
.cal-day {{ width: 6px; height: 6px; border-radius: 1px; }}
.cal-booked {{ background: #ef4444; }}
.cal-avail {{ background: #22c55e; }}
.cal-empty {{ background: #334155; }}
.cal-expand-row td {{ padding: 0 !important; border-top: none !important; background: #0f172a !important; }}
.cal-expand {{ background: #0f172a; padding: 20px; border: 1px solid #334155; border-top: none; }}
.cal-expand .cal-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }}
.cal-expand .cal-header h3 {{ font-size: 16px; color: #f8fafc; margin: 0; }}
.cal-expand .cal-close {{ background: #334155; color: #e2e8f0; border: none; border-radius: 6px; padding: 4px 12px; cursor: pointer; font-size: 13px; }}
.cal-expand .cal-close:hover {{ background: #475569; }}
.cal-expand .cal-legend {{ display: flex; gap: 16px; margin-bottom: 16px; font-size: 12px; color: #94a3b8; }}
.cal-expand .cal-legend span {{ display: flex; align-items: center; gap: 4px; }}
.cal-expand .cal-legend i {{ display: inline-block; width: 12px; height: 12px; border-radius: 2px; }}
.cal-months {{ display: flex; flex-wrap: wrap; gap: 16px; }}
.cal-month {{ background: #1e293b; border-radius: 8px; padding: 12px; min-width: 200px; }}
.cal-month-label {{ font-size: 13px; font-weight: 600; color: #f8fafc; margin-bottom: 8px; display: flex; justify-content: space-between; align-items: center; }}
.cal-month-occ {{ font-size: 11px; font-weight: 400; padding: 2px 8px; border-radius: 10px; }}
.cal-month-occ.high {{ background: #ef444433; color: #ef4444; }}
.cal-month-occ.med {{ background: #fbbf2433; color: #fbbf24; }}
.cal-month-occ.low {{ background: #22c55e33; color: #4ade80; }}
.cal-month-occ.none {{ background: #33415533; color: #64748b; }}
.cal-summary {{ display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }}
.cal-summary-month {{ background: #1e293b; border-radius: 8px; padding: 8px 14px; text-align: center; min-width: 80px; }}
.cal-summary-month .month-name {{ font-size: 11px; color: #94a3b8; margin-bottom: 4px; }}
.cal-summary-month .month-occ {{ font-size: 18px; font-weight: 700; }}
.cal-month-grid {{ display: grid; grid-template-columns: repeat(7, 1fr); gap: 2px; }}
.cal-month-grid .cal-dow {{ font-size: 10px; color: #64748b; text-align: center; padding: 2px 0; }}
.cal-month-grid .cal-cell {{ width: 24px; height: 24px; border-radius: 3px; display: flex; align-items: center; justify-content: center; font-size: 10px; color: #e2e8f0; position: relative; }}
.cal-cell.booked {{ background: #ef4444; }}
.cal-cell.avail {{ background: #22c55e33; color: #4ade80; }}
.cal-cell.unknown {{ background: #1e293b; color: #475569; }}
.cal-cell.today-marker {{ outline: 2px solid #fbbf24; outline-offset: -1px; }}
.cal-cell.past {{ opacity: 0.85; }}
.cal-divider {{ width: 100%; height: 1px; background: #fbbf2466; margin: 4px 0; grid-column: 1 / -1; position: relative; }}
.cal-divider::after {{ content: "TODAY"; position: absolute; right: 0; top: -8px; font-size: 9px; color: #fbbf24; letter-spacing: 0.5px; }}
.note {{ color: #64748b; font-size: 12px; font-style: italic; margin-top: 20px; }}
#tooltip {{ display: none; position: fixed; background: #ffffff; color: #1e293b; padding: 10px 14px; border-radius: 8px; font-size: 13px; max-width: 280px; line-height: 1.5; box-shadow: 0 4px 20px rgba(0,0,0,0.6); border: 1px solid #e2e8f0; pointer-events: none; z-index: 9999; font-weight: 400; text-transform: none; letter-spacing: 0; }}
[data-tip] {{ cursor: help; border-bottom: 1px dashed #64748b; }}
.stat-card [data-tip] {{ border-bottom: none; }}
.date-nav {{ display: flex; align-items: center; gap: 10px; margin: 12px 0 16px 0; }}
.date-nav label {{ color: #94a3b8; font-size: 13px; font-weight: 500; }}
.date-nav select {{ background: #1e293b; color: #e2e8f0; border: 1px solid #475569; border-radius: 6px; padding: 8px 14px; font-size: 14px; cursor: pointer; }}
.filter-bar {{ display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; align-items: center; }}
.filter-bar select, .filter-bar input {{ background: #1e293b; color: #e2e8f0; border: 1px solid #475569; border-radius: 6px; padding: 8px 12px; font-size: 13px; }}
.filter-bar input {{ width: 200px; }}
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
.badge-gc {{ background: #1e3a5f; color: #60a5fa; }}
.badge-rh {{ background: #1e3a2f; color: #4ade80; }}
.badge-wl {{ background: #3a2f1e; color: #fbbf24; }}
.badge-br {{ background: #3a1e2f; color: #f472b6; }}
.badge-cg {{ background: #3a2200; color: #f97316; }}
.badge-other {{ background: #2d2d3a; color: #c084fc; }}
.superhost {{ color: #f472b6; font-weight: 600; }}
.price {{ color: #4ade80; }}
.no-data {{ color: #475569; }}
.badge-score-hot {{ background: #7f1d1d; color: #fca5a5; }}
.badge-score-warm {{ background: #78350f; color: #fbbf24; }}
.badge-score-cold {{ background: #14532d; color: #4ade80; }}
.status-select {{ background: transparent; color: inherit; border: none; font-size: 11px; font-weight: 600; padding: 2px 4px; border-radius: 4px; cursor: pointer; appearance: none; -webkit-appearance: none; }}
.status-select:hover {{ outline: 1px solid #e2e8f0; }}
.status-select.s-not_contacted {{ background: #334155; color: #94a3b8; }}
.status-select.s-contacted {{ background: #1e3a5f; color: #60a5fa; }}
.status-select.s-responded {{ background: #1e3a2f; color: #4ade80; }}
.status-select.s-meeting {{ background: #3a2f1e; color: #fbbf24; }}
.status-select.s-client {{ background: #3a1e3a; color: #c084fc; }}
#statusToast {{ display: none; position: fixed; bottom: 20px; right: 20px; background: #1e293b; color: #e2e8f0; padding: 12px 20px; border-radius: 8px; border: 1px solid #475569; font-size: 13px; z-index: 9999; box-shadow: 0 4px 20px rgba(0,0,0,0.5); }}
#statusToast .copy-btn {{ background: #3b82f6; color: #fff; border: none; border-radius: 4px; padding: 4px 12px; margin-left: 10px; cursor: pointer; font-size: 12px; }}
#statusToast .copy-btn:hover {{ background: #2563eb; }}
.badge-not_contacted {{ background: #334155; color: #94a3b8; }}
.badge-contacted {{ background: #1e3a5f; color: #60a5fa; }}
.badge-responded {{ background: #1e3a2f; color: #4ade80; }}
.badge-meeting {{ background: #3a2f1e; color: #fbbf24; }}
.badge-client {{ background: #3a1e3a; color: #c084fc; }}
.badge-for-sale {{ background: #7f1d1d; color: #fecaca; margin-left: 6px; }}
.badge-for-sale.pending {{ background: #78350f; color: #fbbf24; }}
.badge-for-sale.sold {{ background: #1e293b; color: #94a3b8; }}
.for-sale-note {{ display: block; font-size: 11px; color: #94a3b8; margin-top: 2px; }}
.for-sale-note a {{ color: #cbd5e1; text-decoration: none; }}
.for-sale-note a:hover {{ text-decoration: underline; }}
.badge-flat {{ background: #7f1d1d; color: #fca5a5; }}
.badge-basic {{ background: #78350f; color: #fbbf24; }}
.badge-dynamic {{ background: #14532d; color: #4ade80; }}
.rev-gap-neg {{ color: #ef4444; font-weight: 600; }}
.rev-gap-pos {{ color: #4ade80; font-weight: 600; }}
.portfolio-multi {{ color: #fbbf24; font-weight: 600; }}
th.sortable {{ cursor: pointer; user-select: none; }}
th.sortable:hover {{ color: #e2e8f0; }}
th.sort-asc::after {{ content: " \\25B2"; font-size: 9px; }}
th.sort-desc::after {{ content: " \\25BC"; font-size: 9px; }}
.group-header td {{ background: #334155 !important; font-weight: 700; font-size: 14px; padding: 12px 10px; color: #f8fafc; }}
.btn {{ background: #334155; color: #e2e8f0; border: 1px solid #475569; border-radius: 6px; padding: 8px 16px; font-size: 13px; cursor: pointer; }}
.btn:hover {{ background: #475569; }}
.btn.active {{ background: #3b82f6; border-color: #3b82f6; }}
.prop-thumb {{ width: 36px; height: 36px; border-radius: 4px; object-fit: cover; vertical-align: middle; margin-right: 6px; }}
.chart-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 30px; }}
.compare-bar {{ display: flex; align-items: center; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }}
.compare-select {{ background: #0f172a; color: #e2e8f0; border: 1px solid #475569; border-radius: 6px; padding: 8px 12px; font-size: 13px; flex: 1; max-width: 450px; }}

/* ── Mobile Responsive ── */
@media (max-width: 1024px) {{
  .stats-grid {{ grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; }}
  .market-grid {{ grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); }}
}}

@media (max-width: 768px) {{
  body {{ padding: 10px; }}
  h1 {{ font-size: 22px; }}
  .subtitle {{ font-size: 12px; margin-bottom: 16px; }}
  .stats-grid {{ grid-template-columns: repeat(2, 1fr); gap: 8px; margin-bottom: 16px; }}
  .stat-card {{ padding: 12px; }}
  .stat-card .value {{ font-size: 24px; }}
  .stat-card .label {{ font-size: 11px; }}
  .market-grid {{ grid-template-columns: repeat(2, 1fr); gap: 8px; }}
  h2 {{ font-size: 17px; margin: 20px 0 10px; }}
  .filter-bar {{ flex-direction: column; gap: 8px; }}
  .filter-bar select, .filter-bar input {{ width: 100%; }}
  .filter-bar .btn {{ width: 100%; text-align: center; }}

  /* Hide less critical columns: Portfolio(6), Rev Gap(15), Pricing(16), Weekday(17), Weekend(18), Calendar(19) */
  #propTable th:nth-child(6),  #propTable td:nth-child(6),
  #propTable th:nth-child(15), #propTable td:nth-child(15),
  #propTable th:nth-child(16), #propTable td:nth-child(16),
  #propTable th:nth-child(17), #propTable td:nth-child(17),
  #propTable th:nth-child(18), #propTable td:nth-child(18),
  #propTable th:nth-child(19), #propTable td:nth-child(19) {{
    display: none;
  }}

  td {{ font-size: 12px; padding: 6px 6px; }}
  th {{ font-size: 10px; padding: 8px 6px; }}
  .prop-thumb {{ width: 28px; height: 28px; }}
  .badge {{ font-size: 10px; padding: 1px 5px; }}
  .status-select {{ font-size: 10px; }}
  .cal-day {{ width: 5px; height: 5px; }}

  /* Stack chart grids */
  .chart-grid {{ grid-template-columns: 1fr; gap: 12px; }}

  /* Compare bar full-width on mobile */
  .compare-bar {{ flex-direction: column; align-items: stretch; }}
  .compare-select {{ max-width: 100%; width: 100%; }}
  .compare-bar .label {{ margin-bottom: 0; }}

  #statusToast {{ left: 10px; right: 10px; bottom: 10px; font-size: 12px; text-align: center; }}
  #tooltip {{ max-width: 220px; font-size: 12px; }}
}}

/* Portrait phone — show only the essential sales columns */
@media (max-width: 480px) {{
  body {{ padding: 6px; }}
  h1 {{ font-size: 18px; }}
  .stats-grid {{ grid-template-columns: repeat(2, 1fr); }}
  .stat-card .value {{ font-size: 20px; }}
  .market-grid {{ grid-template-columns: 1fr 1fr; }}

  /* Hide: #(1), Host(5), Portfolio(6), Market(7), BR(8), Guests(9), Rating(10), Rvws(11), RevGap(15), Pricing(16), Weekday(17), Weekend(18), Calendar(19) */
  /* Keep: Score(2), Status(3), Property(4), 30d Occ(12), ADR(13), Est Rev(14) */
  #propTable th:nth-child(1),  #propTable td:nth-child(1),
  #propTable th:nth-child(5),  #propTable td:nth-child(5),
  #propTable th:nth-child(6),  #propTable td:nth-child(6),
  #propTable th:nth-child(7),  #propTable td:nth-child(7),
  #propTable th:nth-child(8),  #propTable td:nth-child(8),
  #propTable th:nth-child(9),  #propTable td:nth-child(9),
  #propTable th:nth-child(10), #propTable td:nth-child(10),
  #propTable th:nth-child(11), #propTable td:nth-child(11),
  #propTable th:nth-child(15), #propTable td:nth-child(15),
  #propTable th:nth-child(16), #propTable td:nth-child(16),
  #propTable th:nth-child(17), #propTable td:nth-child(17),
  #propTable th:nth-child(18), #propTable td:nth-child(18),
  #propTable th:nth-child(19), #propTable td:nth-child(19) {{
    display: none;
  }}

  /* Truncate property name */
  #propTable td:nth-child(4) {{ max-width: 120px; overflow: hidden; text-overflow: ellipsis; }}
  #propTable td:nth-child(4) a {{ display: inline-block; max-width: 80px; overflow: hidden; text-overflow: ellipsis; vertical-align: middle; }}

  td {{ font-size: 11px; padding: 5px 4px; }}
  th {{ font-size: 9px; padding: 6px 4px; }}
  .prop-thumb {{ width: 24px; height: 24px; margin-right: 4px; }}
}}
</style>
</head>
<body>
<div class="container">
<h1>{_report_title}</h1>
<p class="subtitle">Competitive analysis across {total} properties | Data collected {today.strftime("%B %d, %Y")} | {len(with_data)} with metrics | {len(with_pricing)} with pricing</p>
<p class="subtitle" style="color: #9ca3af; font-size: 11px; margin-top: 4px;">Note: Occupancy = calendar unavailability. Airbnb does not distinguish guest bookings from owner blocks, so occupancy may be inflated for some properties. ADR is based on sampled available-date pricing and may not reflect actual booked rates.</p>

<div class="date-nav">
  <a href="#" id="latestLink" style="color: #d4a843; text-decoration: none; font-size: 12px; margin-right: 12px;">&#8592; Latest</a>
  <label>Report Date:</label>
  <select id="dateSelect" onchange="switchDate(this.value)" data-latest="{latest_date}">
    {date_options_html}
  </select>
</div>
<script>
// Base path derived from the current URL so the report works under ANY host or
// path prefix (Vercel /market/, GitHub Pages /str-tracker-market/) with nothing
// hardcoded. Added 2026-07-24 for the cloud/Pages deploy.
function _basePath() {{
  var p = window.location.pathname.replace(/\/reports\/\d{{4}}-\d{{2}}-\d{{2}}\/report\.html$/, '').replace(/\/index\.html$/, '').replace(/\/report\.html$/, '');
  if (!p.endsWith('/')) p += '/';
  return p;
}}
// Defined here, next to the <select>, NOT in the script at the end of the file.
// The report is ~11 MB, so a definition at the bottom is not parsed until long
// after the picker is clickable and every onchange throws ReferenceError until
// then. Moved up 2026-08-14 to fix the dead date dropdown.
function switchDate(dateStr) {{
  const sel = document.getElementById('dateSelect');
  const basePath = _basePath();
  // Use data-latest (embedded at build time) OR first option from dates.json
  const latestDate = sel.dataset.latest || (sel.options.length ? sel.options[0].value : null);
  if (dateStr === latestDate) {{
    window.location.href = basePath;
  }} else {{
    window.location.href = basePath + 'reports/' + dateStr + '/report.html';
  }}
}}
document.getElementById('latestLink').href = _basePath();
// Dynamically load all available dates so archived reports can navigate forward
fetch(_basePath() + 'dates.json').then(r => r.json()).then(dates => {{
  const sel = document.getElementById('dateSelect');
  const current = sel.value;
  sel.innerHTML = '';
  if (dates.length) sel.dataset.latest = dates[0];
  dates.forEach(d => {{
    const opt = document.createElement('option');
    opt.value = d;
    const dt = new Date(d + 'T12:00:00');
    opt.textContent = dt.toLocaleDateString('en-US', {{month: 'short', day: 'numeric', year: 'numeric'}});
    if (d === current) opt.selected = true;
    sel.appendChild(opt);
  }});
}}).catch(() => {{}});
</script>

<div class="stats-section">
  <div class="stats-header simply">Simply VRM Portfolio <span class="stats-count">{simply_total} properties</span></div>
  <div class="stats-grid">
    <div class="stat-card simply-card">
      <div class="label">30d Occupancy</div>
      <div class="value green">{simply_occ:.0%}</div>
    </div>
    <div class="stat-card simply-card">
      <div class="label">Avg ADR</div>
      <div class="value green">{"${:,.0f}".format(simply_avg_adr) if simply_avg_adr else "N/A"}</div>
    </div>
    <div class="stat-card simply-card">
      <div class="label">Avg Est Revenue</div>
      <div class="value green">{"${:,.0f}".format(simply_avg_rev) if simply_avg_rev else "N/A"}</div>
    </div>
    <div class="stat-card simply-card">
      <div class="label">Avg Rating</div>
      <div class="value green">{simply_avg_rating:.2f}</div>
    </div>
    <div class="stat-card simply-card">
      <div class="label">Total Reviews</div>
      <div class="value green">{simply_reviews:,}</div>
    </div>
  </div>
</div>

<div class="stats-section">
  <div class="stats-header market">Market Overview <span class="stats-count">{other_total} properties</span></div>
  <div class="stats-grid">
    <div class="stat-card">
      <div class="label">30d Occupancy</div>
      <div class="value orange">{market_occ:.0%}</div>
    </div>
    <div class="stat-card">
      <div class="label">Avg ADR</div>
      <div class="value orange">{"${:,.0f}".format(other_avg_adr) if other_avg_adr else "N/A"}</div>
    </div>
    <div class="stat-card">
      <div class="label">Avg Est Revenue</div>
      <div class="value orange">{"${:,.0f}".format(other_avg_rev) if other_avg_rev else "N/A"}</div>
    </div>
    <div class="stat-card">
      <div class="label">Avg Rating</div>
      <div class="value yellow">{other_avg_rating:.2f}</div>
    </div>
    <div class="stat-card">
      <div class="label">Total Reviews</div>
      <div class="value purple">{other_reviews:,}</div>
    </div>
    <div class="stat-card">
      <div class="label" data-tip="Properties with prospect score >= 70.">Hot Prospects</div>
      <div class="value" style="color:#fca5a5;">{hot_prospects}</div>
    </div>
    <div class="stat-card">
      <div class="label" data-tip="Sales pipeline: contacted/responded/meeting/client.">Pipeline</div>
      <div class="value blue">{pipeline_counts.get("contacted",0) + pipeline_counts.get("responded",0) + pipeline_counts.get("meeting",0) + pipeline_counts.get("client",0)}</div>
    </div>
  </div>
</div>

<div class="stats-section">
  <div class="stats-header" style="color:#e2e8f0; border-bottom-color:#475569;">Performance by Price Tier</div>
  <table class="tier-table">
    <thead>
      <tr>
        <th>Price Tier</th>
        <th colspan="4" class="tier-group simply-group">Simply VRM</th>
        <th colspan="4" class="tier-group market-group">Market</th>
      </tr>
      <tr>
        <th></th>
        <th class="tier-sub">#</th><th class="tier-sub">Occ</th><th class="tier-sub">ADR</th><th class="tier-sub">Est Rev</th>
        <th class="tier-sub">#</th><th class="tier-sub">Occ</th><th class="tier-sub">ADR</th><th class="tier-sub">Est Rev</th>
      </tr>
    </thead>
    <tbody>'''

import json as _json_tier
for ti, ts in enumerate(tier_stats):
    s_occ = f"{ts['simply_occ']:.0%}" if ts['simply_occ'] is not None else "-"
    s_adr = f"${ts['simply_adr']:,.0f}" if ts['simply_adr'] is not None else "-"
    s_rev = f"${ts['simply_rev']:,.0f}" if ts['simply_rev'] is not None else "-"
    m_occ = f"{ts['market_occ']:.0%}" if ts['market_occ'] is not None else "-"
    m_adr = f"${ts['market_adr']:,.0f}" if ts['market_adr'] is not None else "-"
    m_rev = f"${ts['market_rev']:,.0f}" if ts['market_rev'] is not None else "-"
    occ_win = ' class="tier-win"' if ts['simply_occ'] and ts['market_occ'] and ts['simply_occ'] > ts['market_occ'] else ""
    rev_win = ' class="tier-win"' if ts['simply_rev'] and ts['market_rev'] and ts['simply_rev'] > ts['market_rev'] else ""
    s_click = f' class="tier-n tier-click" onclick="toggleTierList(\'tier-s-{ti}\')"' if ts["simply_n"] > 0 else ' class="tier-n"'
    m_click = f' class="tier-n tier-click" onclick="toggleTierList(\'tier-m-{ti}\')"' if ts["market_n"] > 0 else ' class="tier-n"'
    html += f'''
      <tr>
        <td class="tier-label">{ts["label"]}</td>
        <td{s_click}>{ts["simply_n"]}</td>
        <td{occ_win}>{s_occ}</td><td>{s_adr}</td><td{rev_win}>{s_rev}</td>
        <td{m_click}>{ts["market_n"]}</td>
        <td>{m_occ}</td><td>{m_adr}</td><td>{m_rev}</td>
      </tr>
      <tr class="tier-detail" id="tier-s-{ti}" style="display:none;">
        <td colspan="9"><div class="tier-props">'''
    for sp in ts["simply_props"]:
        name_esc = sp["name"].replace("&", "&amp;").replace("<", "&lt;")
        html += f'<div class="tier-prop"><a href="{sp["url"]}" target="_blank">{name_esc}</a><span>Occ: {sp["occ"]}% | ADR: ${sp["adr"]:,} | Rev: ${sp["rev"]:,}</span></div>'
    html += f'''</div></td>
      </tr>
      <tr class="tier-detail" id="tier-m-{ti}" style="display:none;">
        <td colspan="9"><div class="tier-props">'''
    for mp in sorted(ts["market_props"], key=lambda x: -x["rev"]):
        name_esc = mp["name"].replace("&", "&amp;").replace("<", "&lt;")
        html += f'<div class="tier-prop"><a href="{mp["url"]}" target="_blank">{name_esc}</a><span>Occ: {mp["occ"]}% | ADR: ${mp["adr"]:,} | Rev: ${mp["rev"]:,}</span></div>'
    html += '''</div></td>
      </tr>'''

html += '''
    </tbody>
  </table>
</div>

<h2 data-tip="Properties grouped by sub-market.">Markets</h2>
<div class="market-grid">'''

for mkey in ["government_camp", "rhododendron", "welches", "brightwood", "sandy", "mt_hood_other", "columbia_gorge"]:
    if mkey not in market_stats:
        continue
    ms = market_stats[mkey]
    label = market_labels.get(mkey, mkey)
    avg = (ms["occ_sum"] / ms["occ_n"] * 100) if ms["occ_n"] else 0
    avg_m_adr = (ms["adr_sum"] / ms["adr_n"]) if ms["adr_n"] else 0
    adr_str = f"Avg ADR: ${avg_m_adr:,.0f}" if avg_m_adr else ""
    html += f'''
  <div class="market-card">
    <div class="name">{label}</div>
    <div class="detail">{ms["count"]} properties</div>
    <div class="detail">Avg Occ: {avg:.0f}%</div>
    <div class="detail">{adr_str}</div>
  </div>'''

html += '\n</div>\n'

# Filter bar
html += '''
<h2>All Properties</h2>
<div class="filter-bar">
  <input type="text" id="search" placeholder="Search by name or host..." oninput="filterTable()">
  <select id="marketFilter" onchange="filterTable()">
    <option value="">All Markets</option>
    <option value="government_camp">Government Camp</option>
    <option value="rhododendron">Rhododendron</option>
    <option value="welches">Welches</option>
    <option value="brightwood">Brightwood</option>
    <option value="sandy">Sandy</option>
    <option value="mt_hood_other">Mt Hood Other</option>
    <option value="columbia_gorge">Columbia Gorge</option>
  </select>
  <select id="bedroomFilter" onchange="filterTable()">
    <option value="">All Bedrooms</option>
    <option value="1">1 BR</option>
    <option value="2">2 BR</option>
    <option value="3">3 BR</option>
    <option value="4">4 BR</option>
    <option value="5">5+ BR</option>
  </select>
  <select id="statusFilter" onchange="filterTable()">
    <option value="">All Statuses</option>
    <option value="not_contacted">Not Contacted</option>
    <option value="contacted">Contacted</option>
    <option value="responded">Responded</option>
    <option value="meeting">Meeting</option>
    <option value="client">Simply VRM</option>
  </select>
  <select id="hostFilter" onchange="filterTable()">
    <option value="">All Hosts</option>
'''

# Get unique hosts
hosts = sorted(set(p.get("host_name") or "" for p in props))
for h in hosts:
    if h and h != "?":
        html += f'    <option value="{h}">{h}</option>\n'

html += '''  </select>
  <button class="btn" id="groupHostBtn" onclick="toggleGroupByHost()">Group by Host</button>
</div>
'''

# Table
html += '''<div class="table-wrap"><table id="propTable">
<thead><tr>
<th class="sortable" onclick="sortTable(0)">#</th>
<th class="sortable" onclick="sortTable(1)" data-tip="Prospect score 0-100. Higher = better sales target. Components: Occupancy gap vs market (30%) — low occ = needs help. Revenue gap vs market potential (25%) — leaving money on the table. Pricing sophistication (15%) — flat pricing = no dynamic strategy. Rating below market avg (15%) — lower reviews = needs better management. Portfolio size (15%) — solo hosts are easier to convert.">Score</th>
<th class="sortable" onclick="sortTable(2)" data-tip="Outreach status in sales pipeline.">Status</th>
<th class="sortable" onclick="sortTable(3)" data-tip="The display name of the listing on Airbnb">Property</th>
<th class="sortable" onclick="sortTable(4)" data-tip="The host or property manager name">Host</th>
<th class="sortable" onclick="sortTable(5)" data-tip="Number of properties this host manages in our tracker.">Portfolio</th>
<th class="sortable" onclick="sortTable(6)" data-tip="Geographic sub-market">Market</th>
<th class="sortable" onclick="sortTable(7)" data-tip="Number of bedrooms">BR</th>
<th class="sortable" onclick="sortTable(8)" data-tip="Maximum guest capacity">Guests</th>
<th class="sortable" onclick="sortTable(9)" data-tip="Overall guest review rating out of 5.0">Rating</th>
<th class="sortable" onclick="sortTable(10)" data-tip="Total number of guest reviews">Rvws</th>
<th class="sortable" onclick="sortTable(11)" data-tip="Estimated occupancy next 30 days. % of calendar dates marked unavailable.">30d Occ</th>
<th class="sortable" onclick="sortTable(12)" data-tip="Average Daily Rate from sampled available dates.">ADR</th>
<th class="sortable" onclick="sortTable(13)" data-tip="Estimated revenue for 30 days = booked nights x ADR.">Est Rev</th>
<th class="sortable" onclick="sortTable(14)" data-tip="Revenue gap vs market potential. Red = underperforming market avg.">Rev Gap</th>
<th class="sortable" onclick="sortTable(15)" data-tip="Pricing sophistication. Flat=no variation, Basic=partial, Dynamic=varies by day/season.">Pricing</th>
<th class="sortable" onclick="sortTable(16)" data-tip="Average weekday nightly rate (Mon-Thu)">Weekday</th>
<th class="sortable" onclick="sortTable(17)" data-tip="Average weekend nightly rate (Fri-Sun)">Weekend</th>
<th data-tip="Next 90 days calendar. Red=booked, Green=available.">90d Calendar</th>
</tr></thead>
<tbody>
'''

badge_class = {
    "government_camp": "badge-gc",
    "rhododendron": "badge-rh",
    "welches": "badge-wl",
    "brightwood": "badge-br",
    "columbia_gorge": "badge-cg",
}

status_labels = {
    "not_contacted": "Not Contacted",
    "contacted": "Contacted",
    "responded": "Responded",
    "meeting": "Meeting",
    "client": "Simply VRM",
}
status_sort_order = {"not_contacted": 0, "contacted": 1, "responded": 2, "meeting": 3, "client": 4}
pricing_sort_order = {"Flat": 0, "Basic": 1, "Dynamic": 2}

for i, p in enumerate(props, 1):
    name = (p.get("name") or p.get("nickname") or "Unknown")[:50]
    market = p.get("market", "")
    market_label = market_labels.get(market, market).replace(" Other", "")
    badge_cls = badge_class.get(market, "badge-other")
    br = p.get("bedrooms") or "?"
    guests = p.get("max_guests") or "?"
    rating = f'{p["overall_rating"]:.2f}' if p.get("overall_rating") else "-"
    rating_sort = f'{p["overall_rating"]:.2f}' if p.get("overall_rating") else "0"
    reviews = p.get("review_count") or "-"
    reviews_sort = p.get("review_count") or 0
    host = p.get("host_name") or "-"
    host_display = host[:18]
    if p.get("superhost"):
        host_display = f'<span class="superhost">{host_display}</span>'

    # Prospect score
    score = p.get("prospect_score", 0)
    if score >= 70:
        score_cls = "badge-score-hot"
    elif score >= 40:
        score_cls = "badge-score-warm"
    else:
        score_cls = "badge-score-cold"

    # Outreach status
    status = p.get("outreach_status", "not_contacted")
    status_label = status_labels.get(status, status)
    status_sort = status_sort_order.get(status, 0)

    # Portfolio
    portfolio = p.get("portfolio_count", 1)
    portfolio_cls = ' class="portfolio-multi"' if portfolio > 1 else ""

    occ30 = p.get("occupancy_30d")
    occ30_str = f"{occ30:.0%}" if occ30 is not None else "-"
    occ_pct = int(occ30 * 100) if occ30 is not None else 0
    occ_sort = f"{occ30:.4f}" if occ30 is not None else "0"
    oc = occ_class(occ30)

    # Use raw_adr (from all sampled dates) if adr_30d is zero/missing
    adr = p.get("raw_adr") or p.get("adr_30d") or 0
    adr_str = f'<span class="price">${adr:,.0f}</span>' if adr > 0 else '<span class="no-data">-</span>'

    # Estimate revenue using occupancy * ADR * 30
    occ_for_rev = occ30 if occ30 is not None else 0
    rev = p.get("est_revenue_30d") or 0
    if rev == 0 and adr > 0 and occ_for_rev > 0:
        rev = occ_for_rev * adr * 30
    rev_str = f'${rev:,.0f}' if rev > 0 else '-'

    # Revenue gap
    rgap = p.get("revenue_gap")
    if rgap is not None:
        if rgap > 0:
            rgap_str = f'<span class="rev-gap-neg">${rgap:,.0f}/mo</span>'
        else:
            rgap_str = f'<span class="rev-gap-pos">+${abs(rgap):,.0f}</span>'
        rgap_sort = f"{rgap:.0f}"
    else:
        rgap_str = '<span class="no-data">-</span>'
        rgap_sort = "0"

    # Pricing type
    pricing = p.get("pricing_type")
    if pricing:
        pricing_cls = f"badge-{pricing.lower()}"
        pricing_str = f'<span class="badge {pricing_cls}">{pricing}</span>'
        pricing_sort = pricing_sort_order.get(pricing, 1)
    else:
        pricing_str = '<span class="no-data">-</span>'
        pricing_sort = 9

    wk = p.get("weekday_avg_price") or p.get("raw_weekday")
    we = p.get("weekend_avg_price") or p.get("raw_weekend")
    wk_str = f'${wk:,.0f}' if wk else '-'
    we_str = f'${we:,.0f}' if we else '-'

    url = p.get("url", "#")
    thumb = p.get("thumbnail_url") or ""
    thumb_html = f'<img class="prop-thumb" src="{thumb}" loading="lazy" onerror="this.style.display=\'none\'">' if thumb else ""

    # For-sale badge + listing agent (when this rental is also listed for sale)
    fs_status = p.get("for_sale_status")
    for_sale_html = ""
    if fs_status:
        if fs_status == "Closed":
            fs_badge_text = "SOLD"
            fs_badge_cls = "badge-for-sale sold"
        elif fs_status in ("Pending", "Active Under Contract", "Active with Bumpable Contingency"):
            fs_badge_text = "PENDING"
            fs_badge_cls = "badge-for-sale pending"
        else:
            fs_badge_text = "FOR SALE"
            fs_badge_cls = "badge-for-sale"
        fs_price = p.get("for_sale_price")
        price_str = f"${fs_price:,.0f}" if fs_price else ""
        agent = p.get("for_sale_agent") or ""
        brokerage = p.get("for_sale_brokerage") or ""
        agent_str = agent
        if brokerage:
            agent_str += f" · {brokerage}"
        fs_url = p.get("for_sale_url") or "#"
        sold_date = p.get("for_sale_sold_date")
        date_str = f" · sold {sold_date}" if sold_date else ""
        for_sale_html = (
            f'<span class="badge {fs_badge_cls}">{fs_badge_text}</span>'
            f'<span class="for-sale-note"><a href="{fs_url}" target="_blank">{price_str}'
            f'{date_str}{" · listed by " + agent_str if agent else ""}</a></span>'
        )

    cal_html = f'<div class="cal-grid cal-clickable" onclick="toggleCalendar(\'{p["id"]}\')" title="Click to expand full calendar">'
    cal_days = cal_data.get(p["id"], [])
    for d_date, avail in cal_days:
        cls = "cal-avail" if avail else "cal-booked"
        cal_html += f'<div class="cal-day {cls}" title="{d_date}"></div>'
    for _ in range(cal_days_count - len(cal_days)):
        cal_html += '<div class="cal-day cal-empty"></div>'
    cal_html += '</div>'

    html += f'''<tr data-market="{market}" data-br="{br}" data-host="{host}" data-status="{status}">
<td data-sort="{i}">{i}</td>
<td data-sort="{score}"><span class="badge {score_cls}">{score}</span></td>
<td data-sort="{status_sort}"><select class="status-select s-{status}" data-id="{p['id']}" onchange="changeStatus(this)"><option value="not_contacted"{"selected" if status=="not_contacted" else ""}>Not Contacted</option><option value="contacted"{"selected" if status=="contacted" else ""}>Contacted</option><option value="responded"{"selected" if status=="responded" else ""}>Responded</option><option value="meeting"{"selected" if status=="meeting" else ""}>Meeting</option><option value="client"{"selected" if status=="client" else ""}>Simply VRM</option></select></td>
<td>{thumb_html}<a href="{url}" target="_blank">{name}</a>{for_sale_html}</td>
<td>{host_display}</td>
<td data-sort="{portfolio}"{portfolio_cls}>{portfolio}</td>
<td><span class="badge {badge_cls}">{market_label}</span></td>
<td>{br}</td>
<td>{guests}</td>
<td class="rating" data-sort="{rating_sort}">{rating}</td>
<td data-sort="{reviews_sort}">{reviews}</td>
<td data-sort="{occ_sort}"><span class="occ-bar {oc}" style="width:{occ_pct}px"></span> {occ30_str}</td>
<td data-sort="{adr:.0f}">{adr_str}</td>
<td data-sort="{rev:.0f}">{rev_str}</td>
<td data-sort="{rgap_sort}">{rgap_str}</td>
<td data-sort="{pricing_sort}">{pricing_str}</td>
<td data-sort="{wk or 0:.0f}">{wk_str}</td>
<td data-sort="{we or 0:.0f}">{we_str}</td>
<td>{cal_html}</td>
</tr>
'''

html += '''</tbody></table></div>

<p class="note">Occupancy estimated from calendar availability. ADR from sampled nightly rates via Airbnb API. Revenue = booked nights x ADR. 90-day calendar: Red=booked, Green=available, Gray=no data. Superhost names shown in pink.</p>
'''

# ── Performance Trends Section ──
import json as _json

# Query all historical daily_metrics joined with property market
trend_rows = db.execute("""
    SELECT m.snapshot_date, p.market, m.property_id, p.nickname, p.name,
           m.occupancy_30d, m.adr_30d, m.est_revenue_30d
    FROM daily_metrics m
    JOIN properties p ON p.id = m.property_id
    WHERE m.snapshot_date IS NOT NULL
    ORDER BY m.snapshot_date, p.market
""").fetchall()

# Build market-level trend data: {date -> {market -> {occ_sum, adr_sum, count}}}
trend_dates = sorted(set(r["snapshot_date"] for r in trend_rows))
has_trends = len(trend_dates) >= 2

market_trend = {}  # market -> [{date, occ, adr}]
for d in trend_dates:
    day_rows = [r for r in trend_rows if r["snapshot_date"] == d]
    by_market = {}
    for r in day_rows:
        m = r["market"] or "unknown"
        if m not in by_market:
            by_market[m] = {"occ_sum": 0, "occ_n": 0, "adr_sum": 0, "adr_n": 0}
        if r["occupancy_30d"] is not None:
            by_market[m]["occ_sum"] += r["occupancy_30d"]
            by_market[m]["occ_n"] += 1
        # Prefer raw_adr (more samples) over adr_30d (few samples, 30d window only)
        p_match = [p for p in props if p["id"] == r["property_id"]]
        raw_adr = (p_match[0].get("raw_adr") or 0) if p_match else 0
        if not raw_adr:
            raw_adr = r["adr_30d"] or 0
        if raw_adr > 0:
            by_market[m]["adr_sum"] += raw_adr
            by_market[m]["adr_n"] += 1

    for m, vals in by_market.items():
        if m not in market_trend:
            market_trend[m] = []
        avg_occ = (vals["occ_sum"] / vals["occ_n"] * 100) if vals["occ_n"] else None
        avg_adr = (vals["adr_sum"] / vals["adr_n"]) if vals["adr_n"] else None
        market_trend[m].append({"date": d, "occ": round(avg_occ, 1) if avg_occ is not None else None,
                                "adr": round(avg_adr, 0) if avg_adr is not None else None})

# Build per-property trend data for the dropdown
prop_trend = {}  # property_id -> {name, market, points: [{date, occ, adr, rev}]}
for r in trend_rows:
    pid = r["property_id"]
    if pid not in prop_trend:
        prop_trend[pid] = {
            "name": (r["name"] or r["nickname"] or "Unknown")[:45],
            "market": r["market"] or "unknown",
            "points": []
        }
    p_match = [p for p in props if p["id"] == pid]
    raw_adr = (p_match[0].get("raw_adr") or 0) if p_match else 0
    if not raw_adr:
        raw_adr = r["adr_30d"] or 0
    occ_val = r["occupancy_30d"] or 0
    est_rev = occ_val * raw_adr * 30 if raw_adr > 0 and occ_val > 0 else 0
    prop_trend[pid]["points"].append({
        "date": r["snapshot_date"],
        "occ": round(r["occupancy_30d"] * 100, 1) if r["occupancy_30d"] is not None else None,
        "adr": round(raw_adr, 0) if raw_adr else None,
        "rev": round(est_rev, 0) if est_rev > 0 else None,
    })

# Build Simply VRM portfolio trend (average of client properties per date)
client_ids = set(p["id"] for p in props if p.get("outreach_status") == "client")
simply_trend = {}  # date -> {occ_sum, occ_n, adr_sum, adr_n, rev_sum, rev_n}
for r in trend_rows:
    if r["property_id"] not in client_ids:
        continue
    d = r["snapshot_date"]
    if d not in simply_trend:
        simply_trend[d] = {"occ_sum": 0, "occ_n": 0, "adr_sum": 0, "adr_n": 0, "rev_sum": 0, "rev_n": 0}
    if r["occupancy_30d"] is not None and r["occupancy_30d"] < 1.0:
        simply_trend[d]["occ_sum"] += r["occupancy_30d"]
        simply_trend[d]["occ_n"] += 1
    pm = [p for p in props if p["id"] == r["property_id"]]
    raw_adr_s = (pm[0].get("raw_adr") or 0) if pm else 0
    if not raw_adr_s:
        raw_adr_s = r["adr_30d"] or 0
    if raw_adr_s > 0:
        simply_trend[d]["adr_sum"] += raw_adr_s
        simply_trend[d]["adr_n"] += 1
    occ_s = r["occupancy_30d"] or 0
    rev_s = occ_s * raw_adr_s * 30 if raw_adr_s > 0 and occ_s > 0 else 0
    if rev_s > 0:
        simply_trend[d]["rev_sum"] += rev_s
        simply_trend[d]["rev_n"] += 1

simply_trend_points = []
for d in sorted(simply_trend.keys()):
    v = simply_trend[d]
    avg_occ_s = (v["occ_sum"] / v["occ_n"]) if v["occ_n"] else None
    avg_adr_s = (v["adr_sum"] / v["adr_n"]) if v["adr_n"] else None
    simply_trend_points.append({
        "date": d,
        "occ": round(avg_occ_s * 100, 1) if avg_occ_s is not None else None,
        "adr": round(avg_adr_s, 0) if avg_adr_s is not None else None,
        "revpar": round(avg_occ_s * avg_adr_s, 0) if avg_occ_s is not None and avg_adr_s is not None else None,
        "total_rev": round(v["rev_sum"], 0) if v["rev_sum"] else None,
    })

# Market colors
market_colors = {
    "government_camp": "#60a5fa",
    "rhododendron": "#4ade80",
    "welches": "#fbbf24",
    "brightwood": "#f472b6",
    "sandy": "#fb923c",
    "mt_hood_other": "#c084fc",
    "columbia_gorge": "#f97316",
    "unknown_area": "#94a3b8",
}

html += '\n<h2>Performance Trends</h2>\n'

if not has_trends:
    html += '<div class="stat-card"><p style="color:#94a3b8;padding:20px;">Trend charts will appear after 2+ days of data collection. The daily automation is running — check back tomorrow.</p></div>\n'
else:
    # Embed data as JSON for Chart.js
    # Build per-client-property trend lookup
    client_prop_trends = {pid: prop_trend[pid] for pid in client_ids if pid in prop_trend}
    html += f'<script>const marketTrend={_json.dumps(market_trend)};const propTrend={_json.dumps(prop_trend)};const marketColors={_json.dumps(market_colors)};const simplyTrend={_json.dumps(simply_trend_points)};const clientIds={_json.dumps(list(client_ids))};const clientPropTrends={_json.dumps(client_prop_trends)};const fullCal={_json.dumps(full_cal_json)};const calStart="{_cal_start.isoformat()}";const calDays={_cal_total_days};const reportDate="{today.isoformat()}";</script>\n'
    html += '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>\n'

    # Simply VRM Portfolio section
    non_client_props = sorted(
        [(pid, pd) for pid, pd in prop_trend.items() if pid not in client_ids],
        key=lambda x: x[1]["name"]
    )
    html += '''
<h2>Simply VRM Portfolio</h2>
<div class="compare-bar">
  <div class="label" style="margin:0;">Compare to Property</div>
  <select id="simplyCompare" class="compare-select">
    <option value="">-- None (portfolio only) --</option>
'''
    for pid, pdata in non_client_props:
        ml = market_labels.get(pdata["market"], pdata["market"])
        html += f'    <option value="{pid}">[{ml}] {pdata["name"]}</option>\n'

    html += '''  </select>
</div>
<div class="chart-grid">
  <div class="stat-card">
    <div class="label">Avg Occupancy (30d)</div>
    <canvas id="simplyOccChart" height="200"></canvas>
  </div>
  <div class="stat-card">
    <div class="label">Avg ADR</div>
    <canvas id="simplyAdrChart" height="200"></canvas>
  </div>
  <div class="stat-card">
    <div class="label">Avg RevPAR</div>
    <canvas id="simplyRevparChart" height="200"></canvas>
  </div>
  <div class="stat-card">
    <div class="label">Total Portfolio Revenue (30d)</div>
    <canvas id="simplyRevChart" height="200"></canvas>
  </div>
</div>
'''

    html += '''
<div class="compare-bar">
  <div class="label" style="margin:0;">Compare Property to Market</div>
  <select id="compareSelect" class="compare-select">
    <option value="">-- None (market averages only) --</option>
'''

    # Add property options sorted by name
    sorted_props = sorted(prop_trend.items(), key=lambda x: x[1]["name"])
    for pid, pdata in sorted_props:
        ml = market_labels.get(pdata["market"], pdata["market"])
        html += f'    <option value="{pid}">[{ml}] {pdata["name"]}</option>\n'

    html += '''  </select>
</div>

<div class="chart-grid">
  <div class="stat-card">
    <div class="label">Market Avg Occupancy (30d)</div>
    <canvas id="occChart" height="220"></canvas>
  </div>
  <div class="stat-card">
    <div class="label">Market Avg ADR</div>
    <canvas id="adrChart" height="220"></canvas>
  </div>
</div>

<div class="stat-card" style="margin-bottom:30px;">
  <div class="compare-bar">
    <div class="label" style="margin:0;">Individual Property Trend</div>
    <select id="propSelect" class="compare-select">
'''

    for pid, pdata in sorted_props:
        ml = market_labels.get(pdata["market"], pdata["market"])
        html += f'      <option value="{pid}">[{ml}] {pdata["name"]}</option>\n'

    html += '''    </select>
  </div>
  <canvas id="propChart" height="200"></canvas>
</div>
'''

html += '''
<p class="note">Occupancy estimated from calendar availability. ADR from sampled nightly rates via Airbnb API. Revenue = booked nights x ADR. 90-day calendar: Red=booked, Green=available, Gray=no data. Superhost names shown in pink.</p>
'''

html += '''
<div id="tooltip"></div>
<div id="statusToast"></div>
<script>
const tip = document.getElementById("tooltip");
document.querySelectorAll("[data-tip]").forEach(el => {
  el.addEventListener("mouseenter", e => {
    tip.textContent = el.getAttribute("data-tip");
    tip.style.display = "block";
    const rect = el.getBoundingClientRect();
    let top = rect.top - tip.offsetHeight - 10;
    let left = rect.left + rect.width/2 - tip.offsetWidth/2;
    if (top < 5) top = rect.bottom + 10;
    if (left < 5) left = 5;
    if (left + tip.offsetWidth > window.innerWidth - 5) left = window.innerWidth - tip.offsetWidth - 5;
    tip.style.top = top + "px";
    tip.style.left = left + "px";
  });
  el.addEventListener("mouseleave", () => { tip.style.display = "none"; });
});

function toggleTierList(id) {
  const row = document.getElementById(id);
  row.style.display = row.style.display === 'none' ? 'table-row' : 'none';
}

// switchDate now lives in the inline script beside the <select>, near the top of
// the document, so it exists before the picker can be used.

function filterTable() {
  const search = document.getElementById("search").value.toLowerCase();
  const market = document.getElementById("marketFilter").value;
  const br = document.getElementById("bedroomFilter").value;
  const host = document.getElementById("hostFilter").value;
  const status = document.getElementById("statusFilter").value;
  document.querySelectorAll("#propTable tbody tr:not(.group-header)").forEach(row => {
    const name = row.cells[3].textContent.toLowerCase();
    const rowHost = (row.getAttribute("data-host") || "").toLowerCase();
    const rowMarket = row.getAttribute("data-market");
    const rowBr = row.getAttribute("data-br");
    const rowStatus = row.getAttribute("data-status");
    let show = true;
    if (search && !name.includes(search) && !rowHost.includes(search)) show = false;
    if (market && rowMarket !== market) show = false;
    if (br) {
      if (br === "5" && parseInt(rowBr) < 5) show = false;
      else if (br !== "5" && rowBr !== br) show = false;
    }
    if (host && row.getAttribute("data-host") !== host) show = false;
    if (status && rowStatus !== status) show = false;
    row.style.display = show ? "" : "none";
  });
}

let currentSort = {col: -1, asc: true};
function sortTable(col) {
  const table = document.getElementById("propTable");
  const tbody = table.querySelector("tbody");
  const rows = Array.from(tbody.querySelectorAll("tr:not(.group-header)"));
  const headers = table.querySelectorAll("th");
  if (currentSort.col === col) {
    currentSort.asc = !currentSort.asc;
  } else {
    currentSort.col = col;
    currentSort.asc = true;
  }
  headers.forEach(h => { h.classList.remove("sort-asc", "sort-desc"); });
  if (headers[col]) headers[col].classList.add(currentSort.asc ? "sort-asc" : "sort-desc");
  rows.sort((a, b) => {
    let aVal = a.cells[col].getAttribute("data-sort") || a.cells[col].textContent.trim();
    let bVal = b.cells[col].getAttribute("data-sort") || b.cells[col].textContent.trim();
    let aNum = parseFloat(aVal.replace(/[\\$,%\\/mo]/g, ""));
    let bNum = parseFloat(bVal.replace(/[\\$,%\\/mo]/g, ""));
    if (!isNaN(aNum) && !isNaN(bNum)) {
      return currentSort.asc ? aNum - bNum : bNum - aNum;
    }
    if (aVal === "-" || aVal === "") aVal = currentSort.asc ? "\\uffff" : "";
    if (bVal === "-" || bVal === "") bVal = currentSort.asc ? "\\uffff" : "";
    return currentSort.asc ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal);
  });
  rows.forEach(row => tbody.appendChild(row));
}

let groupByHostActive = false;
function toggleGroupByHost() {
  const btn = document.getElementById("groupHostBtn");
  groupByHostActive = !groupByHostActive;
  btn.classList.toggle("active", groupByHostActive);
  const table = document.getElementById("propTable");
  const tbody = table.querySelector("tbody");
  tbody.querySelectorAll(".group-header").forEach(r => r.remove());
  if (!groupByHostActive) return;
  const rows = Array.from(tbody.querySelectorAll("tr:not(.group-header)"));
  rows.sort((a, b) => {
    const aH = (a.getAttribute("data-host") || "").toLowerCase();
    const bH = (b.getAttribute("data-host") || "").toLowerCase();
    return aH.localeCompare(bH);
  });
  rows.forEach(row => tbody.appendChild(row));
  let lastHost = null;
  const colCount = table.querySelector("thead tr").cells.length;
  for (const row of rows) {
    const host = row.getAttribute("data-host") || "Unknown";
    if (host !== lastHost) {
      const count = rows.filter(r => r.getAttribute("data-host") === host).length;
      const header = document.createElement("tr");
      header.className = "group-header";
      header.innerHTML = '<td colspan="' + colCount + '">' + host + ' (' + count + ' properties)</td>';
      tbody.insertBefore(header, row);
      lastHost = host;
    }
  }
}

const statusChanges = JSON.parse(localStorage.getItem("strStatusChanges") || "{}");

// Apply saved status changes on load
document.querySelectorAll(".status-select[data-id]").forEach(el => {
  const id = el.getAttribute("data-id");
  if (statusChanges[id]) {
    const s = statusChanges[id];
    el.value = s;
    el.className = "status-select s-" + s;
    el.closest("tr").setAttribute("data-status", s);
  }
});

function changeStatus(el) {
  const id = el.getAttribute("data-id");
  const next = el.value;
  el.className = "status-select s-" + next;
  el.closest("tr").setAttribute("data-status", next);
  statusChanges[id] = next;
  localStorage.setItem("strStatusChanges", JSON.stringify(statusChanges));
  showStatusToast();
}

function showStatusToast() {
  const keys = Object.keys(statusChanges);
  if (!keys.length) return;
  const toast = document.getElementById("statusToast");
  const cmds = keys.map(id => "UPDATE properties SET outreach_status='" + statusChanges[id] + "' WHERE id='" + id + "';");
  const sql = cmds.join("\\n");
  toast.innerHTML = keys.length + " status change(s) pending <button class=\\"copy-btn\\" onclick=\\"copyStatusSQL()\\">Copy SQL</button> <button class=\\"copy-btn\\" style=\\"background:#22c55e\\" onclick=\\"applyAndClear()\\">Apply & Clear</button>";
  toast.style.display = "block";
  toast._sql = "sqlite3 data/tracker.db \\"" + cmds.join(" ") + "\\"";
}

function copyStatusSQL() {
  const toast = document.getElementById("statusToast");
  navigator.clipboard.writeText(toast._sql).then(() => {
    const btn = toast.querySelector(".copy-btn");
    btn.textContent = "Copied!";
    setTimeout(() => { btn.textContent = "Copy SQL"; }, 2000);
  });
}

function applyAndClear() {
  localStorage.removeItem("strStatusChanges");
  const toast = document.getElementById("statusToast");
  toast.innerHTML = "Cleared! Run the copied SQL, then regenerate the report.";
  setTimeout(() => { toast.style.display = "none"; }, 3000);
}

showStatusToast();

// ── Full Historical Calendar ──
const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const DAYS = ["Su","Mo","Tu","We","Th","Fr","Sa"];

function toggleCalendar(pid) {
  const existingRow = document.getElementById("cal-row-" + pid);
  if (existingRow) {
    existingRow.remove();
    return;
  }
  // Close any other open calendar
  document.querySelectorAll(".cal-expand-row").forEach(r => r.remove());

  const data = fullCal[pid];
  if (!data) return;

  // Find the table row for this property
  const propRow = document.querySelector('tr .cal-clickable[onclick*="' + pid + '"]');
  if (!propRow) return;
  const tr = propRow.closest("tr");
  const colCount = tr.closest("table").querySelector("thead tr").cells.length;

  // Build month-by-month calendar
  const start = new Date(calStart + "T00:00:00");
  const todayDate = new Date(reportDate + "T00:00:00");
  const end = new Date(start);
  end.setDate(end.getDate() + calDays);

  // Group days by month
  const months = {};
  for (let i = 0; i < calDays; i++) {
    const d = new Date(start);
    d.setDate(d.getDate() + i);
    const key = d.getFullYear() + "-" + String(d.getMonth()+1).padStart(2,"0");
    if (!months[key]) months[key] = {year: d.getFullYear(), month: d.getMonth(), days: []};
    months[key].days.push({day: d.getDate(), dow: d.getDay(), status: data[i] || "U", date: d, idx: i});
  }

  let html = '<div class="cal-expand">';
  // Get property name from the row
  const propName = tr.cells[3] ? tr.cells[3].textContent.trim() : "Property";
  html += '<div class="cal-header"><h3>' + propName + ' — Full Calendar</h3>';
  html += '<button class="cal-close" onclick="document.getElementById(\\'cal-row-' + pid + '\\').remove()">Close</button></div>';
  html += '<div class="cal-legend">';
  html += '<span><i style="background:#ef4444"></i> Booked</span>';
  html += '<span><i style="background:#22c55e33;border:1px solid #4ade80"></i> Available</span>';
  html += '<span><i style="background:#1e293b;border:1px solid #334155"></i> No Data</span>';
  html += '<span><i style="outline:2px solid #fbbf24;background:#0f172a"></i> Today</span>';
  html += '</div>';
  html += '<div class="cal-months">';

  // Compute monthly occupancy for summary bar
  html += '<div class="cal-summary">';
  for (const [key, m] of Object.entries(months)) {
    const knownDays = m.days.filter(d => d.status === "B" || d.status === "A");
    const bookedDays = m.days.filter(d => d.status === "B").length;
    let occPct = knownDays.length > 0 ? Math.round(bookedDays / knownDays.length * 100) : -1;
    let occCls = occPct >= 60 ? "high" : occPct >= 30 ? "med" : occPct >= 0 ? "low" : "none";
    let occStr = occPct >= 0 ? occPct + "%" : "—";
    let occColor = occPct >= 60 ? "#ef4444" : occPct >= 30 ? "#fbbf24" : occPct >= 0 ? "#4ade80" : "#64748b";
    html += '<div class="cal-summary-month">';
    html += '<div class="month-name">' + MONTHS[m.month].slice(0,3) + '</div>';
    html += '<div class="month-occ" style="color:' + occColor + '">' + occStr + '</div>';
    html += '</div>';
  }
  html += '</div>';

  for (const [key, m] of Object.entries(months)) {
    // Monthly occupancy badge
    const knownDays = m.days.filter(d => d.status === "B" || d.status === "A");
    const bookedDays = m.days.filter(d => d.status === "B").length;
    let occPct = knownDays.length > 0 ? Math.round(bookedDays / knownDays.length * 100) : -1;
    let occCls = occPct >= 60 ? "high" : occPct >= 30 ? "med" : occPct >= 0 ? "low" : "none";
    let occStr = occPct >= 0 ? occPct + "%" : "—";

    html += '<div class="cal-month">';
    html += '<div class="cal-month-label"><span>' + MONTHS[m.month] + ' ' + m.year + '</span><span class="cal-month-occ ' + occCls + '">' + occStr + '</span></div>';
    html += '<div class="cal-month-grid">';
    // Day-of-week headers
    for (const dw of DAYS) html += '<div class="cal-dow">' + dw + '</div>';
    // Empty cells before first day
    const firstDow = m.days[0].dow;
    for (let e = 0; e < firstDow; e++) html += '<div class="cal-cell unknown"></div>';
    // Day cells
    for (const day of m.days) {
      const isToday = day.date.toISOString().slice(0,10) === reportDate;
      const isPast = day.date < todayDate;
      let cls = "cal-cell";
      if (day.status === "B") cls += " booked";
      else if (day.status === "A") cls += " avail";
      else cls += " unknown";
      if (isToday) cls += " today-marker";
      if (isPast) cls += " past";
      const title = day.date.toISOString().slice(0,10) + (day.status === "B" ? " (Booked)" : day.status === "A" ? " (Available)" : " (No data)");
      html += '<div class="' + cls + '" title="' + title + '">' + day.day + '</div>';
    }
    html += '</div></div>';
  }

  html += '</div></div>';

  // Insert new row after the property row
  const newRow = document.createElement("tr");
  newRow.id = "cal-row-" + pid;
  newRow.className = "cal-expand-row";
  newRow.innerHTML = '<td colspan="' + colCount + '">' + html + '</td>';
  tr.after(newRow);

  // Scroll into view
  newRow.scrollIntoView({behavior: "smooth", block: "nearest"});
}
'''

if has_trends:
    html += '''
// ── Trend Charts ──
const chartDefaults = {
  responsive: true,
  interaction: { mode: "nearest", intersect: false, axis: "xy" },
  plugins: {
    legend: { labels: { color: "#94a3b8", font: { size: 11 } } },
    tooltip: {
      callbacks: {
        title: function(items) { return items[0] ? items[0].raw.x || items[0].label : ""; },
        label: function(ctx) {
          const lbl = ctx.dataset.label || "";
          const val = ctx.parsed.y;
          if (val == null) return null;
          if (lbl.includes("Occ")) return lbl + ": " + val.toFixed(1) + "%";
          if (lbl.includes("ADR") || lbl.includes("Rev")) return lbl + ": $" + val.toLocaleString();
          return lbl + ": " + val;
        }
      },
      filter: function(item) { return item.parsed.y != null; },
      displayColors: true,
      backgroundColor: "#1e293b",
      titleColor: "#f8fafc",
      bodyColor: "#e2e8f0",
      borderColor: "#475569",
      borderWidth: 1,
      padding: 10
    }
  },
  scales: {
    x: { ticks: { color: "#64748b", font: { size: 10 } }, grid: { color: "#1e293b" } },
    y: { ticks: { color: "#64748b" }, grid: { color: "#1e293b" } }
  },
  onHover: function(evt, items, chart) {
    // Bold the hovered line, dim others
    if (items.length > 0) {
      const idx = items[0].datasetIndex;
      chart.data.datasets.forEach((ds, i) => {
        ds.borderWidth = i === idx ? 4 : 1;
        ds.pointRadius = i === idx ? 5 : 0;
      });
    } else {
      chart.data.datasets.forEach(ds => {
        ds.borderWidth = ds._origWidth || 2;
        ds.pointRadius = ds._origPointRadius != null ? ds._origPointRadius : 3;
      });
    }
    chart.update("none");
  }
};

// Build market datasets
function getMarketDatasets(field) {
  const datasets = [];
  for (const [market, points] of Object.entries(marketTrend)) {
    const color = marketColors[market] || "#94a3b8";
    datasets.push({
      label: market.replace(/_/g, " ").replace(/\\b\\w/g, c => c.toUpperCase()),
      data: points.map(p => ({ x: p.date, y: p[field] })),
      borderColor: color, backgroundColor: color + "33",
      borderWidth: 2, pointRadius: 3, tension: 0.3,
      _origWidth: 2, _origPointRadius: 3
    });
  }
  return datasets;
}

// Add a property overlay to market datasets
function addPropertyOverlay(datasets, pid, field) {
  const data = propTrend[pid];
  if (!data) return datasets;
  const name = data.name;
  datasets.push({
    label: name,
    data: data.points.map(p => ({ x: p.date, y: p[field] })),
    borderColor: "#ffffff", backgroundColor: "#ffffff22",
    borderWidth: 3, pointRadius: 5, tension: 0.3,
    borderDash: [6, 3],
    _origWidth: 3, _origPointRadius: 5
  });
  return datasets;
}

// Market chart instances (so we can rebuild them)
let occChartInstance = null;
let adrChartInstance = null;

function renderMarketCharts(comparePid) {
  // Occupancy chart
  const occCtx = document.getElementById("occChart");
  if (occCtx) {
    if (occChartInstance) occChartInstance.destroy();
    let datasets = getMarketDatasets("occ");
    if (comparePid) datasets = addPropertyOverlay(datasets, comparePid, "occ");
    occChartInstance = new Chart(occCtx, {
      type: "line", data: { datasets },
      options: { ...chartDefaults, scales: { ...chartDefaults.scales,
        y: { ...chartDefaults.scales.y, title: { display: true, text: "Occupancy %", color: "#94a3b8" },
             ticks: { ...chartDefaults.scales.y.ticks, callback: v => v + "%" } }
      }}
    });
  }

  // ADR chart
  const adrCtx = document.getElementById("adrChart");
  if (adrCtx) {
    if (adrChartInstance) adrChartInstance.destroy();
    let datasets = getMarketDatasets("adr");
    if (comparePid) datasets = addPropertyOverlay(datasets, comparePid, "adr");
    adrChartInstance = new Chart(adrCtx, {
      type: "line", data: { datasets },
      options: { ...chartDefaults, scales: { ...chartDefaults.scales,
        y: { ...chartDefaults.scales.y, title: { display: true, text: "ADR ($)", color: "#94a3b8" },
             ticks: { ...chartDefaults.scales.y.ticks, callback: v => "$" + v } }
      }}
    });
  }
}

// ── Simply VRM Portfolio Charts ──
let simplyCharts = {};
function makeSimplyChart(canvasId, field, label, fmt) {
  const ctx = document.getElementById(canvasId);
  if (!ctx) return;
  if (simplyCharts[canvasId]) simplyCharts[canvasId].destroy();
  return { ctx, field, label, fmt };
}

const simplyColors = ["#c084fc","#4ade80","#60a5fa","#fbbf24","#f472b6","#fb923c","#22d3ee","#a3e635","#e879f9","#f87171","#38bdf8","#facc15","#34d399","#818cf8"];

function renderSimplyCharts(comparePid) {
  const configs = [
    { id: "simplyOccChart", field: "occ", label: "Occupancy %", fmt: v => v + "%" },
    { id: "simplyAdrChart", field: "adr", label: "ADR ($)", fmt: v => "$" + v },
    { id: "simplyRevparChart", field: "revpar", label: "RevPAR ($)", fmt: v => "$" + v },
    { id: "simplyRevChart", field: "total_rev", label: "Revenue ($)", fmt: v => "$" + v.toLocaleString() },
  ];
  configs.forEach(cfg => {
    const ctx = document.getElementById(cfg.id);
    if (!ctx) return;
    if (simplyCharts[cfg.id]) simplyCharts[cfg.id].destroy();

    // Individual client property bars
    const datasets = [];
    const clientEntries = Object.entries(clientPropTrends);
    // Use latest date's values for bar chart
    let ci = 0;
    const labels = [];
    const barData = [];
    const barColors = [];
    for (const [pid, pdata] of clientEntries) {
      const color = simplyColors[ci % simplyColors.length];
      const pField = cfg.field === "total_rev" ? "rev" : cfg.field;
      const latest = pdata.points[pdata.points.length - 1];
      const val = latest ? latest[pField] : null;
      labels.push(pdata.name.substring(0, 25));
      barData.push(val);
      barColors.push(color);
      ci++;
    }

    // Add comparison prospect bar
    if (comparePid && propTrend[comparePid]) {
      const cdata = propTrend[comparePid];
      const cField = cfg.field === "total_rev" ? "rev" : cfg.field;
      const latest = cdata.points[cdata.points.length - 1];
      labels.push(cdata.name.substring(0, 25) + " *");
      barData.push(latest ? latest[cField] : null);
      barColors.push("#ef4444");
    }

    datasets.push({
      data: barData,
      backgroundColor: barColors,
      borderColor: barColors.map(c => c),
      borderWidth: 1,
      borderRadius: 4,
    });

    simplyCharts[cfg.id] = new Chart(ctx, {
      type: "bar",
      data: { labels, datasets },
      options: {
        responsive: true,
        indexAxis: "y",
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: function(ctx) { const v = ctx.parsed.x; if (v == null) return null; return cfg.fmt(v); }
            },
            backgroundColor: "#1e293b", titleColor: "#f8fafc", bodyColor: "#e2e8f0",
            borderColor: "#475569", borderWidth: 1, padding: 10
          }
        },
        scales: {
          x: { ticks: { color: "#64748b", callback: cfg.fmt }, grid: { color: "#1e293b" },
               title: { display: true, text: cfg.label, color: "#94a3b8" } },
          y: { ticks: { color: "#94a3b8", font: { size: 10 } }, grid: { display: false } }
        }
      }
    });
  });
}

const simplyCompare = document.getElementById("simplyCompare");
if (simplyCompare) {
  simplyCompare.addEventListener("change", () => renderSimplyCharts(simplyCompare.value || null));
}
renderSimplyCharts(null);

// Compare dropdown
const compareSelect = document.getElementById("compareSelect");
if (compareSelect) {
  compareSelect.addEventListener("change", () => renderMarketCharts(compareSelect.value || null));
}
renderMarketCharts(null);

// Property Trend Chart (individual view)
let propChartInstance = null;
function renderPropChart(pid) {
  const data = propTrend[pid];
  if (!data) return;
  // Also get the market avg for comparison
  const mkt = data.market;
  const mktData = marketTrend[mkt] || [];
  const ctx = document.getElementById("propChart");
  if (propChartInstance) propChartInstance.destroy();
  propChartInstance = new Chart(ctx, {
    type: "line",
    data: {
      labels: data.points.map(p => p.date),
      datasets: [
        {
          label: "Property Occ %", data: data.points.map(p => p.occ),
          borderColor: "#4ade80", backgroundColor: "#4ade8033",
          borderWidth: 2, pointRadius: 4, tension: 0.3, yAxisID: "y",
          _origWidth: 2, _origPointRadius: 4
        },
        {
          label: "Market Avg Occ %", data: mktData.map(p => p.occ),
          borderColor: "#4ade80", backgroundColor: "transparent",
          borderWidth: 1.5, pointRadius: 0, tension: 0.3, yAxisID: "y",
          borderDash: [5, 3],
          _origWidth: 1.5, _origPointRadius: 0
        },
        {
          label: "Property ADR ($)", data: data.points.map(p => p.adr),
          borderColor: "#60a5fa", backgroundColor: "#60a5fa33",
          borderWidth: 2, pointRadius: 4, tension: 0.3, yAxisID: "y1",
          _origWidth: 2, _origPointRadius: 4
        },
        {
          label: "Market Avg ADR ($)", data: mktData.map(p => p.adr),
          borderColor: "#60a5fa", backgroundColor: "transparent",
          borderWidth: 1.5, pointRadius: 0, tension: 0.3, yAxisID: "y1",
          borderDash: [5, 3],
          _origWidth: 1.5, _origPointRadius: 0
        }
      ]
    },
    options: {
      ...chartDefaults,
      scales: {
        x: chartDefaults.scales.x,
        y: { position: "left", title: { display: true, text: "Occupancy %", color: "#4ade80" },
             ticks: { color: "#4ade80", callback: v => v + "%" }, grid: { color: "#1e293b" } },
        y1: { position: "right", title: { display: true, text: "ADR ($)", color: "#60a5fa" },
              ticks: { color: "#60a5fa", callback: v => "$" + v }, grid: { drawOnChartArea: false } }
      }
    }
  });
}

// Init property chart with first property
const propSelect = document.getElementById("propSelect");
if (propSelect) {
  propSelect.addEventListener("change", () => renderPropChart(propSelect.value));
  renderPropChart(propSelect.value);
}
'''

html += '''
</script>
</div>
</body>
</html>'''

# Only overwrite main report when generating for today (not historical regeneration)
if not _explicit_date:
    with open(report_path, "w") as f:
        f.write(html)

# Also update CSV
with open(csv_path, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["Property","Full Name","Host","Market","Comp Set","Bedrooms","Max Guests",
                "Rating","Reviews","Occ 30d","ADR 30d","Est Revenue 30d",
                "Weekday Rate","Weekend Rate","Min Price","Max Price",
                "Prospect Score","Outreach Status","Portfolio Count","Revenue Gap","Pricing Type",
                "Lat","Lon","URL","Date"])
    for r in props:
        rgap = r.get("revenue_gap")
        w.writerow([
            r["nickname"], r["name"], r.get("host_name",""),
            r["market"], r["comp_set"], r.get("bedrooms",""), r.get("max_guests",""),
            r.get("overall_rating",""), r.get("review_count",""),
            f"{r['occupancy_30d']*100:.1f}" if r.get("occupancy_30d") is not None else "",
            f"{r['adr_30d']:.2f}" if r.get("adr_30d") and r["adr_30d"] > 0 else "",
            f"{r['est_revenue_30d']:.2f}" if r.get("est_revenue_30d") and r["est_revenue_30d"] > 0 else "",
            f"{r['weekday_avg_price']:.2f}" if r.get("weekday_avg_price") else "",
            f"{r['weekend_avg_price']:.2f}" if r.get("weekend_avg_price") else "",
            f"{r['min_price']:.2f}" if r.get("min_price") else "",
            f"{r['max_price']:.2f}" if r.get("max_price") else "",
            r.get("prospect_score", ""),
            r.get("outreach_status", "not_contacted"),
            r.get("portfolio_count", 1),
            f"{rgap:.2f}" if rgap is not None else "",
            r.get("pricing_type", ""),
            r.get("latitude",""), r.get("longitude",""), r.get("url",""), r.get("snapshot_date","")
        ])

# Always save a copy to the date-specific archive directory
if _is_market:
    archive_dir = f"market/reports/{today.isoformat()}"
else:
    archive_dir = f"data/reports/{today.isoformat()}"
os.makedirs(archive_dir, exist_ok=True)
with open(os.path.join(archive_dir, "report.html"), "w") as f:
    f.write(html)
if not _explicit_date:
    shutil.copy2(csv_path, os.path.join(archive_dir, "property_report.csv"))

print(f"Report generated: {total} properties, {len(with_pricing)} with pricing")
db.close()

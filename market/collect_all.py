#!/usr/bin/env python3
"""
Market tracker collection — batched, with integrated pricing, retry, and cleanup.

Usage:
    python3 market/collect_all.py --batch 1 --of 4     # Run batch 1 of 4
    python3 market/collect_all.py --batch 4 --of 4     # Run batch 4 of 4
    python3 market/collect_all.py --retry               # Retry today's failures
    python3 market/collect_all.py --finalize            # Report + deploy + cleanup
    python3 market/collect_all.py                       # Run ALL properties (no batching)
"""

import sys
import sqlite3
import logging
import time
import signal
import argparse
import subprocess
import json
import threading
import concurrent.futures
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.collectors.airbnb import AirbnbCollector
from src.utils.http import RateLimiter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent
MARKET_DB = PROJECT_DIR / "market" / "market_tracker.db"
DEPLOY_DIR = PROJECT_DIR / "deploy" / "market"
CONSECUTIVE_FAIL_LIMIT = 3


def get_db():
    db = sqlite3.connect(str(MARKET_DB), timeout=120)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=120000")
    return db


def db_write_with_retry(db, func, max_retries=5):
    """Execute a DB write function with retry on lock errors."""
    for attempt in range(max_retries):
        try:
            func(db)
            return True
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < max_retries - 1:
                time.sleep(2 + attempt * 2)  # 2s, 4s, 6s, 8s backoff
                continue
            raise
    return False


def collect(batch=None, total_batches=None):
    """Collect calendar, details, reviews, AND pricing for a batch of properties."""
    db = get_db()
    today = date.today().isoformat()
    start_time = time.time()

    # Get all active properties, ordered consistently
    all_props = db.execute(
        "SELECT id, platform_id, url, name, host_name, bedrooms FROM properties WHERE active=1 ORDER BY id"
    ).fetchall()

    # Slice for batch
    if batch and total_batches:
        size = len(all_props) // total_batches
        remainder = len(all_props) % total_batches
        # Distribute remainder across first N batches
        start = (batch - 1) * size + min(batch - 1, remainder)
        end = start + size + (1 if batch <= remainder else 0)
        properties = all_props[start:end]
        logger.info(f"Batch {batch}/{total_batches}: properties {start+1}-{end} of {len(all_props)}")
    else:
        properties = all_props
        logger.info(f"Running all {len(properties)} properties (no batching)")

    # Skip properties already collected today
    already_collected = set(
        r[0] for r in db.execute(
            "SELECT DISTINCT property_id FROM calendar_snapshots WHERE snapshot_date = ?",
            (today,)
        ).fetchall()
    )

    to_collect = [p for p in properties if p["id"] not in already_collected]
    logger.info(f"  {len(properties) - len(to_collect)} already collected today, {len(to_collect)} remaining")

    # Randomize order so rate-limit failures hit different properties each run
    import random
    random.shuffle(to_collect)

    # Same-day/next-day cutoff — skip dates within 2 days to avoid false bookings
    _cutoff_date = (date.today() + timedelta(days=2)).isoformat()

    rate_limiter = RateLimiter(delays={"airbnb.com": 3.5}, jitter=1.5)
    collector = AirbnbCollector(rate_limiter)

    collected = 0
    priced = 0
    errors = 0

    def _collect_one(coll, pid):
        """Collect a single property (runs in thread for timeout)."""
        cal = coll.collect_calendar(pid)
        det = coll.collect_details(pid)
        prices = {}
        if cal:
            avail_dates = [
                e.date for e in cal
                if e.available and date.fromisoformat(e.date) > date.today()
            ]
            min_nights_map = {
                e.date: (e.min_nights or 1)
                for e in cal if e.available
            }
            if avail_dates:
                prices = coll.collect_pricing(pid, avail_dates, max_samples=4,
                                              min_nights_map=min_nights_map) or {}
        return cal, det, prices

    for i, prop in enumerate(to_collect):
        prop_id = prop["id"]
        platform_id = prop["platform_id"]

        # Refresh session every 50 properties to avoid stale connections
        if i > 0 and i % 50 == 0:
            logger.info("  Refreshing HTTP session...")
            collector = AirbnbCollector(rate_limiter)

        if (i + 1) % 25 == 0:
            elapsed = time.time() - start_time
            rate = (i + 1) / elapsed * 60 if elapsed > 0 else 0
            remaining = (len(to_collect) - i - 1) / rate if rate > 0 else 0
            logger.info(f"  Progress: {i+1}/{len(to_collect)} ({rate:.0f}/min, ~{remaining:.0f} min left)")

        try:
            # Run collection in a thread with 120s timeout
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_collect_one, collector, platform_id)
                calendar_data, details, prices = future.result(timeout=120)

            def _write_to_db(db):
                if calendar_data:
                    for entry in calendar_data:
                        # Skip same-day/next-day — Airbnb marks as unavailable after check-in cutoff
                        if entry.date < _cutoff_date:
                            continue
                        db.execute("""
                            INSERT OR REPLACE INTO calendar_snapshots
                                (property_id, snapshot_date, calendar_date, available, price, min_nights)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, (
                            prop_id, today, entry.date,
                            1 if entry.available else 0,
                            entry.price,
                            entry.min_nights,
                        ))

                if details:
                    db.execute("""
                        UPDATE properties SET
                            host_name = COALESCE(?, host_name),
                            overall_rating = COALESCE(?, overall_rating),
                            review_count = COALESCE(?, review_count),
                            superhost = COALESCE(?, superhost),
                            max_guests = COALESCE(?, max_guests),
                            bathrooms = COALESCE(?, bathrooms),
                            property_type = COALESCE(?, property_type),
                            updated_at = datetime('now')
                        WHERE id = ?
                    """, (
                        details.host_name,
                        details.overall_rating,
                        details.review_count,
                        1 if details.superhost else 0,
                        details.max_guests,
                        details.bathrooms,
                        details.property_type,
                        prop_id,
                    ))

                if details and details.overall_rating:
                    db.execute("""
                        INSERT OR REPLACE INTO review_snapshots
                            (property_id, snapshot_date, overall_rating, review_count)
                        VALUES (?, ?, ?, ?)
                    """, (
                        prop_id, today,
                        details.overall_rating,
                        details.review_count,
                    ))

                if prices:
                    bedrooms = prop["bedrooms"] or 2
                    max_reasonable = 300 * max(bedrooms, 1)
                    for cal_date, price in prices.items():
                        if price <= 0 or price > max_reasonable:
                            continue
                        db.execute("""
                            UPDATE calendar_snapshots SET price = ?
                            WHERE property_id = ? AND snapshot_date = ? AND calendar_date = ?
                        """, (price, prop_id, today, cal_date))

                db.execute("""
                    UPDATE properties SET updated_at = datetime('now')
                    WHERE id = ? AND updated_at < datetime('now', '-2 days')
                """, (prop_id,))

            db_write_with_retry(db, _write_to_db)

            if prices:
                priced += 1
            collected += 1

        except concurrent.futures.TimeoutError:
            errors += 1
            logger.warning(f"  TIMEOUT [{i+1}] {prop['name'][:40]}: hung for >120s, skipping")
            collector = AirbnbCollector(rate_limiter)
            time.sleep(5)
        except Exception as e:
            errors += 1
            logger.warning(f"  Error [{i+1}] {prop['name'][:40]}: {type(e).__name__}: {str(e)[:80]}")
            # Cooldown after errors to avoid hammering a struggling API
            time.sleep(5)

        # Commit every 25 properties
        if (i + 1) % 25 == 0:
            db.commit()

    db.commit()

    # Compute daily metrics for all properties in this batch
    logger.info("Computing metrics...")
    compute_metrics(db, today, [p["id"] for p in properties])

    duration = time.time() - start_time
    batch_label = f"batch_{batch}_of_{total_batches}" if batch else "full"
    db.execute("""
        INSERT INTO collection_runs (run_date, start_time, status, properties_attempted, properties_succeeded, properties_failed)
        VALUES (?, ?, 'completed', ?, ?, ?)
    """, (today, datetime.now().isoformat(), len(to_collect), collected, errors))
    db.commit()

    # Health summary
    total_active = db.execute("SELECT COUNT(*) FROM properties WHERE active=1").fetchone()[0]
    with_cal = db.execute(
        "SELECT COUNT(DISTINCT property_id) FROM calendar_snapshots WHERE snapshot_date=?", (today,)
    ).fetchone()[0]
    with_price = db.execute(
        "SELECT COUNT(DISTINCT property_id) FROM calendar_snapshots WHERE snapshot_date=? AND price IS NOT NULL AND price > 0", (today,)
    ).fetchone()[0]

    logger.info("=" * 50)
    logger.info("MARKET HEALTH SUMMARY")
    logger.info(f"  Active properties:  {total_active}")
    logger.info(f"  With calendar data: {with_cal}/{total_active} ({with_cal/total_active*100:.0f}%)")
    logger.info(f"  With pricing:       {with_price}/{total_active} ({with_price/total_active*100:.0f}%)")
    logger.info(f"  Collected this run: {collected}")
    logger.info(f"  Priced this run:    {priced}")
    logger.info(f"  Errors:             {errors}")
    logger.info(f"  Duration:           {duration/60:.1f} min")
    if with_cal < total_active:
        logger.warning(f"  MISSING: {total_active - with_cal} properties without calendar data")
    logger.info("=" * 50)
    db.close()


def retry_failures():
    """Retry properties that failed or were missed today."""
    db = get_db()
    today = date.today().isoformat()

    # Find properties with no calendar data today
    missing = db.execute("""
        SELECT p.id, p.platform_id, p.url, p.name, p.host_name, p.bedrooms
        FROM properties p
        WHERE p.active = 1 AND p.id NOT IN (
            SELECT DISTINCT property_id FROM calendar_snapshots WHERE snapshot_date = ?
        )
        ORDER BY p.id
    """, (today,)).fetchall()

    if not missing:
        logger.info("No failures to retry — all properties collected today")
        db.close()
        return

    logger.info(f"Retrying {len(missing)} failed properties...")
    db.close()

    # Reuse collect() but only for missing properties — temporarily mark others inactive
    # Actually simpler: just call collect with no batching, it skips already-collected
    collect()


def finalize():
    """Generate report, deploy, deactivate dead listings, export market comps."""
    db = get_db()
    today = date.today().isoformat()

    # ── Carry forward pricing from previous days ──
    logger.info("Carrying forward pricing from previous days...")
    carry_forward_pricing(db, today)

    # ── Deactivate dead listings ──
    # Properties that haven't been successfully collected in 14+ days
    total_active = db.execute("SELECT COUNT(*) FROM properties WHERE active=1").fetchone()[0]
    stale = db.execute("""
        SELECT p.id, p.name FROM properties p
        WHERE p.active = 1 AND p.id NOT IN (
            SELECT DISTINCT property_id FROM calendar_snapshots
            WHERE snapshot_date >= date(?, '-14 days')
        )
    """, (today,)).fetchall()

    # Safety: never deactivate more than 10% of properties at once
    if stale and len(stale) <= total_active * 0.10:
        logger.info(f"Deactivating {len(stale)} listings (no data in 14+ days):")
        for s in stale:
            logger.info(f"  - {s['name'][:60]}")
            db.execute("UPDATE properties SET active = 0 WHERE id = ?", (s["id"],))
        db.commit()
    elif stale:
        logger.warning(f"Skipping deactivation: {len(stale)} stale listings exceeds 10% safety threshold ({total_active} active)")

    # ── Stats ──
    total = db.execute("SELECT COUNT(*) FROM properties WHERE active=1").fetchone()[0]
    collected = db.execute(
        "SELECT COUNT(DISTINCT property_id) FROM calendar_snapshots WHERE snapshot_date=?",
        (today,)
    ).fetchone()[0]
    with_pricing = db.execute("""
        SELECT COUNT(DISTINCT property_id) FROM calendar_snapshots
        WHERE snapshot_date=? AND price IS NOT NULL AND price > 0
    """, (today,)).fetchone()[0]

    logger.info(f"Today: {collected}/{total} collected, {with_pricing} with pricing")
    db.close()

    # ── Generate report ──
    logger.info("Generating report...")
    subprocess.run(
        [sys.executable, "generate_report.py", "--db", str(MARKET_DB)],
        cwd=str(PROJECT_DIR), check=True
    )

    # ── Deploy ──
    logger.info("Deploying...")
    DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
    report_src = PROJECT_DIR / "market" / "report.html"
    if report_src.exists():
        (DEPLOY_DIR / "index.html").write_bytes(report_src.read_bytes())

        # Copy archived reports
        reports_dir = PROJECT_DIR / "market" / "reports"
        if reports_dir.exists():
            for d in reports_dir.iterdir():
                if d.is_dir() and d.name.startswith("20"):
                    rpt = d / "report.html"
                    if rpt.exists():
                        dest = DEPLOY_DIR / "reports" / d.name
                        dest.mkdir(parents=True, exist_ok=True)
                        (dest / "report.html").write_bytes(rpt.read_bytes())

        # --archive=tgz bypasses a recurring Node SSL "bad record mac" error on plain uploads.
        # Run from inside deploy/ so vercel treats it as the project root.
        result = subprocess.run(
            ["npx", "vercel", "deploy", "--prod", "--yes", "--archive=tgz"],
            cwd=str(PROJECT_DIR / "deploy"), check=False, capture_output=True, text=True
        )
        # Update str-prospects alias to point to new deploy
        deploy_url = None
        for line in (result.stdout or "").splitlines():
            if "logan-6330s-projects.vercel.app" in line and "deploy-" in line:
                deploy_url = line.strip().split()[-1] if line.strip() else None
                break
        if deploy_url:
            subprocess.run(
                ["npx", "vercel", "alias", "set", deploy_url, "str-prospects.vercel.app"],
                cwd=str(PROJECT_DIR), check=False
            )

    # ── Export market comps for audit tool ──
    logger.info("Exporting market comps...")
    export_market_comps()

    logger.info("Finalize complete!")


def carry_forward_pricing(db, today):
    """Copy pricing from the most recent previous day for properties missing it today."""
    missing = db.execute("""
        SELECT DISTINCT p.id, p.name FROM properties p
        WHERE p.active = 1 AND p.id NOT IN (
            SELECT DISTINCT property_id FROM calendar_snapshots
            WHERE snapshot_date = ? AND price IS NOT NULL AND price > 0
        ) AND p.id IN (
            SELECT DISTINCT property_id FROM calendar_snapshots WHERE snapshot_date = ?
        )
    """, (today, today)).fetchall()

    carried = 0
    for p in missing:
        prev = db.execute("""
            SELECT snapshot_date FROM calendar_snapshots
            WHERE property_id = ? AND price IS NOT NULL AND price > 0
            ORDER BY snapshot_date DESC LIMIT 1
        """, (p["id"],)).fetchone()
        if not prev:
            continue

        prev_prices = db.execute("""
            SELECT calendar_date, price FROM calendar_snapshots
            WHERE property_id = ? AND snapshot_date = ? AND price IS NOT NULL AND price > 0
        """, (p["id"], prev["snapshot_date"])).fetchall()

        for row in prev_prices:
            db.execute("""
                UPDATE calendar_snapshots SET price = ?
                WHERE property_id = ? AND snapshot_date = ? AND calendar_date = ?
                  AND (price IS NULL OR price = 0)
            """, (row["price"], p["id"], today, row["calendar_date"]))

        carried += 1

    db.commit()
    if carried:
        logger.info(f"  Carried forward pricing for {carried} properties")

    # Recompute metrics for carried-forward properties
    if carried:
        compute_metrics(db, today)


def compute_metrics(db, snapshot_date, property_ids=None):
    """Compute occupancy, ADR, and pricing metrics from calendar data."""
    if property_ids:
        placeholders = ",".join("?" for _ in property_ids)
        properties = db.execute(
            f"SELECT id FROM properties WHERE active=1 AND id IN ({placeholders})",
            property_ids
        ).fetchall()
    else:
        properties = db.execute("SELECT id FROM properties WHERE active=1").fetchall()

    for prop in properties:
        prop_id = prop[0]

        rows = db.execute("""
            SELECT calendar_date, available, price
            FROM calendar_snapshots
            WHERE property_id = ? AND snapshot_date = ?
              AND calendar_date >= ?
            ORDER BY calendar_date
        """, (prop_id, snapshot_date, snapshot_date)).fetchall()

        if not rows:
            continue

        dates_30 = rows[:30]
        dates_90 = rows[:90]

        def calc_occ(entries):
            total = len(entries)
            if total == 0:
                return None
            booked = sum(1 for e in entries if not e[1])
            return booked / total

        occ_30 = calc_occ(dates_30)
        occ_90 = calc_occ(dates_90)

        # ADR = average of ALL listed prices (not limited to 30/90 day window)
        all_prices = [r[2] for r in rows if r[2] and r[2] > 0]
        adr = sum(all_prices) / len(all_prices) if all_prices else 0

        # Use same ADR for both periods (it's the property's average listed rate)
        adr_30 = adr
        adr_90 = adr
        revpar_30 = adr * occ_30 if occ_30 else 0
        revpar_90 = adr * occ_90 if occ_90 else 0

        price_min = min(all_prices) if all_prices else None
        price_max = max(all_prices) if all_prices else None

        weekday_prices = []
        weekend_prices = []
        for r in rows:
            if r[2] and r[2] > 0:
                try:
                    d = datetime.strptime(r[0], "%Y-%m-%d")
                    if d.weekday() < 5:
                        weekday_prices.append(r[2])
                    else:
                        weekend_prices.append(r[2])
                except ValueError:
                    pass

        price_weekday = sum(weekday_prices) / len(weekday_prices) if weekday_prices else None
        price_weekend = sum(weekend_prices) / len(weekend_prices) if weekend_prices else None

        est_rev_30 = (adr_30 * occ_30 * 30) if adr_30 and occ_30 else None
        est_rev_90 = (adr_90 * occ_90 * 90) if adr_90 and occ_90 else None

        db.execute("""
            INSERT OR REPLACE INTO daily_metrics
                (property_id, snapshot_date, occupancy_30d, occupancy_90d,
                 adr_30d, adr_90d, revpar_30d, revpar_90d,
                 est_revenue_30d, est_revenue_90d,
                 min_price, max_price, weekday_avg_price, weekend_avg_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            prop_id, snapshot_date, occ_30, occ_90,
            adr_30, adr_90, revpar_30, revpar_90,
            est_rev_30, est_rev_90,
            price_min, price_max, price_weekday, price_weekend,
        ))

    db.commit()
    logger.info(f"  Metrics computed for {len(properties)} properties")


def collect_parallel(num_workers=2):
    """Run collection with multiple parallel workers, each handling a slice of properties."""
    import multiprocessing

    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM properties WHERE active=1").fetchone()[0]
    db.close()

    logger.info(f"Starting {num_workers} parallel workers for {total} properties")

    processes = []
    for i in range(1, num_workers + 1):
        p = multiprocessing.Process(target=collect, args=(i, num_workers))
        p.start()
        processes.append((i, p))
        time.sleep(3)  # Stagger starts to desync rate limiters

    # Wait for all workers
    for batch_num, p in processes:
        p.join()
        status = "OK" if p.exitcode == 0 else f"FAILED (exit {p.exitcode})"
        logger.info(f"  Worker {batch_num}/{num_workers}: {status}")

    logger.info("All workers complete")


def export_market_comps():
    """Export market comps JSON for the audit tool — grouped by guest tier."""
    db = get_db()
    today = date.today().isoformat()

    # Guest tier aggregates across the whole Mt Hood market
    tier_rows = db.execute("""
        SELECT
            CASE
                WHEN p.max_guests BETWEEN 1 AND 3 THEN 'tier_1_3'
                WHEN p.max_guests BETWEEN 4 AND 6 THEN 'tier_4_6'
                WHEN p.max_guests BETWEEN 7 AND 10 THEN 'tier_7_10'
                WHEN p.max_guests >= 11 THEN 'tier_11_plus'
            END as tier_key,
            CASE
                WHEN p.max_guests BETWEEN 1 AND 3 THEN '1-3 Guests'
                WHEN p.max_guests BETWEEN 4 AND 6 THEN '4-6 Guests'
                WHEN p.max_guests BETWEEN 7 AND 10 THEN '7-10 Guests'
                WHEN p.max_guests >= 11 THEN '11+ Guests'
            END as tier_label,
            COUNT(*) as cnt,
            ROUND(AVG(m.adr_30d), 0) as avg_adr,
            ROUND(AVG(m.occupancy_30d), 3) as avg_occ_30d,
            ROUND(AVG(m.occupancy_90d), 3) as avg_occ_90d,
            ROUND(AVG(p.overall_rating), 2) as avg_rating
        FROM properties p
        JOIN daily_metrics m ON m.property_id = p.id AND m.snapshot_date = (
            SELECT snapshot_date FROM daily_metrics GROUP BY snapshot_date HAVING COUNT(*) > 100 ORDER BY snapshot_date DESC LIMIT 1
        )
        WHERE p.active = 1 AND m.adr_30d > 0 AND m.occupancy_30d < 1.0
          AND p.max_guests IS NOT NULL
        GROUP BY tier_key
        ORDER BY tier_key
    """).fetchall()

    # Per-listing data for exact matches (now includes max_guests)
    listing_rows = db.execute("""
        SELECT p.id, p.market, p.bedrooms, p.max_guests, p.overall_rating, p.review_count,
               m.adr_30d, m.occupancy_30d, m.occupancy_90d, m.revpar_30d
        FROM properties p
        JOIN daily_metrics m ON m.property_id = p.id AND m.snapshot_date = (
            SELECT snapshot_date FROM daily_metrics GROUP BY snapshot_date HAVING COUNT(*) > 100 ORDER BY snapshot_date DESC LIMIT 1
        )
        WHERE p.active = 1 AND m.adr_30d > 0 AND m.occupancy_30d < 1.0
    """).fetchall()

    comps = {
        "snapshot_date": today,
        "total_listings": len(listing_rows),
        "by_listing": {},
        "by_guest_tier": {},
    }

    for r in listing_rows:
        airbnb_id = r["id"].replace("airbnb_", "")
        comps["by_listing"][airbnb_id] = {
            "market": r["market"],
            "bedrooms": r["bedrooms"],
            "max_guests": r["max_guests"],
            "rating": r["overall_rating"],
            "reviews": r["review_count"],
            "adr": round(r["adr_30d"]),
            "occupancy_30d": round(r["occupancy_30d"], 3) if r["occupancy_30d"] else None,
            "occupancy_90d": round(r["occupancy_90d"], 3) if r["occupancy_90d"] else None,
            "revpar": round(r["revpar_30d"]) if r["revpar_30d"] else None,
        }

    for r in tier_rows:
        comps["by_guest_tier"][r["tier_key"]] = {
            "tier_label": r["tier_label"],
            "count": r["cnt"],
            "avg_adr": r["avg_adr"],
            "avg_occupancy_30d": r["avg_occ_30d"],
            "avg_occupancy_90d": r["avg_occ_90d"],
            "avg_rating": r["avg_rating"],
        }

    out = PROJECT_DIR.parent / "simply-marketing" / "src" / "lib" / "audit" / "market-comps.json"
    if out.parent.exists():
        with open(out, "w") as f:
            json.dump(comps, f, indent=2)
        logger.info(f"  Exported {comps['total_listings']} listings to market-comps.json")
    else:
        logger.warning(f"  Audit tool path not found: {out.parent}")

    db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Market tracker collection")
    parser.add_argument("--batch", type=int, help="Batch number (1-indexed)")
    parser.add_argument("--of", type=int, dest="total_batches", help="Total number of batches")
    parser.add_argument("--workers", type=int, default=1, help="Parallel workers (default 1, recommend 2-3)")
    parser.add_argument("--retry", action="store_true", help="Retry today's failed properties")
    parser.add_argument("--finalize", action="store_true", help="Generate report, deploy, cleanup")
    args = parser.parse_args()

    if args.retry:
        retry_failures()
    elif args.finalize:
        finalize()
    elif args.workers > 1 and not args.batch:
        collect_parallel(args.workers)
    else:
        collect(batch=args.batch, total_batches=args.total_batches)

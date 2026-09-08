#!/usr/bin/env python3
"""Collect calendar availability, details and sampled pricing from Vacasa.com.

Vacasa pulled their Mt Hood homes off Airbnb in March 2026, so the Airbnb
collector never sees them. This scrapes vacasa.com directly and writes into the
same properties / calendar_snapshots / daily_metrics tables as everything else,
with platform='vacasa' and ids like 'vacasa_<unit_id>'.

Flow per unit:
  1. GET /unit/{id}                          -> JSON-LD details + CSRF token
  2. GET /api/unit-api/unit-calendar/{id}    -> day-by-day bookable flags
  3. GET /guest-com-api/get-unit-price-quote -> sampled per-night rates
  4. compute + save daily_metrics

Ported from the retired local Mac pipeline (collect_vacasa.py) on 2026-09-08.
Two things changed on the way in:
  * --max-minutes: a hard wall-clock budget. On the Mac this phase wedged for
    25+ hours several times on ConnectionResetError storms and silently ate the
    next day's run. Here it stops cleanly, logs what it got, and lets the
    workflow move on. The retry pass tomorrow picks up whatever was missed.
  * The unit list lives in config/vacasa_properties.yaml and is pruned to the
    homes still on vacasa.com. 42 of the original 118 left the brand in
    Aug-Sep 2026 (they return an empty calendar and redirect to a region page).

Usage:
    python market/collect_vacasa.py                    # all configured units
    python market/collect_vacasa.py --limit 3          # first 3 (smoke test)
    python market/collect_vacasa.py --max-minutes 110  # stop cleanly after 110 min
"""

import sys
import re
import json
import time
import random
import logging
import argparse
from datetime import date, timedelta
from html import unescape
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analytics.metrics import compute_daily_metrics
from src.models.database import init_database, save_calendar_snapshot, save_daily_metrics

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent
MARKET_DB = PROJECT_DIR / "market" / "market_tracker.db"
CONFIG = PROJECT_DIR / "config" / "vacasa_properties.yaml"

# ── Config ──────────────────────────────────────────────────────────────

_parser = argparse.ArgumentParser(description="Vacasa direct-scrape collection")
_parser.add_argument("--db", default=str(MARKET_DB), help="Path to SQLite database")
_parser.add_argument("--config", default=str(CONFIG), help="Vacasa properties config (yaml)")
_parser.add_argument("--limit", type=int, default=0, help="Only process the first N units (0=all)")
_parser.add_argument("--max-minutes", type=float, default=0,
                     help="Wall-clock budget; stop cleanly once exceeded (0=no limit)")
_args, _ = _parser.parse_known_args()

RATE_DELAY = 4.0  # seconds between requests to vacasa.com — it is sensitive
JITTER = 2.0

# ── Helpers ─────────────────────────────────────────────────────────────

def _create_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    return s


def _rate_wait():
    time.sleep(RATE_DELAY + random.uniform(0, JITTER))


def _extract_json_ld(html):
    """Pull all JSON-LD blocks from HTML."""
    blocks = []
    for m in re.finditer(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL):
        try:
            blocks.append(json.loads(m.group(1)))
        except json.JSONDecodeError:
            pass
    return blocks


def _extract_csrf(html):
    m = re.search(r'csrfmiddlewaretoken.*?value="([^"]+)"', html)
    return m.group(1) if m else None


def _extract_vacasa_unit_js(html):
    """Extract window.Vacasa.Unit inline JS data."""
    data = {}
    m = re.search(r"Rates:\s*\{([^}]+)\}", html)
    if m:
        block = m.group(1)
        for key in ("low", "high", "base_avg_rate"):
            pm = re.search(rf"'{key}':\s*'([^']*)'|{key}:\s*'([^']*)'", block)
            if pm:
                data[f"rate_{key}"] = pm.group(1) or pm.group(2)
    m = re.search(r"max_occupancy:\s*(\d+)", html)
    if m:
        data["max_guests"] = int(m.group(1))
    return data


def _carry_forward_pricing(db, property_id, today):
    """Copy the most recent successful pricing snapshot into today's snapshot.
    Returns the number of dates updated. Only fills empty/null prices."""
    prev = db.execute("""
        SELECT snapshot_date FROM calendar_snapshots
        WHERE property_id = ? AND price IS NOT NULL AND price > 0
          AND snapshot_date < ?
        ORDER BY snapshot_date DESC LIMIT 1
    """, (property_id, today.isoformat())).fetchone()
    if not prev:
        return 0
    rows = db.execute("""
        SELECT calendar_date, price FROM calendar_snapshots
        WHERE property_id = ? AND snapshot_date = ?
          AND price IS NOT NULL AND price > 0
    """, (property_id, prev[0])).fetchall()
    if not rows:
        return 0
    db.execute("BEGIN")
    updated = 0
    for cal_date, price in rows:
        cur = db.execute("""
            UPDATE calendar_snapshots SET price = ?
            WHERE property_id = ? AND snapshot_date = ? AND calendar_date = ?
              AND (price IS NULL OR price = 0)
        """, (price, property_id, today.isoformat(), cal_date))
        updated += cur.rowcount
    db.execute("COMMIT")
    return updated


def _parse_details(json_ld_blocks, unit_js):
    """Build a property details dict from JSON-LD and inline JS."""
    details = {"host_name": "Vacasa"}
    for block in json_ld_blocks:
        ld_type = block.get("@type", "")
        if ld_type == "VacationRental":
            acc = block.get("containsPlace", {})
            occ = acc.get("occupancy", {})
            if occ.get("value"):
                details["max_guests"] = int(occ["value"])
        elif ld_type == "Product":
            details["name"] = unescape(block.get("name", ""))
            desc = unescape(block.get("description", ""))
            m = re.search(r"(\d+)\s*Bed", desc)
            if m:
                details["bedrooms"] = int(m.group(1))
            m = re.search(r"([\d.]+)\s*Bath", desc)
            if m:
                details["bathrooms"] = float(m.group(1))
            agg = block.get("aggregateRating", {})
            if agg.get("ratingValue"):
                details["overall_rating"] = float(agg["ratingValue"])
            if agg.get("reviewCount"):
                details["review_count"] = int(agg["reviewCount"])
            images = block.get("image", [])
            if images:
                details["thumbnail_url"] = images[0] if isinstance(images, list) else images
    if unit_js.get("max_guests"):
        details["max_guests"] = unit_js["max_guests"]
    return details


def _load_units(config_path):
    with open(config_path) as f:
        config = yaml.safe_load(f) or {}
    units = []
    for market_key, market in config.get("markets", {}).items():
        for prop in market.get("properties", []):
            prop = dict(prop)
            prop["_market_key"] = market_key
            units.append(prop)
    return units


# ── Main ────────────────────────────────────────────────────────────────

def main():
    db = init_database(_args.db)
    db.isolation_level = None  # autocommit; explicit BEGIN/COMMIT below
    today = date.today()
    started = time.monotonic()
    budget_s = _args.max_minutes * 60 if _args.max_minutes > 0 else None

    all_props = _load_units(_args.config)
    if _args.limit > 0:
        all_props = all_props[:_args.limit]

    # Skip units already collected today (lets the retry pass be cheap).
    done_today = {
        r[0] for r in db.execute(
            "SELECT DISTINCT property_id FROM calendar_snapshots WHERE snapshot_date = ?",
            (today.isoformat(),)
        ).fetchall()
    }
    todo = [p for p in all_props if f"vacasa_{p['unit_id']}" not in done_today]
    logger.info(f"Vacasa: {len(all_props)} configured, {len(all_props) - len(todo)} already collected today, {len(todo)} to do"
                + (f", budget {_args.max_minutes:g} min" if budget_s else ""))

    random.shuffle(todo)  # spread failures across units run-to-run
    session = _create_session()
    success = fail = 0
    stopped_early = False

    for i, prop in enumerate(todo, 1):
        if budget_s and (time.monotonic() - started) > budget_s:
            logger.warning(f"Wall-clock budget of {_args.max_minutes:g} min reached after {i - 1} units — stopping cleanly. "
                           f"{len(todo) - (i - 1)} units left for the retry pass.")
            stopped_early = True
            break

        unit_id = str(prop["unit_id"])
        nickname = prop.get("nickname", "")
        market = prop["_market_key"]
        comp_set = prop.get("comp_set", "")
        internal_id = f"vacasa_{unit_id}"
        url = f"https://www.vacasa.com/unit/{unit_id}"

        try:
            logger.info(f"[{i}/{len(todo)}] {nickname} (unit {unit_id})")

            # 1. Unit page -> details + CSRF
            _rate_wait()
            resp = session.get(url, timeout=30, allow_redirects=True)
            if resp.status_code != 200:
                logger.warning(f"  Page returned {resp.status_code}, skipping")
                fail += 1
                continue
            if f"/unit/{unit_id}" not in resp.url:
                # Vacasa redirects delisted units to the nearest region page.
                logger.warning(f"  Unit no longer on vacasa.com (redirected to {resp.url}) — marking inactive")
                db.execute("UPDATE properties SET active = 0, updated_at = datetime('now') WHERE id = ?", (internal_id,))
                fail += 1
                continue

            html = resp.text
            csrf = _extract_csrf(html) or session.cookies.get("csrftoken", "")
            if not csrf:
                logger.warning("  No CSRF token found, skipping")
                fail += 1
                continue

            details = _parse_details(_extract_json_ld(html), _extract_vacasa_unit_js(html))
            bedrooms = details.get("bedrooms")
            rating = details.get("overall_rating")
            reviews = details.get("review_count")
            logger.info(f"  Details: {bedrooms}BR, {rating} stars, {reviews} reviews")

            db.execute("BEGIN")
            db.execute("""
                INSERT INTO properties (id, platform, platform_id, url, nickname, market, comp_set,
                                        name, bedrooms, bathrooms, max_guests, overall_rating,
                                        review_count, host_name, thumbnail_url, active)
                VALUES (?, 'vacasa', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(id) DO UPDATE SET
                    name = COALESCE(excluded.name, properties.name),
                    nickname = COALESCE(excluded.nickname, properties.nickname),
                    bedrooms = COALESCE(excluded.bedrooms, properties.bedrooms),
                    bathrooms = COALESCE(excluded.bathrooms, properties.bathrooms),
                    max_guests = COALESCE(excluded.max_guests, properties.max_guests),
                    overall_rating = COALESCE(excluded.overall_rating, properties.overall_rating),
                    review_count = COALESCE(excluded.review_count, properties.review_count),
                    host_name = COALESCE(excluded.host_name, properties.host_name),
                    thumbnail_url = COALESCE(excluded.thumbnail_url, properties.thumbnail_url),
                    market = excluded.market,
                    comp_set = excluded.comp_set,
                    active = 1,
                    updated_at = datetime('now')
            """, (
                internal_id, unit_id, url, nickname, market, comp_set,
                details.get("name"), bedrooms, details.get("bathrooms"),
                details.get("max_guests"), rating, reviews,
                details.get("host_name"), details.get("thumbnail_url"),
            ))
            db.execute("COMMIT")

            # 2. Calendar API
            _rate_wait()
            cal_resp = session.get(
                f"https://www.vacasa.com/api/unit-api/unit-calendar/{unit_id}",
                headers={"X-CSRFToken": csrf, "X-Requested-With": "XMLHttpRequest", "Referer": url},
                timeout=30,
            )
            if cal_resp.status_code != 200:
                logger.warning(f"  Calendar API returned {cal_resp.status_code}")
                fail += 1
                continue
            cal_data = cal_resp.json().get("calendar", {})
            if not cal_data:
                logger.warning("  Empty calendar response")
                fail += 1
                continue

            # bookable=true -> available. Cap at 365 days; Vacasa returns ~2.25 years.
            cutoff = today + timedelta(days=2)
            horizon = today + timedelta(days=365)
            day_dicts = []
            for cal_date_str, flags in sorted(cal_data.items()):
                if cal_date_str < cutoff.isoformat() or cal_date_str > horizon.isoformat():
                    continue
                day_dicts.append({
                    "date": cal_date_str,
                    "available": flags.get("bookable", False),
                    "price": None,  # calendar API has no per-night prices
                    "price_currency": "USD",
                    "min_nights": None,
                })
            if not day_dicts:
                logger.warning("  No calendar dates after cutoff")
                fail += 1
                continue

            save_calendar_snapshot(db, internal_id, today, day_dicts)
            bookable = sum(1 for d in day_dicts if d["available"])
            total = len(day_dicts)
            logger.info(f"  Calendar: {total} days, {bookable} available ({(total - bookable) / total:.0%} occupied)")

            # 3. Sampled pricing via the quote API (up to 4 dates, several stay lengths)
            available_dates = [d["date"] for d in day_dicts if d["available"]]
            if available_dates:
                sample_count = min(4, len(available_dates))
                step = max(1, len(available_dates) // sample_count)
                sample_dates = [available_dates[k * step] for k in range(sample_count)]
                prices = {}
                for check_in in sample_dates:
                    ci = date.fromisoformat(check_in)
                    for nights in (2, 3, 4, 5, 7):
                        try:
                            _rate_wait()
                            co = ci + timedelta(days=nights)
                            q = session.get(
                                "https://www.vacasa.com/guest-com-api/get-unit-price-quote",
                                params={"unit_id": unit_id,
                                        "check_in": ci.strftime("%Y/%m/%d"),
                                        "check_out": co.strftime("%Y/%m/%d"),
                                        "adults": 2, "children": 0, "pets": 0},
                                headers={"X-CSRFToken": csrf, "X-Requested-With": "XMLHttpRequest", "Referer": url},
                                timeout=15,
                            )
                            if q.status_code != 200:
                                continue
                            night_arr = q.json().get("nights", [])
                            if not night_arr:
                                continue  # unavailable for this stay length; try longer
                            rate = night_arr[0].get("rate", {}).get("unformatted")
                            if rate and rate > 0:
                                prices[check_in] = rate
                                break
                        except Exception as e:
                            logger.warning(f"    Quote error for {check_in} ({nights}n): {type(e).__name__}: {e}")
                            break  # network error — don't hammer other stay lengths
                if prices:
                    db.execute("BEGIN")
                    for cal_date, price in prices.items():
                        db.execute("""
                            UPDATE calendar_snapshots SET price = ?
                            WHERE property_id = ? AND snapshot_date = ? AND calendar_date = ?
                        """, (price, internal_id, today.isoformat(), cal_date))
                    db.execute("COMMIT")
                    logger.info(f"  Pricing: {len(prices)}/{len(sample_dates)} samples, avg ${sum(prices.values()) / len(prices):.0f}/night")
                else:
                    carried = _carry_forward_pricing(db, internal_id, today)
                    logger.info(f"  Pricing: 0 fresh, carried forward {carried} from prior day" if carried else "  Pricing: 0 samples returned")

            # 4. Metrics
            metrics = compute_daily_metrics(db, internal_id, today)
            save_daily_metrics(db, internal_id, today, metrics)
            adr = metrics.get("adr_30d", 0)
            logger.info(f"  Metrics: 30d Occ={metrics.get('occupancy_30d', 0):.0%}" + (f", ADR=${adr:.0f}" if adr else ""))
            success += 1

        except Exception as e:
            logger.error(f"  Error: {type(e).__name__}: {e}")
            try:
                db.execute("ROLLBACK")
            except Exception:
                pass
            fail += 1

    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    total_vacasa = db.execute("SELECT COUNT(*) FROM properties WHERE active=1 AND platform='vacasa'").fetchone()[0]
    with_cal = db.execute("""
        SELECT COUNT(DISTINCT cs.property_id) FROM calendar_snapshots cs
        JOIN properties p ON cs.property_id = p.id
        WHERE p.platform = 'vacasa' AND cs.snapshot_date = ?
    """, (today.isoformat(),)).fetchone()[0]
    with_price = db.execute("""
        SELECT COUNT(DISTINCT cs.property_id) FROM calendar_snapshots cs
        JOIN properties p ON cs.property_id = p.id
        WHERE p.platform = 'vacasa' AND cs.snapshot_date = ?
          AND cs.price IS NOT NULL AND cs.price > 0
    """, (today.isoformat(),)).fetchone()[0]
    elapsed = (time.monotonic() - started) / 60

    logger.info("=" * 50)
    logger.info("VACASA COLLECTION SUMMARY")
    logger.info(f"  Active Vacasa properties: {total_vacasa}")
    logger.info(f"  With calendar today:      {with_cal}")
    logger.info(f"  With pricing today:       {with_price}")
    logger.info(f"  Succeeded / failed:       {success} / {fail}")
    logger.info(f"  Elapsed:                  {elapsed:.0f} min" + ("  (stopped at budget)" if stopped_early else ""))
    logger.info("=" * 50)
    db.close()


if __name__ == "__main__":
    main()

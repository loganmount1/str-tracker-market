"""Compute performance metrics from calendar snapshot data."""

import sqlite3
from datetime import date, timedelta


def compute_daily_metrics(db: sqlite3.Connection, property_id: str,
                          snapshot_date: date) -> dict:
    """Compute all derived metrics for a property on a given snapshot date."""
    metrics = {}

    # Get ALL priced dates for this snapshot (not just within a window)
    # so we can extrapolate weekday/weekend rates into windows with no samples
    all_priced = db.execute("""
        SELECT calendar_date, price
        FROM calendar_snapshots
        WHERE property_id = ?
          AND snapshot_date = ?
          AND available = 1
          AND price IS NOT NULL
          AND price > 0
    """, (property_id, snapshot_date.isoformat())).fetchall()

    # Build weekday/weekend averages from all sampled prices
    all_weekday_prices = [r["price"] for r in all_priced
                          if date.fromisoformat(r["calendar_date"]).weekday() < 4]
    all_weekend_prices = [r["price"] for r in all_priced
                          if date.fromisoformat(r["calendar_date"]).weekday() >= 4]
    fallback_weekday = (sum(all_weekday_prices) / len(all_weekday_prices)
                        if all_weekday_prices else None)
    fallback_weekend = (sum(all_weekend_prices) / len(all_weekend_prices)
                        if all_weekend_prices else None)
    # If only one type, use it for both
    if fallback_weekday and not fallback_weekend:
        fallback_weekend = fallback_weekday
    elif fallback_weekend and not fallback_weekday:
        fallback_weekday = fallback_weekend

    for label, window_days in [("30d", 30), ("90d", 90)]:
        # Start 2 days out to skip same-day/next-day check-in cutoff false data
        start_date = snapshot_date + timedelta(days=2)
        end_date = snapshot_date + timedelta(days=window_days)

        rows = db.execute("""
            SELECT calendar_date, available, price
            FROM calendar_snapshots
            WHERE property_id = ?
              AND snapshot_date = ?
              AND calendar_date >= ?
              AND calendar_date < ?
        """, (property_id, snapshot_date.isoformat(),
              start_date.isoformat(), end_date.isoformat())).fetchall()

        if not rows:
            continue

        total_days = len(rows)
        unavailable_days = sum(1 for r in rows if not r["available"])

        # Collect direct prices from this window
        available_with_price = [r["price"] for r in rows
                                if r["available"] and r["price"] is not None
                                and r["price"] > 0]

        # If we have direct prices in-window, use them
        if available_with_price:
            adr = sum(available_with_price) / len(available_with_price)
        elif fallback_weekday:
            # Extrapolate: estimate ADR from weekday/weekend rates applied
            # to the day-of-week mix in this window
            weekday_count = sum(1 for r in rows
                                if date.fromisoformat(r["calendar_date"]).weekday() < 4)
            weekend_count = total_days - weekday_count
            adr = ((fallback_weekday * weekday_count + fallback_weekend * weekend_count)
                   / total_days)
        else:
            adr = 0

        occupancy = unavailable_days / total_days if total_days > 0 else 0
        revpar = occupancy * adr
        est_revenue = unavailable_days * adr

        metrics[f"occupancy_{label}"] = round(occupancy, 4)
        metrics[f"adr_{label}"] = round(adr, 2)
        metrics[f"revpar_{label}"] = round(revpar, 2)
        metrics[f"est_revenue_{label}"] = round(est_revenue, 2)

    metrics.update(_compute_booking_velocity(db, property_id, snapshot_date))
    metrics.update(_compute_price_analysis(db, property_id, snapshot_date))

    return metrics


def _compute_booking_velocity(db: sqlite3.Connection, property_id: str,
                              snapshot_date: date) -> dict:
    """Compare today's calendar to yesterday's to detect new bookings."""
    yesterday = snapshot_date - timedelta(days=1)

    changes = db.execute("""
        SELECT
            t.calendar_date,
            y.available as was_available,
            t.available as is_available
        FROM calendar_snapshots t
        LEFT JOIN calendar_snapshots y
            ON y.property_id = t.property_id
            AND y.calendar_date = t.calendar_date
            AND y.snapshot_date = ?
        WHERE t.property_id = ?
          AND t.snapshot_date = ?
          AND y.available IS NOT NULL
          AND y.available != t.available
    """, (yesterday.isoformat(), property_id, snapshot_date.isoformat())).fetchall()

    new_bookings = sum(1 for c in changes if c["was_available"] and not c["is_available"])
    cancellations = sum(1 for c in changes if not c["was_available"] and c["is_available"])

    return {
        "new_bookings_since_last": new_bookings,
        "cancellations_since_last": cancellations,
    }


def _compute_price_analysis(db: sqlite3.Connection, property_id: str,
                            snapshot_date: date) -> dict:
    """Analyze pricing patterns from available dates."""
    end_date = snapshot_date + timedelta(days=90)

    rows = db.execute("""
        SELECT calendar_date, price
        FROM calendar_snapshots
        WHERE property_id = ?
          AND snapshot_date = ?
          AND calendar_date >= ?
          AND calendar_date < ?
          AND available = 1
          AND price IS NOT NULL
    """, (property_id, snapshot_date.isoformat(),
          snapshot_date.isoformat(), end_date.isoformat())).fetchall()

    if not rows:
        return {"min_price": None, "max_price": None,
                "weekend_avg_price": None, "weekday_avg_price": None}

    prices = [r["price"] for r in rows]

    weekend_prices = [r["price"] for r in rows
                      if date.fromisoformat(r["calendar_date"]).weekday() in (4, 5)]
    weekday_prices = [r["price"] for r in rows
                      if date.fromisoformat(r["calendar_date"]).weekday() not in (4, 5)]

    return {
        "min_price": round(min(prices), 2),
        "max_price": round(max(prices), 2),
        "weekend_avg_price": (round(sum(weekend_prices) / len(weekend_prices), 2)
                              if weekend_prices else None),
        "weekday_avg_price": (round(sum(weekday_prices) / len(weekday_prices), 2)
                              if weekday_prices else None),
    }

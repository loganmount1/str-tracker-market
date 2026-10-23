"""Small live check using the same collector as the daily workflow (no DB writes)."""
import importlib.util
import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.collectors.airbnb import AirbnbCollector
from src.utils.http import RateLimiter


def main():
    logging.basicConfig(level=logging.INFO)
    collector = AirbnbCollector(RateLimiter(delays={'airbnb.com': 3.5}, jitter=1.5))
    print(json.dumps({
        'accept_encoding': collector.session.headers['Accept-Encoding'],
        'brotli_installed': bool(importlib.util.find_spec('brotli')),
    }), flush=True)
    calendars = priced = 0
    for listing_id in ('4134519', '843612154468227481', '16083160'):
        try:
            days = collector.collect_calendar(listing_id)
            future = [d for d in days if d.date > date.today().isoformat()]
            assert len(future) >= 300, 'Incomplete calendar horizon'
            details = collector.collect_details(listing_id)
            available = [d.date for d in future if d.available]
            prices = collector.collect_pricing(
                listing_id, available, max_samples=2,
                min_nights_map={d.date: d.min_nights or 1 for d in future},
            )
            calendars += 1
            priced += bool(prices)
            print(json.dumps({
                'listing': listing_id, 'future_calendar_days': len(future),
                'available_days': len(available), 'rating': details.overall_rating,
                'price_samples': prices,
            }), flush=True)
        except Exception as exc:
            print(json.dumps({'listing': listing_id, 'error': str(exc)}), flush=True)
    print(f'LIVE CHECK: {calendars}/3 calendars; {priced}/3 homes with fresh prices', flush=True)
    return 0 if calendars >= 2 and priced >= 1 else 1


if __name__ == '__main__':
    sys.exit(main())

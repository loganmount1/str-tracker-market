# Mt Hood Airbnb Market Scraper

A public, self-contained scraper for the Mt Hood, Oregon short-term-rental market on Airbnb. It does two things:

1. **Discovery** — searches Airbnb across the Mt Hood corridor to find whole-home listings and records a daily market price snapshot (`collect_market_data.py` + `market/sync_new_listings.py`).
2. **Daily collection** — for every discovered listing, pulls the 12-month availability calendar, listing details/reviews, and sampled nightly pricing, then computes occupancy / ADR / RevPAR metrics (`market/collect_all.py`).

## Towns covered

The discovery pass searches and filters to the Mt Hood corridor communities:

- Brightwood
- Government Camp
- Mt Hood (incl. Mount Hood Village / Wemme)
- Rhododendron
- Welches
- Zigzag

## Data and credentials

This repository contains **only the scraping code**. The SQLite database (`market/market_tracker.db`) and all credentials are kept in a separate private repository and are never committed here. In automated runs the database is restored from the private data repo and credentials are injected via GitHub Actions secrets (see `.github/workflows/scrape.yml`).

## Quick start (local)

```bash
pip install -r requirements.txt

# 1. Discover listings + record a market price snapshot
python collect_market_data.py
python market/init_db.py          # first run only: create market/market_tracker.db
python market/sync_new_listings.py

# 2. Collect calendars, details, reviews, and pricing
python market/collect_all.py --workers 4
python market/collect_all.py --retry      # retry any failures
python market/collect_all.py --finalize   # compute metrics + housekeeping
```

## Notes

- Rate limiting is built in (`src/utils/http.py`). Respect it to avoid IP blocks.
- All dates are stored as ISO strings (`YYYY-MM-DD`).
- Property IDs follow the `airbnb_{listing_id}` convention.
- The Airbnb API key in `src/collectors/airbnb.py` is Airbnb's **public** client-side web key (the same one every browser sends); it is not a private credential.

# MANIFEST

Carve-out of the Mt Hood Airbnb **market scraper** from the private `str-tracker`
project, prepared for publication as a public repo. Source root:
`/Users/simply/str-tracker`.

## Files copied

| Destination (relative)                  | Source path                                                  |
|-----------------------------------------|--------------------------------------------------------------|
| `collect_market_data.py`                | `/Users/simply/str-tracker/collect_market_data.py`           |
| `requirements.txt`                      | `/Users/simply/str-tracker/requirements.txt`                 |
| `market/collect_all.py`                 | `/Users/simply/str-tracker/market/collect_all.py`            |
| `market/init_db.py`                     | `/Users/simply/str-tracker/market/init_db.py`                |
| `market/sync_new_listings.py`           | `/Users/simply/str-tracker/market/sync_new_listings.py`      |
| `src/__init__.py`                       | `/Users/simply/str-tracker/src/__init__.py`                  |
| `src/models/__init__.py`                | `/Users/simply/str-tracker/src/models/__init__.py`           |
| `src/models/database.py`                | `/Users/simply/str-tracker/src/models/database.py`           |
| `src/utils/__init__.py`                 | `/Users/simply/str-tracker/src/utils/__init__.py`            |
| `src/utils/http.py`                     | `/Users/simply/str-tracker/src/utils/http.py`                |
| `src/utils/logging_config.py`           | `/Users/simply/str-tracker/src/utils/logging_config.py`      |
| `src/collectors/__init__.py`            | `/Users/simply/str-tracker/src/collectors/__init__.py`       |
| `src/collectors/base.py`                | `/Users/simply/str-tracker/src/collectors/base.py`           |
| `src/collectors/airbnb.py`              | `/Users/simply/str-tracker/src/collectors/airbnb.py`         |

14 source files copied (verbatim). Newly authored files: `.gitignore`,
`README.md`, `.github/workflows/scrape.yml`, `MANIFEST.md`.

## Import / dependency trace

Traced transitively from the four entry scripts. The full closure:

- `collect_market_data.py` → `src.models.database`, `src.utils.http`
- `market/collect_all.py` → `src.collectors.airbnb`, `src.utils.http`
- `market/init_db.py` → stdlib only (sqlite3, logging)
- `market/sync_new_listings.py` → stdlib only
- `src.collectors.airbnb` → `src.collectors.base`, `src.utils.http`
- `src.collectors.base` → stdlib only (abc, dataclasses, datetime, typing)
- `src.models.database` → stdlib only (sqlite3, json, datetime, pathlib)
- `src.utils.http` → external pip packages `requests`, `tenacity`
- `src.utils.logging_config` → stdlib only (listed in the brief; nothing in the
  closure actually imports it, but copied as instructed — harmless)

No module in the closure imports from `src/enrichment`, `src/sheets`,
`src/analytics`, or any CRM/skip-trace/owner/buyer module.

## Files deliberately EXCLUDED (and why)

- **All database files** (`market/market_tracker.db`, `*.db-shm`, `*.db-wal`,
  `*.db.gz`, `data/tracker.db`, `data/`) — contain real scraped data; kept
  private per the brief.
- **`config/`** — credentials/settings; not needed by the copied scripts and may
  contain secrets.
- **`.git/`, `.vercel/`, `.env`** — repo internals / deploy config / secrets.
- **`src/enrichment/`** (`owner_lookup.py`, `__init__.py`) — PII / owner lookup.
- **`src/sheets/`** (`client.py`, `formatter.py`) — Google Sheets integration.
- **`src/analytics/`** (`metrics.py`, `trends.py`) — not imported by the market
  closure; analytics for the main tracker report, not needed here.
- **`src/collectors/vrbo.py`, `src/collectors/booking.py`** — stub collectors not
  imported by the market pipeline.
- **`src/main.py`** — main (non-market) collection pipeline; pulls in analytics
  and is not part of the market entry points.
- **`sync_to_crm.py`, `skip_trace.py`, `enrich_*.py`, `collect_vacasa.py`,
  `collect_pricing.py`, `sync_market_to_supabase.py`, `track_mt_hood_market.py`,
  `generate_report.py`, `generate_property_report.py`, `drop_alerts.py`** —
  enrichment / CRM / PII / Supabase / Vacasa / Redfin / reporting code, excluded
  per the brief.
- **All `*.log`, `*.sh`, `*.png`, `CLAUDE.md`, `migrations/`, `deploy/`,
  `tests/`, `reports/`** — not part of the minimal market scraper closure.

## Verification results

### Import / compile check (run from the repo root)

- `python3 -m py_compile` on every copied `.py` → **PASS** (all compile).
- `python3 -c "import src.collectors.airbnb, src.models.database, src.utils.http"`
  → **PASS** (only a harmless `urllib3`/LibreSSL `NotOpenSSLWarning` from the
  external `requests` dependency; not an error).
- Each of the four entry scripts loads its `src.*` dependencies successfully
  via `importlib` → **PASS**.
- The only unresolved imports are external pip packages (`requests`, `tenacity`),
  which are listed in `requirements.txt` and expected to be installed — this is
  fine.

### Secret / PII scan

Patterns scanned across the whole tree: `sb_secret`, `service_role`,
`eyJ[A-Za-z0-9]` (JWT), `SUPABASE`, `api_key`, `password`,
`BEGIN ... PRIVATE KEY`, plus owner/buyer data.

- **No Supabase URLs/keys, no service-role keys, no JWTs, no passwords, no
  private keys, no owner/buyer PII** found in any copied file.
- One match for the `api_key` pattern:
  `src/collectors/airbnb.py:18` →
  `AIRBNB_API_KEY = "d306zoyjsyarp7ifhu67rjxn52tv0t20"`.
  This is **NOT a private secret** — it is Airbnb's *public* client-side web API
  key (the same value every browser sends on every Airbnb page; the in-file
  comment documents this). It is not a Simply credential and not PII, so it was
  left in place. No `os.environ` substitution was made or needed.
- Schema column names like `owner_name`, `new_owner_name`, etc. appear only as
  table-structure DDL in `src/models/database.py` (the full schema is shared with
  the private tracker). These are column *names*, not data values, and contain no
  secrets or PII.

## Known limitations (not blockers)

- `market/collect_all.py`'s `--finalize` path shells out to `generate_report.py`
  (intentionally NOT copied — it's the reporting/PII-adjacent layer) and writes a
  comps JSON into a sibling `../simply-marketing/` directory. These are runtime
  side effects, not imports, so they do not affect the import check. In the
  GitHub Actions workflow the finalize step uses `continue-on-error: true`, and
  metric computation (the useful part of finalize) still runs before the report
  subprocess. Publishers who want a standalone report can add their own.

#!/bin/bash
# Build the static market dashboard into _site/ for GitHub Pages deployment.
# Runs in the cloud workflow AFTER collection, so the market DB is already local
# to the runner (no 580MB download to a laptop — that fragile Mac pipeline is what
# this replaces). Regenerates a rolling 30-day window + the main report.
set -e
PYTHON="${PYTHON:-python}"
DB="market/market_tracker.db"

echo "[site] backfilling daily_metrics for the last 30 days (in case finalize didn't persist)..."
$PYTHON - <<'PY'
import sqlite3, sys
sys.path.insert(0, '.')
from market.collect_all import compute_metrics
db = sqlite3.connect('market/market_tracker.db', timeout=120); db.row_factory = sqlite3.Row
have = {r[0] for r in db.execute("SELECT DISTINCT snapshot_date FROM daily_metrics").fetchall()}
want = {r[0] for r in db.execute("SELECT DISTINCT snapshot_date FROM calendar_snapshots WHERE snapshot_date >= date('now','-30 days')").fetchall()}
for d in sorted(want - have):
    compute_metrics(db, d); print("  backfilled", d)
db.close()
PY

echo "[site] generating last 30 daily reports + main..."
for i in $(seq 0 30); do
    D=$($PYTHON -c "from datetime import date,timedelta; print((date.today()-timedelta(days=$i)).isoformat())")
    $PYTHON generate_report.py --db "$DB" --date "$D" 2>&1 | grep -E "Report generated|Error|Traceback" | sed "s/^/  $D: /" || true
done
$PYTHON generate_report.py --db "$DB" 2>&1 | grep -E "Report generated|using latest|Error" | sed "s/^/  MAIN: /" || true

echo "[site] assembling _site/ ..."
rm -rf _site && mkdir -p _site
cp market/report.html _site/index.html
[ -d market/reports ] && cp -r market/reports _site/reports
# dates.json manifest (drives the date dropdown; report derives its own base path)
$PYTHON -c "
import os, json
d = sorted([x for x in os.listdir('_site/reports') if x.startswith('20')], reverse=True) if os.path.isdir('_site/reports') else []
json.dump(d, open('_site/dates.json','w'))
print('  dates.json:', len(d), 'dates, latest', d[0] if d else 'none')
"
# tiny 404 -> keeps Pages happy on unknown paths
echo '<meta http-equiv="refresh" content="0; url=./">' > _site/404.html
echo "[site] done. _site contains $(find _site -name report.html | wc -l | tr -d ' ') report(s)."

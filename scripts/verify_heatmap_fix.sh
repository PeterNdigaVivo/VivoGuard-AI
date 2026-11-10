#!/usr/bin/env bash
# verify_heatmap_fix.sh — proves the durable (delta-published) heatmap
# accumulates MONOTONICALLY instead of resetting every inference slice.
#
# What it does:
#   1. Brings up api, worker-alerts, worker-inference and frontend from
#      the EXISTING images (no --build, no --force-recreate).
#   2. Polls Redis every 30s for 5 minutes, reading vg:heatmap:day:60
#      (Moi Ave Ch4 — one of the Phase 0 long-slice cameras).
#   3. Asserts grid_sum never DECREASES across samples (a decrease is
#      exactly the old reset-on-restart bug). A flat series is reported
#      as inconclusive, not failure — an empty store adds no footfall.
#
# Run from the repo root on the server:  bash scripts/verify_heatmap_fix.sh
set -u

CAMERA_ID="${CAMERA_ID:-60}"
KEY="vg:heatmap:day:${CAMERA_ID}"
SAMPLES="${SAMPLES:-11}"        # 11 samples x 30s = 5 minutes
INTERVAL="${INTERVAL:-30}"

cd "$(dirname "$0")/.."

echo "== verify_heatmap_fix: starting services (existing images) =="
docker compose up -d api worker-alerts worker-inference frontend || {
  echo "FAIL: docker compose up failed"; exit 1; }

grid_sum() {
  # GET the JSON payload from the redis container and sum the grid.
  docker compose exec -T redis redis-cli --raw GET "$KEY" | python3 -c '
import json, sys
raw = sys.stdin.read().strip()
if not raw:
    print("MISSING")
else:
    try:
        payload = json.loads(raw)
        print(sum(sum(row) for row in payload.get("grid", [])))
    except Exception:
        print("UNPARSEABLE")
'
}

echo "== polling ${KEY} every ${INTERVAL}s, ${SAMPLES} samples =="
prev=""
first=""
last=""
decreases=0
for i in $(seq 1 "$SAMPLES"); do
  sum="$(grid_sum)"
  ts="$(date '+%H:%M:%S')"
  echo "[$ts] sample ${i}/${SAMPLES}: grid_sum=${sum}"
  if [ "$sum" = "MISSING" ] || [ "$sum" = "UNPARSEABLE" ]; then
    echo "        (key not readable yet — camera may still be warming up)"
  else
    [ -z "$first" ] && first="$sum"
    last="$sum"
    if [ -n "$prev" ]; then
      # python for the comparison: sums can be floats.
      if python3 -c "import sys; sys.exit(0 if float('$sum') < float('$prev') else 1)"; then
        decreases=$((decreases + 1))
        echo "        DECREASE detected: ${prev} -> ${sum}"
      fi
    fi
    prev="$sum"
  fi
  [ "$i" -lt "$SAMPLES" ] && sleep "$INTERVAL"
done

echo "== result =="
if [ -z "$first" ]; then
  echo "FAIL: ${KEY} was never readable — is camera ${CAMERA_ID} streaming?"
  exit 1
fi
if [ "$decreases" -gt 0 ]; then
  echo "FAIL: grid_sum decreased ${decreases} time(s) — heatmap still resetting"
  exit 1
fi
if python3 -c "import sys; sys.exit(0 if float('$last') > float('$first') else 1)"; then
  echo "SUCCESS: grid_sum monotonically increased (${first} -> ${last}), no resets"
  exit 0
fi
echo "INCONCLUSIVE: grid_sum never decreased but also never grew"
echo "(${first} -> ${last}). No reset observed; re-run while the store has footfall."
exit 0

#!/usr/bin/env bash
# Denný quiet drip — SK 07–20: pomalý spoľahlivý import (web nemrzne).
# 1 leaf × ≤8 ks × sleep≥2s + publish ≤15 + homepage smoke (jidoka).
# Bulk heijunka/overnight ostáva večer / --force-daytime.
#
#   bash bot/run_daily_quiet.sh
#   bash bot/run_daily_quiet.sh --skip-import   # len publish + smoke
set -euo pipefail
cd "$(dirname "$0")/.."

SKIP_IMPORT=0
for arg in "$@"; do
  case "$arg" in
    --skip-import) SKIP_IMPORT=1 ;;
  esac
done

echo "==== DAILY QUIET DRIP $(date -u +%Y-%m-%dT%H:%MZ) ===="
dq="$(python3 bot/daytime_quiet.py 2>/dev/null || true)"
echo "daytime: $dq"

if [[ "$dq" == quiet* ]]; then
  echo "Quiet okno (07–20 SK): drip import + publish + smoke."
  if [[ "$SKIP_IMPORT" -eq 0 ]]; then
    # Pomalý drip — limity clampuje daytime_quiet / --quiet-drip
    python3 bot/run_wurth_cheap_catalog.py \
      --quiet-drip \
      --leaves 1 \
      --limit 8 \
      --sleep 2.0 \
      --publish-limit 15 \
      --max-price 20000 || {
        rc=$?
        echo "quiet drip rc=$rc (2 = jidoka homepage)"
        exit "$rc"
      }
  else
    python3 bot/publish_ready_drafts.py --name-contains Würth --max-price 20000 --limit 15 --sleep 0.6 || true
    python3 bot/publish_ready_drafts.py --max-price 200 --limit 10 --sleep 0.6 || true
    code="$(curl -sI -o /dev/null -w '%{http_code}' https://www.smartgaraz.sk/ || echo 000)"
    echo "homepage HTTP $code"
  fi
  if [[ -f bot/data/wurth_cheap_progress.json ]]; then
    python3 - <<'PY'
import json
from pathlib import Path
p = json.loads(Path("bot/data/wurth_cheap_progress.json").read_text(encoding="utf-8"))
print(f"wurth_leaves={len(p.get('done_cyids') or [])}/{len(p.get('leaf_urls') or [])}")
PY
  fi
  python3 bot/customer_paths_smoke.py --write-report
  echo "==== QUIET DRIP DONE ===="
  exit 0
fi

# Mimo quiet okna — normálny Würth cheap batch (večer)
LEAVES=12
for arg in "$@"; do
  if [[ "$arg" =~ ^[0-9]+$ ]]; then
    LEAVES="$arg"
  fi
done
python3 bot/run_wurth_cheap_catalog.py --leaves "$LEAVES" --limit 35 --sleep 0.4 --max-price 20000
echo "==== QUIET WINDOW CLOSED — evening batch done ===="

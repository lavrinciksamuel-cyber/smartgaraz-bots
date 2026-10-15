#!/usr/bin/env bash
# Večerný SILNÝ import — vyvážene LKQ + Szekop + Würth (stále ≤50/job).
# Default: až po 20:00 Europe/Bratislava (inak exit 0 — cez deň quiet).
#
#   bash bot/run_evening_strong_import.sh           # čaká / beží večer
#   bash bot/run_evening_strong_import.sh --now     # force (CI / manuál)
#   SG_EVENING_WAVES=15 SG_EVENING_LIMIT=40 bash bot/run_evening_strong_import.sh --now
#
# Toyota Way: dávky ≤50, publish + smoke medzi vlnami, žiadne tisícky naraz.
# Round-robin: LKQ wave → Würth leaf batch → (každá N-tá) Szekop TecDoc.
set -euo pipefail
cd "$(dirname "$0")/.."

FORCE_NOW=0
for arg in "$@"; do
  case "$arg" in
    --now|--force) FORCE_NOW=1 ;;
  esac
done

WAVES="${SG_EVENING_WAVES:-12}"
JOBS="${SG_EVENING_JOBS:-10}"
LIMIT="${SG_EVENING_LIMIT:-40}"
SLEEP="${SG_EVENING_SLEEP:-0.3}"
SZEKOP_EVERY="${SG_EVENING_SZEKOP_EVERY:-1}"   # každá LKQ vlna → Szekop (nikto nezaostáva)
SZEKOP_JOBS="${SG_EVENING_SZEKOP_JOBS:-5}"
SZEKOP_LIMIT="${SG_EVENING_SZEKOP_LIMIT:-40}"
# Expand TecDoc listy (nové leafy) — soft-fail; seeds ostávajú fallback.
SZEKOP_EXPAND="${SG_SZEKOP_EXPAND:-1}"
# Fitment repair spomaľuje tok — default vypnuté; zapni SG_EVENING_FITMENT=1
FITMENT="${SG_EVENING_FITMENT:-0}"
WURTH_LEAVES="${SG_EVENING_WURTH_LEAVES:-6}"   # Würth leafov medzi LKQ vlnami
WURTH_LIMIT="${SG_EVENING_WURTH_LIMIT:-40}"
WURTH_EVERY="${SG_EVENING_WURTH_EVERY:-1}"     # 1 = každá LKQ vlna

# Cap heijunka
if [[ "$LIMIT" -gt 50 ]]; then LIMIT=50; fi
if [[ "$JOBS" -gt 12 ]]; then JOBS=12; fi
if [[ "$SZEKOP_LIMIT" -gt 50 ]]; then SZEKOP_LIMIT=50; fi
if [[ "$WURTH_LIMIT" -gt 50 ]]; then WURTH_LIMIT=50; fi
if [[ "$WURTH_LEAVES" -gt 8 ]]; then WURTH_LEAVES=8; fi

echo "==== EVENING STRONG IMPORT $(date -u +%Y-%m-%dT%H:%MZ) ===="

# --- Quiet gate (SK 07–20) ---
if [[ "$FORCE_NOW" -eq 0 ]]; then
  dq="$(python3 bot/daytime_quiet.py 2>/dev/null || true)"
  echo "daytime: $dq"
  if [[ "$dq" == quiet* ]]; then
    echo "Ešte quiet okno — silný import až po 20:00 SK."
    echo "CI / manuál: bash bot/run_evening_strong_import.sh --now"
    echo "Alebo nastav SG_DAYTIME_QUIET=force_run"
    exit 0
  fi
fi

export SG_DAYTIME_QUIET=force_run
export AI_MODE=cheap
export IMPORT_IN_STOCK=1
export LKQ_HTTP_TIMEOUT="${LKQ_HTTP_TIMEOUT:-90}"
export WURTH_B2B="${WURTH_B2B:-1}"

smoke() {
  code="$(curl -sI -o /dev/null -w '%{http_code}' https://www.smartgaraz.sk/ || echo 000)"
  echo "homepage: $code"
  if [[ "$code" != "200" ]]; then
    echo "JIDOKA: homepage != 200 — STOP" >&2
    exit 2
  fi
}

echo "profile: waves=$WAVES lkq_jobs=$JOBS limit=$LIMIT szekop_every=$SZEKOP_EVERY szekop_expand=$SZEKOP_EXPAND fitment=$FITMENT wurth_leaves=$WURTH_LEAVES"
python3 bot/prepare_parts_import.py --skip-sync || true
python3 bot/sync_taxonomy.py --ensure-only || true
smoke

# Reset failed na začiatku večera (1×)
python3 bot/heijunka_vehicle_parts.py --wave3-only --jobs 1 --limit 1 --dry-run --reset-failed >/dev/null 2>&1 || true
python3 bot/heijunka_szekop_parts.py --jobs 1 --limit 1 --dry-run --reset-failed --seeds-only >/dev/null 2>&1 || true

for wave in $(seq 1 "$WAVES"); do
  echo "==== LKQ WAVE $wave / $WAVES $(date -u +%H:%M:%S) ===="

  # Prvá polovica večera = Wave3 (filtre+kotúče), potom Wave6 mop-up cats
  if [[ "$wave" -le $((WAVES / 2)) ]]; then
    python3 bot/heijunka_vehicle_parts.py \
      --wave3-only --jobs "$JOBS" --limit "$LIMIT" --sleep "$SLEEP" \
      || echo "lkq_wave_rc=$?"
  else
    python3 bot/heijunka_vehicle_parts.py \
      --wave6-only --jobs "$JOBS" --limit "$LIMIT" --sleep "$SLEEP" \
      || echo "lkq_wave6_rc=$?"
  fi

  python3 bot/publish_ready_drafts.py --limit 50 --sleep 0.5 || true
  smoke

  # Interleave Würth cheap catalog (vyvážené popri LKQ/Szekop)
  if (( wave % WURTH_EVERY == 0 )); then
    echo "==== WURTH BATCH after wave $wave (leaves=$WURTH_LEAVES limit=$WURTH_LIMIT) ===="
    python3 bot/run_wurth_cheap_catalog.py \
      --force-daytime \
      --leaves "$WURTH_LEAVES" \
      --limit "$WURTH_LIMIT" \
      --sleep "$SLEEP" \
      --publish-limit 40 \
      --max-price 20000 \
      || echo "wurth_rc=$?"
    smoke
  fi

  # Interleave Szekop — seeds (overené listy); expand len SG_SZEKOP_EXPAND=1
  if (( wave % SZEKOP_EVERY == 0 )); then
    echo "==== SZEKOP BATCH after wave $wave (expand=$SZEKOP_EXPAND) ===="
    sz_args=(--jobs "$SZEKOP_JOBS" --limit "$SZEKOP_LIMIT" --sleep "$SLEEP")
    if [[ "$SZEKOP_EXPAND" == "1" ]]; then
      sz_args+=(--expand-filters)
    else
      sz_args+=(--seeds-only)
    fi
    python3 bot/heijunka_szekop_parts.py "${sz_args[@]}" \
      || echo "szekop_rc=$?"
    python3 bot/publish_ready_drafts.py --limit 40 --sleep 0.5 || true
    smoke
  fi

  # Fitment repair (opt-in) — nespomaľovať hlavný LKQ tok
  if [[ "$FITMENT" == "1" ]] && (( wave % 2 == 0 )); then
    for slug in kabinove-filtre olejove-filtre vzduchove-filtre brzdove-kotuce brzdove-dosticky; do
      python3 bot/expand_fitment_from_crossref.py --repair --only-missing \
        --category-slug "$slug" --limit 40 --order desc >/dev/null 2>&1 || true
    done
  fi

  smoke

  # Stav fronty
  python3 - <<'PY'
import importlib.util
import json
from pathlib import Path
spec = importlib.util.spec_from_file_location("h", "bot/heijunka_vehicle_parts.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
st = m._load_state()
done, failed = st.get("done", {}), st.get("failed", {})
for name, q in [("w3", m.WAVE3_ALL_CARS), ("w6", m.WAVE6_MOPUP)]:
    pend = sum(1 for j in q if m._job_key(j) not in done and m._job_key(j) not in failed)
    print(f"STATUS {name}_pending={pend} done={len(done)} failed={len(failed)}")
wp = Path("bot/data/wurth_cheap_progress.json")
if wp.is_file():
    p = json.loads(wp.read_text(encoding="utf-8"))
    print(f"STATUS wurth={len(p.get('done_cyids') or [])}/{len(p.get('leaf_urls') or [])}")
PY
done

# Záverečný Szekop mop — seeds; expand len opt-in
echo "==== SZEKOP FINAL mop (expand=$SZEKOP_EXPAND) ===="
sz_final=(--jobs 4 --limit "$SZEKOP_LIMIT" --sleep "$SLEEP")
if [[ "$SZEKOP_EXPAND" == "1" ]]; then
  sz_final+=(--expand-filters)
else
  sz_final+=(--seeds-only)
fi
python3 bot/heijunka_szekop_parts.py "${sz_final[@]}" || true
python3 bot/publish_ready_drafts.py --limit 50 --sleep 0.5 || true
smoke
python3 bot/site_eval.py || true

echo "==== EVENING STRONG IMPORT DONE $(date -u +%H:%M:%S) ===="

# Masívny import autodielov — LKQ + Szekop (staged pilot)

**Nie bulk tisícky naraz.** Toyota Way: **≤20–50 SKU / job**, ďalšia dávka až po QA + smoke.

Vlastný shop strom = `taxonomy.py` / `PARTS_TAXONOMY.md`.  
Dodávatelia = crawl seed + inšpirácia pokrytia (nie kópia TecDoc stromu).  
Licencia / zákaz scrape konkurencie: `CATALOG_LICENSING.md`.

## Pripravenosť (checklist)

| Položka | Stav |
|---------|------|
| Shop taxonómia + L3 listy | ✅ `sync_taxonomy.py --ensure-only` |
| Fitment `sgv-*` + validácia | ✅ `validate_vehicle_hierarchy.py` |
| LKQ login secrets | ✅ `LKQ_USER` / `LKQ_PASSWORD` |
| LKQ Heijunka (auto×needle) | ✅ `heijunka_vehicle_parts.py` |
| Szekop oleje (univerzal) | ✅ `DEFAULT_CATEGORY_URLS` |
| Szekop TecDoc diely (vozidlo) | ✅ leaf crawl + `heijunka_szekop_parts.py` |
| Večerný silný import | ✅ `run_evening_strong_import.sh` / workflow 20:05 SK |
| Obrázky / bulk LKQ+Szekop | ⛔ stále NO-GO na tisícky (heijunka ≤50/job) |

Spusti pred každou vlnou:

```bash
python bot/prepare_parts_import.py
```

## Fáza 1 — LKQ (hlavný tok)

1. Nájdi Garage URL (needle zo `supplier_category_map.json`):
   ```bash
   python bot/lkq_find_vehicle_category.py \
     --make skoda --model 'octavia iii' --engine '1.6 TDI' --category filtre
   ```
2. Dry-run (žiadny zápis do WC):
   ```bash
   python bot/import_pipeline.py --supplier lkq --limit 5 --cheap --in-stock --dry-run \
     --category-url 'https://www.lkq.sk/Catalog/…'
   ```
3. Pilot dávka:
   ```bash
   python bot/import_pipeline.py --supplier lkq --limit 20 --cheap --fast --in-stock \
     --category-url '…' --force-daytime   # len ak treba cez deň
   python bot/publish_ready_drafts.py --limit 40
   python bot/site_eval.py   # homepage smoke 200
   ```
4. Autonómna fronta (večer / `SG_DAYTIME_QUIET=force_run`):
   ```bash
   bash bot/run_heijunka_loop.sh 80 5 20 '' 1          # Wave3 filtre+kotúče
   bash bot/run_heijunka_loop.sh 80 5 20 0/2 0         # mop-up + shard
   # Wave 6 (stierače/zapaľovanie/ložiská/…):
   python bot/heijunka_vehicle_parts.py --wave6-only --jobs 5 --limit 20
   ```

**Nikdy** nespúšťaj bare `--supplier lkq` na diely — default je olejová kategória.

## Fáza 2 — Szekop TecDoc (pilot) — ready

Entry: `https://szekopsk.nextis.cz/sk/katalog/tecdoc/osobne`  
Príklad motor: `…/skoda/octavia-iii-…/1-6-tdi/106/11195/58760`  
Top skupiny = `?category-id=` (Filtre=`100005`, Brzdy=`100006`, …) — pozri  
`parts_pilot_seeds.json` → `tecdoc_top_category_ids`.  
Overené listy: `tecdoc_pilot_leaves` (olejový / vzduchový / kabínový filter).

Expand listov (API, nie Woo ID):

```bash
python bot/szekop_tecdoc_categories.py \
  --url 'https://szekopsk.nextis.cz/sk/katalog/tecdoc/osobne/skoda/octavia-iii-5e3-nl3-nr3/1-6-tdi/106/11195/58760' \
  --expand 100005
```

Quiet drip (denný, nikoho neruší — ≤5–8 ks, sleep≥2s):

```bash
bash bot/run_szekop_quiet_drip.sh
# alebo:
python bot/import_pipeline.py --supplier szekop --quiet-drip --cheap --in-stock \
  --limit 5 --sleep 2.5 \
  --category-url 'https://szekopsk.nextis.cz/…/olejovy-filter/…/100259/?path=100005~100259'
```

Oleje / univerzal: default `--supplier szekop` (bez TecDoc leaf).

## Večerný SILNÝ import (od 20:00 SK)

Profile: **LKQ hlavný tok** Wave3→Wave6 (`jobs=8`, `limit=40`) + Szekop
interleaved na **overených seeds** (`--seeds-only`, oleje/filtre OK).
TecDoc `--expand-filters` je opt-in (`SG_SZEKOP_EXPAND=1`) — večer často padá.
Fitment repair default vypnutý (`SG_EVENING_FITMENT=1` ak treba). Homepage smoke.

```bash
# Automat: GitHub Actions „Evening strong parts import“ — cron 18:05 UTC (20:05 SK)
# Ručne po 20:00:
bash bot/run_evening_strong_import.sh
# CI / test cez deň:
bash bot/run_evening_strong_import.sh --now
# Len Szekop fronta (overené listy):
python bot/heijunka_szekop_parts.py --seeds-only --jobs 3 --limit 35 --force-daytime
# Opt-in expand (nestabilné):
SG_SZEKOP_EXPAND=1 python bot/heijunka_szekop_parts.py --expand-filters --jobs 3 --limit 35 --force-daytime
```

Stále **nie** tisícky naraz — heijunka max 50/job, publish + jidoka.
Staged pokrytie celého katalógu; filtre nespomaľujú hlavný import.

## Seeds

`bot/data/parts_pilot_seeds.json` — overené LKQ Garage URL + Szekop entry/map.  
Path ID v URL = len navigácia; do Woo **neukladáme**.

## Denný quiet

SK 07–20: bulk heijunka pauza (web speed-max). Večer / noc / `force_run`.

## Po každej dávke (Jidoka)

1. `publish_ready_drafts.py --limit 40`
2. 1–2 PDP + homepage 200
3. `expand_fitment_from_crossref.py --repair` na filtre/brzdy (každú 2. vlnu)
4. Report do `bot/reports/ops-parts-pilot-*.md`

# smartgaraz-bots

Public GitHub Actions runners for SmartGaráž supplier imports (LKQ, Szekop, Würth).

**Why public:** free unlimited Actions minutes on GitHub-hosted runners.
**What is public:** import bot Python code + workflows only.
**What stays private:** shop theme, HQ, accounting, LavroDoc data, credentials (GitHub Secrets + HQ).

## Workflows
- `catalog-lkq.yml` — scheduled LKQ import
- `catalog-szekop.yml` — scheduled Szekop catalog
- `szekop-stock.yml` — scheduled Szekop stock update
- `supplier-import.yml` — manual import per supplier
- `wurth-sk-feed.yml` — manual Würth file import
- `wurth-catalog-sync.yml` — weekly Würth catalog sync
- `evening-strong-import.yml` — manual only
- `heijunka-parts-nightly.yml` — manual only
- `refresh-clicked.yml` — every 6 h, B2B price/stock refresh of clicked products (skips early if queue empty) — moved from smartgaraz-web
- `cheap-ops.yml` — 2×/day customer smoke → publish ready drafts → cost guard (shares concurrency with promote-drafts) — moved from smartgaraz-web
- `daily-site-eval.yml` — 04:00 UTC site eval + product QA fix (≤40) — moved from smartgaraz-web
- `security-monitor.yml` — every 15 min external security checks of all sites (uptime, SSL, DNS, headers, defacement; hourly exposed files, WordPress, blacklists) → HQ Bezpečnosť. Stdlib only, counts-only logs. Secret `SEC_SCANNER_SECRET` (optional `SEC_HQ_URL`); without it the job skips in seconds. Test: `cd security && python -m unittest test_monitor`
- `photo-integrity.yml` — Sunday 03:25 UTC, product image integrity. Counts only in the log. The row list goes to HQ when `HQ_SYNC_TOKEN` is set. Test: `cd bot && python -m unittest test_image_integrity`

## Secrets (repo Settings → Secrets)
`WC_URL`, `WC_CONSUMER_KEY`, `WC_CONSUMER_SECRET`, `HQ_SYNC_TOKEN`, `SG_INSIGHTS_KEY` (refresh-clicked + daily-site-eval; without it refresh-clicked skips in ~10 s)
Optional fallbacks (normally from HQ): `LKQ_*`, `SZEKOP_*`, `WURTH_*`

Supplier logins are fetched at runtime from HQ (`HQ_SYNC_TOKEN`), not stored in this repo.

## Public logs (this repo is public)
Actions logs are world-readable. Every workflow sets `SG_PUBLIC_LOGS=1` and
`PYTHONPATH=bot/public_ci`, so `bot/public_ci/sitecustomize.py` wraps stdout/stderr
of every Python process with `bot/log_redact.py`: purchase (B2B) prices, margins,
profit, supplier URLs (host only) and customer search terms are masked (`•••`).
If the redactor can't load, Python exits (fail closed).

Rules for new workflows:
- triggers: `schedule` + `workflow_dispatch` only (never `pull_request*` with secrets);
- no `actions/upload-artifact` / no committing `bot/reports/` — use
  `python bot/public_report_summary.py <glob>` (counts-only, redacted);
- supplier logins via `python -m suppliers.hq_credentials <slug…>`, never GitHub secrets;
- set `permissions: contents: read` and `timeout-minutes`.

Test: `cd bot && python -m unittest test_log_redact`

# smartgaraz-bots

Public GitHub Actions runners for SmartGaráž supplier imports (LKQ, Szekop, Würth).

**Why public:** free unlimited Actions minutes on GitHub-hosted runners.
**What is public:** import bot Python code + workflows only.
**What stays private:** shop theme, HQ, accounting, LavroDoc data, credentials (GitHub Secrets + HQ).

## Workflows
- `supplier-import.yml` — HQ-triggered / manual full or prices import per supplier
- `evening-strong-import.yml` — staged LKQ + Würth + Szekop into drafts
- `heijunka-parts-nightly.yml` — nightly staged LKQ mop
- `refresh-clicked.yml` — every 6 h, B2B price/stock refresh of clicked products (skips early if queue empty) — moved from smartgaraz-web
- `cheap-ops.yml` — 2×/day customer smoke → publish ready drafts → cost guard (shares concurrency with promote-drafts) — moved from smartgaraz-web
- `daily-site-eval.yml` — 04:00 UTC site eval + product QA fix (≤40) — moved from smartgaraz-web

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

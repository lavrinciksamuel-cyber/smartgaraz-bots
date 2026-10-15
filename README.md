# smartgaraz-bots

Public GitHub Actions runners for SmartGaráž supplier imports (LKQ, Szekop, Würth).

**Why public:** free unlimited Actions minutes on GitHub-hosted runners.
**What is public:** import bot Python code + workflows only.
**What stays private:** shop theme, HQ, accounting, LavroDoc data, credentials (GitHub Secrets + HQ).

## Workflows
- `supplier-import.yml` — HQ-triggered / manual full or prices import per supplier
- `evening-strong-import.yml` — staged LKQ + Würth + Szekop into drafts
- `heijunka-parts-nightly.yml` — nightly staged LKQ mop

## Secrets (repo Settings → Secrets)
`WC_URL`, `WC_CONSUMER_KEY`, `WC_CONSUMER_SECRET`, `HQ_SYNC_TOKEN`
Optional fallbacks (normally from HQ): `LKQ_*`, `SZEKOP_*`, `WURTH_*`

Supplier logins are fetched at runtime from HQ (`HQ_SYNC_TOKEN`), not stored in this repo.

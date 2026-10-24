#!/usr/bin/env python3
"""Cheap pre-check for refresh-clicked: is there anything in the refresh queue?

stdlib only (runs before `pip install`). Prints only the COUNT (queue items are
customer click data — never log product names/IDs in the public repo).
Writes `count=<n>` and `run=true|false` to $GITHUB_OUTPUT.
Exit code is always 0 so a missing key / network hiccup just skips the run.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request


def main() -> int:
    base = (os.environ.get("WC_URL") or "").strip().rstrip("/")
    key = (os.environ.get("SG_INSIGHTS_KEY") or "").strip()
    hours = os.environ.get("REFRESH_HOURS", "24")
    limit = os.environ.get("REFRESH_LIMIT", "30")
    count, run = 0, False
    if not base or not key:
        print("::notice::refresh-clicked skipped: repo secret SG_INSIGHTS_KEY (or WC_URL) is not set")
    else:
        url = f"{base}/wp-json/smartgaraz/v1/refresh-queue?" + urllib.parse.urlencode({"limit": limit, "hours": hours})
        req = urllib.request.Request(url, headers={"X-SG-Insights-Key": key, "User-Agent": "smartgaraz-bot"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
            count = len(data.get("items") or [])
            run = count > 0
            print(f"refresh queue: {count} item(s)")
        except urllib.error.HTTPError as e:
            print(f"::warning::refresh-queue HTTP {e.code} — skipping run")
        except Exception as e:  # network etc.
            print(f"::warning::refresh-queue unreachable ({type(e).__name__}) — skipping run")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"count={count}\nrun={'true' if run else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

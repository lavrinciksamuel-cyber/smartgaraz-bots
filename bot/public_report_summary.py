#!/usr/bin/env python3
"""Print a SAFE short summary of bot reports to the (public) Actions log.

Replaces `actions/upload-artifact` in this public repo: artifacts are
world-readable and the reports contain supplier catalog URLs, product lists
and customer search data. Full reports stay on the runner and are discarded.

  python bot/public_report_summary.py bot/reports/ops-heijunka-*.md promote-report.json

* *.md  → headings + short "- key: value" bullets only, redacted, no URLs,
          search/insights sections dropped, ≤ MAX_LINES per file.
* *.json→ only numeric counters from "summary" (no examples / product names).
"""
from __future__ import annotations

import glob
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from log_redact import redact  # noqa: E402

MAX_LINES = 25
_DROP_SECTION = re.compile(r"(?i)(search|hľadan|insight|query|quer|dopyt|zero|klik|click|zazmluv|doplniť|runs|zoznam|examples)")
_BULLET = re.compile(r"^\s*[-*]\s+[^\s].{0,120}$")


def md_summary(path: Path) -> list[str]:
    out: list[str] = []
    skip_level = 0  # >0 → inside a dropped section of this heading level
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.rstrip()
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            if skip_level and level > skip_level:
                continue  # sub-heading of a dropped section stays dropped
            skip_level = level if _DROP_SECTION.search(line) else 0
            if not skip_level:
                out.append(line)
            continue
        skip = bool(skip_level)
        if skip or not _BULLET.match(line):
            continue
        if "http" in line or "`" in line:  # URLs / supplier keys
            continue
        out.append(redact(line))
        if len(out) >= MAX_LINES:
            out.append("  … (truncated)")
            break
    return out


def json_summary(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"(unreadable: {type(exc).__name__})"]
    summary = data.get("summary") if isinstance(data, dict) else None
    if not isinstance(summary, dict):
        return ["(no summary)"]
    lines = []
    for k, v in summary.items():
        if isinstance(v, (int, float, bool)):
            lines.append(f"- {k}: {v}")
        elif isinstance(v, dict) and all(isinstance(x, (int, float)) for x in v.values()):
            lines.append(f"- {k}: " + ", ".join(f"{a}={b}" for a, b in v.items()))
    return [redact(x) for x in lines]


def main(argv: list[str]) -> int:
    files: list[Path] = []
    for pattern in argv:
        files += [Path(p) for p in sorted(glob.glob(pattern))]
    if not files:
        print("(no reports)")
        return 0
    for p in files:
        if p.name.endswith("-latest.md"):
            continue
        print(f"=== {p.name}")
        body = json_summary(p) if p.suffix == ".json" else md_summary(p)
        print("\n".join(body) if body else "(empty)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

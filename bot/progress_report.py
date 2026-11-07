"""Live progress for long runs. Counts only. No URLs or names.

Prints a line about every 2 minutes or 200 items, and POSTs the same counts
to the existing HQ alert endpoint (kind=progress, then kind=done).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

INTERVAL_S = 120
EVERY_N = 200
BAR_WIDTH = 16


def _pct(done: int, total: Optional[int]) -> Optional[int]:
    if not total or total <= 0:
        return None
    return max(0, min(100, int(done * 100 / total)))


def format_progress(bot: str, done: int, total: Optional[int], ok: int, changed: int, errors: int) -> str:
    """Bar and percent only when total is known."""
    pct = _pct(done, total)
    tail = f"ok={int(ok)} changed={int(changed)} errors={int(errors)}"
    if pct is None:
        return f"progress bot={bot} done={int(done)} {tail}"
    filled = int(BAR_WIDTH * pct / 100)
    bar = "#" * filled + "-" * (BAR_WIDTH - filled)
    return f"progress bot={bot} [{bar}] {pct}% {int(done)}/{int(total)} {tail}"


def counts_payload(bot: str, done: int, total: Optional[int], ok: int, changed: int, errors: int) -> dict:
    pct = _pct(done, total)
    return {
        "bot": bot,
        "done": int(done),
        "total": int(total) if total else None,
        "percent": pct,
        "ok": int(ok),
        "changed": int(changed),
        "errors": int(errors),
    }


def post_hq(kind: str, counts: dict) -> None:
    """POST counts to /api/sync/bot-alert. Unknown fields are for HQ to store."""
    token = (os.environ.get("HQ_SYNC_TOKEN") or "").strip()
    if not token:
        return
    if len(token) >= 4:
        print(f"::add-mask::{token}", file=sys.stderr, flush=True)
    try:
        from hq_url import hq_url
        base = hq_url()
    except Exception:
        return
    body = {
        "workflow": os.environ.get("GITHUB_WORKFLOW") or "progress",
        "repo": os.environ.get("GITHUB_REPOSITORY") or "",
        "title": kind,
        "body": json.dumps(counts, ensure_ascii=False),
        "url": "",
        "level": "info",
        "kind": kind,
    }
    body.update(counts)
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        base + "/api/sync/bot-alert",
        data=data,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "User-Agent": "smartgaraz-bot",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read(64)
    except urllib.error.HTTPError as exc:
        print(f"hq http {int(exc.code)}", flush=True)
    except Exception as exc:
        print(f"hq {type(exc).__name__}", flush=True)


class Progress:
    def __init__(
        self,
        bot: str,
        total: Optional[int] = None,
        *,
        interval_s: float = INTERVAL_S,
        every_n: int = EVERY_N,
        post: Optional[Callable[[str, dict], None]] = None,
        clock: Optional[Callable[[], float]] = None,
    ):
        self.bot = bot
        self.total = total
        self.interval_s = interval_s
        self.every_n = every_n
        self._post = post if post is not None else post_hq
        self._clock = clock or time.monotonic
        self.done = 0
        self.ok = 0
        self.changed = 0
        self.errors = 0
        self._last_at = self._clock()
        self._last_done = 0
        self.events: list[str] = []

    def set(self, *, done: int, ok: int = 0, changed: int = 0, errors: int = 0, total: Optional[int] = None, final: bool = False) -> None:
        self.done = int(done)
        self.ok = int(ok)
        self.changed = int(changed)
        self.errors = int(errors)
        if total is not None:
            self.total = total
        if final:
            self._emit("done")
            return
        due_n = self.done - self._last_done >= self.every_n
        due_t = self._clock() - self._last_at >= self.interval_s
        if self.done > 0 and (due_n or due_t):
            self._emit("progress")

    def finish(self) -> None:
        self._emit("done")

    def _emit(self, kind: str) -> None:
        line = format_progress(self.bot, self.done, self.total, self.ok, self.changed, self.errors)
        print(line, flush=True)
        counts = counts_payload(self.bot, self.done, self.total, self.ok, self.changed, self.errors)
        try:
            self._post(kind, counts)
        except Exception as exc:
            print(f"hq {type(exc).__name__}", flush=True)
        self._last_at = self._clock()
        self._last_done = self.done
        self.events.append(kind)

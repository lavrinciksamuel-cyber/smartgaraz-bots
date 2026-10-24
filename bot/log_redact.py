"""Redact business data from logs of the PUBLIC smartgaraz-bots repo.

Public repo => Actions logs, step summaries and artifacts are world-readable.
Never show: purchase (B2B) prices, margins/profit, supplier product URLs,
customer search terms. Retail prices are public on the shop anyway, but on a
line that also mentions cost/margin every amount is masked.

Activated for every Python process in a workflow by:
    env:
      SG_PUBLIC_LOGS: "1"
      PYTHONPATH: ${{ github.workspace }}/bot/public_ci   # loads sitecustomize.py
which wraps sys.stdout / sys.stderr with RedactingStream.

CLI (filter a file or stdin, e.g. a report before printing it):
    python bot/log_redact.py [FILE ...]
"""
from __future__ import annotations

import atexit
import io
import os
import re
import sys
from typing import Iterable, TextIO
from urllib.parse import urlparse

# Hosts whose URLs are fine to show (own shop, infra). Everything else = path hidden.
SAFE_HOSTS = (
    "smartgaraz.sk",
    "github.com",
    "githubusercontent.com",
    "pythonhosted.org",
    "pypi.org",
    "dataovozidlech.cz",  # public open-data source (lavrodoc-cz-rsv)
)

# Words that make a line "pricing-sensitive" (SK/CZ/EN, with/without diacritics).
_SENSITIVE = re.compile(
    r"(?i)(n[áa]kup|zisk|mar[žz]|margin|profit|cost|b2b|purchase|wholesale|"
    r"ve[ľl]koobchod|dealer|netto|net_price|nett?o_|buy_?price|min_profit|"
    r"cenotvorba|velkoobchodn|dph_in|eur_cost)"
)
# key=value / "key": value for sensitive keys (integers too).
_SENSITIVE_KV = re.compile(
    r"(?i)(\"?[a-z_]*(?:cost|nakup|nákup|margin|marza|marža|profit|zisk|b2b|purchase|buy_price|netto)[a-z_]*\"?\s*[:=]\s*)"
    r"(\"?)-?\d+(?:[.,]\d+)?(\"?)"
)
_MONEY = re.compile(r"(?i)-?\d[\d\s]*(?:[.,]\d+)?\s*(?:€|eur\b|kč|czk\b|%)")
_DECIMAL = re.compile(r"(?<![\w#/.-])-?\d+[.,]\d+(?![\w.])")
_URL = re.compile(r"https?://[^\s\"'<>)\]]+")
# Customer search data (search_insights / refresh-queue): never print query strings.
_SEARCH = re.compile(r"(?i)((?:query|search(?:_term)?|hľadan[ée]|hladane|q)\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|\S+)")

MASK = "•••"


def _url_repl(m: re.Match) -> str:
    url = m.group(0)
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return "<url>"
    if any(host == h or host.endswith("." + h) for h in SAFE_HOSTS):
        return url
    return f"https://{host}/<redacted>" if host else "<url>"


def redact(line: str) -> str:
    """Redact one line (no newline handling)."""
    if not line:
        return line
    # GitHub workflow commands (::add-mask:: etc.) must stay byte-exact.
    if line.startswith("::"):
        return line
    out = _URL.sub(_url_repl, line)
    out = _SEARCH.sub(lambda m: m.group(1) + MASK, out)
    out = _SENSITIVE_KV.sub(lambda m: m.group(1) + m.group(2) + MASK + m.group(3), out)
    if _SENSITIVE.search(out):
        out = _MONEY.sub(MASK, out)
        out = _DECIMAL.sub(MASK, out)
    return out


def redact_text(text: str) -> str:
    return "\n".join(redact(x) for x in text.split("\n"))


class RedactingStream(io.TextIOBase):
    """Line-buffered proxy around a text stream that redacts every line."""

    def __init__(self, inner: TextIO):
        self._inner = inner
        self._buf = ""

    # --- proxy plumbing -------------------------------------------------
    def __getattr__(self, name):  # encoding, buffer, fileno, isatty, ...
        return getattr(self._inner, name)

    def writable(self) -> bool:
        return True

    @property
    def encoding(self):  # type: ignore[override]
        return getattr(self._inner, "encoding", "utf-8")

    def fileno(self) -> int:
        return self._inner.fileno()

    def isatty(self) -> bool:
        return False

    # --- redaction ------------------------------------------------------
    def write(self, s) -> int:
        if not isinstance(s, str):
            s = str(s)
        self._buf += s
        if "\n" in self._buf:
            *lines, self._buf = self._buf.split("\n")
            self._inner.write("".join(redact(x) + "\n" for x in lines))
        return len(s)

    def flush(self) -> None:
        if self._buf:
            self._inner.write(redact(self._buf))
            self._buf = ""
        try:
            self._inner.flush()
        except Exception:
            pass


def install() -> bool:
    """Wrap stdout/stderr when SG_PUBLIC_LOGS=1. Idempotent."""
    if os.environ.get("SG_PUBLIC_LOGS", "").strip() not in {"1", "true", "yes"}:
        return False
    for name in ("stdout", "stderr"):
        cur = getattr(sys, name, None)
        if cur is None or isinstance(cur, RedactingStream):
            continue
        wrapped = RedactingStream(cur)
        setattr(sys, name, wrapped)
        atexit.register(wrapped.flush)
    return True


def _cli(paths: Iterable[str]) -> int:
    paths = list(paths)
    if not paths:
        for line in sys.stdin:
            sys.stdout.write(redact(line.rstrip("\n")) + "\n")
        return 0
    for p in paths:
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    sys.stdout.write(redact(line.rstrip("\n")) + "\n")
        except OSError as exc:
            print(f"[redact] skip {p}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))

"""Auto-loaded by Python when PYTHONPATH points here (public workflows only).

Wraps stdout/stderr so purchase prices, margins, supplier URLs and customer
search terms never reach the world-readable Actions log. See bot/log_redact.py.
"""
import os
import sys

try:  # Woo batch odpovede majú pri publish 2× Set-Cookie na produkt (>100 hlavičiek)
    import http.client

    http.client._MAXHEADERS = max(getattr(http.client, "_MAXHEADERS", 100), 2000)
except Exception:
    pass

_BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    if _BOT not in sys.path:
        sys.path.append(_BOT)
    import log_redact  # noqa: E402

    log_redact.install()
except Exception as _exc:  # never break the interpreter
    sys.__stderr__.write(f"[sitecustomize] log redaction NOT active: {_exc}\n")
    # Fail closed: in a public workflow an unredacted run is worse than no run.
    if os.environ.get("SG_PUBLIC_LOGS", "").strip() in {"1", "true", "yes"}:
        sys.__stderr__.flush()
        os._exit(3)

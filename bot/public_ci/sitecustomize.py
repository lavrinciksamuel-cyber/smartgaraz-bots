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

for _secret_name in ("HQ_URL", "SEC_HQ_URL", "SG_GUARD_HQ_URL", "SEC_LOGIN_PATH"):
    _secret_val = (os.environ.get(_secret_name) or "").strip()
    if len(_secret_val) >= 4:
        sys.__stderr__.write(f"::add-mask::{_secret_val}\n")
        sys.__stderr__.flush()

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

try:  # self-healing HTTP: retry transient 5xx/timeouts/odd headers with backoff (bot/http_retry.py)
    # Scheduled supplier jobs set SG_HTTP_RETRIES=0.
    if os.environ.get("SG_POLITE_HOSTS", "").strip():
        os.environ.setdefault("SG_HTTP_RETRIES", "0")
    import http_retry  # noqa: E402

    http_retry.install()
except Exception as _exc:  # never break the interpreter
    sys.__stderr__.write(f"[sitecustomize] http retry not active: {_exc}\n")

try:  # optional request wrapper (bot/supplier_pace.py)
    import supplier_pace  # noqa: E402

    supplier_pace.install_from_env()
except Exception as _exc:  # never break the interpreter
    sys.__stderr__.write(f"[sitecustomize] supplier pace not active: {_exc}\n")

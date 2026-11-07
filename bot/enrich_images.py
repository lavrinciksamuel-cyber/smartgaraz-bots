#!/usr/bin/env python3
"""Doplnenie odložených fotiek (meta _sg_pending_image) do konceptov.

Beží ako 1 worker, šetrne (WC batch po 10, shop_guard), aby HostCreators
nepadal. Import konceptov tak nemusí čakať ~100 s/produkt na upload fotky.
  python bot/enrich_images.py --budget-min 50
"""
from __future__ import annotations
import argparse, json, logging, os, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from image_integrity import fetch_verified, upload_jpeg  # noqa: E402
from shop_guard import wait_if_slow  # noqa: E402

LOGGER = logging.getLogger("enrich_images")
STATE = ROOT / "data" / "enrich_images_state.json"


def _mask(value: str) -> None:
    text = (value or "").strip()
    if len(text) >= 4:
        print(f"::add-mask::{text}", file=sys.stderr, flush=True)


def main() -> int:
    import requests
    from urllib.parse import urlparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    for name in ("WC_URL", "WC_CONSUMER_KEY", "WC_CONSUMER_SECRET"):
        _mask(os.environ.get(name) or "")
    _mask(urlparse(os.environ.get("WC_URL") or "").hostname or "")
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget-min", type=float, default=50)
    ap.add_argument("--batch", type=int, default=10)
    a = ap.parse_args()
    url = os.environ["WC_URL"].rstrip("/") + "/wp-json/wc/v3/products"
    auth = (os.environ["WC_CONSUMER_KEY"], os.environ["WC_CONSUMER_SECRET"])
    session = requests.Session()
    session.headers["User-Agent"] = "smartgaraz-bot"
    st = json.loads(STATE.read_text()) if STATE.exists() else {"page": 1, "done": 0}
    deadline = time.time() + a.budget_min * 60
    done = 0
    skipped = 0
    while time.time() < deadline:
        wait_if_slow()
        r = requests.get(url, auth=auth, timeout=120, params={"status": "draft", "per_page": 50, "page": st["page"],
                         "orderby": "id", "order": "asc", "_fields": "id,images,meta_data"})
        if r.status_code in (403, 429):
            LOGGER.warning("stop %s", r.status_code)
            break
        if not r.ok:
            LOGGER.warning("list %s", r.status_code)
            time.sleep(60)
            continue
        rows = r.json()
        todo = []
        halted = False
        for p in rows:
            pend = next((m.get("value") for m in p.get("meta_data") or [] if m.get("key") == "_sg_pending_image"), "")
            if not pend or p.get("images"):
                continue
            time.sleep(0.4)
            data, reason = fetch_verified(session, pend, timeout=40, pause=1.0, verify=False, retries=1)
            if reason in ("http_403", "http_429"):
                LOGGER.warning("stop %s", reason)
                halted = True
                break
            if reason or not data:
                skipped += 1
                LOGGER.info("skip %s", reason or "empty")
                continue
            hosted, status = upload_jpeg(data, f"p{p['id']}.jpg")
            if status in (403, 429):
                LOGGER.warning("stop %s", status)
                halted = True
                break
            if not hosted:
                skipped += 1
                LOGGER.info("skip upload")
                continue
            todo.append({"id": p["id"], "images": [{"src": hosted}],
                         "meta_data": [{"key": "_sg_pending_image", "value": ""}]})
        if halted:
            break
        for i in range(0, len(todo), a.batch):
            if time.time() > deadline:
                break
            wait_if_slow()
            chunk = todo[i:i + a.batch]
            b = requests.post(url + "/batch", auth=auth, json={"update": chunk}, timeout=300)
            ok = b.ok
            done += len(chunk) if ok else 0
            LOGGER.info("batch %d fotiek → %s (spolu %d)", len(chunk), b.status_code, done)
            time.sleep(2 if ok else 60)
        if len(rows) < 50:
            st["page"] = 1
            if not todo:
                break
        else:
            st["page"] += 1
        st["done"] = st.get("done", 0) + 0
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(st))
    st["done"] = st.get("done", 0) + done
    STATE.write_text(json.dumps(st))
    print("SG_RESULT " + json.dumps({"ok": True, "images": done, "skipped": skipped, "page": st["page"]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

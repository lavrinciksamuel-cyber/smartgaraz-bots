"""Public pages for LKQ product and photo discovery. No login."""
from __future__ import annotations

from urllib.parse import urlparse

from public_pages import (
    UA,
    PublicFind,
    classify_http,
    discover_public,
    is_product_url,
    jsonld_products,
    product_image,
    robots_allows,
)

ORIGINS = (
    "https://www.autokelly.sk",
    "https://www.autokelly.cz",
)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def is_public_host(url: str) -> bool:
    host = _host(url)
    return host == "autokelly.sk" or host.endswith(".autokelly.sk") or host == "autokelly.cz" or host.endswith(".autokelly.cz")


def collect(session, limit: int = 8) -> PublicFind:
    """Product URLs from public pages. A block on any origin stops."""
    urls: list[str] = []
    structured = 0
    status = 200
    for origin in ORIGINS:
        found = discover_public(session, origin, limit=limit)
        status = found.http_status or status
        if found.status == "blocked":
            return PublicFind("blocked", [], status, structured)
        structured += found.structured
        for url in found.urls:
            if is_product_url(url) and url not in urls:
                urls.append(url)
            if len(urls) >= limit:
                return PublicFind("ok", urls[:limit], status, structured)
    return PublicFind("ok", urls, status, structured)


def _get(session, url: str) -> tuple[int, str]:
    try:
        response = session.get(url, timeout=25, headers={"User-Agent": UA})
    except Exception:
        return 0, ""
    status = int(getattr(response, "status_code", 0) or 0)
    try:
        body = response.text or ""
    except Exception:
        body = ""
    return status, body


def image_for(session, supplier_url: str = "", sku: str = "") -> tuple[str, str]:
    """Return ``(image_url, "")`` or ``("", reason)``. ``http_403`` / ``http_429`` stop the caller."""
    if is_public_host(supplier_url):
        parsed = urlparse(supplier_url.split("#")[0])
        robots_status, robots = _get(session, f"{parsed.scheme}://{parsed.hostname}/robots.txt")
        if classify_http(robots_status, robots) == "blocked":
            code = robots_status if robots_status in (403, 429) else 403
            return "", f"http_{code}"
        path = parsed.path or "/"
        if robots_status == 200 and not robots_allows(robots, path):
            return "", "no_public"
        status, body = _get(session, parsed.geturl())
        kind = classify_http(status, body)
        if kind == "blocked":
            code = status if status in (403, 429) else 403
            return "", f"http_{code}"
        image = _match_image(body, sku)
        return (image, "") if image else ("", "no_image")

    found = collect(session, limit=8)
    if found.status == "blocked":
        code = found.http_status if found.http_status in (403, 429) else 403
        return "", f"http_{code}"
    if not sku:
        return "", "no_public"
    for url in found.urls:
        status, body = _get(session, url)
        kind = classify_http(status, body)
        if kind == "blocked":
            code = status if status in (403, 429) else 403
            return "", f"http_{code}"
        image = _match_image(body, sku)
        if image:
            return image, ""
    return "", "no_public"


def _match_image(html: str, sku: str) -> str:
    image = product_image(html)
    if not image:
        return ""
    if not sku:
        return image
    blob = " ".join(
        f"{row.get('sku') or ''} {row.get('url') or ''} {row.get('name') or ''}"
        for row in jsonld_products(html)
    )
    if sku.casefold() in blob.casefold():
        return image
    return ""

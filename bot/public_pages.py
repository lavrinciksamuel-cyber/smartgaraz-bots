"""Read public catalog pages the same way a crawler may.

Sitemap, robots.txt, JSON-LD and OpenGraph only. An honest user agent.
403, 429 and challenge pages stop the run. No proxy and no bypass.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

UA = "smartgaraz-bot"
_CHALLENGE = (
    "just a moment",
    "cf-browser-verification",
    "cf-challenge",
    "attention required",
    "g-recaptcha",
    "hcaptcha",
    "please verify you are human",
    "verify you are a human",
)
_ASSET = re.compile(r"\.(?:css|js|png|jpe?g|gif|svg|webp|ico|pdf|woff2?)(?:$|\?)", re.I)


@dataclass
class PublicFind:
    status: str
    urls: list[str] = field(default_factory=list)
    http_status: int = 0
    structured: int = 0


def classify_http(status: int, body: str = "") -> str:
    """``ok``, ``missing``, or ``blocked``."""
    if status in (401, 403, 429, 468, 503):
        return "blocked"
    sample = (body or "")[:5000].lower()
    if any(mark in sample for mark in _CHALLENGE):
        return "blocked"
    if status == 404:
        return "missing"
    if 200 <= status < 300:
        return "ok"
    if status >= 400:
        return "blocked"
    return "blocked"


def robots_sitemaps(text: str) -> list[str]:
    found = []
    for line in (text or "").splitlines():
        if line.lower().startswith("sitemap:"):
            url = line.split(":", 1)[1].strip()
            if url.startswith("http"):
                found.append(url)
    return found


def robots_allows(text: str, path: str) -> bool:
    """User-agent * rules. A matching Allow beats a matching Disallow."""
    path = path if path.startswith("/") else "/" + (path or "")
    agents: list[tuple[str, str]] = []
    current = ""
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = [part.strip() for part in line.split(":", 1)]
        low = key.lower()
        if low == "user-agent":
            current = value.lower()
        elif low in {"allow", "disallow"} and current in {"*", ""}:
            agents.append((low, value))
    best = ""
    allowed = True
    for kind, rule in agents:
        if not rule:
            continue
        if path.startswith(rule) and len(rule) >= len(best):
            best = rule
            allowed = kind == "allow"
    return allowed


def sitemap_urls(xml: str) -> list[str]:
    found = []
    for chunk in (xml or "").split("<loc>")[1:]:
        loc = chunk.split("</loc>", 1)[0].strip()
        if loc.startswith("http"):
            found.append(loc)
    return found


def _meta(html: str, prop: str) -> str:
    match = re.search(
        rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']*)',
        html or "",
        re.I,
    )
    if match:
        return match.group(1).strip()
    match = re.search(
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']{re.escape(prop)}["\']',
        html or "",
        re.I,
    )
    return match.group(1).strip() if match else ""


def jsonld_products(html: str) -> list[dict]:
    """Product records from JSON-LD only."""
    found: list[dict] = []
    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html or "",
        re.I | re.S,
    ):
        try:
            data = json.loads(match.group(1))
        except (ValueError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        i = 0
        while i < len(stack):
            item = stack[i]
            i += 1
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            if isinstance(graph, list):
                stack.extend(graph)
            types = item.get("@type")
            types = types if isinstance(types, list) else [types]
            if "Product" not in types:
                continue
            image = item.get("image") or ""
            if isinstance(image, list):
                image = image[0] if image else ""
            if isinstance(image, dict):
                image = image.get("url") or ""
            found.append({
                "name": str(item.get("name") or ""),
                "sku": str(item.get("sku") or item.get("mpn") or ""),
                "image": str(image or ""),
                "url": str(item.get("url") or ""),
            })
    return found


def product_image(html: str) -> str:
    """JSON-LD product photo. Site logos and OpenGraph chrome are ignored."""
    for row in jsonld_products(html):
        image = str(row.get("image") or "")
        if image and "logo" not in image.lower():
            return image
    return ""


def is_product_url(url: str) -> bool:
    return "/product/" in (urlparse(url).path or "").lower()


def products_from_html(html: str) -> list[dict]:
    """Product records from JSON-LD, then OpenGraph if that is all the page has."""
    found = jsonld_products(html)
    if found:
        return found
    title = _meta(html, "og:title")
    if not title:
        return []
    return [{"name": title, "sku": "", "image": _meta(html, "og:image"), "url": _meta(html, "og:url")}]


def product_links(html: str, page_url: str, robots: str = "") -> list[str]:
    host = (urlparse(page_url).hostname or "").lower()
    found = []
    seen = set()
    for href in re.findall(r'href=["\']([^"\'#]+)["\']', html or "", re.I):
        if href.startswith("{{") or _ASSET.search(href):
            continue
        absolute = urljoin(page_url, href).split("#")[0]
        parsed = urlparse(absolute)
        if (parsed.hostname or "").lower() != host:
            continue
        if not parsed.path or parsed.path == "/":
            continue
        if robots and not robots_allows(robots, parsed.path):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        found.append(absolute)
    return found


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


def discover_public(session, origin: str, limit: int = 8) -> PublicFind:
    """Robots, then sitemap or the public entry page. Stops on a block."""
    root = (origin or "").rstrip("/")
    status, body = _get(session, root + "/robots.txt")
    kind = classify_http(status, body)
    if kind == "blocked":
        return PublicFind("blocked", [], status, 0)
    robots = body if kind == "ok" else ""
    urls: list[str] = []
    structured = 0
    for sitemap in robots_sitemaps(robots)[:1]:
        st, xml = _get(session, sitemap)
        if classify_http(st, xml) == "blocked":
            return PublicFind("blocked", urls, st, structured)
        locs = sitemap_urls(xml)
        if locs and all("sitemap" in loc.lower() or loc.lower().endswith(".xml") for loc in locs[:5]):
            st, xml = _get(session, locs[0])
            if classify_http(st, xml) == "blocked":
                return PublicFind("blocked", [], st, structured)
            locs = sitemap_urls(xml)
        for loc in locs:
            path = urlparse(loc).path or "/"
            if robots and not robots_allows(robots, path):
                continue
            if loc not in urls:
                urls.append(loc)
            if len(urls) >= limit:
                break
        if len(urls) >= limit:
            break
    if not urls:
        st, html = _get(session, root + "/")
        if classify_http(st, html) == "blocked":
            return PublicFind("blocked", [], st, 0)
        for row in products_from_html(html):
            structured += 1
            if row.get("url"):
                urls.append(row["url"])
        for link in product_links(html, root + "/", robots):
            if link not in urls:
                urls.append(link)
            if len(urls) >= limit:
                break
    return PublicFind("ok", urls[:limit], status, structured)

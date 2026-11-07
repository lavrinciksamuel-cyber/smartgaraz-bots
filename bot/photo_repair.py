"""Decide which product images to remove. No network and no uploads.

High-confidence failures only: missing end marker, decoder error, or a flat
mid-gray band. Timeouts and 403/429 never delete. A run that flags more than
20% of checked images does not delete anything.
"""
from __future__ import annotations

import html
from typing import Optional

HIGH_CONFIDENCE = frozenset({"jpeg_eoi", "png_iend", "decode", "gray_fill"})
DELETE_CAP = 200
BROKEN_RATIO = 0.20

# Existing media title / slug. Matched at runtime; never printed.
_TITLE = "foto sa pripravuje"
_SLUGS = frozenset({"foto-sa-pripravuje"})


def _norm(text: str) -> str:
    return " ".join(html.unescape(text or "").casefold().split())


def media_title(row: dict) -> str:
    title = row.get("title")
    if isinstance(title, dict):
        return str(title.get("rendered") or "")
    return str(title or "")


def media_src(row: dict) -> str:
    return str(row.get("source_url") or row.get("src") or "")


def is_generic_placeholder_src(src: str) -> bool:
    low = (src or "").lower()
    return "woocommerce-placeholder" in low or "/assets/images/placeholder" in low


def placeholder_search() -> str:
    return _TITLE


def matches_placeholder(title: str, slug: str = "", src: str = "") -> bool:
    if is_generic_placeholder_src(src):
        return False
    if _norm(title) == _TITLE:
        return True
    stem = (slug or "").casefold().strip()
    return stem in _SLUGS


def placeholder_id_from_setting(payload: dict) -> Optional[int]:
    value = str((payload or {}).get("value") or "").strip()
    if value.isdigit() and int(value) > 0:
        return int(value)
    return None


def accept_attachment(*, src: str, title: str, slug: str, from_setting: bool) -> bool:
    if is_generic_placeholder_src(src):
        return False
    if from_setting:
        return True
    return matches_placeholder(title, slug, src)


def pick_media_id(rows: list) -> Optional[int]:
    found: list[int] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            media_id = int(row.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if media_id <= 0:
            continue
        if matches_placeholder(media_title(row), str(row.get("slug") or ""), media_src(row)):
            found.append(media_id)
    return min(found) if found else None


def classify_for_delete(reason: Optional[str]) -> str:
    """``delete``, ``stop``, or ``skip``. Stop is only 403/429."""
    text = (reason or "").strip()
    if text in {"http_403", "http_429"}:
        return "stop"
    if "timeout" in text.lower():
        return "skip"
    if text in HIGH_CONFIDENCE:
        return "delete"
    return "skip"


def ratio_exceeded(checked: int, broken: int, threshold: float = BROKEN_RATIO) -> bool:
    if checked <= 0 or broken <= 0:
        return False
    return (broken / checked) > threshold


def plan_gallery(images: list, broken_ids: set[int], placeholder_id: Optional[int]) -> dict:
    """Featured image is the first remaining id. Placeholder is never deleted."""
    ordered: list[int] = []
    seen: set[int] = set()
    for image in images or []:
        if isinstance(image, dict):
            raw = image.get("id")
        else:
            raw = image
        try:
            media_id = int(raw or 0)
        except (TypeError, ValueError):
            continue
        if media_id <= 0 or media_id in seen:
            continue
        seen.add(media_id)
        ordered.append(media_id)
    blocked = set(broken_ids or ())
    if placeholder_id:
        blocked.discard(int(placeholder_id))
    delete_ids = [media_id for media_id in ordered if media_id in blocked]
    keep_ids = [media_id for media_id in ordered if media_id not in blocked]
    if keep_ids:
        return {
            "keep_ids": keep_ids,
            "delete_ids": delete_ids,
            "images": [{"id": media_id} for media_id in keep_ids],
            "placeholder_set": False,
            "cleared": False,
        }
    if placeholder_id and delete_ids:
        return {
            "keep_ids": [],
            "delete_ids": delete_ids,
            "images": [{"id": int(placeholder_id)}],
            "placeholder_set": True,
            "cleared": False,
        }
    if delete_ids:
        return {
            "keep_ids": [],
            "delete_ids": delete_ids,
            "images": [],
            "placeholder_set": False,
            "cleared": True,
        }
    return {
        "keep_ids": keep_ids,
        "delete_ids": [],
        "images": [{"id": media_id} for media_id in keep_ids],
        "placeholder_set": False,
        "cleared": False,
    }


def within_cap(pending: int, used: int, cap: int = DELETE_CAP) -> bool:
    return pending > 0 and used + pending <= cap

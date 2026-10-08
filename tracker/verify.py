"""Sanity checks that stop one-off glitches and silent product swaps from reaching the history or Telegram."""
from __future__ import annotations

from difflib import SequenceMatcher
from html import escape
from urllib.parse import urlparse

BIG_CHANGE_PCT = 15.0  # price moves this large must be seen twice before they count
SAME_PRICE_PCT = 1.0  # two readings within this are "the same price"
TITLE_SIMILARITY_MIN = 0.5  # below this, the product name has changed enough to be a different product


def is_big_change(prev: dict | None, point: dict) -> bool:
    """A reading that differs sharply from the last confirmed one: a big price move or a stock flip."""
    if not prev:
        return False  # first reading: nothing to compare against
    old, new = prev.get("price"), point.get("price")
    if old and new and abs(new - old) / old * 100 >= BIG_CHANGE_PCT:
        return True
    a, b = prev.get("in_stock"), point.get("in_stock")
    return a is not None and b is not None and a != b


def readings_match(a: dict | None, b: dict | None) -> bool:
    if not a or not b:
        return False
    pa, pb = a.get("price"), b.get("price")
    if (pa is None) != (pb is None):
        return False
    if pa is not None and abs(pa - pb) / max(pa, pb) * 100 > SAME_PRICE_PCT:
        return False
    return a.get("in_stock") == b.get("in_stock")


def _norm_path(url: str | None) -> str:
    p = urlparse(url or "")
    return f"{p.netloc.lower().removeprefix('www.')}{p.path.rstrip('/').lower()}"


def identity_warning(item: dict, url: str, retailer: str, slot: dict, offer) -> str | None:
    """Compare this reading's product id / title / landing URL with what was first seen for the link.

    Records the baseline on first sight. Returns a warning (once per change) if the link now shows a different
    product; the baseline is then updated so the warning isn't repeated every run.
    """
    seen = slot.get("identity")
    kind, _, value = (offer.product_id or "").partition(":")
    now_ids = {kind: value} if value else {}
    if not seen:
        slot["identity"] = {"ids": now_ids, "title": offer.title}
        return None
    seen_ids = seen.get("ids") or {}
    # Ids come in kinds (Shopify id, SKU, GTIN) depending on which method read the page this run;
    # only compare the same kind, and remember new kinds as they appear.
    shared = set(seen_ids) & set(now_ids)
    reason = None
    if any(seen_ids[k] != now_ids[k] for k in shared):
        k = next(k for k in shared if seen_ids[k] != now_ids[k])
        reason = f"product id changed ({k} {seen_ids[k]} → {now_ids[k]})"
    elif not shared and seen.get("title") and offer.title:
        ratio = SequenceMatcher(None, seen["title"].lower(), offer.title.lower()).ratio()
        if ratio < TITLE_SIMILARITY_MIN:
            reason = "product name changed"
    if offer.final_url and _norm_path(offer.final_url) != _norm_path(url):
        reason = reason or f"link now redirects to {offer.final_url}"
    if not reason:
        slot["identity"] = {"ids": {**seen_ids, **now_ids}, "title": offer.title or seen.get("title")}
        return None
    slot["identity"] = {"ids": now_ids, "title": offer.title}
    slot["identity_note"] = f"{reason}: was “{seen.get('title')}”, now “{offer.title}”"
    return (f"🔀 <b>{escape(item['name'])}</b> at {escape(retailer)}: {escape(reason)}.\n"
            f"Was “{escape(seen.get('title') or '?')}”, now “{escape(offer.title or '?')}”. "
            f"Check the link is still the product you want:\n{escape(url)}")

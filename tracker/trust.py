"""Only reputable shops count. Comparison sites (PriceSpy) list every seller, including grey importers with no
reviews; a price only matters if you'd actually buy from that shop.

`profile.trusted_shops` in wishlist.yaml lists the shops you trust (matched case- and punctuation-insensitively).
Offers from anyone else are still shown on the dashboard, but never set the price and never alert.
"""
from __future__ import annotations

import re

DEFAULT_TRUSTED = [
    # Fragrance & beauty
    "Chemist Warehouse", "Farmers", "Life Pharmacy", "Unichem", "Bargain Chemist", "MECCA", "Sephora",
    "Ballantynes", "David Jones", "Smith & Caughey's",
    # Electronics & home office
    "PB Tech", "JB Hi-Fi", "Noel Leeming", "Harvey Norman", "Mighty Ape", "Computer Lounge", "Ascent",
    "The Warehouse", "Warehouse Stationery",
    # Footwear
    "JD Sports", "Foot Locker", "Platypus", "Hype DC", "Rebel Sport", "Dr Martens", "Birkenstock",
]


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def is_trusted(shop: str, profile: dict) -> bool:
    trusted = (profile or {}).get("trusted_shops") or DEFAULT_TRUSTED
    n = _norm(shop)
    return any(n == _norm(t) or n.startswith(_norm(t)) for t in trusted)


def apply_trust(offer, profile: dict) -> None:
    """For comparison-site offers, take the best *trusted*, new-condition offer; flag if only untrusted remain."""
    rows = offer.shop_offers
    if rows is None:
        return
    for r in rows:
        r["trusted"] = is_trusted(r["shop"], profile) and (r.get("condition") in (None, "New"))
    trusted = [r for r in rows if r["trusted"]]
    if not trusted:
        offer.trust_status = "untrusted_only"
        offer.untrusted_best = min(rows, key=lambda r: r["price"]) if rows else None
        offer.price = offer.was_price = None
        offer.in_stock = None
        return
    live = [r for r in trusted if r.get("in_stock") is not False]
    best = min(live or trusted, key=lambda r: r["price"])
    offer.trust_status = "trusted"
    offer.price, offer.was_price, offer.shop = best["price"], best.get("was"), best["shop"]
    offer.in_stock = bool(live)

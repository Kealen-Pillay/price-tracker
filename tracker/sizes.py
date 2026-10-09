"""Shoe sizes: which of a product's sizes are *yours*, across US / UK / EU systems and brand quirks.

Your sizes live in the `profile:` block of wishlist.yaml, e.g.

  profile:
    gender: men
    shoe_size: {system: US, min: 10, max: 11.5}
    brand_sizes:
      Dr Martens: {UK: [9, 10]}
      Birkenstock: {EU: [43, 44], width: Regular}

`brand_sizes` wins where given; otherwise the US range is converted with the brand's chart below. A size a brand
doesn't make (e.g. a half size at Dr Martens) maps to both neighbouring whole sizes.
"""
from __future__ import annotations

import math
import re

# US men's -> other systems. Each converter returns the exact (possibly fractional) size; rounding to what the brand
# actually makes happens in `_neighbours`.
_GENERIC_EU = {7: 40, 7.5: 40.5, 8: 41, 8.5: 42, 9: 42.5, 9.5: 43, 10: 44, 10.5: 44.5, 11: 45, 11.5: 45.5,
               12: 46, 12.5: 47, 13: 47.5}
_SALOMON_EU = {7.5: 40.5, 8: 41, 8.5: 42, 9: 42.5, 9.5: 43, 10: 44, 10.5: 44.5, 11: 45, 11.5: 46, 12: 46.5,
               12.5: 47, 13: 48}  # from Salomon's men's chart, as shown on JD Sports NZ

BRAND_CHARTS = {
    # brand (lower case): {system: (converter, half_sizes_made)}
    "dr martens": {"UK": (lambda us: us - 1, False), "EU": (lambda us: _GENERIC_EU.get(us, us + 34), False)},
    "birkenstock": {"EU": (lambda us: us + 33, False)},  # US M10 = EU 43, M11 = EU 44 (no half sizes)
    "salomon": {"UK": (lambda us: us - 0.5, True), "EU": (lambda us: _SALOMON_EU.get(us, us + 34), True)},
    "_generic": {"UK": (lambda us: us - 1, True), "EU": (lambda us: _GENERIC_EU.get(us, us + 34), True)},
}

SIZE_NUM_RE = re.compile(r"(\d+(?:[.,]\d)?)")
SYSTEM_RE = re.compile(r"(?i)\b(US|UK|EU|EUR)\b")


def _halves(lo: float, hi: float) -> list[float]:
    return [lo + i * 0.5 for i in range(int(round((hi - lo) * 2)) + 1)]


def _neighbours(x: float, half_sizes: bool) -> set[float]:
    step = 0.5 if half_sizes else 1.0
    lo, hi = math.floor(x / step) * step, math.ceil(x / step) * step
    return {round(lo, 1), round(hi, 1)}


def wanted_sizes(profile: dict, brand: str | None, system: str) -> set[float] | None:
    """Your sizes in `system` for `brand`, or None if the profile has no shoe size."""
    shoe = (profile or {}).get("shoe_size")
    if not shoe:
        return None
    system = system.upper().replace("EUR", "EU")
    override = _brand_override(profile, brand)
    if system in override:
        lo, hi = override[system] if len(override[system]) == 2 else (min(override[system]), max(override[system]))
        return set(_halves(float(lo), float(hi)))
    us = _halves(float(shoe["min"]), float(shoe["max"]))
    if system == shoe.get("system", "US").upper():
        return set(us)
    chart = BRAND_CHARTS.get((brand or "").lower()) or {}
    conv, halves = chart.get(system) or BRAND_CHARTS["_generic"].get(system, (None, True))
    if conv is None:
        return None
    out: set[float] = set()
    for u in us:
        out |= _neighbours(conv(u), halves)
    return out


def wanted_width(profile: dict, brand: str | None) -> str | None:
    return _brand_override(profile, brand).get("width")


def _brand_override(profile: dict, brand: str | None) -> dict:
    for name, cfg in ((profile or {}).get("brand_sizes") or {}).items():
        if brand and name.lower() == brand.lower():
            return {k.upper() if k.lower() != "width" else "width": v for k, v in cfg.items()}
    return {}


def parse_size(label: str, default_system: str | None) -> tuple[str | None, float | None]:
    """'UK 9' -> ('UK', 9.0); '10.5' with default 'US' -> ('US', 10.5); 'One Size' -> (None, None)."""
    if not label:
        return None, None
    sys_m = SYSTEM_RE.search(label)
    num_m = SIZE_NUM_RE.search(label)
    if not num_m:
        return None, None
    system = (sys_m.group(1).upper().replace("EUR", "EU") if sys_m else (default_system or "").upper()) or None
    return system, float(num_m.group(1).replace(",", "."))


def fmt_size(system: str | None, n: float) -> str:
    return f"{system + ' ' if system else ''}{n:g}"


def apply_profile(offer, item: dict, profile: dict, retailer: dict) -> None:
    """Narrow a shoe offer to your sizes: stock, price and 'was' price come only from sizes you'd buy.

    Sets offer.size_status: 'ok' (your sizes found), 'none' (store doesn't list your sizes), 'unknown' (couldn't
    read sizes; any-size stock is shown instead).
    """
    if item.get("kind") != "shoes" or not (profile or {}).get("shoe_size"):
        return
    if not offer.sizes:
        offer.size_status = "unknown"
        return
    brand = item.get("brand")
    width = wanted_width(profile, brand)
    mine = []
    for s in offer.sizes:
        system, n = parse_size(s["size"], retailer.get("size_system"))
        if n is None or not system:
            continue
        wanted = wanted_sizes(profile, brand, system)
        if wanted is None or round(n, 1) not in wanted:
            continue
        if width and s.get("width") and s["width"].lower() != width.lower():
            continue
        mine.append({**s, "label": fmt_size(system, n), "n": n})
    if not mine:
        offer.size_status, offer.in_stock, offer.my_sizes = "none", False, []
        return
    # One entry per size (a store can list a size more than once, e.g. per width or colour): in stock if any is.
    merged: dict[str, dict] = {}
    for s in mine:
        m = merged.get(s["label"])
        if m is None or (s.get("in_stock") and not m.get("in_stock")) or (
                s.get("in_stock") == m.get("in_stock") and (s.get("price") or 1e9) < (m.get("price") or 1e9)):
            merged[s["label"]] = s
    mine = sorted(merged.values(), key=lambda s: s["n"])
    live = [s for s in mine if s.get("in_stock")]
    pool = live or mine
    priced = [s for s in pool if s.get("price") is not None]
    if priced:
        best = min(priced, key=lambda s: s["price"])
        page_was = offer.was_price  # page-level RRP (e.g. JD's compare-at price) when sizes don't carry one
        offer.price = best["price"]
        was = best.get("was") or page_was
        offer.was_price = was if was and was > best["price"] else None
    offer.in_stock = bool(live)
    offer.my_sizes = [{"label": s["label"], "in_stock": bool(s.get("in_stock"))} for s in mine]
    offer.size_status = "ok"

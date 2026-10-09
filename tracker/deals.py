"""How good is today's best price? Compared with what the item normally costs, not just the shop's own "was".

Labels: great (≥15% below its 90-day typical price), good (≥7% below), typical, high (≥7% above), or
unknown (fewer than MIN_DAYS days of history; then a *verified* RRP discount of ≥20% can still rate it good).
A "was" price counts as verified only if the item has actually been seen selling at roughly that price —
otherwise it may be an inflated RRP that makes a normal price look like a sale.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median

WINDOW_DAYS = 90
MIN_DAYS = 7
GREAT_PCT, GOOD_PCT = 15.0, 7.0
RRP_GOOD_PCT = 20.0


def _buyable(h: dict) -> bool:
    return h.get("price") is not None and h.get("in_stock") is not False


def compute(entry: dict, now: datetime | None = None) -> dict | None:
    now = now or datetime.now(timezone.utc)
    offers = {u: o for u, o in (entry.get("offers") or {}).items()
              if o.get("tracked", True) is not False and u in (entry.get("urls") or [u])}
    live = [(u, o) for u, o in offers.items()
            if o.get("price") is not None and not o.get("error") and o.get("in_stock") is not False]
    if not live:
        return None
    url, best = min(live, key=lambda x: x[1]["price"])
    price = best["price"]

    since = now - timedelta(days=WINDOW_DAYS)
    daily: dict[str, float] = {}
    seen_prices: list[float] = []
    for o in offers.values():
        for h in o.get("history") or []:
            if not _buyable(h):
                continue
            seen_prices.append(h["price"])
            if datetime.fromisoformat(h["t"]) >= since:
                day = h["t"][:10]
                daily[day] = min(daily.get(day, h["price"]), h["price"])
    typical = median(daily.values()) if len(daily) >= MIN_DAYS else None

    was = best.get("was")
    was_verified = bool(was) and any(p >= was * 0.98 for p in seen_prices)
    others = sorted(o["price"] for u, o in live if u != url)

    reasons, label, vs_typical = [], "unknown", None
    if typical:
        vs_typical = (typical - price) / typical * 100
        label = ("great" if vs_typical >= GREAT_PCT else "good" if vs_typical >= GOOD_PCT
                 else "high" if vs_typical <= -GOOD_PCT else "typical")
        reasons.append(f"{abs(vs_typical):.0f}% {'below' if vs_typical >= 0 else 'above'} its {WINDOW_DAYS}-day "
                       f"typical ${typical:,.2f}")
    else:
        reasons.append(f"{len(daily)} day(s) of history so far; typical price needs {MIN_DAYS}")
    if was:
        off = (was - price) / was * 100
        if was_verified:
            reasons.append(f"{off:.0f}% off a verified RRP of ${was:,.2f}")
            if label == "unknown" and off >= RRP_GOOD_PCT:
                label = "good"
        else:
            reasons.append(f"store says was ${was:,.2f} ({off:.0f}% off), but it hasn't been seen selling at that price")
    if others:
        reasons.append(f"${others[0] - price:,.2f} cheaper than the next store" if others[0] > price
                       else "same price as another store")
    return {"label": label, "price": price, "retailer": best.get("retailer"), "shop": best.get("shop"), "url": url,
            "typical": typical, "pct_vs_typical": vs_typical, "was": was, "was_verified": was_verified,
            "days": len(daily), "reasons": reasons}

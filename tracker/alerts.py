"""Decide which price changes deserve a notification, without repeating the same alert every run."""
from __future__ import annotations

from html import escape

MIN_DROP_PCT = 5.0  # ignore tiny fluctuations
FAILURES_BEFORE_WARNING = 3


def fmt(p: float | None) -> str:
    return "?" if p is None else f"${p:,.2f}"


def evaluate(item: dict, url: str, retailer: str, offer: dict, prev: dict | None,
             history: list[dict], state: dict) -> list[str]:
    """Return alert lines for one retailer offer. Mutates state['alerts'] for de-duplication."""
    key = f"{item['id']}|{url}"
    sent = state.setdefault("alerts", {}).setdefault(key, {})
    price, in_stock = offer.get("price"), offer.get("in_stock")
    if price is None:
        return []
    lines: list[str] = []
    label = f"<b>{escape(item['name'])}</b> at {escape(retailer)}: {fmt(price)}"
    if offer.get("was"):
        label += f" (was {fmt(offer['was'])}, save {fmt(offer['was'] - price)})"
    link = f'\n<a href="{escape(url)}">View deal</a>'

    prev_price = prev.get("price") if prev else None
    past = [h["price"] for h in history if h.get("price") is not None]  # history before this check

    # 1. Target price reached (alert again only if it drops further below the last alerted price).
    target = item.get("target_price")
    if target is not None and price <= float(target) and in_stock is not False:
        if sent.get("target_at") is None or price < sent["target_at"]:
            lines.append(f"🎯 Target hit (≤ {fmt(float(target))}) — {label}{link}")
            sent["target_at"] = price
    elif target is not None and price > float(target):
        sent.pop("target_at", None)  # re-arm once it goes back above target

    # 2. Meaningful drop vs the previous check, flagged as an all-time low where applicable.
    if prev_price and price < prev_price and not lines:
        pct = (prev_price - price) / prev_price * 100
        if pct >= MIN_DROP_PCT:
            atl = " — 🏆 lowest price seen" if past and price < min(past) else ""
            lines.append(f"📉 Price drop {pct:.0f}% (from {fmt(prev_price)}) — {label}{atl}{link}")

    # 3. Back in stock.
    if prev and prev.get("in_stock") is False and in_stock is True:
        lines.append(f"✅ Back in stock — {label}{link}")

    return lines


def failure(item: dict, url: str, retailer: str, error: str, state: dict) -> list[str]:
    key = f"{item['id']}|{url}"
    fails = state.setdefault("failures", {})
    fails[key] = fails.get(key, 0) + 1
    if fails[key] == FAILURES_BEFORE_WARNING:
        return [f"⚠️ Can't read {escape(retailer)} for <b>{escape(item['name'])}</b> "
                f"({fails[key]} checks in a row): {escape(error[:200])}\n{escape(url)}"]
    return []


def recovered(item: dict, url: str, state: dict) -> None:
    state.setdefault("failures", {}).pop(f"{item['id']}|{url}", None)

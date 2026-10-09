"""Decide which price changes deserve a notification, without repeating the same alert every run."""
from __future__ import annotations

from html import escape

MIN_DROP_PCT = 5.0  # ignore tiny fluctuations
FAILURES_BEFORE_WARNING = 3


def fmt(p: float | None) -> str:
    return "?" if p is None else f"${p:,.2f}"


def buyable_prices(history: list[dict]) -> list[float]:
    return [h["price"] for h in history if h.get("price") is not None and h.get("in_stock") is not False]


def evaluate(item: dict, url: str, retailer: str, offer: dict, prev: dict | None,
             history: list[dict], state: dict) -> list[str]:
    """Return alert lines for one retailer offer. Mutates state['alerts'] for de-duplication."""
    key = f"{item['id']}|{url}"
    sent = state.setdefault("alerts", {}).setdefault(key, {})
    price, in_stock = offer.get("price"), offer.get("in_stock")
    if price is None or in_stock is False:
        return []  # out-of-stock offers can't be bought, so they never trigger alerts
    lines: list[str] = []
    label = f"<b>{escape(item['name'])}</b> at {escape(retailer)}: {fmt(price)}"
    if offer.get("was"):
        label += f" (was {fmt(offer['was'])}, save {fmt(offer['was'] - price)})"
    sizes_in = offer.get("sizes_in")
    if sizes_in is not None:
        label += f"\nYour sizes in stock: {escape(', '.join(sizes_in))}"
    link = f'\n<a href="{escape(url)}">View deal</a>'

    back_in_stock = bool(prev) and prev.get("in_stock") is False
    prev_price = prev.get("price") if prev and not back_in_stock else None
    # Buyable prices before this check; out-of-stock prices don't count towards "lowest seen".
    past = buyable_prices(history)
    atl = " — 🏆 lowest price seen" if past and price < min(past) else ""

    # 1. Target price reached (alert again only if it drops further below the last alerted price).
    target = item.get("target_price")
    if target is not None and price <= float(target):
        if sent.get("target_at") is None or price < sent["target_at"]:
            lines.append(f"🎯 Target hit (≤ {fmt(float(target))}) — {label}{atl}{link}")
            sent["target_at"] = price
    elif target is not None:
        sent.pop("target_at", None)  # re-arm once it goes back above target

    # 2. Back in stock (replaces the drop check, since there's no buyable previous price to compare).
    if back_in_stock and not lines:
        what = "Your size is back in stock" if sizes_in is not None else "Back in stock"
        lines.append(f"✅ {what} — {label}{atl}{link}")

    # 3. Meaningful drop vs the previous check.
    elif prev_price and price < prev_price and not lines:
        pct = (prev_price - price) / prev_price * 100
        if pct >= MIN_DROP_PCT:
            lines.append(f"📉 Price drop {pct:.0f}% (from {fmt(prev_price)}) — {label}{atl}{link}")

    # 4. Down to the last of your sizes (once each time it happens).
    prev_in = (prev or {}).get("sizes_in")
    if sizes_in is not None and len(sizes_in) == 1 and prev_in and len(prev_in) > 1 and not lines:
        lines.append(f"⏳ Only {escape(sizes_in[0])} left in your sizes — {label}{link}")

    return lines


DEAL_EMOJI = {"great": "🔥", "good": "👍", "typical": "•", "high": "⬆️", "unknown": "•"}


def deal_alert(item: dict, deal: dict | None, state: dict) -> list[str]:
    """One alert when an item becomes a great deal (re-armed once it stops being one)."""
    deals_state = state.setdefault("deals", {})
    label = (deal or {}).get("label")
    was_great = deals_state.get(item["id"]) == "great"
    deals_state[item["id"]] = label
    if label != "great" or was_great:
        return []
    where = deal.get("retailer") or "?"
    if deal.get("shop"):
        where += f" → {deal['shop']}"
    return [f"🔥 <b>Great deal: {escape(item['name'])}</b> — {fmt(deal['price'])} at {escape(where)}\n"
            + "\n".join(f"• {escape(r)}" for r in deal["reasons"])
            + f'\n<a href="{escape(deal["url"])}">View deal</a>']


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

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
    price_line = f"<b>{fmt(price)}</b> at {escape(retailer)}"
    if offer.get("was"):
        price_line += f" · was {fmt(offer['was'])} (save {fmt(offer['was'] - price)})"
    sizes_in = offer.get("sizes_in")
    sizes_line = f"\n👟 Your sizes: {escape(', '.join(sizes_in))}" if sizes_in else ""
    link = f'\n<a href="{escape(url)}">View deal →</a>'

    back_in_stock = bool(prev) and prev.get("in_stock") is False
    prev_price = prev.get("price") if prev and not back_in_stock else None
    # Buyable prices before this check; out-of-stock prices don't count towards "lowest seen".
    past = buyable_prices(history)
    atl = "\n🏆 Lowest price seen" if past and price < min(past) else ""

    def alert(emoji: str, headline: str, with_atl: bool = True) -> str:
        """One alert: the emoji tags its section (see notify.SECTIONS); the item name leads the body."""
        return (f"{emoji} <b>{escape(item['name'])}</b>\n{price_line}\n{headline}"
                f"{atl if with_atl else ''}{sizes_line}{link}")

    # 1. Target price reached (alert again only if it drops further below the last alerted price).
    target = item.get("target_price")
    if target is not None and price <= float(target):
        if sent.get("target_at") is None or price < sent["target_at"]:
            lines.append(alert("🎯", f"Target hit: at or below {fmt(float(target))}"))
            sent["target_at"] = price
    elif target is not None:
        sent.pop("target_at", None)  # re-arm once it goes back above target

    # 2. Back in stock (replaces the drop check, since there's no buyable previous price to compare).
    if back_in_stock and not lines:
        lines.append(alert("✅", "Your size is back in stock" if sizes_in is not None else "Back in stock"))

    # 3. Meaningful drop vs the previous check.
    elif prev_price and price < prev_price and not lines:
        pct = (prev_price - price) / prev_price * 100
        if pct >= MIN_DROP_PCT:
            lines.append(alert("📉", f"Down {pct:.0f}% from {fmt(prev_price)}"))

    # 4. Down to the last of your sizes (once each time it happens).
    prev_in = (prev or {}).get("sizes_in")
    if sizes_in is not None and len(sizes_in) == 1 and prev_in and len(prev_in) > 1 and not lines:
        lines.append(alert("⏳", f"Only {escape(sizes_in[0])} left in your sizes", with_atl=False))

    return lines


DEAL_EMOJI = {"great": "🔥", "good": "👍", "typical": "•", "high": "⬆️", "unknown": "•"}
DEAL_NAME = {"great": "Great deal", "good": "Good deal", "typical": "Typical price", "high": "Above its usual price"}


def with_deal_line(message: str, deal: dict | None) -> str:
    """Add the item's deal rating to a price/stock alert, so every alert says how good the price really is."""
    if not deal or deal["label"] == "unknown" or not message.startswith(("🎯", "📉", "✅", "⏳")):
        return message
    head, sep, link = message.rpartition("\n<a href=")
    line = f"\n{DEAL_EMOJI[deal['label']]} {DEAL_NAME[deal['label']]}: {escape(deal['reasons'][0])}"
    return f"{head}{line}{sep}{link}" if sep else message + line


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
    return [f"🔥 <b>{escape(item['name'])}</b>\n<b>{fmt(deal['price'])}</b> at {escape(where)}\n"
            + "\n".join(f"• {escape(r)}" for r in deal["reasons"])
            + f'\n<a href="{escape(deal["url"])}">View deal →</a>']


def failure(item: dict, url: str, retailer: str, error: str, state: dict) -> list[str]:
    key = f"{item['id']}|{url}"
    fails = state.setdefault("failures", {})
    fails[key] = fails.get(key, 0) + 1
    if fails[key] == FAILURES_BEFORE_WARNING:
        return [f"⚠️ <b>{escape(item['name'])}</b> at {escape(retailer)}\n"
                f"Couldn't read it {fails[key]} checks in a row: {escape(error[:200])}\n"
                f'<a href="{escape(url)}">Open link →</a>']
    return []


def recovered(item: dict, url: str, state: dict) -> None:
    state.setdefault("failures", {}).pop(f"{item['id']}|{url}", None)

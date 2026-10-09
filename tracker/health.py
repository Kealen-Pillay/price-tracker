"""Per-store health: is each retailer still readable? Recorded every run, published to data/health.json for the
dashboard, and alerted once when a store degrades (and again when it recovers).

A single link failing three times in a row is handled by alerts.failure; this catches the broader case of a whole
store changing its site or starting to block us.
"""
from __future__ import annotations

from html import escape

WINDOW = 12  # most recent attempts kept per store
DEGRADED_RATE = 0.5  # below this success rate (with at least MIN_ATTEMPTS) the store is "degraded"
MIN_ATTEMPTS = 4


def record(health: dict, host: str, name: str, ok: bool, when: str, error: str | None = None) -> None:
    s = health.setdefault(host, {"name": name, "attempts": []})
    s["name"] = name
    s["attempts"] = (s.get("attempts", []) + [1 if ok else 0])[-WINDOW:]
    if ok:
        s["last_ok"] = when
    else:
        s["last_error"], s["last_error_at"] = error, when


def status(s: dict) -> str:
    a = s.get("attempts") or []
    if len(a) < MIN_ATTEMPTS:
        return "new"
    return "degraded" if sum(a) / len(a) < DEGRADED_RATE else "ok"


def alerts(health: dict, state: dict) -> list[str]:
    """Messages for stores that just became degraded or just recovered."""
    seen = state.setdefault("store_status", {})
    out = []
    for host, s in health.items():
        now, before = status(s), seen.get(host)
        if now == "degraded" and before != "degraded":
            a = s["attempts"]
            out.append(f"🩺 <b>{escape(s['name'])}</b> is failing: {len(a) - sum(a)} of the last {len(a)} reads failed. "
                       f"Last error: {escape((s.get('last_error') or '?')[:160])}")
        elif now == "ok" and before == "degraded":
            out.append(f"🩺 <b>{escape(s['name'])}</b> is readable again.")
        if now != "new":
            seen[host] = now
    return out


def summary(health: dict) -> dict:
    """What the dashboard shows."""
    return {host: {"name": s["name"], "status": status(s),
                   "ok_rate": round(sum(s["attempts"]) / len(s["attempts"]), 2) if s.get("attempts") else None,
                   "attempts": len(s.get("attempts") or []), "last_ok": s.get("last_ok"),
                   "last_error": s.get("last_error"), "last_error_at": s.get("last_error_at")}
            for host, s in sorted(health.items(), key=lambda kv: kv[1]["name"].lower())}

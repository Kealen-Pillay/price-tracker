"""When alerts go out: quiet hours hold non-urgent alerts and send them as one morning batch.

  profile:
    quiet_hours: "22:00-07:00"   # NZ time; omit to send everything immediately

Target hits (🎯) and great deals (🔥) are urgent and always sent straight away — deals can sell out overnight.
"""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from . import telegram

NZ = ZoneInfo("Pacific/Auckland")
URGENT_PREFIXES = ("🎯", "🔥")


def in_quiet_hours(spec: str | None, now: datetime) -> bool:
    if not spec:
        return False
    start_s, end_s = (part.strip() for part in spec.split("-"))
    start, end = time.fromisoformat(start_s), time.fromisoformat(end_s)
    t = now.astimezone(NZ).time()
    return (start <= t or t < end) if start > end else (start <= t < end)


def deliver(messages: list[str], profile: dict, state: dict, now: datetime | None = None) -> list[str]:
    """Send what should go now, queue the rest. Returns the messages actually sent (for logging/tests)."""
    now = now or datetime.now(NZ)
    queue = state.setdefault("queued_alerts", [])
    if in_quiet_hours((profile or {}).get("quiet_hours"), now):
        urgent = [m for m in messages if m.startswith(URGENT_PREFIXES)]
        queue += [m for m in messages if not m.startswith(URGENT_PREFIXES)]
        outgoing = urgent
    else:
        outgoing = ([f"🌅 <b>Overnight</b> ({len(queue)} alert{'s' if len(queue) != 1 else ''})"] + queue
                    if queue else []) + messages
        queue.clear()
    if outgoing:
        telegram.send("\n\n".join(outgoing))
    return outgoing

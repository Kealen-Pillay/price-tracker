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

# Each alert starts with the emoji of its section; sections are sent in this order. Price alerts drop their emoji
# under the heading; the "needs attention" ones keep it, since that section mixes three kinds.
SECTIONS = [
    ("🎯", "Target price reached", True),
    ("🔥", "Great deals", True),
    ("📉", "Price drops", True),
    ("✅", "Back in stock", True),
    ("⏳", "Running low in your sizes", True),
    (("🔀", "⚠️", "🩺"), "Needs attention", False),
]


def in_quiet_hours(spec: str | None, now: datetime) -> bool:
    if not spec:
        return False
    start_s, end_s = (part.strip() for part in spec.split("-"))
    start, end = time.fromisoformat(start_s), time.fromisoformat(end_s)
    t = now.astimezone(NZ).time()
    return (start <= t or t < end) if start > end else (start <= t < end)


def render(messages: list[str], overnight: int = 0) -> str:
    """One message: a summary line, then the alerts grouped under a heading per section."""
    groups: list[list[str]] = [[] for _ in SECTIONS] + [[]]  # last group: anything without a known prefix
    for m in messages:
        i = next((i for i, (prefix, _, _) in enumerate(SECTIONS) if m.startswith(prefix)), len(SECTIONS))
        strip = i < len(SECTIONS) and SECTIONS[i][2]
        groups[i].append(m.split(" ", 1)[1] if strip and " " in m else m)
    n = len(messages)
    head = f"🛍 <b>Price tracker</b> · {n} update{'s' if n != 1 else ''}"
    if overnight:
        head += f"\n🌅 Includes {overnight} held overnight"
    parts = [head]
    for (prefix, title, _), group in zip(SECTIONS + [("•", "Other", False)], groups):
        if group:
            emoji = prefix if isinstance(prefix, str) else "🛠"
            # The heading shares a block with the first alert so Telegram's message splitting never orphans it.
            parts.append(f"{emoji} <b>{title.upper()}</b>\n" + "\n\n".join(group))
    return "\n\n".join(parts)


def deliver(messages: list[str], profile: dict, state: dict, now: datetime | None = None) -> list[str]:
    """Send what should go now, queue the rest. Returns the alerts actually sent (for logging/tests)."""
    now = now or datetime.now(NZ)
    queue = state.setdefault("queued_alerts", [])
    if in_quiet_hours((profile or {}).get("quiet_hours"), now):
        urgent = [m for m in messages if m.startswith(URGENT_PREFIXES)]
        queue += [m for m in messages if not m.startswith(URGENT_PREFIXES)]
        outgoing, overnight = urgent, 0
    else:
        outgoing, overnight = queue + messages, len(queue)
        queue.clear()
    if outgoing:
        telegram.send(render(outgoing, overnight))
    return outgoing

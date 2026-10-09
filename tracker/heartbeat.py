"""Heartbeat: warn on Telegram if prices haven't been updated for a while (checks failing, or GitHub skipping the
schedule). Standard library only, so it still works if the tracker's own dependencies are what broke.

Run every 3 hours by .github/workflows/heartbeat.yml. With no state of its own, it warns when the data first goes
STALE_HOURS old and then once a day while it stays stale (each window is one cron interval wide).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

STALE_HOURS = 18
WINDOW_HOURS = 3  # matches the heartbeat cron interval
PRICES = Path(__file__).resolve().parent.parent / "data" / "prices.json"


def should_warn(age_h: float) -> bool:
    if age_h < STALE_HOURS:
        return False
    return (age_h - STALE_HOURS) % 24 < WINDOW_HOURS


def main() -> int:
    updated = json.loads(PRICES.read_text()).get("updated")
    age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(updated)).total_seconds() / 3600 if updated else 1e9
    print(f"Prices last updated {updated} ({age_h:.1f} h ago)")
    if not should_warn(age_h):
        return 0
    run = os.environ.get("RUN_URL", "")
    text = (f"💤 Price checks haven't run for {age_h:.0f} hours (last update {updated}). "
            f"GitHub may be skipping the schedule or the check is failing.\n{run}")
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print(text)
        return 0
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=30)
    return 0


if __name__ == "__main__":
    sys.exit(main())

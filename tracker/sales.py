"""Known recurring NZ sale events — scraping can't see future sales, so we keep a calendar instead."""
from __future__ import annotations

import calendar
from datetime import date, timedelta


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    days = [d for d in calendar.Calendar().itermonthdates(year, month) if d.month == month and d.weekday() == weekday]
    return days[n - 1]


RULES = {
    "black_friday": lambda y: _nth_weekday(y, 11, calendar.THURSDAY, 4) + timedelta(days=1),
    "cyber_monday": lambda y: _nth_weekday(y, 11, calendar.THURSDAY, 4) + timedelta(days=4),
    "mothers_day": lambda y: _nth_weekday(y, 5, calendar.SUNDAY, 2),
    "fathers_day": lambda y: _nth_weekday(y, 9, calendar.SUNDAY, 1),
}


def occurrences(event: dict, year: int) -> tuple[date, date]:
    if "rule" in event:
        day = RULES[event["rule"]](year)
        return day - timedelta(days=event.get("lead_days", 0)), day
    sm, sd = map(int, event["start"].split("-"))
    em, ed = map(int, event["end"].split("-"))
    start = date(year, sm, sd)
    end = date(year + (1 if (em, ed) < (sm, sd) else 0), em, ed)  # wraps over New Year
    return start, end


def upcoming(events: list[dict], today: date, within_days: int = 14) -> list[tuple[dict, date, date, bool]]:
    """Events active now or starting within `within_days`. Returns (event, start, end, is_active)."""
    horizon = today + timedelta(days=within_days)
    out = []
    for ev in events:
        for year in (today.year - 1, today.year, today.year + 1):
            start, end = occurrences(ev, year)
            if start <= today <= end:
                out.append((ev, start, end, True))
                break
            if today < start <= horizon:
                out.append((ev, start, end, False))
                break
    return sorted(out, key=lambda x: x[1])

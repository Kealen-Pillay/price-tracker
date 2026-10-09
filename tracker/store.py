"""Load/save the wishlist, price history (data/prices.json) and bot state (data/state.json)."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WISHLIST = ROOT / "wishlist.yaml"
RETAILERS = ROOT / "retailers.yaml"
CALENDAR = ROOT / "sales_calendar.yaml"
PRICES = ROOT / "data" / "prices.json"
STATE = ROOT / "data" / "state.json"

# Keep one history point per day when the price is unchanged; always record changes.
UNCHANGED_RECORD_INTERVAL_H = 24
WISHLIST_HEADER = (
    "# Wishlist — items to track. Edit by hand, from the dashboard, or via Telegram (/add, /link, /target, /remove).\n"
    "# Each item lists one product URL per retailer. target_price (NZD) is optional: you get an alert when any "
    "retailer is at or below it.\n"
    "# profile: who you're shopping for. gender skips products detected for other audiences (unisex/unlabelled kept).\n"
    "# shoe_size applies to items with `kind: shoes`: stock and price come only from your sizes. brand_sizes overrides the\n"
    "# built-in US->UK/EU conversion for a brand (half sizes a brand doesn't make map to both neighbouring sizes).\n"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "item"


def _load_yaml(path: Path) -> dict:
    return (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}


def load_retailers() -> dict:
    return _load_yaml(RETAILERS)


def load_calendar() -> list[dict]:
    return _load_yaml(CALENDAR).get("events", [])


def load_settings() -> dict:
    """Top-level wishlist settings (everything except `items`), e.g. only_for: men."""
    return {k: v for k, v in _load_yaml(WISHLIST).items() if k != "items"}


def load_profile() -> dict:
    """The `profile:` block (gender, shoe_size, brand_sizes). The older top-level `only_for` still sets gender."""
    settings = load_settings()
    profile = dict(settings.get("profile") or {})
    if settings.get("only_for") and not profile.get("gender"):
        profile["gender"] = settings["only_for"]
    return profile


def load_wishlist() -> list[dict]:
    items = _load_yaml(WISHLIST).get("items") or []
    for it in items:
        it.setdefault("id", slugify(it.get("name", "item")))
        it.setdefault("urls", [])
    return items


def save_wishlist(items: list[dict]) -> None:
    body = yaml.safe_dump({**load_settings(), "items": items}, sort_keys=False, allow_unicode=True)
    WISHLIST.write_text(WISHLIST_HEADER + body)


def _load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")


def load_prices() -> dict:
    return _load_json(PRICES, {"updated": None, "items": {}})


def save_prices(data: dict) -> None:
    _save_json(PRICES, data)


def load_state() -> dict:
    return _load_json(STATE, {"telegram_offset": 0, "alerts": {}, "failures": {}})


def save_state(data: dict) -> None:
    _save_json(STATE, data)


def record(history: list[dict], point: dict) -> bool:
    """Append a price point if it differs from the last one or the last is stale. Returns True if appended."""
    if history:
        last = history[-1]
        same = all(last.get(k) == point.get(k) for k in ("price", "was", "in_stock", "sizes_in"))
        age_h = (parse_iso(point["t"]) - parse_iso(last["t"])).total_seconds() / 3600
        if same and age_h < UNCHANGED_RECORD_INTERVAL_H:
            return False
    history.append(point)
    return True

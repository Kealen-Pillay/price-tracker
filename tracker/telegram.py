"""Telegram bot: send alerts and handle wishlist commands sent from your phone.

Commands (only accepted from TELEGRAM_CHAT_ID):
  /wishlist                  numbered list of your items with their ids
  /list                      numbered list with current best prices
  /add <url> [target]        track new items; put several URLs (one per line) in one message to add them all
  /remove <item> ...         stop tracking items, e.g. /remove 2 5 7 or /remove 3-6
  /undo                      bring back the items removed by the last /remove
  /link <item> <url>         add another retailer's URL to an existing item
  /target <item> <price>     set or change the target price ("none" to clear)
  /discover <item>           search other stores for this item; replies with numbered candidates
  /approve <item> 1 3        track the numbered candidates from /discover
  /help                      show this list
<item> is a number from the last /wishlist or /list, an item id, or a unique part of its name.
Commands are processed at the start of each scheduled run, so replies can take up to a few hours.
"""
from __future__ import annotations

import hashlib
import os
import re

import httpx

API = "https://api.telegram.org/bot{token}/{method}"
HELP = __doc__.split("Commands", 1)[1].split("Commands are", 1)[0].strip().removeprefix("(only accepted from TELEGRAM_CHAT_ID):").strip()


def configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def _call(method: str, **params) -> dict:
    url = API.format(token=os.environ["TELEGRAM_BOT_TOKEN"], method=method)
    r = httpx.post(url, json=params, timeout=30)
    r.raise_for_status()
    return r.json()


def send(text: str) -> None:
    """Send an HTML-formatted message, splitting it to stay under Telegram's 4096-char limit."""
    if not configured():
        print("[telegram not configured] would send:\n" + text)
        return
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) + 2 > 3800 and cur:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n\n{block}" if cur else block
    if cur:
        chunks.append(cur)
    for c in chunks:
        _call("sendMessage", chat_id=os.environ["TELEGRAM_CHAT_ID"], text=c,
              parse_mode="HTML", disable_web_page_preview=True)


def menu_commands() -> list[dict]:
    """The commands in HELP, in the shape Telegram's setMyCommands wants (shown when you type "/")."""
    out = []
    for line in HELP.splitlines():
        if m := re.match(r"\s*/(\w+)\S*(?:\s+\S+)*?\s{2,}(.+)", line):
            out.append({"command": m[1], "description": m[2].strip()[:256]})
    return out


def sync_menu(state: dict) -> None:
    """Register the command menu with Telegram once, and again whenever the command list changes."""
    if not configured():
        return
    cmds = menu_commands()
    digest = hashlib.sha1(repr(cmds).encode()).hexdigest()
    if state.get("menu_hash") == digest:
        return
    try:
        _call("setMyCommands", commands=cmds)
        state["menu_hash"] = digest
    except httpx.HTTPError as e:  # cosmetic only; never fail a run over it
        print(f"[telegram] couldn't set command menu: {e}")


def pending_commands(offset: int) -> tuple[list[str], int]:
    """Return (command texts from the owner chat, next offset)."""
    if not configured():
        return [], offset
    data = _call("getUpdates", offset=offset, timeout=0, allowed_updates=["message"])
    owner = str(os.environ["TELEGRAM_CHAT_ID"])
    texts = []
    for upd in data.get("result", []):
        offset = max(offset, upd["update_id"] + 1)
        msg = upd.get("message") or {}
        if str(msg.get("chat", {}).get("id")) == owner and (msg.get("text") or "").startswith("/"):
            texts.append(msg["text"].strip())
    return texts, offset

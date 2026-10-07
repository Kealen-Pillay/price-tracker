"""Wishlist price tracker CLI.

  python -m tracker check [--dry-run]   scrape every wishlist URL, record prices, send alerts
  python -m tracker probe <url>...      test what the scraper sees on any product page
  python -m tracker digest              send the weekly summary + upcoming sale events
  python -m tracker build-site          assemble the dashboard into _site/
  python -m tracker serve               build and serve the dashboard at http://localhost:8000
"""
from __future__ import annotations

import argparse
import functools
import http.server
import shutil
import sys
from datetime import date, timedelta, timezone, datetime
from html import escape
from urllib.parse import urlparse

from . import alerts, sales, store, telegram
from .fetch import Fetcher, ScrapeError

NZ = timezone(timedelta(hours=12))  # good enough for "which day is it" in NZ


# ---------------------------------------------------------------- helpers
def best_offer(entry: dict) -> tuple[str, dict] | None:
    live = [(u, o) for u, o in entry.get("offers", {}).items()
            if o.get("price") is not None and not o.get("error") and o.get("in_stock") is not False]
    return min(live, key=lambda x: x[1]["price"]) if live else None


def all_time_low(entry: dict) -> float | None:
    prices = [h["price"] for o in entry.get("offers", {}).values() for h in o.get("history", []) if h.get("price")]
    return min(prices) if prices else None


def summary_line(item: dict, entry: dict | None) -> str:
    entry = entry or {}
    best = best_offer(entry)
    target = item.get("target_price")
    s = f"<b>{escape(item['name'])}</b> <code>{item['id']}</code>\n"
    if best:
        url, o = best
        s += f"  Best now: {alerts.fmt(o['price'])} at {escape(o['retailer'])}"
        if o.get("was"):
            s += f" (was {alerts.fmt(o['was'])})"
    else:
        s += "  No price yet"
    atl = all_time_low(entry)
    if atl:
        s += f" · lowest seen {alerts.fmt(atl)}"
    if target is not None:
        s += f" · target {alerts.fmt(float(target))}"
    return s


# ---------------------------------------------------------------- telegram commands
def handle_commands(fetcher: Fetcher, items: list[dict], prices: dict, state: dict) -> bool:
    """Process pending Telegram commands. Returns True if the wishlist changed."""
    texts, state["telegram_offset"] = telegram.pending_commands(state.get("telegram_offset", 0))
    changed = False
    by_id = {it["id"]: it for it in items}
    for text in texts:
        cmd, *args = text.split()
        cmd = cmd.split("@")[0].lower()
        try:
            if cmd == "/add" and args:
                url = args[0]
                target = float(args[1]) if len(args) > 1 else None
                try:
                    offer, _ = fetcher.get_offer(url)
                    name = (offer.title or "").strip() or None
                except ScrapeError:
                    name = None
                name = name or urlparse(url).path.rstrip("/").split("/")[-1].replace("-", " ").title()
                base = store.slugify(name)
                iid, n = base, 2
                while iid in by_id:
                    iid, n = f"{base}-{n}", n + 1
                item = {"id": iid, "name": name, "target_price": target, "urls": [url]}
                if target is None:
                    del item["target_price"]
                items.append(item)
                by_id[iid] = item
                changed = True
                telegram.send(f"➕ Tracking <b>{escape(name)}</b> as <code>{iid}</code>. "
                              f"Add other stores with /link {iid} &lt;url&gt;")
            elif cmd == "/link" and len(args) == 2 and args[0] in by_id:
                if args[1] not in by_id[args[0]]["urls"]:
                    by_id[args[0]]["urls"].append(args[1])
                    changed = True
                telegram.send(f"🔗 Added {escape(urlparse(args[1]).netloc)} to <code>{args[0]}</code>")
            elif cmd == "/target" and len(args) == 2 and args[0] in by_id:
                if args[1].lower() in ("none", "off", "clear"):
                    by_id[args[0]].pop("target_price", None)
                else:
                    by_id[args[0]]["target_price"] = float(args[1].lstrip("$"))
                changed = True
                telegram.send(f"🎯 Target for <code>{args[0]}</code> set to {escape(args[1])}")
            elif cmd == "/remove" and args and args[0] in by_id:
                items.remove(by_id.pop(args[0]))
                changed = True
                telegram.send(f"🗑 Stopped tracking <code>{args[0]}</code>")
            elif cmd == "/list":
                lines = [summary_line(it, prices["items"].get(it["id"])) for it in items]
                telegram.send("\n\n".join(lines) or "Wishlist is empty. Use /add &lt;url&gt;")
            else:
                telegram.send(f"Didn't understand <code>{escape(text)}</code>\n\n{escape(telegram.HELP)}")
        except Exception as e:  # never let a bad command break the price check
            telegram.send(f"Error handling <code>{escape(text)}</code>: {escape(str(e))}")
    return changed


# ---------------------------------------------------------------- commands
def cmd_check(args) -> int:
    items = store.load_wishlist()
    prices = store.load_prices()
    state = store.load_state()
    messages: list[str] = []

    with Fetcher(store.load_retailers()) as fetcher:
        if not args.dry_run and handle_commands(fetcher, items, prices, state):
            store.save_wishlist(items)

        tracked = {it["id"] for it in items}
        for iid, entry in prices["items"].items():
            entry["tracked"] = iid in tracked

        for item in items:
            entry = prices["items"].setdefault(item["id"], {"offers": {}})
            entry.update(name=item["name"], target_price=item.get("target_price"), tracked=True,
                         urls=list(item["urls"]))
            hosts = [urlparse(u).netloc for u in item["urls"]]
            for url in item["urls"]:
                retailer_name = fetcher.retailer_for(url)["name"]
                shared_host = hosts.count(urlparse(url).netloc) > 1  # e.g. several colours at one store
                slot = entry["offers"].setdefault(url, {"history": []})
                prev = {k: slot.get(k) for k in ("price", "was", "in_stock")} if slot.get("price") is not None else None
                checked = store.now_iso()
                try:
                    offer, retailer = fetcher.get_offer(url)
                except ScrapeError as e:
                    print(f"  ✗ {item['name']} @ {retailer_name}: {e}")
                    slot.update(retailer=retailer_name, error=str(e), checked=checked)
                    messages += alerts.failure(item, url, retailer_name, str(e), state)
                    continue
                point = {"t": checked, "price": offer.price, "was": offer.was_price, "in_stock": offer.in_stock}
                print(f"  ✓ {item['name']} @ {retailer['name']}: {alerts.fmt(offer.price)}"
                      f"{' (was ' + alerts.fmt(offer.was_price) + ')' if offer.was_price else ''}"
                      f"{'' if offer.in_stock is not False else ' OUT OF STOCK'}  [{offer.method}]")
                label = f"{retailer['name']} ({offer.title})" if shared_host and offer.title else retailer["name"]
                messages += alerts.evaluate(item, url, label, point, prev, slot["history"], state)
                alerts.recovered(item, url, state)
                store.record(slot["history"], point)
                slot.update(retailer=retailer["name"], title=offer.title, price=offer.price, was=offer.was_price,
                            in_stock=offer.in_stock, method=offer.method, checked=checked, error=None)
            for stale in set(entry["offers"]) - set(item["urls"]):
                entry["offers"][stale]["tracked"] = False

    prices["updated"] = store.now_iso()
    if args.dry_run:
        print("\n[dry run] alerts that would be sent:\n" + ("\n\n".join(messages) or "(none)"))
        return 0
    store.save_prices(prices)
    store.save_state(state)
    if messages:
        telegram.send("\n\n".join(messages))
    return 0


def cmd_probe(args) -> int:
    with Fetcher(store.load_retailers(), polite_delay=(0, 0)) as fetcher:
        for url in args.urls:
            try:
                offer, retailer = fetcher.get_offer(url)
                print(f"{retailer['name']}: {offer.to_dict()}")
                if offer.method == "json-ld-range":
                    print("  ⚠ This page lists several sizes; the price shown is the cheapest. "
                          "Use the URL for the exact size you want.")
            except ScrapeError as e:
                print(f"{fetcher.retailer_for(url)['name']}: ERROR {e}")
    return 0


def cmd_digest(args) -> int:
    items = store.load_wishlist()
    prices = store.load_prices()
    today = datetime.now(NZ).date()
    parts = ["🗓 <b>Weekly wishlist digest</b>"]
    events = sales.upcoming(store.load_calendar(), today, within_days=14)
    if events:
        lines = []
        for ev, start, end, active in events:
            when = f"on now until {end:%a %d %b}" if active else f"{start:%a %d %b} – {end:%a %d %b}"
            lines.append(f"• {escape(ev['name'])}: {when}")
        parts.append("<b>Sales on now / in the next 2 weeks</b>\n" + "\n".join(lines))
    else:
        parts.append("No known sale events in the next 2 weeks.")
    parts += [summary_line(it, prices["items"].get(it["id"])) for it in items]
    msg = "\n\n".join(parts)
    if args.dry_run:
        print(msg)
    else:
        telegram.send(msg)
    return 0


def cmd_build_site(args) -> int:
    site = store.ROOT / "_site"
    shutil.rmtree(site, ignore_errors=True)
    shutil.copytree(store.ROOT / "dashboard", site)
    (site / "data").mkdir(exist_ok=True)
    if store.PRICES.exists():
        shutil.copy(store.PRICES, site / "data" / "prices.json")
    shutil.copy(store.WISHLIST, site / "wishlist.yaml")
    print(f"Built {site}")
    return 0


def cmd_serve(args) -> int:
    cmd_build_site(args)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(store.ROOT / "_site"))
    print(f"Serving dashboard at http://localhost:{args.port}  (Ctrl+C to stop)")
    http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler).serve_forever()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="tracker", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--dry-run", action="store_true", help="print results; don't save or send anything")
    c.set_defaults(fn=cmd_check)
    pr = sub.add_parser("probe")
    pr.add_argument("urls", nargs="+")
    pr.set_defaults(fn=cmd_probe)
    d = sub.add_parser("digest")
    d.add_argument("--dry-run", action="store_true")
    d.set_defaults(fn=cmd_digest)
    sub.add_parser("build-site").set_defaults(fn=cmd_build_site)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

import json
from datetime import date
from pathlib import Path

import pytest

from tracker import alerts, sales, store
from tracker.parse import money, parse_html, parse_shopify_js

FIX = Path(__file__).parent / "fixtures"
RETAILERS = store.load_retailers()


def fixture(name):
    return (FIX / name).read_text(errors="ignore")


# ---- parsers against real saved pages (re-save fixtures if a retailer changes its markup)
def test_chemist_warehouse_microdata_and_rrp():
    o = parse_html(fixture("chemistwarehouse_sauvage_edp.html"), RETAILERS["www.chemistwarehouse.co.nz"])
    assert (o.price, o.was_price, o.in_stock, o.currency) == (249.99, 259.0, True, "NZD")
    assert "Sauvage Eau De Parfum 100ml" in o.title


def test_pricespy_aggregate_offer_uses_low_price():
    o = parse_html(fixture("pricespy_sauvage_edp.html"))
    assert o.price == 255.0 and o.method == "json-ld"


def test_mecca_multi_size_page_is_flagged_as_range():
    o = parse_html(fixture("mecca_vilhelm.html"))
    assert o.price == 338.0 and o.method == "json-ld-range"


def test_shopify_json_prices_are_cents():
    o = parse_shopify_js(json.loads(fixture("bargainchemist_sauvage_edt.json")))
    assert o.price == 164.99 and o.was_price == 190.0 and o.in_stock is True


@pytest.mark.parametrize("raw,expected", [("$1,249.99", 1249.99), ("NZ$ 99", 99.0), (255, 255.0), ("Why Pay $259.00?", 259.0), (None, None)])
def test_money(raw, expected):
    assert money(raw) == expected


def test_jsonld_graph_and_out_of_stock():
    html = """<script type="application/ld+json">{"@graph":[{"@type":"WebPage"},
      {"@type":["Product"],"name":"X","offers":[{"@type":"Offer","price":"120.00","availability":"https://schema.org/OutOfStock"}]}]}</script>"""
    o = parse_html(html)
    assert o.price == 120.0 and o.in_stock is False


# ---- alerting
ITEM = {"id": "x", "name": "Thing", "target_price": 200}


def test_target_alert_once_then_rearms():
    state = {}
    pt = {"price": 199.0, "in_stock": True}
    assert any("Target hit" in m for m in alerts.evaluate(ITEM, "u", "Shop", pt, {"price": 250}, [], state))
    assert alerts.evaluate(ITEM, "u", "Shop", pt, {"price": 199.0}, [], state) == []  # no repeat
    assert any("Target hit" in m for m in alerts.evaluate(ITEM, "u", "Shop", {"price": 190.0}, {"price": 199}, [], state))
    alerts.evaluate(ITEM, "u", "Shop", {"price": 260.0}, {"price": 190}, [], state)  # back above → re-armed
    assert any("Target hit" in m for m in alerts.evaluate(ITEM, "u", "Shop", {"price": 195.0}, {"price": 260}, [], state))


def test_drop_threshold_and_all_time_low():
    item = {"id": "y", "name": "Thing"}
    assert alerts.evaluate(item, "u", "S", {"price": 98.0}, {"price": 100.0}, [], {}) == []  # 2% → ignored
    msgs = alerts.evaluate(item, "u", "S", {"price": 80.0}, {"price": 100.0}, [{"price": 100.0}, {"price": 90.0}], {})
    assert "20%" in msgs[0] and "lowest price seen" in msgs[0]


def test_back_in_stock_and_failure_warning_once():
    item = {"id": "z", "name": "<Thing & co>"}
    msgs = alerts.evaluate(item, "u", "S", {"price": 50.0, "in_stock": True}, {"price": 50.0, "in_stock": False}, [], {})
    assert "Back in stock" in msgs[0] and "&lt;Thing &amp; co&gt;" in msgs[0]
    state = {}
    out = [alerts.failure(item, "u", "S", "boom", state) for _ in range(5)]
    assert [bool(o) for o in out] == [False, False, True, False, False]


def test_record_skips_unchanged_within_a_day():
    h = []
    assert store.record(h, {"t": "2026-10-01T00:00:00+00:00", "price": 10, "was": None, "in_stock": True})
    assert not store.record(h, {"t": "2026-10-01T06:00:00+00:00", "price": 10, "was": None, "in_stock": True})
    assert store.record(h, {"t": "2026-10-01T12:00:00+00:00", "price": 9, "was": None, "in_stock": True})
    assert store.record(h, {"t": "2026-10-02T13:00:00+00:00", "price": 9, "was": None, "in_stock": True})


# ---- sales calendar
def test_sales_rules_and_upcoming():
    assert sales.RULES["black_friday"](2026) == date(2026, 11, 27)
    assert sales.RULES["fathers_day"](2026) == date(2026, 9, 6)
    assert sales.RULES["mothers_day"](2026) == date(2026, 5, 10)
    events = store.load_calendar()
    names = [e["name"] for e, *_ in sales.upcoming(events, date(2026, 11, 14), 14)]
    assert any("Black Friday" in n for n in names) and any("Christmas" in n for n in names)
    active = sales.upcoming(events, date(2027, 1, 2), 3)  # Boxing Day window wraps over New Year
    assert any("Boxing" in e["name"] and is_active for e, _, _, is_active in active)


# ---- telegram commands (no network)
def test_telegram_commands(monkeypatch):
    import tracker.__main__ as cli
    from tracker import telegram
    from tracker.parse import Offer

    sent = []
    cmds = ["/add https://shop.example/products/foo-100ml 150", "/link foo-eau-de-parfum https://other.example/p/1",
            "/target foo-eau-de-parfum 140", "/list", "/bogus", "/remove nothing-here"]
    monkeypatch.setattr(telegram, "pending_commands", lambda off: (cmds, off + len(cmds)))
    monkeypatch.setattr(telegram, "send", sent.append)

    class FakeFetcher:
        def get_offer(self, url):
            return Offer(price=160.0, title="Foo Eau de Parfum"), {"name": "Shop"}

    items, state = [], {"telegram_offset": 10}
    assert cli.handle_commands(FakeFetcher(), items, {"items": {}}, state)
    assert items == [{"id": "foo-eau-de-parfum", "name": "Foo Eau de Parfum", "target_price": 140.0,
                      "urls": ["https://shop.example/products/foo-100ml", "https://other.example/p/1"]}]
    assert state["telegram_offset"] == 16
    assert "Tracking" in sent[0] and "Foo Eau de Parfum" in sent[3]
    assert all("Didn't understand" in s for s in sent[4:6])  # unknown command and unknown item id


def test_jsonld_per_size_offers_prefer_in_stock():
    html = """<script type="application/ld+json">{"@type":"Product","name":"XT-6","offers":[
      {"@type":"Offer","price":"200.00","availability":"https://schema.org/OutOfStock"},
      {"@type":"Offer","price":"220.00","availability":"https://schema.org/InStock"},
      {"@type":"Offer","price":"200.00","availability":"https://schema.org/InStock"}]}</script>"""
    o = parse_html(html)
    assert o.price == 200.0 and o.in_stock is True
    sold_out = html.replace("InStock", "OutOfStock")
    assert parse_html(sold_out).in_stock is False


def test_jsonld_product_group_pools_variant_offers():
    html = """<script type="application/ld+json">{"@type":"ProductGroup","name":"XT-6","hasVariant":[
      {"@type":"Product","name":"XT-6 - 3","offers":{"@type":"Offer","price":"200.00","availability":"http://schema.org/OutOfStock"}},
      {"@type":"Product","name":"XT-6 - 9","offers":{"@type":"Offer","price":"200.00","availability":"http://schema.org/InStock"}}]}</script>"""
    o = parse_html(html)
    assert (o.price, o.in_stock, o.title) == (200.0, True, "XT-6")


# ---- out-of-stock offers are disregarded
def test_out_of_stock_offers_never_alert():
    item = {"id": "o", "name": "Thing", "target_price": 100}
    state = {}
    assert alerts.evaluate(item, "u", "S", {"price": 50.0, "in_stock": False}, {"price": 150.0, "in_stock": True}, [], state) == []
    assert "target_at" not in state["alerts"]["o|u"]  # a later in-stock hit still alerts
    assert alerts.evaluate(item, "u", "S", {"price": 90.0, "in_stock": True}, {"price": 50.0, "in_stock": False}, [], state)


def test_lowest_seen_ignores_out_of_stock_history():
    item = {"id": "o", "name": "Thing"}
    history = [{"price": 100.0, "in_stock": True}, {"price": 60.0, "in_stock": False}]
    msgs = alerts.evaluate(item, "u", "S", {"price": 80.0, "in_stock": True}, {"price": 100.0, "in_stock": True}, history, {})
    assert "20%" in msgs[0] and "lowest price seen" in msgs[0]
    assert alerts.buyable_prices(history) == [100.0]


def test_back_in_stock_is_not_reported_as_a_drop():
    item = {"id": "o", "name": "Thing"}
    msgs = alerts.evaluate(item, "u", "S", {"price": 80.0, "in_stock": True}, {"price": 200.0, "in_stock": False}, [], {})
    assert len(msgs) == 1 and "Back in stock" in msgs[0]


def test_all_time_low_and_best_offer_skip_out_of_stock():
    import tracker.__main__ as cli
    entry = {"offers": {
        "a": {"retailer": "A", "price": 50.0, "in_stock": False, "history": [{"price": 50.0, "in_stock": False}]},
        "b": {"retailer": "B", "price": 90.0, "in_stock": True, "history": [{"price": 95.0, "in_stock": True}, {"price": 90.0, "in_stock": True}]},
    }}
    assert cli.best_offer(entry)[0] == "b" and cli.all_time_low(entry) == 90.0


# ---- men's-only filter
from tracker.parse import detect_gender, excluded_for


@pytest.mark.parametrize("text,expected", [
    ("XT-6 Women's", ["women"]), ("Arizona Kids EVA", ["kids"]), ("Bleu de Chanel Pour Homme", ["men"]),
    ("Versace Dylan Blue Pour Femme EDP 100ml for women", ["women"]), ("Unisex Tee", ["men", "women"]),
    ("XT-6", None), ("Superman Tee", None), ("Manchester United Jersey", None),
])
def test_detect_gender(text, expected):
    assert detect_gender(text) == expected


def test_excluded_for_men():
    assert excluded_for(["women"], "men") and excluded_for(["kids"], "men")
    assert not excluded_for(["men", "women"], "men")  # unisex is kept
    assert not excluded_for(None, "men")  # unknown is kept
    assert not excluded_for(["women"], None)  # filter off


def test_shopify_gender_comes_from_gender_tags_only():
    data = {"title": "Arizona", "type": "Sandals", "available": True, "variants": [{"price": 30000, "available": True}],
            "tags": ["gender:Mens", "gender:Womens", "Collection:Womens Sale"]}
    assert parse_shopify_js(data).genders == ["men", "women"]
    data["tags"] = ["gender:Womens", "Collection:Mens"]
    assert parse_shopify_js(data).genders == ["women"]


def test_telegram_add_refuses_womens_product(monkeypatch):
    import tracker.__main__ as cli
    from tracker import telegram
    from tracker.parse import Offer

    sent = []
    monkeypatch.setattr(telegram, "pending_commands", lambda off: (["/add https://x.example/products/xt6-w"], off + 1))
    monkeypatch.setattr(telegram, "send", sent.append)
    monkeypatch.setattr(store, "load_settings", lambda: {"only_for": "men"})

    class FakeFetcher:
        def get_offer(self, url):
            return Offer(price=360.0, title="XT-6 Women's", genders=["women"]), {"name": "JD"}

    items = []
    assert not cli.handle_commands(FakeFetcher(), items, {"items": {}}, {"telegram_offset": 0})
    assert items == [] and "Not added" in sent[0]


def test_gender_falls_back_to_url_slug(monkeypatch):
    from tracker.fetch import Fetcher
    from tracker.parse import Offer

    f = Fetcher({})
    monkeypatch.setattr(f, "_get_offer", lambda url: (Offer(price=1.0, title="XT-6"), {"name": "JD"}))
    assert f.get_offer("https://www.jdsports.co.nz/products/xt6-womens-120525865")[0].genders == ["women"]
    assert f.get_offer("https://www.jdsports.co.nz/products/xt6-120733321")[0].genders is None


# ---- Phase 1: geo/currency safety, confirmation of big changes, product identity
from tracker import verify


def test_big_change_and_matching():
    prev = {"price": 100.0, "in_stock": True}
    assert not verify.is_big_change(None, {"price": 50.0, "in_stock": True})
    assert not verify.is_big_change(prev, {"price": 90.0, "in_stock": True})  # 10%: small
    assert verify.is_big_change(prev, {"price": 80.0, "in_stock": True})  # 20%: big
    assert verify.is_big_change(prev, {"price": 100.0, "in_stock": False})  # stock flip
    assert verify.readings_match({"price": 100.0, "in_stock": True}, {"price": 100.5, "in_stock": True})
    assert not verify.readings_match({"price": 100.0, "in_stock": True}, {"price": 100.0, "in_stock": False})
    assert not verify.readings_match(None, {"price": 1.0})


def _offer(**kw):
    from tracker.parse import Offer
    return Offer(**{"price": 10.0, **kw})


def test_identity_baseline_rename_and_swap():
    item, slot, url = {"name": "Mat"}, {}, "https://shop.example/products/mat"
    assert verify.identity_warning(item, url, "S", slot, _offer(product_id="shopify:1", title="Desk Mat Slim")) is None
    # Same product id, renamed: followed quietly (Orbitkey renamed "Desk Mat Slim" → "Desk Mat Pro Slim").
    assert verify.identity_warning(item, url, "S", slot, _offer(product_id="shopify:1", title="Desk Mat Pro Slim")) is None
    assert slot["identity"]["title"] == "Desk Mat Pro Slim"
    # Different product id: warn once, then accept the new baseline.
    w = verify.identity_warning(item, url, "S", slot, _offer(product_id="shopify:2", title="Laptop Sleeve"))
    assert w and "product id changed" in w
    assert verify.identity_warning(item, url, "S", slot, _offer(product_id="shopify:2", title="Laptop Sleeve")) is None


def test_identity_title_and_redirect_without_ids():
    item, url = {"name": "Perfume"}, "https://cw.example/buy/1/dior-sauvage"
    slot = {}
    verify.identity_warning(item, url, "CW", slot, _offer(title="Dior Sauvage EDP 100ml"))
    assert "name changed" in verify.identity_warning(item, url, "CW", slot, _offer(title="Panadol 20 tablets"))
    slot = {}
    verify.identity_warning(item, url, "CW", slot, _offer(title="A", final_url=url + "/"))  # trailing slash is fine
    w = verify.identity_warning(item, url, "CW", slot, _offer(title="A", final_url="https://cw.example/clearance"))
    assert w and "redirects" in w


def test_wrong_currency_is_rejected(monkeypatch):
    from tracker.fetch import Fetcher, ScrapeError
    f = Fetcher({})
    monkeypatch.setattr(f, "_get_offer", lambda url: (_offer(price=130.0, currency="AUD"), {"name": "Orbitkey"}))
    with pytest.raises(ScrapeError, match="AUD"):
        f.get_offer("https://www.orbitkey.com.au/products/desk-mat-slim")


def test_shopify_unavailable_is_confirmed_on_the_product_page(monkeypatch):
    from tracker.fetch import Fetcher
    f = Fetcher({}, polite_delay=(0, 0))
    monkeypatch.setattr(f, "_shopify", lambda url: _offer(in_stock=False, currency="NZD"))
    monkeypatch.setattr(f, "_page_offer", lambda url, r: _offer(in_stock=True))
    assert f.get_offer("https://life.example/products/prada")[0].in_stock is True
    monkeypatch.setattr(f, "_page_offer", lambda url, r: _offer(in_stock=False))
    assert f.get_offer("https://life.example/products/prada")[0].in_stock is False


def _run_check(monkeypatch, tmp_path, readings, recheck=None):
    """Run cmd_check against a fake fetcher; `readings` are returned in order for the single URL."""
    import argparse
    import tracker.__main__ as cli
    from tracker import telegram

    url = "https://shop.example/products/thing"
    monkeypatch.setattr(store, "PRICES", tmp_path / "prices.json")
    monkeypatch.setattr(store, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(store, "load_wishlist", lambda: [{"id": "thing", "name": "Thing", "urls": [url]}])
    monkeypatch.setattr(store, "load_settings", lambda: {})
    monkeypatch.setattr(telegram, "pending_commands", lambda off: ([], off))
    sent = []
    monkeypatch.setattr(telegram, "send", sent.append)

    class FakeFetcher:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *e): pass
        def retailer_for(self, u): return {"name": "Shop"}
        def get_offer(self, u): return readings.pop(0), {"name": "Shop"}
        def recheck(self, u): return recheck

    monkeypatch.setattr(cli, "Fetcher", FakeFetcher)
    cli.cmd_check(argparse.Namespace(dry_run=False))
    return store.load_prices()["items"]["thing"]["offers"][url], sent


def test_big_drop_needs_confirmation(monkeypatch, tmp_path):
    slot, sent = _run_check(monkeypatch, tmp_path, [_offer(price=100.0, in_stock=True)])
    assert slot["price"] == 100.0 and not sent
    # A 50% "drop" that a fresh-session recheck doesn't reproduce: held as pending, nothing recorded or sent.
    slot, sent = _run_check(monkeypatch, tmp_path, [_offer(price=50.0, in_stock=True)],
                            recheck=_offer(price=100.0, in_stock=True))
    assert slot["price"] == 100.0 and slot["pending"]["price"] == 50.0 and len(slot["history"]) == 1 and not sent
    # Seen again next run: confirmed, recorded and alerted.
    slot, sent = _run_check(monkeypatch, tmp_path, [_offer(price=50.0, in_stock=True)], recheck=None)
    assert slot["price"] == 50.0 and "pending" not in slot and len(slot["history"]) == 2
    assert "Price drop 50%" in sent[0]


def test_big_drop_confirmed_by_immediate_recheck(monkeypatch, tmp_path):
    _run_check(monkeypatch, tmp_path, [_offer(price=100.0, in_stock=True)])
    slot, sent = _run_check(monkeypatch, tmp_path, [_offer(price=70.0, in_stock=True)],
                            recheck=_offer(price=70.0, in_stock=True))
    assert slot["price"] == 70.0 and "Price drop 30%" in sent[0]


def test_glitch_that_reverts_is_discarded(monkeypatch, tmp_path):
    _run_check(monkeypatch, tmp_path, [_offer(price=100.0, in_stock=True)])
    _run_check(monkeypatch, tmp_path, [_offer(price=100.0, in_stock=False)], recheck=_offer(price=100.0, in_stock=True))
    slot, sent = _run_check(monkeypatch, tmp_path, [_offer(price=100.0, in_stock=True)])
    assert "pending" not in slot and slot["in_stock"] is True and not sent
    assert [h["in_stock"] for h in slot["history"]] == [True]

import pytest

from tracker import __main__ as cli
from tracker import telegram

ITEMS = [
    {"id": "dior-sauvage-edp", "name": "Dior Sauvage EDP 100ml", "kind": "fragrance", "urls": ["https://a.nz/1"]},
    {"id": "dr-martens-1460", "name": "Dr Martens 1460", "kind": "shoes", "urls": ["https://b.nz/2"]},
    {"id": "bleu-de-chanel", "name": "Bleu de Chanel EDP", "kind": "fragrance", "urls": ["https://c.nz/3"]},
    {"id": "orbitkey-ring", "name": "Orbitkey Ring", "urls": ["https://d.nz/4"]},
]


@pytest.fixture
def bot(monkeypatch):
    """Feed commands in, collect replies."""
    sent, inbox = [], []
    monkeypatch.setattr(telegram, "pending_commands", lambda off: (inbox[:], off + len(inbox)))
    monkeypatch.setattr(telegram, "send", sent.append)
    monkeypatch.setattr(telegram, "sync_menu", lambda state: None)

    def run(*texts, items=None, state=None):
        inbox[:] = texts
        sent.clear()
        items = items if items is not None else [dict(i) for i in ITEMS]
        state = state if state is not None else {}
        changed = cli.handle_commands(None, items, {"items": {}}, state)
        return items, state, changed, list(sent)
    return run


def ids(items):
    return [i["id"] for i in items]


def test_wishlist_is_numbered_in_display_order(bot):
    _, state, _, sent = bot("/wishlist")
    # grouped by kind: fragrances first (first seen), then shoes, then other
    assert state["wishlist_order"] == ["dior-sauvage-edp", "bleu-de-chanel", "dr-martens-1460", "orbitkey-ring"]
    assert "1. Dior Sauvage" in sent[0] and "2. Bleu de Chanel" in sent[0] and "4. Orbitkey" in sent[0]


def test_remove_several_by_number_and_range(bot):
    _, state, _, _ = bot("/wishlist")
    items, state, changed, sent = bot("/remove 1, 3-4", state=state)
    assert changed and ids(items) == ["bleu-de-chanel"]
    assert "3 item(s)" in sent[0] and "Dr Martens 1460" in sent[0]


def test_numbers_use_the_list_you_saw_even_after_adds(bot):
    _, state, _, _ = bot("/wishlist")
    items = [dict(i) for i in ITEMS]
    items.insert(0, {"id": "new", "name": "New", "kind": "fragrance", "urls": ["u"]})
    items, _, _, _ = bot("/remove 2", items=items, state=state)
    assert "bleu-de-chanel" not in ids(items) and "new" in ids(items)


def test_remove_by_id_or_name_part_and_reports_problems(bot):
    items, _, _, sent = bot("/remove dr-martens-1460 orbitkey edp nope 9")
    assert ids(items) == ["dior-sauvage-edp", "bleu-de-chanel"]
    assert "matches 2 items" in sent[0] and "nothing matches" in sent[0] and "no item 9" in sent[0]


def test_undo_restores_last_removal(bot):
    items, state, _, _ = bot("/remove 1 2")
    items, state, changed, sent = bot("/undo", items=items, state=state)
    assert changed and sorted(ids(items)) == sorted(ids(ITEMS))
    assert bot("/undo", items=items, state=state)[3] == ["Nothing to undo"]


def test_add_several_urls_in_one_message(bot, monkeypatch):
    calls = []

    def fake_add(fetcher, url, target, items, by_id):
        calls.append((url, target))
        items.append({"id": url[-1], "name": url, "urls": [url]})
        return f"added {url}", True
    monkeypatch.setattr(cli, "add_item", fake_add)
    items, _, changed, sent = bot("/add https://x.nz/a 150\nhttps://x.nz/b\nhttps://x.nz/c $99.50")
    assert calls == [("https://x.nz/a", 150.0), ("https://x.nz/b", None), ("https://x.nz/c", 99.5)]
    assert changed and len(sent) == 1 and sent[0].count("added") == 3


def test_target_and_link_accept_numbers(bot):
    _, state, _, _ = bot("/wishlist")
    items, _, _, _ = bot("/target 3 180", "/link 3 https://e.nz/5", state=state)
    dm = next(i for i in items if i["id"] == "dr-martens-1460")
    assert dm["target_price"] == 180.0 and "https://e.nz/5" in dm["urls"]


def test_menu_commands_parsed_from_help():
    names = [c["command"] for c in telegram.menu_commands()]
    assert names == ["wishlist", "list", "add", "remove", "undo", "link", "target", "discover", "approve", "help"]
    assert telegram.menu_commands()[3]["description"].startswith("stop tracking items")

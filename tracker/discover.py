"""Find the same product at other stores, so adding a store to an item doesn't mean hunting for links by hand.

Each searchable store has a `search:` block in retailers.yaml:
  search: {type: shopify}                                   # /search/suggest.json
  search: {type: magento}                                   # GraphQL products(search:), uses magento_store
  search: {type: browser, url: "https://…?q={q}", link: "/buy/\\d+/[^\"?#]+"}   # read links off a search page
and `categories: [fragrance, shoes, electronics]` so a fragrance isn't searched for at a shoe shop.

Every candidate is then read like a normal tracked link (real title, NZD price, stock, gender) and scored against
the item's name; only close matches are suggested, and nothing is added without your approval.
"""
from __future__ import annotations

import re
from urllib.parse import quote, urljoin, urlparse

import httpx

from .fetch import MAGENTO_QUERY, Fetcher, ScrapeError, log
from .parse import excluded_for

MAGENTO_SEARCH = """query ($q: String) { products(search: $q, pageSize: 10) { items { name url_key url_suffix } } }"""
MIN_SCORE = 0.6
PER_STORE = 3  # candidates verified per store
STOP = {"the", "and", "with", "for", "de", "of", "a", "eau", "spray", "mens", "men", "black", "brown", "red", "le", "la"}
_ = MAGENTO_QUERY  # (re-exported for callers that build their own queries)


# Words that make a fragrance a different product: Sauvage EDT ≠ Sauvage Elixir, even at the same size.
# The most specific concentration word wins ("Sauvage Elixir Parfum" is an Elixir); modifiers must match exactly
# ("Stronger With You Intensely" ≠ "Stronger With You Powerfully", "MYSLF Le Parfum" ≠ "MYSLF L'Absolu Parfum").
CONCENTRATIONS = ["elixir", "extrait", "parfum", "edp", "edt", "cologne"]
MODIFIERS = {"intense", "intensely", "absolu", "powerfully", "only", "noir", "sport"}


def _concentration(tokens: set[str]) -> str | None:
    return next((c for c in CONCENTRATIONS if c in tokens), None)
_SYNONYMS = [(r"eau\s+de\s+parfum", "edp"), (r"eau\s+de\s+toilette", "edt"), (r"eau\s+de\s+cologne", "cologne"),
             (r"(\d)\s*-?\s*in\s*-?\s*(\d)", r" \1in\2 ")]  # "Pro2-in-1" -> "pro 2in1"
_N_IN_M = re.compile(r"^\d+in\d+$")


def _normalise(text: str) -> str:
    text = (text or "").lower()
    for pat, rep in _SYNONYMS:
        text = re.sub(pat, rep, text)
    return re.sub(r"(\d+)\s*ml\b", r"\1ml", text)


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", _normalise(text)) if t not in STOP and len(t) > 1}


def search_query(item: dict) -> str:
    return item.get("search") or re.sub(r"\s*\(.*?\)", "", item["name"]).strip()


def _ordered_tokens(text: str) -> list[str]:
    toks = _tokens(text)
    seen, out = set(), []
    for w in re.findall(r"[a-z0-9]+", _normalise(text)):
        if w in toks and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def score(query: str, title: str) -> float:
    """Share of the query's words found in the title, with hard rules: the size (100ml), the fragrance
    concentration and modifiers must match, and the product's own name words must be there — the first word
    (usually the brand) may be missing, and long names may miss up to a third ("Charger" vs "Charging Pad")."""
    q, t = _tokens(query), _tokens(title)
    if not q:
        return 0.0
    significant = [w for w in _ordered_tokens(query)
                   if len(w) >= 4 and not w.endswith("ml") and w not in CONCENTRATIONS and w not in MODIFIERS]
    required = significant[1:] if len(significant) > 1 else significant
    # The model name (first word after the brand: Arizona, Paradigme, Activiva…) is never optional.
    if required and required[0] not in t:
        return 0.0
    if len([w for w in required if w not in t]) > len(required) // 3:
        return 0.0
    if {w for w in q if _N_IN_M.match(w)} != {w for w in t if _N_IN_M.match(w)}:
        return 0.0  # 2-in-1 ≠ 3-in-1
    if significant and significant[0] not in t and len(q) > 1:
        q = q - {significant[0]}  # stores often leave their own brand out of titles ("Boston Soft Footbed")
    sizes_q, sizes_t = {x for x in q if x.endswith("ml")}, {x for x in t if x.endswith("ml")}
    if sizes_q and sizes_t and not (sizes_q & sizes_t):
        return 0.0
    if _concentration(q) and _concentration(q) != _concentration(t):
        return 0.0
    if (q & MODIFIERS) != (t & MODIFIERS):
        return 0.0
    return len(q & t) / len(q)


def _norm_url(u: str) -> str:
    p = urlparse(u)
    return f"{p.netloc.lower().removeprefix('www.')}{p.path.rstrip('/').lower()}"


class Searcher:
    def __init__(self, fetcher: Fetcher):
        self.f = fetcher

    def shopify(self, host: str, q: str) -> list[str]:
        r = self.f.client.get(f"https://{host}/search/suggest.json",
                              params={"q": q, "resources[type]": "product", "resources[limit]": 10})
        return [urljoin(f"https://{host}", p["url"].split("?")[0])
                for p in r.json()["resources"]["results"]["products"]]

    def magento(self, host: str, q: str, store: str | None) -> list[str]:
        r = self.f.client.post(f"https://{host}/graphql", headers={"Store": store} if store else {},
                               json={"query": MAGENTO_SEARCH, "variables": {"q": q}})
        items = (r.json().get("data") or {}).get("products", {}).get("items") or []
        return [f"https://{host}/{it['url_key']}{it.get('url_suffix') or ''}" for it in items if it and it.get("url_key")]

    def browser(self, host: str, q: str, cfg: dict) -> list[str]:
        html = self.f._browser_html(cfg["url"].format(q=quote(q)))
        links = re.findall(rf'href="((?:https?://{re.escape(host)})?{cfg["link"]})"', html)
        return [urljoin(f"https://{host}", u) for u in dict.fromkeys(links)]

    def store(self, host: str, cfg: dict, q: str) -> list[str]:
        kind = cfg["search"]["type"]
        try:
            if kind == "shopify":
                return self.shopify(host, q)
            if kind == "magento":
                return self.magento(host, q, cfg.get("magento_store"))
            if kind == "browser":
                return self.browser(host, q, cfg["search"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.info("search %s for %r failed: %r", host, q, e)
        except Exception as e:  # browser errors
            log.info("search %s for %r failed: %r", host, q, e)
        return []


def discover(item: dict, retailers: dict, fetcher: Fetcher, profile: dict, known_ids: set[str] | None = None) -> list[dict]:
    """Candidate links for an item at stores it isn't tracked at yet, best first.

    `known_ids` are product ids (SKU / GTIN values) already seen on this item's tracked links; a candidate with the
    same id is the identical product (e.g. Platypus and Dr Martens both list Adrian SKU 14573601.RED)."""
    q = search_query(item)
    tracked = {_norm_url(u) for u in item.get("urls", [])}
    kind = item.get("kind")
    searcher = Searcher(fetcher)
    found = []
    for host, cfg in retailers.items():
        if not isinstance(cfg, dict) or not cfg.get("search") or cfg.get("blocked"):
            continue
        if kind and cfg.get("categories") and kind not in cfg["categories"]:
            continue
        urls = [u for u in searcher.store(host, cfg, q) if _norm_url(u) not in tracked]
        # Cheap pre-filter on the URL slug before reading pages.
        ranked = sorted(urls, key=lambda u: score(q, urlparse(u).path.replace("-", " ")), reverse=True)
        for url in ranked[:PER_STORE]:
            try:
                offer, retailer = fetcher.get_offer(url)
            except ScrapeError as e:
                log.info("candidate %s unreadable: %s", url, e)
                continue
            pid = (offer.product_id or "").partition(":")[2]
            exact = bool(pid) and pid in (known_ids or set())
            s = 1.0 if exact else score(q, offer.title or "")
            if s < MIN_SCORE or excluded_for(offer.genders, (profile or {}).get("gender")):
                continue
            found.append({"url": url, "store": retailer["name"], "title": offer.title, "price": offer.price,
                          "in_stock": offer.in_stock, "score": round(s, 2), "exact": exact})
    return sorted(found, key=lambda c: (not c["exact"], -c["score"], c["price"] or 1e9))


def known_ids(entry: dict | None) -> set[str]:
    """Product id values recorded for an item's tracked links (from their identity baselines)."""
    out = set()
    for o in (entry or {}).get("offers", {}).values():
        out |= set(((o.get("identity") or {}).get("ids") or {}).values())
    return out

"""Extract price, "was" price, stock and title from a product page.

Extraction chain (first hit wins): JSON-LD Product/Offer -> schema.org microdata -> meta tags,
with optional per-retailer CSS selector overrides layered on top.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict

from bs4 import BeautifulSoup

GENDER_PATTERNS = {
    # Order matters only for readability; "women" never matches the men pattern thanks to \b.
    "women": re.compile(r"(?i)\b(women'?s?|womens|woman|ladies|female|femme|for her|\(w\))(?![a-z])"),
    "men": re.compile(r"(?i)\b(men'?s?|mens|man|male|homme|for him|\(m\))(?![a-z])"),
    "kids": re.compile(r"(?i)\b(kids?'?s?|junior|youth|toddler|infant|baby|girls?'?|boys?'?|child(ren)?)(?![a-z])"),
    "unisex": re.compile(r"(?i)\bunisex\b"),
}
# Shopify pages state the currency actually shown to this visitor; themes often hard-code the store's home
# currency in their microdata (Orbitkey labels NZ$139 as "AUD"), so this wins when present.
SHOPIFY_ACTIVE_CURRENCY_RE = re.compile(r'Shopify\.currency\s*=\s*\{[^}]*"active"\s*:\s*"([A-Z]{3})"')
MONEY_RE = re.compile(r"(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")


@dataclass
class Offer:
    price: float | None = None
    was_price: float | None = None
    in_stock: bool | None = None
    title: str | None = None
    currency: str | None = None
    method: str | None = None  # which extractor found the price
    genders: list[str] | None = None  # e.g. ["men"], ["men", "women"] (unisex), ["kids"]; None = unknown
    product_id: str | None = None  # stable identity (Shopify id, GTIN, SKU) to notice a URL changing product
    final_url: str | None = None  # where the page actually ended up after redirects

    def to_dict(self) -> dict:
        return asdict(self)


def money(value) -> float | None:
    """Parse '$1,249.99', '249.99', 249 or 24999 (cents are NOT inferred) into a float."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = MONEY_RE.search(str(value).replace("\xa0", " "))
    return float(m.group(1).replace(",", "")) if m else None


def detect_gender(*texts) -> list[str] | None:
    """Which audiences a product is for, from its name, category and gender fields. None if nothing says."""
    found = set()
    for t in texts:
        if isinstance(t, dict):  # schema.org PeopleAudience
            t = " ".join(str(v) for v in t.values())
        if not t or not isinstance(t, str):
            continue
        found |= {g for g, pat in GENDER_PATTERNS.items() if pat.search(t)}
    if "unisex" in found:
        found |= {"men", "women"}
    found.discard("unisex")
    return sorted(found) or None


def _availability(value) -> bool | None:
    if value is None:
        return None
    v = str(value).lower()
    if any(s in v for s in ("outofstock", "out of stock", "soldout", "discontinued", "unavailable")):
        return False
    if any(s in v for s in ("instock", "in stock", "limitedavailability", "onlineonly", "preorder", "backorder")):
        return True
    return None


def _walk_jsonld(node):
    """Yield every dict in a JSON-LD tree (handles @graph and nested lists)."""
    if isinstance(node, list):
        for n in node:
            yield from _walk_jsonld(n)
    elif isinstance(node, dict):
        yield node
        for v in node.values():
            if isinstance(v, (list, dict)):
                yield from _walk_jsonld(v)


def _jsonld_id(node: dict) -> str | None:
    for key in ("productGroupID", "gtin13", "gtin", "gtin14", "gtin12", "gtin8", "sku", "productID", "mpn"):
        v = node.get(key)
        if isinstance(v, (str, int)) and str(v).strip():
            return f"{key}:{str(v).strip()}"
    return None


def _types(node: dict) -> set[str]:
    t = node.get("@type", [])
    return {x.lower() for x in (t if isinstance(t, list) else [t]) if isinstance(x, str)}


def from_jsonld(soup: BeautifulSoup) -> Offer | None:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        nodes = list(_walk_jsonld(data))
        # A ProductGroup (one Product per size/colour) is checked first, with all its variants' offers pooled.
        groups = [n for n in nodes if "productgroup" in _types(n)]
        for g in groups:
            variants = g.get("hasVariant") or []
            g["offers"] = [o for v in (variants if isinstance(variants, list) else [variants]) if isinstance(v, dict)
                           for o in (v.get("offers") if isinstance(v.get("offers"), list) else [v.get("offers")]) if o]
        for node in groups + nodes:
            if not _types(node) & {"product", "productgroup"}:
                continue
            offers = node.get("offers")
            offers = offers if isinstance(offers, list) else [offers] if offers else []
            cands: list[Offer] = []
            for o in offers:
                if not isinstance(o, dict):
                    continue
                is_range = False
                if "aggregateoffer" in _types(o):
                    price = money(o.get("lowPrice") or o.get("price"))
                else:
                    raw = o.get("price")
                    price = money(raw)
                    is_range = isinstance(raw, str) and bool(re.search(r"\d\s*[-–]\s*\$?\d", raw))
                    spec = o.get("priceSpecification")
                    if price is None and isinstance(spec, dict):
                        price = money(spec.get("price"))
                if price is None:
                    continue
                cand = Offer(
                    price=price,
                    in_stock=_availability(o.get("availability")),
                    title=node.get("name"),
                    currency=o.get("priceCurrency"),
                    method="json-ld-range" if is_range else "json-ld",
                )
                cands.append(cand)
            if cands:
                # Per-size/colour offers: cheapest in-stock one wins; in stock if any offer is.
                live = [c for c in cands if c.in_stock is not False] or cands
                best = min(live, key=lambda c: c.price)
                if any(c.in_stock for c in cands):
                    best.in_stock = True
                best.product_id = _jsonld_id(node)
                best.genders = detect_gender(node.get("name"), node.get("gender"), node.get("audience"),
                                             node.get("category") if isinstance(node.get("category"), str) else None)
                return best
    return None


def from_microdata(soup: BeautifulSoup) -> Offer | None:
    el = soup.find(attrs={"itemprop": "price"})
    if not el:
        return None
    price = money(el.get("content") or el.get_text(" ", strip=True))
    if price is None:
        return None
    cur = soup.find(attrs={"itemprop": "priceCurrency"})
    avail = soup.find(attrs={"itemprop": "availability"})
    name = soup.find(attrs={"itemprop": "name"})
    pid = None
    for key in ("gtin13", "gtin", "sku", "productID", "mpn"):
        el_id = soup.find(attrs={"itemprop": key})
        val = el_id and (el_id.get("content") or el_id.get_text(strip=True))
        if val:
            pid = f"{key}:{val}"
            break
    return Offer(
        product_id=pid,
        price=price,
        in_stock=_availability(avail and (avail.get("content") or avail.get("href"))),
        title=name and (name.get("content") or name.get_text(" ", strip=True)),
        currency=cur and cur.get("content"),
        method="microdata",
    )


def from_meta(soup: BeautifulSoup) -> Offer | None:
    def meta(*names):
        for n in names:
            el = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if el and el.get("content"):
                return el["content"]
        return None

    price = money(meta("product:price:amount", "og:price:amount"))
    if price is None:
        return None
    return Offer(
        price=price,
        in_stock=_availability(meta("product:availability", "og:availability")),
        title=meta("og:title"),
        currency=meta("product:price:currency", "og:price:currency"),
        method="meta",
    )


def _select_money(soup: BeautifulSoup, selector: str | None) -> float | None:
    if not selector:
        return None
    el = soup.select_one(selector)
    return money(el.get("content") or el.get_text(" ", strip=True)) if el else None


def parse_html(html: str, retailer: dict | None = None) -> Offer | None:
    retailer = retailer or {}
    soup = BeautifulSoup(html, "lxml")
    offer = None
    override = _select_money(soup, retailer.get("price_selector"))
    if override is not None:
        offer = Offer(price=override, method="selector")
    for extractor in (from_jsonld, from_microdata, from_meta):
        found = extractor(soup)
        if found:
            if offer is None:
                offer = found
            else:  # keep the selector price, borrow the other fields
                offer.in_stock = found.in_stock
                offer.title = found.title
                offer.currency = found.currency
            break
    if offer is None:
        return None
    was = _select_money(soup, retailer.get("was_price_selector"))
    if was and was > offer.price:
        offer.was_price = was
    if not offer.title:
        t = soup.find("meta", attrs={"property": "og:title"}) or soup.title
        offer.title = t.get("content") if t and t.name == "meta" else (t.get_text(strip=True) if t else None)
    if offer.genders is None:
        offer.genders = detect_gender(offer.title)
    if m := SHOPIFY_ACTIVE_CURRENCY_RE.search(html):
        offer.currency = m.group(1)
    return offer


def excluded_for(genders: list[str] | None, wanted: str | None) -> bool:
    """True if a product is known to be only for other audiences (unknown and unisex products are kept)."""
    return bool(wanted and genders and wanted not in genders)


def parse_shopify_js(data: dict) -> Offer:
    """Parse Shopify's public /products/<handle>.js JSON (prices are in cents)."""
    variants = data.get("variants") or []
    avail = [v for v in variants if v.get("available")] or variants
    v = min(avail, key=lambda x: x.get("price") or 0) if avail else {}
    price = (v.get("price") if v else data.get("price")) or 0
    was = v.get("compare_at_price") if v else data.get("compare_at_price")
    title = data.get("title")
    colour = next((t.split(":", 1)[1] for t in data.get("tags") or [] if "PRIMARYCOLOUR:" in t.upper()), None)
    if title and colour and colour.lower() not in title.lower():  # e.g. JD Sports titles every colourway "XT-6"
        title = f"{title} – {colour.title()}"
    return Offer(
        price=price / 100,
        was_price=(was / 100) if was and was > price else None,
        in_stock=bool(data.get("available")),
        title=title,
        currency=None,
        method="shopify",
        product_id=f"shopify:{data['id']}" if data.get("id") else None,
        # Only gender-ish tags (e.g. Birkenstock "gender:Mens"); other tags often name unrelated categories.
        genders=detect_gender(data.get("title"), data.get("type"),
                              *[t.split(":", 1)[-1] for t in data.get("tags") or [] if "gender" in t.lower()]),
    )

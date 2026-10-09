"""Fetch product pages: plain HTTP first, Shopify JSON where available, headless Chromium as a fallback."""
from __future__ import annotations

import random
import re
import time
from urllib.parse import parse_qsl, urlencode, urlparse

import httpx
import logging

from .parse import Offer, detect_gender, parse_html, parse_pricespy_offers, parse_shopify_js

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-NZ,en;q=0.9",
}
log = logging.getLogger("tracker")
BLOCK_STATUSES = {401, 403, 429, 503}
CURRENCY = "NZD"
# Shopify picks a "market" (currency, shipping country, availability) from the visitor's location. GitHub's
# runners are in the US, which made Orbitkey answer in AUD and Life Pharmacy report items as unavailable.
# These cookies pin every request to New Zealand; /cart.js then confirms which currency we actually got.
NZ_COOKIES = {"localization": "NZ", "cart_currency": CURRENCY}


PRODUCT_DATA_READY_JS = """() =>
    !!document.querySelector('[itemprop="price"]') ||
    [...document.querySelectorAll('script[type="application/ld+json"]')]
        .some(s => /"@type"\\s*:\\s*\\[?\\s*"Product(Group)?"/.test(s.textContent))"""


MAGENTO_QUERY = """query ($key: String) { products(filter: {url_key: {eq: $key}}) { items {
  sku name stock_status
  price_range { minimum_price { final_price { value currency } regular_price { value } } }
  ... on ConfigurableProduct { variants { attributes { code label } product {
    sku stock_status price_range { minimum_price { final_price { value currency } regular_price { value } } } } } }
} } }"""


class ScrapeError(Exception):
    pass


class Fetcher:
    def __init__(self, retailers: dict, polite_delay: tuple[float, float] = (2.0, 5.0)):
        self.retailers = retailers
        self.polite_delay = polite_delay
        self.client = httpx.Client(headers=HEADERS, cookies=NZ_COOKIES, follow_redirects=True, timeout=30)
        self._pw = None
        self._browser = None
        self._last_host: dict[str, float] = {}
        self._shopify_currency: dict[str, str | None] = {}
        # Hosts that rate-limited/blocked plain requests this run (GitHub's shared IPs get HTTP 429 from Shopify):
        # their remaining links go straight to the headless browser instead of waiting through retries.
        self._blocked_hosts: set[str] = set()

    # -- lifecycle -------------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.client.close()
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    def retailer_for(self, url: str) -> dict:
        host = urlparse(url).netloc.lower()
        cfg = self.retailers.get(host) or self.retailers.get(host.removeprefix("www.")) or {}
        return {"name": cfg.get("name") or host.removeprefix("www."), **cfg}

    # -- fetching --------------------------------------------------------
    def _throttle(self, host: str):
        last = self._last_host.get(host)
        if last is not None:
            wait = random.uniform(*self.polite_delay) - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_host[host] = time.time()

    def _shopify(self, url: str) -> Offer | None:
        p = urlparse(url)
        if "/products/" not in p.path or p.netloc in self._blocked_hosts:
            return None
        handle = p.path.split("/products/", 1)[1].strip("/").split("/")[0]
        currency = self._store_currency(p)  # settle the market first, so the price below is in this currency
        try:
            # country=NZ selects NZD pricing on multi-market stores; GitHub's runners are in the US.
            r = self.client.get(f"{p.scheme}://{p.netloc}/products/{handle}.js", params={"country": "NZ"})
            if r.status_code != 200:
                log.info("shopify json %s -> HTTP %s; falling back", handle, r.status_code)
                if r.status_code in BLOCK_STATUSES:
                    self._blocked_hosts.add(p.netloc)
                return None
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.info("shopify json %s failed: %r; falling back to the page", handle, e)
            return None
        if not (isinstance(data, dict) and "variants" in data):
            return None
        offer = parse_shopify_js(data)
        offer.currency = currency
        return offer

    def _cart_currency(self, base: str) -> str | None:
        try:
            return (self.client.get(f"{base}/cart.js").json().get("currency") or "").upper() or None
        except (httpx.HTTPError, ValueError, AttributeError):
            return None

    def _magento(self, url: str, store_code: str | None) -> Offer | None:
        """Magento stores (Dr Martens NZ) answer a public GraphQL query with price, RRP and stock per size."""
        p = urlparse(url)
        key = p.path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".html")
        try:
            # Without a Store header the API answers for the store's default view (Dr Martens: Australia, AUD).
            r = self.client.post(f"{p.scheme}://{p.netloc}/graphql", headers={"Store": store_code} if store_code else {},
                                 json={"query": MAGENTO_QUERY, "variables": {"key": key}})
            items = [i for i in (r.json().get("data") or {}).get("products", {}).get("items") or [] if i]
        except (httpx.HTTPError, ValueError, AttributeError) as e:
            log.info("magento graphql %s failed: %r; falling back to the page", key, e)
            return None
        if not items:
            log.info("magento graphql %s -> HTTP %s, no product; falling back to the page", key, r.status_code)
            return None
        it = items[0]

        def prices(node):
            mp = node["price_range"]["minimum_price"]
            return mp["final_price"]["value"], mp["regular_price"]["value"], mp["final_price"].get("currency")

        sizes = []
        for v in it.get("variants") or []:
            size = next((a["label"] for a in v["attributes"] if "size" in a["code"].lower()), None)
            if size is None:
                continue
            final, regular, _ = prices(v["product"])
            sizes.append({"size": size, "width": None, "in_stock": v["product"]["stock_status"] == "IN_STOCK",
                          "price": final, "was": regular if regular and regular > final else None})
        final, regular, currency = prices(it)
        live = [s for s in sizes if s["in_stock"]]
        return Offer(price=min((s["price"] for s in live), default=final), was_price=regular if regular > final else None,
                     in_stock=bool(live) if sizes else it["stock_status"] == "IN_STOCK", title=it["name"],
                     currency=currency, method="magento", product_id=f"sku:{it['sku']}",
                     sizes=sorted(sizes, key=lambda s: float(s["size"]) if s["size"].replace(".", "").isdigit() else 0) or None)

    def _store_currency(self, p) -> str | None:
        """Currency a Shopify store is pricing this session in (from /cart.js), cached per host.

        If the store ignored our NZ cookies (seen from GitHub's US runners), ask it to switch country the way its
        own country picker does — POST /localization — and check again.
        """
        if p.netloc not in self._shopify_currency:
            base = f"{p.scheme}://{p.netloc}"
            cur = self._cart_currency(base)
            if cur and cur != CURRENCY:
                log.info("%s priced in %s; requesting NZ via /localization", p.netloc, cur)
                try:
                    self.client.post(f"{base}/localization", data={
                        "form_type": "localization", "utf8": "✓", "_method": "put",
                        "country_code": "NZ", "return_to": "/"})
                except httpx.HTTPError as e:
                    log.info("/localization failed: %r", e)
                cur = self._cart_currency(base)
                log.info("%s now priced in %s", p.netloc, cur)
            self._shopify_currency[p.netloc] = cur
        return self._shopify_currency[p.netloc]

    @staticmethod
    def _nz_page_url(url: str) -> str:
        """Shopify product pages honour ?country=NZ even when the visitor's IP says otherwise."""
        p = urlparse(url)
        if "/products/" not in p.path:
            return url
        query = dict(parse_qsl(p.query))
        query.setdefault("country", "NZ")
        return p._replace(query=urlencode(query)).geturl()

    def _page_offer(self, url: str, retailer: dict) -> Offer | None:
        try:
            r = self.client.get(self._nz_page_url(url))
            return parse_html(r.text, retailer) if r.status_code == 200 else None
        except httpx.HTTPError:
            return None

    def _browser_html(self, url: str) -> str:
        if self._browser is None:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                args=["--disable-blink-features=AutomationControlled"]
            )
        ctx = self._browser.new_context(user_agent=UA, locale="en-NZ", viewport={"width": 1366, "height": 900})
        ctx.add_cookies([{"name": k, "value": v, "url": url} for k, v in NZ_COOKIES.items()])
        try:
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            try:
                # Product data is usually server-rendered: stop as soon as it's there instead of waiting for
                # the network to go quiet (which took ~15s per page on analytics-heavy sites).
                # Wait for *product* data specifically: some sites render an Organization block first and
                # inject the Product one later.
                page.wait_for_function(PRODUCT_DATA_READY_JS, timeout=8_000)
            except Exception:
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass
            return page.content()
        finally:
            ctx.close()

    def recheck(self, url: str, wait: tuple[float, float] = (20, 40)) -> Offer | None:
        """Read a URL again a little later with a brand-new session (fresh cookies, no cached currency)."""
        time.sleep(random.uniform(*wait))
        old_client, old_cur = self.client, self._shopify_currency
        self.client = httpx.Client(headers=HEADERS, cookies=NZ_COOKIES, follow_redirects=True, timeout=30)
        self._shopify_currency = {}
        try:
            return self.get_offer(url)[0]
        except ScrapeError:
            return None
        finally:
            self.client.close()
            self.client, self._shopify_currency = old_client, old_cur

    def get_offer(self, url: str) -> tuple[Offer, dict]:
        offer, retailer = self._get_offer(url)
        if offer.currency and offer.currency.upper() != CURRENCY:
            # Never record a price in the wrong currency: it would look like a price change.
            raise ScrapeError(f"{retailer['name']} answered in {offer.currency.upper()}, not {CURRENCY}; "
                              f"reading discarded")
        if offer.genders is None:  # fall back to the URL slug, e.g. JD's ".../xt6-womens-120525865"
            offer.genders = detect_gender(re.sub(r"[-_/]+", " ", urlparse(url).path))
        return offer, retailer

    def _get_offer(self, url: str) -> tuple[Offer, dict]:
        retailer = self.retailer_for(url)
        if retailer.get("blocked"):
            raise ScrapeError(f"{retailer['name']} is not supported: {retailer['blocked']}")
        self._throttle(urlparse(url).netloc)

        if retailer.get("platform") == "magento" and (offer := self._magento(url, retailer.get("magento_store"))):
            return offer, retailer

        offer = self._shopify(url)
        if offer:
            if offer.in_stock is False:
                # Shopify's JSON can say "unavailable" just because it thinks we're shipping overseas.
                # Only believe it if the product page agrees.
                page = self._page_offer(url, retailer)
                if page and page.in_stock:
                    offer.in_stock = True
            return offer, retailer

        html, status, final_url = None, None, url
        host = urlparse(url).netloc
        page_url = self._nz_page_url(url)
        if not retailer.get("js") and host not in self._blocked_hosts:
            try:
                r = self.client.get(page_url)
                if r.status_code in BLOCK_STATUSES:  # often a transient rate limit: wait and retry once
                    time.sleep(random.uniform(8, 15))
                    r = self.client.get(page_url)
                status = r.status_code
                if r.status_code == 404:
                    raise ScrapeError("Product page not found (404) — the URL may have changed")
                if r.status_code not in BLOCK_STATUSES:
                    html, final_url = r.text, str(r.url)
                else:
                    self._blocked_hosts.add(host)
            except httpx.HTTPError as e:
                status = str(e)
            if html is None:
                log.info("page %s -> %s; trying the headless browser", url, status)
        if html:
            offer = parse_html(html, retailer)
            if offer and retailer.get("platform") == "pricespy":
                offer.shop_offers = parse_pricespy_offers(html)
            if offer and offer.currency and offer.currency.upper() != CURRENCY:
                log.info("page %s priced in %s; retrying in the browser with NZ cookies", url, offer.currency)
                offer = None
            if offer:
                offer.final_url = final_url
                return offer, retailer

        # Fall back to a real (headless) browser for JS-rendered or bot-protected pages.
        try:
            html = self._browser_html(page_url)
        except Exception as e:
            raise ScrapeError(f"Browser fetch failed ({status=}): {e}") from e
        offer = parse_html(html, retailer)
        if not offer:
            # Some sites (e.g. Dr Martens) occasionally serve a 200 page with no product data, likely a bot check.
            time.sleep(random.uniform(5, 10))
            try:
                offer = parse_html(self._browser_html(page_url), retailer)
            except Exception:
                offer = None
            log.info("no product data in browser page %s; retried once: %s", url, "ok" if offer else "still none")
        if offer:
            offer.final_url = url
        if not offer:
            raise ScrapeError(
                f"No price found on page (http status {status}). The site may block bots or need a "
                f"price_selector in retailers.yaml"
            )
        return offer, retailer

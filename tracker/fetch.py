"""Fetch product pages: plain HTTP first, Shopify JSON where available, headless Chromium as a fallback."""
from __future__ import annotations

import random
import re
import time
from urllib.parse import urlparse

import httpx
import logging

from .parse import Offer, detect_gender, parse_html, parse_shopify_js

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

    def _page_offer(self, url: str, retailer: dict) -> Offer | None:
        try:
            r = self.client.get(url)
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
        if not retailer.get("js") and host not in self._blocked_hosts:
            try:
                r = self.client.get(url)
                if r.status_code in BLOCK_STATUSES:  # often a transient rate limit: wait and retry once
                    time.sleep(random.uniform(8, 15))
                    r = self.client.get(url)
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
            if offer and offer.currency and offer.currency.upper() != CURRENCY:
                log.info("page %s priced in %s; retrying in the browser with NZ cookies", url, offer.currency)
                offer = None
            if offer:
                offer.final_url = final_url
                return offer, retailer

        # Fall back to a real (headless) browser for JS-rendered or bot-protected pages.
        try:
            html = self._browser_html(url)
        except Exception as e:
            raise ScrapeError(f"Browser fetch failed ({status=}): {e}") from e
        offer = parse_html(html, retailer)
        if offer:
            offer.final_url = url
        if not offer:
            raise ScrapeError(
                f"No price found on page (http status {status}). The site may block bots or need a "
                f"price_selector in retailers.yaml"
            )
        return offer, retailer

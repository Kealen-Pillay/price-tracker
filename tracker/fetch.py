"""Fetch product pages: plain HTTP first, Shopify JSON where available, headless Chromium as a fallback."""
from __future__ import annotations

import random
import time
from urllib.parse import urlparse

import httpx

from .parse import Offer, parse_html, parse_shopify_js

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-NZ,en;q=0.9",
}
BLOCK_STATUSES = {401, 403, 429, 503}


class ScrapeError(Exception):
    pass


class Fetcher:
    def __init__(self, retailers: dict, polite_delay: tuple[float, float] = (2.0, 5.0)):
        self.retailers = retailers
        self.polite_delay = polite_delay
        self.client = httpx.Client(headers=HEADERS, follow_redirects=True, timeout=30, http2=False)
        self._pw = None
        self._browser = None
        self._last_host: dict[str, float] = {}
        self._warmed: set[str] = set()

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
        if "/products/" not in p.path:
            return None
        handle = p.path.split("/products/", 1)[1].strip("/").split("/")[0]
        try:
            # country=NZ selects NZD pricing on multi-market stores; GitHub's runners are in the US.
            r = self.client.get(f"{p.scheme}://{p.netloc}/products/{handle}.js", params={"country": "NZ"})
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and "variants" in data:
                    return parse_shopify_js(data)
        except (httpx.HTTPError, ValueError):
            pass
        return None

    def _browser_html(self, url: str) -> str:
        if self._browser is None:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                args=["--disable-blink-features=AutomationControlled"]
            )
        ctx = self._browser.new_context(user_agent=UA, locale="en-NZ", viewport={"width": 1366, "height": 900})
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

    def get_offer(self, url: str) -> tuple[Offer, dict]:
        retailer = self.retailer_for(url)
        if retailer.get("blocked"):
            raise ScrapeError(f"{retailer['name']} is not supported: {retailer['blocked']}")
        host = urlparse(url).netloc
        self._throttle(host)
        if retailer.get("warmup") and host not in self._warmed:
            # Some bot filters reject a cold first request; visiting the homepage first sets their cookies.
            self._warmed.add(host)
            try:
                self.client.get(f"https://{host}/")
                time.sleep(random.uniform(2, 4))
            except httpx.HTTPError:
                pass

        offer = self._shopify(url)
        if offer:
            return offer, retailer

        html, status = None, None
        if not retailer.get("js"):
            try:
                r = self.client.get(url)
                if r.status_code in BLOCK_STATUSES:  # often a transient rate limit: wait and retry once
                    time.sleep(random.uniform(8, 15))
                    r = self.client.get(url)
                status = r.status_code
                if r.status_code == 404:
                    raise ScrapeError("Product page not found (404) — the URL may have changed")
                if r.status_code not in BLOCK_STATUSES:
                    html = r.text
            except httpx.HTTPError as e:
                status = str(e)
        if html:
            offer = parse_html(html, retailer)
            if offer:
                return offer, retailer

        # Fall back to a real (headless) browser for JS-rendered or bot-protected pages.
        try:
            html = self._browser_html(url)
        except Exception as e:
            raise ScrapeError(f"Browser fetch failed ({status=}): {e}") from e
        offer = parse_html(html, retailer)
        if not offer:
            raise ScrapeError(
                f"No price found on page (http status {status}). The site may block bots or need a "
                f"price_selector in retailers.yaml"
            )
        return offer, retailer

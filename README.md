# Wishlist Price Tracker (NZ)

Tracks prices for your wishlist across NZ retailers 4× a day, keeps price history, and messages you on
Telegram when something hits your target price, drops meaningfully, reaches a new low, or comes back in stock.
A weekly digest lists upcoming sale events (Black Friday, Boxing Day, Father's Day…). Everything runs free on
GitHub Actions + GitHub Pages — no paid APIs.

## How prices are read

| Source | Method | Notes |
|---|---|---|
| Chemist Warehouse NZ | schema.org microdata in page HTML | also reads the "Why pay $X" RRP |
| PriceSpy NZ | page data (`offerRows`) | every shop's offer; only trusted shops count |
| Bargain Chemist, Life Pharmacy, JB Hi-Fi, Birkenstock, JD Sports, Orbitkey (any Shopify store) | public `/products/<handle>.js?country=NZ` JSON | exact variant price + compare-at price, in NZD |
| Dr Martens NZ | Magento GraphQL (`Store: nz`) | price, RRP and stock per UK size |
| PB Tech | JSON-LD (often via headless browser) | |
| MECCA | JSON-LD via headless Chromium | use the **size-specific** URL (ends in `I-xxxxxx`) |
| Any other store | JSON-LD → microdata → meta tags, browser fallback | add CSS selectors in `retailers.yaml` if needed |
| **Farmers** | ❌ blocked | Akamai firewall rejects all automated traffic. Add Farmers' PriceSpy listing instead. |

Test any product URL before adding it:

```bash
python -m tracker probe "https://www.chemistwarehouse.co.nz/buy/93191/christian-dior-sauvage-eau-de-parfum-100ml"
```

## Managing the wishlist

Three ways, all editing `wishlist.yaml`:

1. **Dashboard** (GitHub Pages) — *+ Add item*, edit target, add store, remove.
2. **Telegram** — `/add <url> [target]`, `/link <item-id> <url>`, `/target <item-id> <price>`, `/remove <item-id>`, `/list`
   (processed at the next scheduled run).
3. **By hand** — edit `wishlist.yaml` and push.

### Your profile: gender and sizes

The `profile:` block at the top of `wishlist.yaml`:

```yaml
profile:
  gender: men                                  # skip products detected as women's/kids' (unisex & unlabelled kept)
  shoe_size: {system: US, min: 10, max: 11.5}  # US men's 10, 10.5, 11, 11.5
  brand_sizes:                                 # optional per-brand overrides of the built-in conversion
    Dr Martens: {UK: [9, 10]}
    Birkenstock: {EU: [43, 44], width: Regular}
    Salomon: {UK: [9.5, 11]}
```

For items marked `kind: shoes` (add `brand:` so the right size chart is used), **stock and price come only from
your sizes**: an item is "in stock" only if one of your sizes is, the price is the cheapest of your sizes, and
alerts say which of your sizes are available ("✅ Your size is back in stock", "⏳ Only US 11 left"). Without a
`brand_sizes` entry, the US range is converted with the brand's chart (Dr Martens, Birkenstock, Salomon, or a
generic one); a size the brand doesn't make maps to both neighbouring sizes. Telegram `/add` marks an item as
shoes automatically when the store lists numeric sizes.

Where sizes come from: Shopify size/width options (Birkenstock EU, JD Sports US), JSON-LD product-group variants
(JD's page fallback), and Dr Martens' Magento GraphQL API (UK sizes, stock per size, RRP). If a store's sizes
can't be read, the dashboard says so and any-size stock is used.

## Setup (one-off, ~10 minutes)

1. **Create a GitHub repo and push this folder**
   ```bash
   git init && git add . && git commit -m "Initial commit"
   gh repo create price-tracker --public --source . --push
   ```
   Public repos get unlimited Actions minutes and free Pages. (Private works too, but Pages on a private repo
   needs a paid plan — use `python -m tracker serve` locally instead.)

2. **Enable Pages**: repo *Settings → Pages → Source: GitHub Actions*
   (or `gh api -X POST repos/<you>/price-tracker/pages -f build_type=workflow`).

3. **Create the Telegram bot**
   - Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
   - Send any message to your new bot, then open
     `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `message.chat.id`.
   - Save both as repo secrets:
     ```bash
     gh secret set TELEGRAM_BOT_TOKEN
     gh secret set TELEGRAM_CHAT_ID
     ```

4. **Run it**: *Actions → Check prices → Run workflow* (or `gh workflow run check.yml`).
   The dashboard appears at `https://<you>.github.io/price-tracker/`.

5. **Connect the dashboard for editing**: create a
   [fine-grained token](https://github.com/settings/personal-access-tokens/new) for **only this repo** with
   *Contents: Read and write*, then paste it in the dashboard's ⚙︎ settings. It's stored only in your browser.

## Local use

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/playwright install chromium
.venv/bin/python -m tracker check --dry-run   # scrape and print, save nothing
.venv/bin/python -m tracker digest --dry-run  # preview the weekly digest
.venv/bin/python -m tracker serve             # dashboard at http://localhost:8000
.venv/bin/python -m pytest -q
```

## Trusted shops and deal quality

- **Trusted shops only.** PriceSpy lists every seller, including grey importers with no reviews. Each shop offer
  on a PriceSpy page is read, and only offers from `profile.trusted_shops` (new condition) count: the dashboard
  shows "PriceSpy → PB Tech". If only untrusted sellers list an item, that's shown but never priced or alerted.
- **Deal score.** Each item's best trusted, in-stock price is rated against its 90-day typical price (median of
  daily lows, once there are 7 days of history), the store's "was" price, and the next-cheapest store:
  🔥 great (≥15% below typical), good (≥7% below), typical, or above typical. A "was" price only counts as a real
  discount once the item has actually been seen selling at that price. You get a 🔥 alert when an item first
  becomes a great deal; the dashboard and weekly digest show the rating and why.

## Accuracy safeguards

- **NZ location pinned.** Every request sends Shopify's `localization=NZ` / `cart_currency=NZD` cookies, because
  GitHub's runners are in the US and some stores otherwise answer in AUD or mark items unavailable. Each Shopify
  store's currency is confirmed via `/cart.js`, and any reading not in NZD is discarded, never recorded.
- **"Unavailable" is double-checked** against the product page before an item is treated as sold out.
- **Big changes need confirmation.** A move of ≥15% or a stock flip is re-read with a fresh session ~30s later;
  if that doesn't agree, it's held as *unconfirmed* (shown on the dashboard) until the next run sees it again.
  One-off glitches never reach the history or Telegram.
- **Product identity.** The first reading of each link records its product id (Shopify id / GTIN / SKU) and
  title. If the id changes, the name changes completely, or the link starts redirecting elsewhere, you get a
  🔀 Telegram warning and a note on the dashboard.
- **Cleaning history.** `python -m tracker history show <item-id>` lists points;
  `python -m tracker history drop <item-id|all> [--url …] [--at …] [--price …] [--out-of-stock]` previews
  removals, and `--apply` performs them (current values are re-synced to the last good point).

## Tuning

- Alert thresholds: `MIN_DROP_PCT` and `FAILURES_BEFORE_WARNING` in `tracker/alerts.py`; confirmation and
  identity thresholds in `tracker/verify.py`.
- Check frequency: the cron lines in `.github/workflows/check.yml`.
- Sale events: `sales_calendar.yaml` (approximate — retailers move dates each year).

## Limitations

- Sites change their markup or add bot protection; if a store fails 3 checks in a row you get a Telegram warning.
  Re-save the fixture in `tests/fixtures/` and adjust `retailers.yaml`/`tracker/parse.py`.
- Future sales aren't published by retailers — the calendar is a best-effort guide, and price history tells you
  whether a "sale" is actually a good price.
- Keep checks low-volume (the default is one request per product, 4× a day). Retailer terms often discourage
  scraping; this is meant for personal use.

# Wishlist Price Tracker (NZ)

Tracks prices for your wishlist across NZ retailers 4× a day, keeps price history, and messages you on
Telegram when something hits your target price, drops meaningfully, reaches a new low, or comes back in stock.
A weekly digest lists upcoming sale events (Black Friday, Boxing Day, Father's Day…). Everything runs free on
GitHub Actions + GitHub Pages — no paid APIs.

## How prices are read

| Source | Method | Notes |
|---|---|---|
| Chemist Warehouse NZ | schema.org microdata in page HTML | also reads the "Why pay $X" RRP |
| PriceSpy NZ | JSON-LD `AggregateOffer` | lowest price across the NZ shops PriceSpy lists |
| Bargain Chemist, Life Pharmacy, JB Hi-Fi, Birkenstock, JD Sports, Orbitkey (any Shopify store) | public `/products/<handle>.js?country=NZ` JSON | exact variant price + compare-at price, in NZD |
| Dr Martens NZ, PB Tech | JSON-LD | |
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

### Men's only

`only_for: men` at the top of `wishlist.yaml` skips any product detected as women's or kids' — from the product
name, category, gender tags/fields, or the URL (e.g. `…/xt6-womens-…`). Unisex and unlabelled products are kept.
Skipped links show the reason on the dashboard and never alert; Telegram `/add` refuses them. Remove the line to
turn the filter off.

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

## Tuning

- Alert thresholds: `MIN_DROP_PCT` and `FAILURES_BEFORE_WARNING` in `tracker/alerts.py`.
- Check frequency: the cron lines in `.github/workflows/check.yml`.
- Sale events: `sales_calendar.yaml` (approximate — retailers move dates each year).

## Limitations

- Sites change their markup or add bot protection; if a store fails 3 checks in a row you get a Telegram warning.
  Re-save the fixture in `tests/fixtures/` and adjust `retailers.yaml`/`tracker/parse.py`.
- Future sales aren't published by retailers — the calendar is a best-effort guide, and price history tells you
  whether a "sale" is actually a good price.
- Keep checks low-volume (the default is one request per product, 4× a day). Retailer terms often discourage
  scraping; this is meant for personal use.

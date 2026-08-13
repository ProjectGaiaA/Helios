# Project Helios — Solar/Home-Energy Price Tracker

## What This Is
Solar and home-energy price comparison skeleton (Phase A). Scrapes Shopify
retailers politely, appends JSONL price history, builds a static site with
$/Wh comparisons. Not deployed; no domain yet. Ported from the plant price
tracker (project_gaia) — its scraper defect history is baked into the
comments here as constraints.

## Health Check Protocol

**Run this at the start of every session before doing anything else.**

1. Read `data/last_manifest.json` — check:
   - `pipeline_status`: should be `"healthy"`. If `"degraded"`, report which retailers and why.
   - `degraded_retailers`: should be empty. If not, list them with context.
   - `timestamp`: skeleton phase has no schedule, so just report how old it is.
2. Check price freshness — read the last line of each JSONL in `data/prices/`,
   report the newest timestamp per product.
3. If ANY issues found, report them **before** doing anything else the user asked for:

```
HEALTH CHECK:
- [OK/WARN/FAIL] Pipeline status: healthy|degraded
- [OK/WARN/FAIL] Last scrape: <timestamp> (<N> hours ago)
- [OK/WARN/FAIL] Price freshness: <details>
- [OK/WARN/FAIL] Degraded retailers: none|<list>
```

## Key Commands

```bash
# Always -X utf8 on Windows — the data contains non-ASCII retailer text
python -X utf8 -m scrapers.runner --dry-run
python -X utf8 -m scrapers.runner --retailer shop-solar-kits --skip-promos
python -X utf8 -m scrapers.runner --products ecoflow-river-2-pro --retailer shop-solar-kits --skip-promos
python -X utf8 build.py
python -X utf8 -m pytest
python -X utf8 -m ruff check .
```

## Architecture

- `build.py` — Minimal static site generator. Loads products.json + prices/
  JSONL, renders Jinja2 templates to site/. Owns variant classification
  (unit vs bundle) and the $/Wh rules.
- `scrapers/runner.py` — Orchestrator. Runs each retailer scraper, writes
  JSONL (LF-only, `newline="\n"`), saves data/last_manifest.json. Enforces
  `active` on the product catalog. Exit 1 = unknown retailer; exit 2 = a
  retailer at zero products two runs straight (dead retailer alarm).
- `scrapers/shopify.py` — Shopify JSON scraper. `.json` for prices (dollar
  strings), `.js` for per-variant availability (its prices are CENTS —
  never read them). NO HTML fallback, deliberately (PLAN section 5).
- `scrapers/common.py` — FetchResult, extract_handle_from_url, log-only
  recovery stubs (full recovery system is Phase B).
- `scrapers/polite.py` — Bot UA, browser UA rotation, robots.txt (fail-open
  with warning), 5-15s delays.
- `data/products.json` — Product catalog (LIST). `"active": false` = skipped
  by BOTH runner and build. `specs.capacity_wh` may be null — that means
  "withhold $/Wh", not "fill it in".
- `data/retailers.json` — Retailer configs. `affiliate` may be null;
  inactive entries carry `inactive_reason`.
- `data/handle_maps.json` — {retailer_id: {product_id: shopify_handle}}.
  Seeded from live discovery 2026-08-13; missing file = empty mapping.
- `data/prices/*.jsonl` — Append-only price history, one file per product,
  LF-only (enforced by .gitattributes AND the writer).
- `templates/` — base, home, product (inline CSS, no external assets).
- `site/` — Generated output, committed (gaia convention).
- `.github/workflows/scrape.yml` — workflow_dispatch ONLY. No cron until
  the project deploys.

## Key Decisions

- **Checkout is for humans.** Both active retailers' robots.txt say so
  explicitly. Helios links out; it never automates checkout, carts,
  payment, or order placement. This is a standing constraint, not a
  preference (PLAN section 0).
- **$/Wh withhold discipline (PLAN 2b).** $/Wh renders ONLY for a variant
  classified `unit` on a product with non-null `capacity_wh`. Bundles get
  a badge and a price, never a $/Wh — a kit price over a battery capacity
  is wrong by construction (real DELTA Max data, C15). Unknown classifies
  as bundle. When in doubt, show nothing: a missing number is a gap,
  a wrong number is a defect.
- **Multi-pack variants are kept.** The plant tracker's pack-skip regex is
  deleted; "2-Pack" is a legitimate solar product.
- **Stock truth comes from `.js` only.** A missing `available` is unknown
  (renders "Check site"), never coerced to in-stock.
- **Anomalies are recorded, not discarded.** A >50% move is flagged on the
  row (`price_anomaly`) and written; blocking writes freezes defects in.
- **Slug collisions suffix with variant_id** instead of last-write-wins.
- **`no_sizes_readable`** key name is kept (not renamed to variants) because
  runner.py's hit-rate health consumes it under that name.
- Affiliate links render `rel="nofollow sponsored"`; `link_template` stays
  empty until programs are actually joined.

## Verification

Run `python -X utf8 -m ruff check .` and `python -X utf8 -m pytest` before
calling any change done. A green suite is context, not proof — exercise the
changed path against the fixtures or a limited live scrape (2-3 products,
one retailer, 5-15s delays) and read the output.

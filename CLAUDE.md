# Project Helios — Solar/Home-Energy Price Tracker

## What This Is
Solar and home-energy price comparison site. Scrapes Shopify retailers
politely, appends JSONL price history, builds a static site with $/Wh
comparisons. Deployed: Vercel project "helios" auto-deploys main →
https://helios-projectgaiaas-projects.vercel.app/ (no custom domain yet).
Ported from the plant price tracker (project_gaia) — its scraper defect
history is baked into the comments here as constraints.

## Health Check Protocol

**Run this at the start of every session before doing anything else.**

1. **Heartbeat first**: `python -X utf8 heartbeat.py --no-api`. One command,
   one question — has the pipeline published inside the 30h window? Exit 0
   healthy, 1 alarm, and it prints the age of the last commit touching
   `data/prices/`, the manifest's own timestamp, and whether the two agree.
   Locally use `--no-api` (there is no `GITHUB_TOKEN`, so the Scrape-run
   check cannot be made and downgrades to a warning); CI runs it without
   that flag. **Read its verdict before the manifest**: the manifest is the
   pipeline's account of itself and can be fresh while nothing was
   published. Ask this one first because a dead pipeline makes every answer
   below stale rather than wrong, which is harder to notice.
2. Read `data/last_manifest.json` — check:
   - `pipeline_status`: should be `"healthy"`. If `"degraded"`, report which retailers and why.
   - `degraded_retailers`: should be empty. If not, list them with context.
   - `timestamp`: the scrape runs 11:00 and 21:30 UTC, so anything past ~24h
     is one missed cycle and past 30h is the heartbeat's alarm window.
3. Check price freshness — read the last line of each JSONL in `data/prices/`,
   report the newest timestamp per product.
4. If ANY issues found, report them **before** doing anything else the user asked for:

```
HEALTH CHECK:
- [OK/WARN/FAIL] Heartbeat: exit <0|1> (<findings, e.g. DATA_STALE 41.5h>)
- [OK/WARN/FAIL] Pipeline status: healthy|degraded
- [OK/WARN/FAIL] Last scrape: <timestamp> (<N> hours ago)
- [OK/WARN/FAIL] Price freshness: <details>
- [OK/WARN/FAIL] Degraded retailers: none|<list>
```

The same check runs unattended twice a day in
`.github/workflows/heartbeat.yml` (13:00 and 23:30 UTC, offset 2h after each
scrape). It fails the job with `::error::` annotations, which is how GitHub
tells Brandon; there are no alert secrets to configure, and no SMTP —
see the workflow's comments for the reasoning and for how to add Slack or
email later. Windows and failure codes are documented in
`docs/HEARTBEAT_LOG.md`.

**Cron status (2026-08-14):** `scrape.yml` carries the 2x-daily cron
(11:00 / 21:30 UTC) plus workflow_dispatch. One completed CI run exists — a
manual dispatch on 2026-08-14 that published `f295a13`; its job showed red
only because the audit step ran `--all` into the 25-request politeness
budget (fixed in `35c0df6`: scheduled runs sample, per PLAN 4c.2). The
first *scheduled* run is expected 2026-08-14 21:30 UTC. A red heartbeat
before the grace expiry (2026-08-15 13:00 UTC) is startup noise, not a
defect. See `docs/HEARTBEAT_LOG.md` §8.

## Key Commands

```bash
# Always -X utf8 on Windows — the data contains non-ASCII retailer text
python -X utf8 -m scrapers.runner --dry-run
python -X utf8 -m scrapers.runner --retailer shop-solar-kits --skip-promos
python -X utf8 -m scrapers.runner --products ecoflow-river-2-pro --retailer shop-solar-kits --skip-promos
python -X utf8 build.py
python -X utf8 audit.py            # sample of 10 triples
python -X utf8 audit.py --all      # every triple (still budget-capped)
python -X utf8 heartbeat.py --no-api          # is the pipeline still alive?
python -X utf8 heartbeat.py --no-api --now 2026-09-01T00:00:00Z   # time travel
python -X utf8 -m pytest
python -X utf8 -m ruff check .
```

## Audit verdict taxonomy (PLAN 4b/4c — the correctness loop)

`audit.py` compares each sampled (product, retailer, variant) triple on
TWO independent hops. Render hop: site HTML (via `data-*` provenance
attributes) vs the latest JSONL row. Freshness hop: that row vs the live
retailer `.json`+`.js` (UCP as a freshness source is pending the
live-payload parser fix; the agent profile itself is live).

- `RENDER_DEFECT` — site disagrees with its own store. The ONLY defect
  class: alarm + quarantine + exit 3.
- `STALE` — store disagrees with live. Expected (flash sales): notice +
  re-scrape recommendation. NEVER quarantines.
- `CLEAN`; non-verdicts `NO_ROW` (pair never scraped), `NO_BASELINE`
  (row predates the sku field), `UNRESOLVED` (variant absent live /
  fetch failed), `NOT_AUDITED` (budget exhausted).
- Exits: 0 clean, 3 render defect, 4 incomplete/unverified (never let
  an audit that could not verify read as success), 1 usage/config.
- Quarantined variants (`data/quarantine.json`, keyed
  `retailer:product:variant_id` — every part non-empty) are withheld from
  BOTH pages with `data-withheld="quarantine"` and always re-sampled. A
  recheck needs POSITIVE marker evidence on both surfaces plus a SHADOW
  REBUILD (entry suppressed) that renders correctly, plus clean
  freshness — only then does the entry clear. Leaks are RENDER_DEFECT;
  absence is UNRESOLVED; any not-clean recheck feeds the 5-audit TTL.
- SKU drift (stored vs live, both non-null) and capacity CONTRADICTED
  are alarms surfaced in the report, not taxonomy verdicts — the
  workflow's alarm step reads `alarms[]` out of data/audit_report.json
  and fails the run on any, even when the audit exit is 0.
- **data/quarantine.json and data/audit_report.json must be TRACKED from
  the first commit**: the workflow's rebuild-if-changed uses `git diff`,
  which silently no-ops on untracked files.

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
- `scrapers/ucp.py` — UCP/MCP catalog client (search/lookup/get_product
  ONLY — checkout tools are never wrapped). Fixture-tested; live
  lookup_catalog proven under the Helios-hosted agent profile (served
  from site/.well-known + vercel.json headers — never gaia's profile).
  Pipeline use pending the live-payload parser fix: the live response
  nests price differently than the published example. Money is integer
  minor units; compare in cents, never float ==.
- `audit.py` — the end-to-end correctness loop (see taxonomy above).
  Outputs data/audit_report.json + data/quarantine.json.
- `heartbeat.py` — the dead-man's switch. Every other guard runs as part of
  a scrape, so a scrape that never happens trips none of them; this asks
  whether the pipeline published at all. Reads git (age of the last commit
  touching `data/prices/`), the manifest (its own timestamp +
  `pipeline_status`, cross-examined against git — fresh manifest with no
  data commit is a half-run), and the Scrape workflow's latest run
  conclusion via the GitHub API. **Stdlib only and imports nothing from this
  repo** — a monitor that shares code or dependencies with the thing it
  watches inherits its failures. Exit 0/1/2.
- `build.py` staleness: rows older than 168h render withheld
  (`data-withheld="stale"`); every price shows "as of Nh/Nd ago"; the
  clock is injected (`build_site(now=...)`) — never sprinkle
  datetime.now() into view code.
- `data/products.json` — Product catalog (LIST). `"active": false` = skipped
  by BOTH runner and build. `specs.capacity_wh` may be null — that means
  "withhold $/Wh", not "fill it in".
- `data/retailers.json` — Retailer configs. `affiliate` may be null;
  inactive entries carry `inactive_reason`.
- `data/handle_maps.json` — {retailer_id: {product_id: shopify_handle}}.
  Seeded from live discovery 2026-08-13; missing file = empty mapping.
- `data/prices/*.jsonl` — Append-only price history, one file per product,
  LF-only (enforced by .gitattributes AND the writer).
- `templates/` — base, home, product, guide, article (+ `_macros`,
  `articles_index`), about, disclosure (inline CSS, no external assets).
- `site/` — Generated output, committed (gaia convention).
- `.github/workflows/scrape.yml` — cron 11:00 & 21:30 UTC plus
  workflow_dispatch. Scrapes all active retailers, tests, builds, runs the
  SAMPLED audit as the publish gate, commits site/ + data/. Alarms are the
  job's own failure annotations.

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

# Project Helios — Solar/Home-Energy Price Tracker: Repo Skeleton Plan (v2)

Working codename `project_helios`. Repo:
`C:\Users\BrandonHall\OneDrive - YA\Documents\CC\project_helios`.
v1 was red-teamed by an independent Opus agent on 2026-08-13: verdict UNSOUND,
4 CRITICAL / 7 MAJOR / 7 MINOR findings, all accepted. v2 incorporates every
correction; the register below is the corrected claim set. See CHANGELOG.

## 0. Goal of the skeleton (Phase A)

A **walking skeleton**: config → polite Shopify scrape → append-only JSONL
price history → static build with $/Wh — green tests, no plant remnants.
Not the launchable site (non-goals §5).

Standing constraint recorded from retailer robots.txt: both active seeds
explicitly state checkouts are for humans — Helios never automates checkout,
payment, or order placement; it links out only.

## 1. Claims register (corrected)

| # | Claim | Evidence |
|---|---|---|
| C1 | Shopify `.json` product endpoint has no availability; `.js` must be fetched separately (and returns price in cents) | `gaia/scrapers/shopify.py:1-20`; red team live-verified on shopsolarkits |
| C2 | shopify.py needs FOUR recovery symbols: `FetchResult` (module import, `:36`) **plus function-local** `record_broken`, `record_redirect_candidate`, `extract_handle_from_url` (`:200-204`, used `:212,:214,:237`) — the local import fails only at live-scrape time on 301/404 branches | red team, verified |
| C3 | runner.py port must delete, not just un-import: starkbros branch (`:416-463`), `validate_confirmed_candidates` (`:346-404`) + its unconditional call (`:689`), recovery-budget block (`:752-763`) | red team, verified |
| C4 | Price-row inner objects carry `variant_id` (143,220 of 180,958 corpus-wide) used for `?variant=` affiliate deep links (`shopify.py:394-409`); `retailer_name` is OPTIONAL (32 rows lack it); optional top-level `price_anomaly` exists (`runner.py:565-568`) | red team, verified |
| C5 | gaia CI: 2 crons + workflow_dispatch, 180-min timeout, concurrency guard | `.github/workflows/scrape.yml:3-28` |
| C6 | retailers.json schema beyond v1: optional `inactive_reason`, `trust_builder` (consumed `build.py:666`), top-level `notes`, `affiliate: null` allowed, `shipping` omitted on 6 entries | red team, verified |
| C7 | ~392 case-insensitive "plant" occurrences in `scrapers/` — rename-and-generalize port | red team recount |
| C8 | shopsolarkits, wildoaktrail, richsolar, altestore: open `/products.json`, robots-permitted (incl. `.js` endpoint); both active seeds' robots.txt re-verified directly by red team | probe 2026-08-13 + red team |
| C9 | Handles `ecoflow-delta-max`, `anker-solix-f2600`, `eg4-lifepower4-lithium-battery` live at shopsolarkits (HTTP 200 on `.json`, red team, 2026-08-13). DELTA Max variants ALL sold out today — good edge case, bad acceptance product | red team live check |
| C10 | runner.py promo detection: patterns `:58-75`, `_extract_promos_from_html` `:198-234`, hard-coded nursery `_SAMPLE_PRODUCT_PATHS` `:264-274`, writes `data/promos.json` (`:53,:339`) | red team, verified |
| C11 | polite.py exports needed: `USER_AGENTS, polite_delay, log_request, is_allowed_by_robots, make_polite_session, random_ua, polite_headers, BOT_USER_AGENT, _robots_cache` (last four imported by tests); plant strings at `:26,:29,:88,:91`; `BOT_USER_AGENT` advertises plantpricetracker.com/bot | red team, verified |
| C12 | Runtime data requirements: `handle_maps.json` `{retailer_id: {product_id: handle}}` opened with NO exists() guard (`shopify.py:1276-1295`); catalog must be a LIST of `{id,...}` (`runner.py:674`); `active` is NOT filtered by gaia's runner | red team, verified |
| C13 | Test infra requires: `tests/__init__.py`, `tests/conftest.py` (autouse robots stub whose patch list must match EXISTING modules), `tests/fixtures/`, `pyproject.toml` with `[tool.pytest.ini_options] pythonpath=["."] testpaths=["tests"]`, `responses==0.25.8` | red team, verified |
| C14 | shopify.py plant-shaped hazards for solar: pack-skip regex `:352` drops "N-Pack" variants; `_normalize_size` (`:1009-1135`) gallon logic + last-write-wins tier collision (`:395`); `no_sizes_readable` set only in HTML path (`:725`) but consumed by hit-rate health (`runner.py:524→:614-636`) | red team, verified |
| C15 | Bundle reality: shopsolarkits DELTA Max = 8 variants of ONE 2,016Wh battery differing by bundled panels ($1408–$1838) — naive per-variant $/Wh is wrong by construction | red team live check |
| C16 | Python 3.11 everywhere: CI pin, ruff `target-version="py311"`, local 3.11.9 | red team (closes v1-O4) |
| C17 | Probe result files contain summaries/examples only, NOT product listings — handle seeding must be live discovery; title-based discovery is ambiguous (5 distinct shopsolarkits products all titled "EcoFlow WAVE 2...") | red team, verified |

## 2. Skeleton contents (corrected)

```
project_helios/
  CLAUDE.md
  docs/PLAN.md
  requirements.txt           # requests, jinja2, pytest, ruff, responses==0.25.8 — pins deliberate
  pyproject.toml             # [tool.pytest.ini_options] pythonpath=["."] testpaths=["tests"] + ruff py311
  .gitignore
  .gitattributes             # *.jsonl text eol=lf  (Windows CRLF must never enter the price store)
  scrapers/
    __init__.py
    polite.py                # port; bot UA → "HeliosPriceBot/0.1 (+contact: brandon.william.hall@gmail.com)" [Brandon may swap for a domain later]
    common.py                # NEW: FetchResult dataclass; extract_handle_from_url (pure fn, copied);
                             # record_broken/record_redirect_candidate as LOG-ONLY stubs (recovery system is Phase B)
    shopify.py               # port with:
                             #  - all 4 recovery symbols from common (C2)
                             #  - HTML fallback (_scrape_product_html, ~:431-922) DELETED (FGT-specific);
                             #    no_sizes_readable=True set on JSON path when variants=={} (C14 health fix)
                             #  - pack-skip regex DELETED — multi-packs are legitimate solar variants (C14)
                             #  - _normalize_size → _normalize_variant: slug from variant title, NO
                             #    gallon logic; on slug collision append -{variant_id} instead of overwrite (C14)
                             #  - handle_maps.json read gets exists() guard (C12)
    runner.py                # port with C3 deletions; adds active-filter on catalog (products.json
                             #  `active` becomes ENFORCED, unlike gaia); promo detection kept but
                             #  _SAMPLE_PRODUCT_PATHS reseeded empty (homepage-only for skeleton)
  data/
    retailers.json           # §3; schema incl. optional fields per C6 (trust_builder kept, unused by minimal build)
    products.json            # §4; LIST of {id, name, brand, category, specs{capacity_wh, output_w,
                             #   chemistry, weight_lb}, active, notes} — capacity_wh nullable
    handle_maps.json         # seeded via live discovery (C17): disambiguate by vendor+SKU+variant
                             #   inspection from ONE paginated products.json pull per active retailer
    prices/                  # committed, like gaia; LF-only via .gitattributes
    promos.json              # written by runner (C10)
  templates/base.html, home.html, product.html
  build.py                   # NEW minimal (gaia's is 91KB and NOT ported): renders home + product pages;
                             #   affiliate deep links use variant_id (C4); treats retailer_name as optional
  site/                      # committed (gaia convention)
  tests/
    __init__.py
    conftest.py              # trimmed autouse robots stub patching ONLY {polite, shopify, runner} (C13)
    fixtures/                # authored FRESH from real solar products.json/.js responses (plant fixtures don't port, C13)
    test_polite.py           # adapted (bot UA assertion updated)
    test_shopify.py          # adapted: fixtures-based, covers variant_id passthrough, pack variants
                             #   KEPT, slug-collision suffixing, empty-variants → no_sizes_readable
    test_catalog.py          # schema validation incl. optional fields (C6), list-shape (C12)
    test_dollars_per_wh.py   # §2b rules incl. bundle-withhold and null-capacity-withhold
    test_build.py            # fixture build; edge: missing capacity, missing retailer_name, sold-out product
  .github/workflows/scrape.yml   # workflow_dispatch ONLY (no cron until deploy)
```

### 2b. Price-row schema and $/Wh rules (corrected per C4/C15)

Row: `{retailer_id, retailer_name?, timestamp, url, variants: {tier_key:
{price, was_price, available, raw_variant, variant_id}}, in_stock,
price_anomaly?}` — `?` = optional; consumers must not KeyError.

$/Wh discipline (withhold-when-unknown, same as gaia's availability ethic):
- computed ONLY when (a) product `specs.capacity_wh` is non-null AND (b) the
  variant is classified `unit` (not `bundle`).
- Variant classification (v2.1, extended per red team #2 — the v2 signal
  list was the hole): `bundle` if raw title matches
  `(kit|bundle|\+|&|\bw/|\bwith\b|\band\b|\d+\s*-?\s*packs?\b|expansion|`
  `extra\s+batter|spare\s+batter|panel(s)?\b.*\bx\b|x\s*\d+\s*w)`
  (case-insensitive) OR contains a second wattage token; else `unit`.
  Multi-pack = bundle (capacity multiplier unknown → withhold).
  Conservative: unknown → `bundle` (withhold). Rule is test-covered with
  real DELTA Max titles (C15) plus 7 adversarial titles from red team #2.
- Bundles render with price + "bundle" badge, no $/Wh — on product pages
  AND on home cells: a cell's price and $/Wh must describe the SAME
  variant (v2.1, red team #2 MAJOR-1).

## 3. Seeded retailers (unchanged from v1 except schema completeness per C6)

Active: shop-solar-kits, wild-oak-trail. Inactive: rich-solar, alte-store
(Phase B), ecoflow + bluetti (**O1 still open**: robots + endpoint check during
build; drop from seeds if closed), signature-solar (`custom scraper needed`).
Affiliate metadata from 2026-08-13 research.

## 4. Seeded products (corrected per C11/C17)

Verified-carriage seeds only: EcoFlow DELTA Max (sold-out edge case, C9),
Anker SOLIX F2600, EG4 LifePower4 (C9). The rest of the ~8 (RIVER 2 Pro,
DELTA 3, Bluetti AC180/AC200L, Rich Solar kit) are **provisional**: seeded
into products.json ONLY if live discovery (§2 handle_maps note) confirms
carriage at an active retailer; otherwise left out with a note in CHANGELOG.
Acceptance-scrape product: chosen at build time from confirmed IN-STOCK
products (C9: DELTA Max disqualified).

## 5. Explicit non-goals (Phase B+)

As v1: Signature Solar/BigCommerce scraper; WhichWatts oracle; Keepa; Wayback
backfill; alerts; deploy/domain; SEO content; audits/heartbeat; recovery
system (stubs only); verify.py; promo UI surfacing; taxonomy. Plus explicitly:
HTML-fallback scraping (deleted, not deferred — a future retailer needing it
gets its own scraper module); in-repo catalog discovery
(`ShopifyScraper.discover_products` deleted v2.1 — discovery is an offline,
operator-run step); per-scraper promo scanning
(`ShopifyScraper.scrape_promo_codes` deleted v2.1 — the runner's homepage
scan is the only promo surface).

## 6. Open checks

- **O1**: ecoflow/bluetti robots + products.json openness (during build).
- ~~O2 handle_maps/catalog shapes~~ → resolved as C12 (stated requirement).
- ~~O3 fixture portability~~ → resolved: fixtures authored fresh (C13).
- ~~O4 Python version~~ → resolved as C16.

## 7. Build order (corrected)

1. Scaffold: dirs, .gitignore, .gitattributes, pyproject, requirements,
   CLAUDE.md, git init (NO commit — Brandon commits).
2. Port polite.py; write common.py; port shopify.py per §2 spec; port
   runner.py per §2 spec.
3. Resolve O1. Live handle discovery: one paginated `/products.json` pull per
   ACTIVE retailer (polite delays), disambiguate per C17, write
   retailers/products/handle_maps seeds. Verify provisional products (§4).
4. Minimal build.py + templates.
5. Test infra (C13) + tests; `ruff check .` clean; `pytest` green.
6. Acceptance: `runner --dry-run`; then one polite live scrape limited to
   2–3 products at ONE retailer → JSONL rows appear (LF-only) → `build.py`
   renders them → hand-check one $/Wh against calculator; confirm bundle
   variants show no $/Wh.
7. Self-verification protocol phases 1–5 with pasted outputs.

## 8. Red-team protocol

Red team #1 (plan): done — see CHANGELOG. Red team #2 (build): independent
Opus, mechanically read-only, attacks the built skeleton against this plan and
the gaia reference; must run tests itself and verify the §7.6 artifacts.

## CHANGELOG

- v2.1 (2026-08-13): Red team #2 (independent, read-only) audited the built
  skeleton: plan-conformant and green, but FAIL on 3 MAJOR + 6 MINOR
  evidenced defects. All fixed same day. MAJOR-1: home cell paired the
  cheapest variant's price with a DIFFERENT variant's $/Wh (proven with
  captured wild-oak-trail data: $509 bundle price beside the $569 unit's
  $0.74/Wh) — rule is now same-variant-only, badge on cheapest-bundle
  cells, regression-tested. MAJOR-2: the v2 §2b classification regex
  treated absence of bundle signal as evidence of unit (7/13 adversarial
  titles misclassified, incl. "&", "w/", "and", N-Pack — the repo's own
  2-Pack fixture would have shown $1.20/Wh vs $0.60 truth); signals
  extended per §2b above, multi-pack now = bundle. MAJOR-3: workflow
  pushed `main` while the unborn local branch was `master` — branch
  renamed via symbolic-ref. MINOR: guaranteed-unique slug-collision
  suffixing (numeric fallback when variant ids are missing); build sort
  no longer TypeErrors on string prices; `--products ""` now exits 1
  instead of silently full-crawling; `* text=auto` added to .gitattributes
  and all writers (manifest, promos, handle_maps, site HTML) emit LF
  explicitly; dead code deleted (scrape_promo_codes — the only fetch path
  with no robots check — discover_products, discovery_delay; capabilities
  moved to §5 non-goals; save_handle_map_entry kept as the named Phase B
  surface); dry-run manifest summary no longer overwrites real totals
  with empties.
- v2 amendment (2026-08-13, build step 3): O1 resolved — us.ecoflow.com robots
  permits `/products.json` and the endpoint is open JSON; bluettipower.com
  robots also permits it but the endpoint returns 200 with a non-JSON body
  (effectively closed). Neither robots disallows, so both stay seeded
  inactive; neither dropped.
- v2 amendment (2026-08-13, build step 3): provisional-seed outcomes from live
  discovery — RIVER 2 Pro confirmed (both actives), Bluetti AC180 + AC200L
  confirmed (wild-oak-trail; AC180 shares one listing with AC180P, so its
  capacity_wh is null per 2b withhold). Plain "DELTA 3" NOT carried by either
  active (only DELTA 3 Plus/1000 Air/Max/Ultra — distinct models) — left out.
  "Rich Solar kit" too ambiguous to disambiguate (36/75 vendor matches, no
  single canonical kit) — left out.
- v2 (2026-08-13): Red team #1 (independent Opus) verdict on v1: UNSOUND.
  Accepted 18/18 findings. Corrections: C2 four recovery symbols (was: only
  FetchResult — would have passed tests and failed live); C4/2b variant_id
  kept for deep links, retailer_name/price_anomaly optional; C13 full test
  infra enumerated (conftest/__init__/fixtures/pythonpath/responses); C15/2b
  bundle-aware $/Wh (was: naive per-variant — wrong on real DELTA Max data);
  C3 runner deletions enumerated by line; C14 pack-skip regex deleted,
  _normalize_variant spec'd with collision suffixing, no_sizes_readable moved
  to JSON path; .gitattributes added (CRLF hazard); responses dep added;
  handle discovery re-spec'd as live + disambiguated (probe files hold no
  listings); provisional seeds gated on verified carriage; bot UA decision
  (email contact, no dead domain URL); active-filter made enforced;
  trust_builder kept as documented optional; build.py size corrected to 91KB;
  acceptance product must be in-stock. Full findings preserved in red team
  transcript (session 2026-08-13).
- v1 (2026-08-13): initial plan.

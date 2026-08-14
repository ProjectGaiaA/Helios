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
{price, was_price, available, raw_variant, variant_id, sku}}, in_stock,
price_anomaly?}` — `?` = optional; consumers must not KeyError. `sku` is the
retailer-reported variant SKU, None when absent/blank (v2.2): the
cross-retailer identity key for hard goods and the input to the SKU-drift
tripwire (§4b).

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

## 4b. Phase A2 — the correctness loop (BEFORE any Phase B feature)

Decision (Brandon, 2026-08-13): correctness verification is foundational, not
deferrable — "if information can't be correct there is no point." Gaia's
failure mode: every check was self-referential; errors at one hop were
invisible at the next. Phase A2 closes the loop and GATES everything else —
no new retailers, no deploy, no features until it runs green.

1. **End-to-end audit job**: nightly + on-demand: sample N (product,
   retailer, variant) triples; fetch the RENDERED site page and the
   retailer's live `.json` + `.js` with a cache-buster; compare the actual
   displayed numbers (price, was-price, availability, $/Wh arithmetic).
   Mismatch = alarm. This is the pattern Google Merchant Center runs at
   planetary scale (feed-vs-landing-page crawl; mismatched items delisted).
2. **SKU-drift tripwire**: alarm when the SKU set behind a mapped handle
   changes — the retailer swapped the product under the handle and every
   downstream number becomes confidently wrong.
3. **Provenance surfaced**: every displayed price shows scraped-at ("as of
   N hours ago"); every rendered number traceable to (retailer, handle,
   variant_id, sku, timestamp).
4. **Withhold on doubt, everywhere**: mismatch or unknown → the number comes
   OFF the page until re-verified (Google's delisting discipline; gaia's
   availability ethic generalized).
5. **UCP as the live-truth query (v2.3)**: every Shopify store exposes an
   unauthenticated UCP/MCP endpoint at `/api/ucp/mcp` with `search_catalog`,
   `lookup_catalog` (batch by identifier), `get_product` — the surface both
   active seeds' robots.txt explicitly direct agents to. Verified live
   2026-08-13: shop-solar-kits returns a full tools list; **bluetti returns
   200** (reopens a retailer whose products.json is junk — revisit O1).
   The audit should query it as the arbiter; Phase B should evaluate
   `lookup_catalog` as the PRIMARY scrape path (batch variants + real
   availability would replace the .json+.js request pair). TRAPS: UCP money
   is integer MINOR UNITS ({"amount": 600} = $6.00) — a fresh cents/dollars
   reading hazard, spec the conversion with tests; checkout/cart tools exist
   on the same endpoint and are OFF-LIMITS (§0 checkout-is-for-humans).

### 4c. Phase A2 build spec (v2.5 — corrected per red team #3: 5 CRITICAL / 14 MAJOR / 8 MINOR, all accepted)

New claims (corrected):

| # | Claim | Evidence |
|---|---|---|
| C18 | UCP endpoint live at `{store}/api/ucp/mcp` (13 tools; 10 are checkout/cart/order = off-limits). `tools/list` needs NO initialize/session/meta and returns plain JSON. **`tools/call` is GATED**: every catalog tool requires `meta.ucp-agent.profile` = a public HTTPS URL the MERCHANT fetches (no redirects, Cache-Control public max-age>=60, JSON `{"ucp":{"version":...,"capabilities":{...}}}`); without it → HTTP 422 with JSON-RPC error `invalid_profile_url`. tools/list success does NOT imply tools/call success. wild-oak-trail also advertises UCP via `/.well-known/ucp` (endpoint on `.myshopify.com` host). robots can_fetch true on both actives | red team #3 live probes 2026-08-13 |
| C19 | UCP money: integer MINOR units + ISO 4217 currency; identical wording on all three CATALOG tools (not just checkout) | red team #3 captured schemas |
| C20 | Live tool names confirmed: search_catalog, lookup_catalog (batch ≤10 ids, Product or ProductVariant gids, results grouped by product, per-variant `inputs[]` match exact/featured), get_product (Product ID only — **no handle lookup on any tool**) | red team #3 captured inputSchemas |
| C21 | INPUT schemas resolved (envelope `{meta:{ucp-agent:{profile}}, catalog:{...}}`; `catalog.context{address_country,currency,...}`, `catalog.filters.available` **defaults true = sold-out items hidden**). OUTPUT shape known only from Shopify's published example (`result.structuredContent.products[].variants[]` with `price{amount,currency}`, `availability{available,status,running_low}`, `sku`, NO compare-at/was-price field, per-variant `checkout_url` which is never followed) — live confirmation blocked by the profile gate. **v2.5.1 amendment (gaia `UCP_API_RUNBOOK.md`, verified live on merchants):** (1) profile placement is EXACTLY `params.arguments.meta["ucp-agent"].profile` — eight other placements all fail "Missing profile uri"; (2) the HTTP header `UCP-Agent: profile="<url>"` is required IN ADDITION to the meta field — meta without header = bare HTTP 422 with NO diagnostic body (decoder: bare 422 -> suspect missing header first); (3) catalog payload in `params.arguments.catalog` (confirmed); (4) the agent-profile JSON needs a `services` block and store discovery docs carry `ucp.services["dev.ucp.shopping"]` as a LIST, not an object | red team #3 + gaia UCP_API_RUNBOOK.md |
| C22 | Version pinning surfaces exist: response header `x-shopify-ucp-mcp-api-version` + `/.well-known/ucp` `supported_versions`; schema meaning is pinned to 2026-04-08 | red team #3 |

**Verdict taxonomy (the core design — replaces "mismatch = alarm").** The
audit computes TWO independent comparisons per triple: the **render hop**
(site HTML vs latest JSONL row) and the **freshness hop** (latest JSONL row
vs the retailer's LIVE source). Verdicts:
- `RENDER_DEFECT` (render hop disagrees) — the only defect class: alarm,
  quarantine, exit 3.
- `STALE` (render agrees, freshness disagrees) — NOT a defect: a price
  changing between scrape and audit is expected (flash sales). Notice +
  re-scrape recommendation. NEVER quarantines.
- `CLEAN`, plus non-verdicts: `NO_ROW` (mapped pair never scraped — coverage
  gap, not mismatch), `NO_BASELINE` (no stored sku → drift not evaluable),
  `UNRESOLVED` (variant absent from live source / non-USD / schema surprise),
  `NOT_AUDITED` (budget exhausted). Console summary leads with
  `verified N / attempted M`.
- Exit codes: 0 clean; 3 = any RENDER_DEFECT; **4 = incomplete/unverified**
  (any error, NOT_AUDITED, or M > N) — an audit that could not verify must
  never read as success; 1 usage/config.

Components:

1. **Live source & arbiter**: primary TODAY = `.json` + `.js` (has
   compare_at_price and availability; UCP has no was-price field, C21).
   `scrapers/ucp.py` is built and fully tested against canned fixtures but
   its live use is **gated on O5** (hosted agent profile). When activated,
   `lookup_catalog` becomes the freshness-hop source: ONE request per
   (product, retailer) pair (batch ≤10 variant gids covers the widest seeded
   product), endpoint resolved from `/.well-known/ucp` (fallback
   `{store}/api/ucp/mcp`), robots checked against the host actually called,
   `context={"address_country":"US","currency":"USD"}` always sent,
   `filters.available` explicitly probed both ways ONCE against the known
   sold-out DELTA Max variant and the answer pinned in code (C21 default
   hides sold-out items — absence must classify UNRESOLVED, never drift).
   Money compared as integer cents (UCP minor units native; site/JSONL
   dollars ×100), never float ==. Non-USD → UNRESOLVED + withheld. Version
   headers recorded in the report; a version change raises SCHEMA_REVERIFY
   notice. Client wraps CATALOG READS ONLY — checkout/cart/order tools are
   never wrapped, and `checkout_url` in responses is never followed (§0).
   Error taxonomy handled distinctly: transport error; non-200 WITH JSON-RPC
   error body (the profile gate returns HTTP 422 + diagnostic — never
   raise_for_status before parsing); 200 with result.isError; schema shape
   violation.
2. **`audit.py`** (repo root): samples triples from **latest JSONL rows**
   (the only variant-bearing store) intersected with handle_maps; ALL
   quarantined variants are always included, random sampling fills to N
   (default 10, `--all` supported). Reads displayed numbers by parsing
   `data-*` provenance attributes with stdlib html.parser (+html.unescape) —
   never prose regex. Compares price, was-price (render hop always;
   freshness hop only vs `.json` compare_at_price), availability (same
   variant on both sides), $/Wh re-derived then compared via the SAME
   formatter (string equality). SKU-drift: stored vs live, both non-null
   only. capacity_wh cross-check: search live title/body for a Wh figure →
   CONFIRMED / ABSENT / CONTRADICTED (CONTRADICTED = alarm) — closes the
   loop's one blind spot (capacity is hand-authored; products.json gains
   `specs.capacity_source`). All-available smell: NOTICE only when sample
   ≥8 variants across ≥3 products at that retailer. Outputs parameterized:
   `--data-dir --site-dir --report-out --quarantine-out` (defaults =
   real paths); LF-only writers; ASCII-only console. Budget ≤25 live
   requests; exhaustion → remaining triples NOT_AUDITED + exit 4.
3. **Quarantine** (`data/quarantine.json`, committed): a keyed MAP
   `{"{retailer_id}:{product_id}:{variant_id}": {sku, tier_last_seen,
   reason, observed, expected, first_seen, last_seen,
   consecutive_failures}}` — variant_id is the stable identity (tier is
   regenerated per scrape and can orphan/mis-target). Entry lifecycle:
   written only by RENDER_DEFECT; cleared when its (always-sampled) recheck
   is CLEAN; TTL-expired with logged reason when unobservable for 5 audits;
   removed when product goes inactive or unmapped. Build applies quarantine
   BEFORE cheapest-variant selection: a quarantined cheapest variant
   withholds the WHOLE cell with reason (never silently substitutes
   next-cheapest under a "lowest price" heading).
4. **`build.py` + templates — provenance & withhold**: every product row
   gets `data-variant-id data-tier data-sku data-scraped-at`; every home
   cell gets `data-variant-id data-scraped-at` (and home-cell availability
   must come from the SAME cheapest variant, not row-level in_stock — fixes
   the residual mixed-variant defect). Visible "as of <age>" on every price.
   `build_site(..., now=None)` — staleness (STALE_MAX_HOURS=168) and ages
   derive from the injected clock ONLY (no bare datetime.now(); existing
   absolute-timestamp test fixtures convert to now-relative offsets;
   boundary tests at 167h/169h). Distinct markers:
   `data-withheld="stale"` vs `data-withheld="quarantine"`.
5. **Wiring**: conftest autouse robots stub extended to `scrapers.ucp` +
   `audit`; a test asserts the UCP client opens zero real sockets;
   scrape.yml order becomes scrape → build → audit → rebuild-if-quarantine-
   changed → commit, with `data/quarantine.json` + `data/audit_report.json`
   added to the git add list and the audit step continue-on-error with its
   exit code surfaced in the alarm step. CLAUDE.md documents the audit
   command + verdict taxonomy.

**O5 (BLOCKS UCP activation only, not the A2 build)**: host
`helios-agent-profile.json` at a public HTTPS URL, no redirects,
`Cache-Control: public, max-age>=60`. **v2.5.1 corrections (gaia
`UCP_API_RUNBOOK.md`)**: the minimal capabilities-only JSON previously
inlined here is `profile_malformed` — the profile ALSO needs a `services`
block, and capability values must be arrays of version objects (reference
shape: `tests/fixtures/ucp/helios_agent_profile.json`; authoritative
example: shopify.dev `valid-with-capabilities.json`). Catalog-read
capabilities ONLY — no cart/checkout/order/payment_handlers, ever.
Resolution path: a HELIOS-hosted profile on the future helios GitHub
Pages or its own domain — explicitly NOT gaia's plantpricetracker.com
profile URL (reputation and merchant blocking attach to the profile; the
projects must not share fate). URL lands in config as
`UCP_AGENT_PROFILE`, which stays UNSET until then. Until resolved the
freshness hop runs on `.json`+`.js` and ucp.py stays fixture-tested.

Acceptance (§7-A2):
1. ruff + pytest fully clean.
2. Live: fresh scrape of the 2 priced products (rows gain `sku` — the store
   currently predates the field), rebuild, then `python -X utf8 audit.py
   --all` → report written; summary leads with verified/attempted; expected
   verdicts: CLEAN or STALE for priced pairs, NO_ROW for never-scraped
   pairs (6 of 8 today — never a mismatch).
3. **Render-hop injection (3a)**: tamper a displayed price in a SCRATCH
   copy of site/ → audit (all outputs → tmp) classifies RENDER_DEFECT,
   exit 3, quarantines exactly that variant.
   **Freshness-hop injection (3b)**: tamper a JSONL COPY + rebuild to
   scratch → audit classifies STALE, does NOT quarantine, exits without
   defect. A run that quarantines on 3b has the taxonomy bug.
   SKU-drift: tamper stored sku in the scratch copy → drift alarm.
   Afterward: `git status` clean (all outputs were parameterized to tmp).
4. Quarantine flow with `now` pinned fresh: entry present → rebuild →
   `data-withheld="quarantine"` marker on that variant and its price string
   absent from BOTH index.html and the product page; CLEAN recheck →
   entry removed → renders again. Assert on markers, not absence alone.

## 5. Explicit non-goals (Phase B+)

As v1: Signature Solar/BigCommerce scraper; WhichWatts oracle (complements,
does not replace, the §4b source-of-truth audit); Keepa; Wayback
backfill; alerts; deploy/domain; SEO content; heartbeat; recovery
system (stubs only); promo UI surfacing; taxonomy. NOTE (v2.2):
"audits"/"verify.py" REMOVED from this list — superseded by §4b Phase A2.
Plus explicitly:
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

- v2.5.2 (2026-08-13): Red team #4 on the built A2: FAIL, 2 CRITICAL /
  7 MAJOR / 6 MINOR, all probe-proven, all fixed same day. CRITICAL-1/2:
  quarantine recheck ignored the home surface and read ABSENCE as a clean
  recheck — now positive marker evidence is required on BOTH surfaces
  (leak = RENDER_DEFECT + consecutive_failures++, absence = UNRESOLVED +
  TTL counter++, and any not-clean recheck feeds the TTL). MAJOR-3:
  clear-on-compliant-withhold let a persistent build defect oscillate
  wrong-price/withheld — entries now clear only after a SHADOW REBUILD
  with the entry suppressed renders correctly AND freshness is clean;
  entries are mutated in place, never delete+recreated (6-cycle
  oscillation regression added). MAJOR-4: verified 0 / attempted 0 now
  exits 4 ("nothing audited"). MAJOR-5: workflow alarm step consumes
  report alarms[] (SKU drift / capacity CONTRADICTED fail the run even
  at audit exit 0). MAJOR-6: missing .js availability for a compared
  variant is UNRESOLVED, never verified-CLEAN. MAJOR-7: empty variant_id
  is non-joinable end to end (parser segregates, audit UNRESOLVED,
  quarantine keys validated non-empty, build stamps no attr). MAJOR-8:
  home-cell $/Wh now compared (same-formatter string equality). MAJOR-9:
  ucp.py rate docstring corrected to the runbook's RETRACTED claim
  (planting-tree 429 + 1933s lockout after ~93 calls); >=1.5s sequential
  throttle; 429/503 = hard-stop UcpRateLimited carrying retry-after, no
  retry; list_price extracted (4 of 6 retailers return it — restores a
  was-price freshness comparison for UCP-arbiter mode). MINOR: data-sku /
  data-scraped-at attrs audited as provenance (lies = RENDER_DEFECT) and
  "as of" got a data-field; UCP well-known fetch now uses the bot UA +
  robots + throttle; quarantine/audit_report must be tracked from first
  commit (git-diff no-ops on untracked — documented); quarantine shape
  validated before any live spend (exit 1); non-finite prices (JSON
  Infinity) classify UNRESOLVED; home in-stock cells got visible text.
- v2.5.1 (2026-08-13, mid-A2-build): C21/O5 amended from gaia's
  UCP_API_RUNBOOK.md (proven live integration — supersedes red team #3
  intel where they conflict): dual profile transport (meta field AND
  UCP-Agent header) with the bare-422 decoder rule; exact profile
  placement; agent-profile JSON needs `services` (the O5 inline example
  was profile_malformed); discovery `ucp.services["dev.ucp.shopping"]`
  is a list. O5 resolution = Helios-hosted profile, never gaia's URL.
- v2.5 (2026-08-13): Red team #3 on the v2.4 spec: UNSOUND, 5 CRITICAL /
  14 MAJOR / 8 MINOR, all accepted. Headline corrections: UCP tools/call is
  profile-gated (v2.4's client was uncallable as specced) → dual-arbiter
  design (.json+.js today, UCP gated on O5); single mismatch bucket →
  render-hop/freshness-hop verdict taxonomy (v2.4 would have quarantined
  every flash-sale price change and emptied the site); injected-error test
  split 3a/3b (v2.4's version tested the innocent case); quarantine re-keyed
  to variant_id map w/ lifecycle (tier keys orphan/mis-target); exit 4
  incomplete≠success; provenance data-* attributes (site HTML lacked
  auditable identity); was-price never compared vs UCP (field doesn't
  exist); filters.available default-true trap pinned by probe; injected
  clock (calendar-red tests); sku NO_BASELINE semantics (store predates
  field); budget restated per-pair w/ exhaustion semantics; conftest
  coverage for new modules; parameterized audit outputs (residue-free
  acceptance); capacity_wh blind spot closed via title cross-check +
  capacity_source. Full findings + captured UCP schemas in red team #3
  transcript. C18-C22 rewritten/added.
- v2.4 (2026-08-13): §4c Phase A2 build spec added (UCP client, audit.py
  end-to-end loop, sku-drift, provenance/withhold in build, tests,
  acceptance incl. injected-error catch). C18-C21 added; C21 records that
  catalog-tool schemas are unverified until build. Queued for red team #3.
- v2.3 (2026-08-13): UCP discovered as the sanctioned agent surface (Shopify
  + Google open standard; per-store unauthenticated MCP at /api/ucp/mcp).
  Verified live on shop-solar-kits (full tools list) and bluetti (200 —
  candidate to reopen despite junk products.json). Added §4b item 5: UCP as
  audit arbiter now, candidate primary scrape source in Phase B; minor-units
  money trap and checkout-tools-off-limits recorded. Context: a parallel
  gaia session measured FGT via UCP against gaia's published data — gaia
  showed 121/121 in stock vs ~half sold out per UCP+live page, prices ~10%
  stale-low — the two-source §4b argument confirmed on real data, with the
  stale source being the tracker itself.
- v2.2 (2026-08-13): Brandon's correctness decision. (a) `sku` added to the
  variant schema (scraper + tests) — the v2/v2.1 schema stored only
  variant_id, an orchestrator miss: hard-goods SKUs are the cross-retailer
  identity key gaia never had. Existing JSONL rows predate the field and
  lack it (consumers must .get()). (b) NEW §4b: the correctness loop
  (end-to-end site-vs-source audit, SKU-drift tripwire, provenance display,
  withhold-on-doubt) is Phase A2 and GATES Phase B — reversing v2's deferral
  of audits/verify.py to non-goals. Rationale: gaia's recurring
  wrong-on-site defects were never a matching problem alone; the loop that
  catches all four error classes (identity, reading, render, delivery) was
  the missing structure.
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

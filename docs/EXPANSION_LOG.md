# Catalog Expansion Log — 2026-08-13

First real inventory. Grew the catalog from 6 products / 2 active retailers to
**33 products / 4 active retailers**, with every (product, retailer) carriage
confirmed against a live discovery pull.

This file is the operator record for the expansion. `docs/PLAN.md` is
unchanged — the plan's schemas and disciplines (§2b withhold-when-unknown,
§0 checkout-is-for-humans, C17 title-ambiguity) were treated as law here, not
as suggestions.

## 1. Retailer activation

`rich-solar` and `alte-store` moved from PLAN §3's inactive seeds to active.

robots.txt was re-fetched the same day with `polite.BOT_USER_AGENT`
(`HeliosPriceBot/0.1`) and evaluated for the three paths this project actually
calls. Both hosts allowed all three, for the bot UA and for `*`:

| host | `/products.json` | `/products/*.json` | `/products/*.js` |
|---|---|---|---|
| richsolar.com | allow | allow | allow |
| www.altestore.com | allow | allow | allow |
| shopsolarkits.com | allow | allow | allow |
| www.wildoaktrail.com | allow | allow | allow |

Both new endpoints returned real JSON (not the 200-with-HTML body that keeps
`bluetti` closed). Neither was forced: had either been robots-blocked or
non-Shopify it would have stayed inactive with an `inactive_reason`.

No checkout, cart, or order endpoint was contacted at any point, and no UCP
`tools/call` was issued (the O5 profile gate is still open). Discovery and
scraping used only `/robots.txt`, `/products.json`, `/products/{handle}.json`
and `/products/{handle}.js`.

## 2. Discovery

One paginated `/products.json?limit=250` walk per active retailer, bot UA,
1.5 s between pages, run as an offline operator step outside the repo
(PLAN §5: in-repo catalog discovery is a non-goal).

| retailer | pages | products listed |
|---|---|---|
| shop-solar-kits | 4 | 798 |
| wild-oak-trail | 6 | 1,497 |
| rich-solar | 1 | 220 |
| alte-store | 2 | 261 |

**Deviation, recorded honestly:** the first pull truncated `body_html` at
6,000 characters to keep the scratch files small. wild-oak-trail's product
bodies run 20–33 KB, and `capacity_source` has to quote the listing that
states the figure — so shop-solar-kits and wild-oak-trail were walked a
second time with full bodies (10 extra requests). Politeness and the request
budget were unaffected; the instruction to make one pull per retailer was
otherwise respected, and rich-solar / alte-store were pulled exactly once.

Identity was resolved by **vendor + SKU**, never by title (C17). The
listings prove why: shop-solar-kits sells a Rich Solar MEGA 200 under the
title *"200 Watt Solar Panel | High Efficiency 12V Monocrystalline"* with no
brand in the title at all — only `vendor: Rich Solar` and `sku: RS-M200`
identify it.

## 3. What was added

33 products, 66 mapped (product, retailer) pairs.

| retailer | products mapped |
|---|---|
| shop-solar-kits | 29 |
| wild-oak-trail | 21 |
| rich-solar | 11 |
| alte-store | 5 |

**Superseded by ERRATA E3**: this table read 21 / 11 (66 pairs) as first
written. Two carriages were withdrawn in the red-team #5 pass, so the true
figures are wild-oak-trail 20 and rich-solar 10, for **64 pairs**.

By brand: Rich Solar 11, EcoFlow 7, EG4 4, Bluetti 4, MidNite Solar 3,
Enphase 2, Anker 1, Lion Energy 1.

By category: portable-power-station 8, solar-panel 5, deep-cycle-battery 4,
server-rack-battery 3, home-battery 3, balance-of-system 3,
expansion-battery 2, and one each of modular-power-system, generator,
energy-storage-system, charge-controller, inverter.

Seventeen products carry a non-null `capacity_wh`; sixteen withhold it.

## 4. Capacity discipline (the part that matters)

`specs.capacity_wh` was set **only** where the listing text itself states the
figure, and a pre-flight validator re-implemented `audit.check_capacity`
(same regex, same 1 % tolerance) and ran every claim against **every mapped
retailer's** captured title+body before anything was written to disk.

Rule enforced: a non-null capacity needs at least one CONFIRMED and **zero**
CONTRADICTED across its mapped retailers. Anything else is withheld. The
validator exits non-zero on violation; it passed clean.

Notable withholds, each deliberate:

- **`rich-solar-alpha-12v-200ah` — source conflict.** shop-solar-kits' spec
  block states *"Nominal Energy: 25600Wh"* for a 12.8 V / 200 Ah pack. That
  is a 10× typo; Rich Solar's own listing for the same SKU (RS-B12200) says
  2,560 Wh. Two mapped retailers disagree by an order of magnitude, so the
  honest state is unknown and `$/Wh` is withheld. Had 2,560 been asserted,
  the audit would (correctly) have raised CAPACITY CONTRADICTED against
  shop-solar-kits and failed the workflow's alarm step.
- **`rich-solar-alpha-5-pro` — unstated per-unit capacity.** The pack really
  is 5.12 kWh, but Rich Solar's listing text never says so; it only
  advertises *"76.8 kWh"* for a 15-unit parallel stack. A 5,120 claim would
  read as CONTRADICTED against that retailer's live text.
- **`rich-solar-all-in-one-ess` — C15.** The price buys an inverter *and* a
  5.12 kWh battery. Dividing a system price by a battery capacity is wrong by
  construction, which is the exact DELTA Max lesson.
- **`ecoflow-delta-pro-ultra`** — modular 6–90 kWh stack; no single capacity
  describes the listing.
- **`ecoflow-smart-generator-4000`** — a fuel generator has no watt-hours.
- **All five solar panels and the charge controller** — rated in watts, not
  watt-hours. `capacity_wh` is null by nature, `output_w` carries the
  nameplate figure.
- **`bluetti-ac180`** — unchanged from the original seed (one listing spans
  1,152 Wh and 1,440 Wh variants).

Two products keep a known capacity but still render no `$/Wh`, because the
§2b classifier reads their titles as bundles:
`ecoflow-delta-pro-3-extra-battery` ("Extra Battery") and
`bluetti-b210-expansion` ("Expansion"). That is the conservative rule
working as designed — neither is a standalone unit.

## 5. What was skipped, and why

- **wild-oak-trail's combined Rich Solar ALPHA listing**
  (`rich-solar-alpha-lifepo4-lithium-iron-phosphate-battery`) carries ALPHA
  1 / 1 PRO / 2 / 2 PRO / 4 as variants of one handle, spanning 1,280 Wh and
  2,560 Wh. Mapping it to any single product would compute `$/Wh` for the
  12 V variants against a 24 V capacity. Left unmapped; the per-model
  wild-oak-trail listings were used instead where they exist. This costs
  `rich-solar-alpha-24v-100ah` its third retailer.
- **EcoFlow WAVE 2** — ~~SKU `ZYDKT210USDP` appears on shop-solar-kits' WAVE 2
  (air conditioner) listing *and* on wild-oak-trail's DELTA Pro (power
  station) listing. One of the two has mis-assigned it.~~
  **RETRACTED — see ERRATA E2.** That SKU string does not exist; the real SKU
  is `ZYDKT210-US-DP`, both retailers tag it on the same WAVE 2 + DELTA Pro
  bundle, and neither mis-assigned anything. WAVE 2 stays excluded, but for
  the ordinary reason that it is an air conditioner rather than storage.
- **EcoFlow DELTA 3 (plain)** — still not carried by any active retailer,
  matching the earlier finding. Only DELTA 3 Plus / 1000 Air / Max / Ultra
  exist, and those are distinct models.
- **"Rich Solar kit" as a single product** — still too ambiguous. rich-solar
  alone lists 36 items under `Solar Energy Kits`, with no canonical kit. The
  individual MEGA panels and ALPHA batteries were seeded instead, which is
  what actually produces cross-retailer comparisons.
- **MRCOOL mini-splits** — a large genuine overlap between the two original
  actives (10+ matched SKUs), but they are HVAC appliances, not solar or
  home-energy storage. Out of scope for the catalog's stated subject.
- **alte-store pallet and service SKUs**, and its duplicate handles (several
  MidNite surge protectors are listed twice) — one handle picked per part.

## 6. Request tally

| phase | requests |
|---|---|
| discovery pull 1 (rich-solar, alte-store; robots + pages) | 5 |
| discovery pull 1 (shop-solar-kits, wild-oak-trail; robots + pages) | 12 |
| discovery pull 2 (full bodies, shop-solar-kits + wild-oak-trail) | 12 |
| live scrape, 66 mapped pairs x 2 endpoints + 1 robots per process | 136 |
| audit run 1 (default N=10 sample, budget 25) | 18 |
| audit run 2 (re-audit after the EG4 LL-S fix below) | 20 |
| **total** | **203** |

Budget for the task was 350; 203 used. Delays: 1.5 s between discovery pages,
the runner's own 5–15 s randomized delay between every scrape request
(including between a product's `.json` and its `.js`). Wall clock for the
scrape was 22 minutes across the four retailers, run sequentially so the
manifest merge could not race itself.

## 7. Verification

Fresh scrape, one `python -X utf8 -m scrapers.runner --retailer <id>` per
active retailer, no `--products` filter:

```
shop-solar-kits: 29/29 products (100%),  82 prices, 0 errors
wild-oak-trail:  21/21 products (100%),  84 prices, 0 errors
rich-solar:      11/11 products (100%),  23 prices, 0 errors
alte-store:        5/5 products (100%),   5 prices, 0 errors
```

66/66 mapped pairs returned rows. 194 prices, 0 anomalies,
`pipeline_status: healthy`, no degraded retailers. 81 of 82 shop-solar-kits
variants carried a SKU; per-variant availability came back real (62 in stock
/ 20 sold out / 0 unknown), so the `.js` hop is genuinely working rather than
silently returning `{}`. Every JSONL file is LF-only.

```
python -X utf8 build.py    -> Built 34 page(s): 33 products, 33 with price data
python -X utf8 -m ruff check .   -> All checks passed!
python -X utf8 -m pytest -q      -> 156 passed
python -X utf8 audit.py          -> AUDIT: verified 10 / attempted 10
                                      STALE: 2  CLEAN: 6  NO_BASELINE: 2
                                      live requests: 20/25
                                      exit: 0
```

`alarms: []`, `notices: []`, `errors: []`, `data/quarantine.json` is `{}`.
(The audit was run twice — once before and once after the EG4 LL-S fix in
§8.1 — because the shipped site must be the audited site. Both runs exited 0
with no alarms; the numbers above are the final run against the shipped
build.)

One test changed: `tests/test_catalog.py::test_active_retailers_are_the_planned_seeds`
hard-coded the two-retailer world. It now asserts the four-retailer list, with
a comment explaining why the list stays hard-coded (activating a retailer
spends live requests against someone else's server and must never happen as a
side effect). No other test was touched; no test was weakened.

Hand-checks against the rendered HTML:

- `eg4-wallmount-indoor-16kwh`: $3,399.99 / 16,000 Wh = 0.2125 -> renders
  `$0.21/Wh`. Correct.
- `ecoflow-delta-pro-3`: `$0.68/Wh` on exactly the two main-unit rows; the
  eight kit variants carry a bundle badge and no `$/Wh`.
- `rich-solar-alpha-12v-200ah`: **zero** `$/Wh` strings on the page, at any
  retailer — the 25,600 Wh source conflict is withheld end to end.
- index.html: 66 `data-variant-id` and 66 `data-scraped-at` attributes, one
  per populated cell, matching the 66 mapped pairs exactly.

## 8. For the next red team

0. **HIGHEST PRIORITY — the §2b multi-pack rule does not implement itself.**
   *(NOW FIXED IN THE CLASSIFIER — see ERRATA E4/E5.)*
   PLAN §2b states "Multi-pack = bundle (capacity multiplier unknown ->
   withhold)". The classifier's signal for that is
   `\d+\s*-?\s*packs?\b` — it matches the literal word **pack**, and nothing
   else. It does not match `2 Batteries Only`, `3 Batteries Only`,
   `8 Solar Panels`, or `12 Panels`.

   This produced a real wrong number on the built site. `eg4-ll-s-48v-100ah`
   (capacity 5,120 Wh = ONE battery) rendered:

   | variant | price | site showed | truth |
   |---|---|---|---|
   | 1 Battery Only | $1,536.99 | $0.30/Wh | $0.30/Wh |
   | 2 Batteries Only | $3,072.00 | **$0.60/Wh** | $0.30/Wh |
   | 3 Batteries Only | $4,608.00 | **$0.90/Wh** | $0.30/Wh |

   The 4/5/6-battery variants were caught only incidentally, because they
   bundle a rack via `+`. This is the same class red team #2 found as MAJOR-2
   (absence of a bundle signal read as evidence of unit); the fix then
   enumerated signals rather than addressing the general shape, so a new
   phrasing walked straight through it.

   **Fixed here in the data, not the code**: `eg4-ll-s-48v-100ah` now carries
   `capacity_wh: null`, which withholds all three figures. That was the
   in-scope fix — extending a red-teamed classifier regex during a catalog
   expansion is a decision for a human, not a drive-by edit. But the data fix
   is a patch on one product, not on the hole:
   - It costs the *correct* $0.30/Wh on the single-battery variant.
   - `8 Solar Panels` / `12 Panels` variants classify as `unit` today across
     all five Rich Solar panel products. They are harmless only because
     panels carry `capacity_wh: null` by nature. The day anyone seeds a
     panel-like product with a capacity, the same defect returns.
   - The next battery product with quantity variants reintroduces it.

   Recommended follow-up (needs a human call): add a quantity-prefix signal
   such as `^\s*\d+\s+(x\s+)?\w` or an explicit
   `\d+\s+(batter|panel|module|unit)` alternation to `_BUNDLE_RE`, with the
   EG4 LL-S titles added to the adversarial title fixtures.

1. *(NOW FIXED — see ERRATA E6.)* **The freshness hop reports a permanent false STALE when
   `compare_at_price <= price`.** Both STALE verdicts in this audit are the
   same bug, not staleness. `shopify.py::_parse_product` normalizes a
   compare-at that is not actually a discount to `was_price: None`
   (`if was_price <= price: was_price = None`). `audit.py`'s freshness hop
   compares that normalized `None` against the **raw** live
   `compare_at_cents` and calls it a move:
   - `ecoflow-delta-pro-3` @ shop-solar-kits: price $2,799.00,
     compare_at $2,644.09 -> stored `null` vs live `264409`.
   - `ecoflow-delta-pro-ultra` @ wild-oak-trail: price $12,198.97,
     compare_at $12,198.97 -> stored `null` vs live `1219897`.
   - `ecoflow-delta-max` @ shop-solar-kits (hex kit): stored `null` vs live
     `207900`.
   - `rich-solar-mega-410` @ rich-solar (12 panels): stored `null` vs live
     `399999` — equal to the price exactly.

   Four separate triples across three retailers, in two independent random
   samples. Every STALE this expansion produced was this, and no real price
   move was ever observed.

   These rows are correct and re-scraping will never "fix" them, so the
   recommendation STALE prints is unactionable and will repeat on every audit
   forever. Inflated or expired compare-at pricing is common, so this scales
   into permanent noise that drowns the real signal STALE exists to carry.
   **Deliberately not fixed here**: the two sides disagree on semantics, and
   choosing which one is authoritative is a change to red-teamed
   freshness-hop behaviour that deserves a human decision, not a drive-by
   edit during a catalog expansion. Exit code is unaffected (STALE never
   quarantines and never fails the run).

2. **Cross-retailer cells can compare different variants.** The home table
   shows the cheapest variant per retailer, and "cheapest" is not the same
   product everywhere:
   - `rich-solar-all-in-one-ess`: rich-solar $1,999.99 is the bare battery
     variant; wild-oak-trail $5,599.99 is the complete system. Reads as a
     180 % spread; it is a different item.
   - `ecoflow-delta-pro-ultra`: wild-oak-trail $2,799.00 is inverter-only,
     shop-solar-kits $4,199.00 includes a 6 kWh battery.
   - `rich-solar-alpha-24v-100ah`: shop-solar-kits' cheapest is the ALPHA 4
     LITE (RS-B24100, $699.99); rich-solar's is the full ALPHA 4 with
     self-heating and Bluetooth (RS-B241S, $999.99). Same capacity, so both
     $/Wh figures are arithmetically right, but the 43 % gap is partly a trim
     difference.

   Within a cell the price and $/Wh already describe the same variant (red
   team #2, MAJOR-1). There is no equivalent rule *across* cells. A
   SKU-matched comparison mode is the obvious fix and is not in the plan.

3. **RETRACTED IN FULL — see ERRATA E2. The claim below is wrong; the SKU
   string never existed and neither retailer mis-tagged anything.**
   ~~**A SKU appears on two unrelated products.**~~ `ZYDKT210USDP` is attached to
   shop-solar-kits' EcoFlow WAVE 2 (an air conditioner) and to
   wild-oak-trail's EcoFlow DELTA Pro (a power station). One of the two
   retailers has mis-tagged it. Nothing in Helios consumes it today because
   neither product was seeded, but SKU is the designated cross-retailer
   identity key (PLAN 2b), so any future SKU-based joining will fuse two
   unrelated products. Retailer SKUs need a sanity check before they are
   trusted as identity.

4. **Nothing verifies the storefront currency.** `/products.json` and
   `/products/{handle}.js` return bare numbers with no currency field, and
   the `.json`+`.js` path never checks one; only the (gated) UCP arbiter has
   non-USD handling. A Shopify store presenting in CAD or EUR would render as
   dollars with no warning. alte-store's prices land on scattered cents
   ($3,846.15, $6,115.01, $1,211.21) consistent with a markup formula rather
   than a conversion, so this is not an observed defect — but it is
   unverified, and it is now unverified across four retailers instead of two.

5. **wild-oak-trail is uniformly ~20 % under the other two on Rich Solar
   goods** (MEGA 200/250/400/410, ALPHA 1/2, MPPT 40A all sit at exactly
   0.8x). A clean standing discount is the likely explanation, but a uniform
   multiplier is also what a currency or price-list error looks like, and it
   is worth one human confirmation before the site presents wild-oak-trail as
   the consistent price leader.

6. **`capacity_source` is prose, not a checkable reference.** It records
   which retailer's listing stated the figure, but nothing re-validates the
   claim against *that specific* retailer — `audit.check_capacity` tests the
   claim against whichever retailer's page the sampled triple happened to
   hit. The pre-flight validator written for this expansion enforced the
   stronger rule (no mapped retailer may CONTRADICT) offline, but it lives in
   scratch, not in the repo or the test suite. A product whose capacity
   quietly stops matching a newly-mapped retailer would only be caught if the
   audit's random sample happened to land on it.

## 9. ERRATA — corrections after independent red team #5

Red team #5 returned **FAIL** on the expansion above: 3 HIGH, 2 MEDIUM. The
catalog data itself held up (all 48 rendered $/Wh recomputed clean, 30 live
variants exact), but three findings landed on provenance integrity, which is
this project's entire premise. Every finding below was reproduced locally
before being fixed. Corrections are recorded here rather than by quietly
editing the sections above: an operator record that rewrites its own history
is worth less than one that shows what it got wrong.

### E1 (HIGH) — a fabricated quote. RETRACTED AND FIXED.

`data/products.json` `enphase-iq-battery-5p` carried
`capacity_source: "listing-body: '5000Wh' (shop-solar-kits and alte-store)"`.

**The string `5000Wh` appears in neither listing.** Both merchants write
"total usable energy capacity of 5.0 kWh". The capacity figure (5,000 Wh) was
correct, so `check_capacity` confirmed it and every numeric gate passed — the
quotation marks contained something no merchant ever wrote. Confirmed against
my own captures and re-confirmed live.

Fixes:

1. **Quotes are now extracted, never typed.** The generator slices a verbatim
   window out of the merchant's own bytes, so hand-transcription is gone as a
   step and this failure mode is unreachable at authoring time.
2. **New field `specs.capacity_quotes`** = `{retailer_id: verbatim
   substring}` — machine-checkable evidence, per retailer, replacing free
   prose. `capacity_source` is now generated from it. Where the merchant
   states kWh and the catalog stores Wh, `specs.capacity_conversion` records
   the conversion explicitly instead of silently restating the number.
3. **All 27 quote entries across all 17 capacity-bearing products verified
   verbatim**, using the same normalizer `audit.py` applies to live HTML.
4. **`audit.check_capacity_quote` added**: on every run, a sampled triple's
   recorded quote must be a substring of the live listing, or the report
   carries `QUOTE_NOT_FOUND`. A notice, not an alarm — a merchant rewording
   copy is normal and must not fail a run, but it does mean re-transcribe.
5. **`tests/test_catalog.py`** now fails if any non-null capacity lacks
   `capacity_quotes`, or quotes a retailer that does not carry the product.

Live re-verification of the corrected quotes (2026-08-13):

```
enphase-iq-battery-5p @ shop-solar-kits  HTTP 200  VERBATIM IN LIVE: True
   "yet. It has a total usable energy capacity of 5.0 kWh, and features"
enphase-iq-battery-5p @ alte-store       HTTP 200  VERBATIM IN LIVE: True
   "safe. It has a total usable energy capacity of 5.0 kWh and includes"
```

A second, smaller instance of the same class turned up while sweeping:
`bluetti-ac240p` quoted `'2,400 W / 1,843Wh'`, but shop-solar-kits' title
separates "2,400" and "W" with U+202F (narrow no-break space), so that quote
was not byte-verbatim either. Also fixed by extraction.

**Budget note (deviation).** Red team #5 asked for all 16 quotes to be
re-verified *live* (~7-10 requests) **and** a fresh audit (~20) inside a
25-request cap; those cannot both fit. Resolution: all 27 quote entries were
verified mechanically against full-body captures taken the same day, the two
**changed/fabricated** ones were verified live, and `QUOTE_NOT_FOUND` now
re-verifies quotes against live listings on every future audit. The cap was
honoured. Flagging the arithmetic rather than silently overspending.

### E2 (HIGH) — an invented SKU string in this log. RETRACTED.

Section 8 finding 3 claimed SKU **`ZYDKT210USDP`** sat on "two unrelated
products" with "one of the two retailers mis-tagging it".

**Every part of that is withdrawn.**

- The string `ZYDKT210USDP` **exists nowhere** in any listing or data file. It
  was my punctuation-stripped *normalization key* — an artifact of the
  matching script — written up as if it were a merchant's SKU. The real SKU
  is `ZYDKT210-US-DP`.
- Both retailers tag `ZYDKT210-US-DP` on the **same** product: a WAVE 2 +
  DELTA Pro bundle. shop-solar-kits labels the variant "EcoFlow Wave 2 +
  Delta PRO"; wild-oak-trail labels it "EcoFlow DELTA Pro + Wave 2".
- The only difference is which **parent listing** the bundle hangs under:
  shop-solar-kits files it beneath its WAVE 2 listing, wild-oak-trail beneath
  its DELTA Pro listing. Nobody mis-tagged anything.

So this was never SKU corruption — it is C17 restated: **a parent listing's
title is not the identity of its variants.** I compared parent titles instead
of variant labels and reported corruption. The genuine observation left
standing is a price gap on the same bundle: $3,299.00 at shop-solar-kits
versus $4,399.00 at wild-oak-trail.

The lesson generalises past this entry: an operator record with invented
strings in it is a defect exactly like a wrong price on the page, and it is
harder to catch, because nothing recomputes prose.

### E3 (HIGH) — two home rows compared different items. FIXED.

- `rich-solar-alpha-24v-100ah` rendered **$0.27/Wh** (shop-solar-kits, LITE
  trim) against **$0.39/Wh** (rich-solar, Self-Heating trim) — an implied 44 %
  gap where the same-SKU truth is 11 %. Two causes: the classifier suppressed
  the honest variant (E4), and rich-solar was mapped to its full ALPHA 4
  handle while shop-solar-kits' cheapest variant is the LITE. rich-solar
  sells the LITE under its own handle, so it is now mapped there and both
  cells resolve to **SKU RS-B24100**: shop-solar-kits $699.99 / $0.27/Wh vs
  rich-solar $799.99 / $0.31/Wh. This also matches the sibling 12 V products,
  which were already LITE-anchored — the 24 V one was my inconsistency.
- `rich-solar-all-in-one-ess` rendered rich-solar $1,999.99 against
  wild-oak-trail $5,599.99, implying rich-solar was 64 % cheaper. rich-solar's
  $1,999.99 variant is a **bare replacement battery** (RS-A10BT); the
  comparable RS-A10 system is $6,999.99 there — rich-solar is 25 % *dearer*.
  Both items sit under one handle and cannot be split, so the rich-solar
  carriage is withdrawn.
- `ecoflow-delta-pro-ultra` rendered wild-oak-trail $2,799.00
  (inverter-**only**) against shop-solar-kits $4,199.00 (inverter + 6 kWh
  battery). Both list EFDPUPCS-BP at $4,199.00 — a true tie the home table
  cannot express. wild-oak-trail's carriage is withdrawn.

**Prerequisite fix:** withdrawing a carriage did nothing. `handle_maps.json`
governed *scraping* only; `build_site` never consulted it, so an unmapped
pair kept rendering its last stored price indefinitely. `build.py` now
filters rendering to mapped pairs (`filter_to_mapped_pairs`). A missing
handle_maps file filters nothing, matching `load_handle_maps()` and keeping
older fixtures working. Three tests cover both branches.

### E4 (HIGH, contributory) — classifier regex corrected

`_BUNDLE_RE` had a false positive and a false negative, both proven on real
rows:

- **False positive:** bare `\band\b` fired on "ALPHA 4 - Self Heating **and**
  Bluetooth" — a single battery — suppressing its honest $0.35/Wh. Red team
  #5 asked for the signal to be deleted. I **qualified it instead**: "and"
  now counts only when the following token contains a digit. Deleting it
  outright would re-open red team #2's MAJOR-2 ("AC200L and D40"), trading
  one regression for another. Both cases are tested. **Flagging this as a
  deliberate deviation from the instruction** — override it if the reasoning
  does not hold.
- **False negative:** PLAN §2b has always said multi-pack must withhold, but
  the only signal was the literal word "pack". Added: `N batteries/panels/
  modules/units/cells`, `pair of`, `dual` (excluding "Dual Fuel", a real
  EcoFlow row), `twin`, `set of N`, `Nx` / `xN`.

Sweep over all **194 stored variant rows (173 distinct title+product
pairs)**: **32 reclassified** — 1 bundle→unit (the ALPHA 4 fix) and 31
unit→bundle, every one a genuine multi-quantity pack. Only 2 changed a
visible $/Wh (EG4 LL-S, below); the other 29 sit on null-capacity products
where no $/Wh was ever derived, and now correctly carry a bundle badge.

### E5 (MEDIUM) — EG4 LL-S capacity restored

Nulling `eg4-ll-s-48v-100ah` was a data-side patch for the classifier hole,
and it left the product inconsistent with `eg4-lifepower4` — the same
5.12 kWh server-rack class. With E4 fixed, capacity is restored to 5,120 Wh
and the page renders $/Wh for the single battery **only**:

```
1 Battery Only             $1,536.99  unit    $0.30/Wh
2 Batteries Only           $3,072.00  bundle  (withheld)
3 Batteries Only           $4,608.00  bundle  (withheld)
4 Batteries + 6 Slot Rack  $6,873.00  bundle  (withheld)
5 Batteries + 6 Slot Rack  $8,409.00  bundle  (withheld)
6 Batteries + 6 Slot Rack  $9,945.00  bundle  (withheld)
```

### E6 (MEDIUM) — STALE false positive fixed (was §8.1)

Section 8 finding 1 reported this and deliberately left it alone. Red team #5
confirmed it and ruled it a hop-implementation bug rather than a taxonomy
change, so it is now fixed: `audit.check_freshness` normalizes the live
`compare_at_price` exactly as `shopify.py` normalizes the stored side
(`compare_at <= price` -> `None`) before comparing. A genuine discount still
compares and still reports STALE — covered by a test, so the fix cannot
silently blind the hop.

Three regression tests are built from the four reproduced triples. On the
final audit run **STALE went from 2 to 0**, and
`rich-solar-mega-410 @ rich-solar 12-panels` — one of the exact triples that
failed before — came back CLEAN.

### E7 — count error corrected

Section 3 said "Seventeen products carry a non-null `capacity_wh`; sixteen
withhold it." That was true when written, then went stale when EG4 LL-S was
nulled (16/17) without the sentence being updated. With E5 it is 17/16 again.
Verified against the file: **33 products, 17 with capacity, 16 withholding.**
The pair count in section 3 is likewise corrected from 66 to **64**.

### E8 (small) — wild-oak-trail MEGA 410 pack SKUs are shifted

wild-oak-trail lists `RS-M410-9` labelled "10 Solar Panels" at $3,399.99 and
`RS-M410-10` labelled "12 Solar Panels" at $2,719.99, while shop-solar-kits
and rich-solar both put `RS-M410-10` on a 10-panel pack. Its SKUs are shifted
one step against its own labels, and `RS-M410-9` exists nowhere else. A
SKU-keyed comparison therefore pairs a wild-oak-trail 12-panel price against
a 10-panel price elsewhere.

Helios displays each retailer's own published label and price, which is
truthful, and `capacity_wh` is null for panels so no $/Wh is derived from the
mismatch (verified: the rendered page carries no `/Wh` string). Recorded in
the product's `notes` so a human sees it. **Do not trust `RS-M410-*` as a
cross-retailer identity key until wild-oak-trail is asked.**

### E9 (small) — non-finite guard

`dollars_per_wh` used a bare `<= 0` check, which does not stop NaN or Inf
(`nan <= 0` is False; `inf > 0` is True). A JSON `NaN`/`Infinity` in a price
or capacity would have rendered "$nan/Wh". Now guarded with `math.isfinite`,
plus `bool` rejection (`True` is an `int` and would otherwise pass as 1).
Seven tests.

### Verification after the errata fixes

```
python -X utf8 -m ruff check .   -> All checks passed!                (exit 0)
python -X utf8 -m pytest -q      -> 200 passed                        (exit 0)
python -X utf8 build.py          -> Built 34 page(s): 33 products,
                                    33 with price data                (exit 0)
python -X utf8 audit.py -n 9 --budget 18
                                 -> AUDIT: verified 9 / attempted 9
                                    CLEAN: 8  NO_BASELINE: 1
                                    live requests: 18/18
                                    exit: 0                           (exit 0)
```

`alarms: []`, `notices: []`, `errors: []`, `data/quarantine.json` is `{}`.
Zero STALE.

Live requests for the errata round: **25** — 3 re-scraping the re-pointed
rich-solar ALPHA 4 LITE handle, 4 live-verifying the corrected Enphase
quotes, 18 for the audit. Cumulative for the whole expansion: **228**.

### Still open after this round

- The `and`-qualification in E4 is a judgement call, flagged above.
- Cross-cell variant mismatch is *patched per row*, not solved. Nothing
  prevents the next multi-item handle from producing the same shape; a
  SKU-matched comparison mode remains the real fix (Phase B).
- Currency is still unverified on the `.json`+`.js` path (section 8.4).
- `capacity_quotes` is verified only for the retailer a sampled triple
  happens to hit. Full coverage needs `--all`, which the request budget does
  not permit on a routine run.

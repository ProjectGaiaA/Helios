# Content Layer Log — 2026-08-14

First content layer: three ranked buying guides, an About page, an affiliate
disclosure, SEO metadata, and a sitemap. Built from commit `cb9ed7c` (clean
tree).

`docs/PLAN.md` is unchanged. Its disciplines were treated as law here:
§2b withhold-when-unknown, §4c.4 provenance-on-every-number, §0
checkout-is-for-humans, §2 no-external-assets. This file is the operator
record, in the same spirit as `docs/EXPANSION_LOG.md`.

**No live HTTP was issued during this task.** Everything below was produced
from the committed catalog, the committed JSONL price store, and fixtures.
`audit.py` was NOT run, because its freshness hop requires live retailer
requests — see §7.1.

## 1. The governing rule

Every number a visitor sees derives from a scraped row at build time and
carries provenance attributes. There are no baked-in prices, no hand-typed
rankings, and no superlative the data does not support.

The mechanism for that is **shared assembly, not parallel assembly**. The
guides do not re-implement anything:

| concern | function reused |
|---|---|
| unit vs bundle | `build.classify_variant` |
| the rating itself | `build.dollars_per_wh` (price / quantity + guards) |
| money and rating strings | `build.money`, `format_dollars_per_wh` |
| staleness + ages | `build.age_hours`, `age_display`, `is_stale`, `STALE_MAX_HOURS` |
| quarantine | `build._quarantine_key_for`, `load_quarantine` |
| carriage contract | `build.filter_to_mapped_pairs` |
| deep links | `build._buy_url` |
| retailer naming | `build._retailer_name` |

Two new formatters were added (`format_dollars_per_watt`, `format_percent`)
because no `/W` or `%` formatter existed. `dollars_per_wh` is reused
unchanged for $/W by passing `output_w` as the divisor: the arithmetic is
the same price/quantity division and the valuable part — the unit-only,
finite, positive, non-bool guards (E9) — must not be duplicated.

A guide that computed its own $/Wh could disagree with the product page for
the same variant. Two different numbers for one fact is the exact defect
class this project exists to prevent, so it is prevented structurally rather
than by review.

## 2. What was built

### Guides (`templates/guide.html`, one template, three data-driven pages)

Scope is a **query over the catalog's own `category` field**, not a
hand-kept id list. A product added to a tracked category joins its guide on
the next build; one removed leaves it.

| guide | categories | metric | ranked / in scope |
|---|---|---|---|
| `server-rack-and-wall-mount-battery-cost-per-kwh` | `server-rack-battery`, `home-battery` | $/Wh | 5 / 6 |
| `portable-power-stations-compared-by-real-prices` | `portable-power-station` | $/Wh | 7 / 8 |
| `solar-panel-pallets-cheapest-cost-per-watt` | `solar-panel` | $/W | 3 / 5 |

Each guide renders: a ranked list; every tracked retailer's own price for
every variant with its own "as of" age and a `rel="nofollow sponsored"` deep
link; a "tracked, but not ranked" section naming the rule that withheld each
number; and a methodology footer whose every count is computed.

### As rendered, 2026-08-14

**Server-rack & wall-mount battery cost per kWh** — 5 of 6 ranked

| # | product | best $/Wh | price | retailer |
|---|---|---|---|---|
| 1 | EG4 Indoor WallMount Battery 48V 314Ah 16kWh | $0.21/Wh | $3,399.99 | Shop Solar Kits |
| 2 | EG4 LifePower4 V2 48V 100Ah | $0.29/Wh | $1,470.99 | Shop Solar Kits |
| 3 | EG4 LL-S 48V 100Ah Server Rack Battery | $0.30/Wh | $1,536.99 | Shop Solar Kits |
| 4 | MidNite Solar MNPowerFlo16 48V 16kWh LFP | $0.38/Wh | $6,115.01 | altE Store |
| 5 | Enphase IQ Battery 5P | $0.73/Wh | $3,669.00 | Shop Solar Kits |

Not ranked: **Rich Solar ALPHA 5 PRO** — capacity not established
(EXPANSION_LOG §4: the pack really is 5.12 kWh but no listing text says so).

**Portable power stations compared by real prices** — 7 of 8 ranked

| # | product | best $/Wh | price | retailer |
|---|---|---|---|---|
| 1 | Anker SOLIX F2600 | $0.49/Wh | $1,254.00 | Shop Solar Kits |
| 2 | EcoFlow DELTA Max 2000 | $0.52/Wh | $1,049.00 | Shop Solar Kits |
| 3 | EcoFlow DELTA Pro 3 | $0.68/Wh | $2,799.00 | Shop Solar Kits |
| 4 | EcoFlow RIVER 2 Pro | $0.74/Wh | $569.00 | Shop Solar Kits |
| 5 | Bluetti AC200L | $0.78/Wh | $1,599.00 | Wild Oak Trail |
| 6 | EcoFlow RIVER 3 | $0.81/Wh | $199.00 | Shop Solar Kits |
| 7 | Bluetti AC240P | $0.84/Wh | $1,539.00 | Shop Solar Kits |

Not ranked: **Bluetti AC180** — one listing spans 1,152 Wh and 1,440 Wh
variants, so capacity is null by the §2b rule.

**Solar panel pallets: cheapest cost per watt** — 3 of 5 ranked

| # | product | best $/W | price | retailer |
|---|---|---|---|---|
| 1 | Rich Solar MEGA 200 200W 12V | $0.96/W | $191.99 | Wild Oak Trail |
| 2 | Rich Solar MEGA 250 250W 12V | $0.96/W | $239.99 | Wild Oak Trail |
| 3 | Rich Solar MEGA 200 Briefcase 200W | $1.30/W | $259.88 | Shop Solar Kits |

Not ranked: **MEGA 400** and **MEGA 410** — sold only as pallets of
8/10/12/36, every variant classifies as a bundle, so $/W is withheld at
every retailer. Their pallet prices are still published.

Ranks 1 and 2 both display `$0.96/W` (true values 0.95995 and 0.95996).
The ordering is by the unrounded value; the displayed strings are equal.
This is honest rounding, but it looks odd and is flagged in §7.

### Same-SKU spreads (power stations only)

Five spreads found, joined on the retailers' own SKU strings among displayed
(non-withheld, real-price) offers:

| SKU | product | low | high | gap |
|---|---|---|---|---|
| DELTA2000-US | EcoFlow DELTA Max | $1,049.00 SSK | $1,599.00 WOT | $550.00 (52%) |
| RIVER2PRO-160-1-US | EcoFlow RIVER 2 Pro | $569.00 WOT | $899.00 SSK | $330.00 (58%) |
| P-AC240P-US-GY-BL-010 | Bluetti AC240P | $1,539.00 SSK | $1,799.00 WOT | $260.00 (17%) |
| EFDELTAPRO3-US | EcoFlow DELTA Pro 3 | $2,799.00 both | — | identical |
| EFRIVER3-US | EcoFlow RIVER 3 | $199.00 both | — | identical |

### About and disclosure

Modelled on gaia's tone (read read-only as reference), written from Helios's
own data. Every count and name comes from `build.site_facts()`, so the prose
cannot go stale against the catalog. Deliberately absent: traffic numbers,
"trusted by", founder biography, years of experience, endorsements, and any
refresh cadence claim the workflow does not implement.

The load-bearing honest statements:

- **Cadence.** "Scrapes are run on demand, not on a fixed schedule." That is
  the truth — `.github/workflows/scrape.yml` is `workflow_dispatch` only,
  with no cron. A test asserts the strings "updated daily" and "hourly" do
  not appear on any page.
- **Affiliate status.** `facts.affiliate_live` is **computed** from whether
  any retailer has a non-empty `affiliate.link_template`. Every template is
  empty, so the page says *"Helios earns nothing today"* and lists every
  retailer as "Not joined". A test proves the sentence flips to the
  commission wording the moment a template is populated — the disclosure
  corrects itself rather than needing to be remembered.
- **Commission rates are not published.** retailers.json carries researched
  rates ("6%", "5-10%") explicitly marked unverified. Printing an unverified
  number on the page whose whole purpose is trustworthiness would be a poor
  place to start. altE Store is named as having no affiliate relationship at
  all, and it ranks #4 on the rack guide on the merits.

### SEO and sitemap

- Per-page `<title>`, `<meta name="description">` and `<link rel="canonical">`
  are computed in `build.py` (not hand-written in templates), derived from
  live counts. Example, panel guide: *"1 of 3 tracked solar panels ranked by
  $/W..."* on the fixture catalog; *"3 of 5..."* on the real one.
- Descriptions are clipped to 155 chars on a word boundary.
- `site/sitemap.xml` is generated from a list accumulated **at page-write
  time**, so it structurally cannot list an unrendered page or omit a
  rendered one. 39 URLs today: 1 home + 33 products + 3 guides + 2 info.
  `lastmod` comes from the injected clock.
- `SITE_BASE_URL` duplicates the `homepage` in `ucp-agent-profile.json`
  rather than reading it (build.py must not depend on an artifact it
  publishes); a test asserts the two agree so they cannot drift.

### Navigation and link depth

`base.html` gained a nav bar linking all three guides, About and the
disclosure, plus a disclosure link in the footer. Links now use a
`root_prefix` computed from each page's own depth instead of the previous
absolute `/index.html`. The old form would 404 on the published site, which
is a GitHub Pages **project** site served under `/Helios/`. This also makes
`file://` browsing work.

## 3. Design decisions worth an eyeball

1. **$/Wh, not $/kWh, on a page titled "cost per kWh".** The guide title
   targets how people search; the figures use the existing
   `format_dollars_per_wh`. Rendering a second `$/kWh` string would create a
   number that can visibly disagree with the first under rounding
   ($0.29/Wh vs $287.30/kWh), and it would not be covered by the audit's
   same-formatter string equality. The lede states the conversion in words
   with no baked-in numeral. **Reversible if you'd rather show $/kWh.**

2. **Pallet variants get no $/W, on a page called "solar panel pallets".**
   `8 Solar Panels` × 410 W is arithmetic anyone can do, but deriving it
   means trusting a panel count read off a merchant's variant label — and
   EXPANSION_LOG **E8** proves that exact trust is misplaced here
   (wild-oak-trail's `RS-M410-*` SKUs are shifted one step against its own
   labels). The classifier already calls these bundles. They are listed with
   their real pallet prices and no rating. The page says so in its first
   paragraph. **This is the biggest tension between the requested title and
   the data; flagging it rather than quietly resolving it.**

3. **Rank comes from the best *unit* rating, never a cheaper bundle.** A
   product whose cheapest variant is a discounted kit ranks on its standalone
   unit. The headline price shown beside the rating is that same variant's
   price — same-variant discipline (red team #2 MAJOR-1) extended to guides.
   Regression-tested with a fixture where a $900 kit undercuts a $1,400 unit.

4. **Same-SKU conflict detection is semantic, not string equality.** My first
   implementation flagged any wording difference between two retailers'
   labels for one SKU. On real data that fired on **all five** spreads —
   "DELTA MAX [Unit Only]" vs "EcoFlow DELTA Max Portable Power Station(Main
   Unit ONLY)" is the same item worded differently. A scary warning on every
   row is noise that buries the one case that matters. Replaced with two real
   signals: **quantity disagreement** (`_quantity_tokens`: "10 Solar Panels"
   vs "12 Solar Panels" → {10} vs {12}, which is precisely E8) and
   **classification disagreement** (unit at one retailer, bundle at the
   other). On the current data this fires **zero** times, correctly. Both
   signals are unit-tested, including the E8 shape.

5. **Per-retailer spec provenance, surfaced.** EXPANSION_LOG §8.6 flagged
   that `capacity_source` is prose and nothing re-validates a claim against
   *that specific* retailer. Every rating now carries
   `data-spec-provenance`: `quoted` (this retailer's own listing is quoted
   verbatim), `cross-retailer` (quoted, but from a different retailer), or
   `unquoted`. `output_w` has no `capacity_quotes`-style field, so **every
   $/W is `unquoted`** and the panel guide says so in prose above the table
   and in its methodology footer. That is the "state values conservatively"
   instruction implemented as a machine-readable attribute rather than a
   disclaimer.

6. **`pages_written` kept its old meaning** (home + product pages = 34), with
   `guide_pages`, `info_pages`, `total_pages_written` and `sitemap_urls`
   added alongside. This left every existing test in `test_build.py`
   untouched rather than editing assertions to accommodate new work.

## 4. Files

Created:

| file | lines |
|---|---|
| `templates/guide.html` | 254 |
| `templates/about.html` | 134 |
| `templates/disclosure.html` | 100 |
| `tests/test_guides.py` | 792 |
| `docs/CONTENT_LOG.md` | 397 |

Modified:

| file | lines (was) | change |
|---|---|---|
| `build.py` | 1271 (589) | guides engine, site_facts, sitemap, SEO, formatters |
| `templates/base.html` | 74 (41) | nav, meta/canonical, root_prefix, content-layer CSS |
| `templates/home.html` | 47 (45) | guide links, root_prefix, title block removed |
| `templates/product.html` | 59 (60) | title block removed (now computed in build.py) |

Generated (committed output, per gaia convention): `site/about.html`,
`site/disclosure.html`, `site/guides/*.html` (3), `site/sitemap.xml`, plus
all 34 existing pages rebuilt with the new nav/head.

**No commits were made. `docs/PLAN.md` was not touched.**

## 5. Verification

```
python -X utf8 -m ruff check .        -> All checks passed!          exit 0
python -X utf8 -m pytest -q           -> 242 passed                  exit 0
python -X utf8 build.py               -> 34 pages + 3 guides + 2 info
                                         sitemap 39 URLs             exit 0
```

200 tests before this task, 242 after: **42 added, none modified, none
weakened, none skipped.**

### Independent re-derivation of every rendered number

A throwaway checker (scratch, not committed) parsed all three built guides
with `html.parser` — reading `data-*` attributes, never prose regex — and
re-derived every value straight from `data/prices/*.jsonl`:

```
rows checked: 153
rated rows:   28
failures:     0
```

It asserted, per row: `data-sku` equals the stored SKU; `data-scraped-at`
equals the row timestamp; the bundle badge matches `classify_variant`; the
displayed price equals `money(stored_price)`; a rating exists **iff** the
§2b rule permits one and equals the independently recomputed string; no
withheld row carries a rating; every outbound link is
`rel="nofollow sponsored"`. It also cross-checked that every guide $/Wh
string is byte-identical to the product page's $/Wh for the same variant.

### Hand-checks

- `3399.99 / 16000 = 0.21249…` → renders `$0.21/Wh` ✓
- `1470.99 / 5120 = 0.28730…` → renders `$0.29/Wh` ✓
- `1254.00 / 2560 = 0.48984…` → renders `$0.49/Wh` ✓
- `191.99 / 200 = 0.95995` → renders `$0.96/W` ✓
- `259.88 / 200 = 1.2994` → renders `$1.30/W` ✓
- `1599.00 − 1049.00 = 550.00`; `550/1049 = 52.4%` → renders `$550.00 (52%)` ✓
- MEGA 410 page and guide: **zero** `/W` strings on any pallet variant ✓
- `rich-solar-alpha-12v-200ah` (the 25,600 Wh source conflict): still zero
  `$/Wh` strings anywhere ✓

### Regressions checked

- `audit.parse_provenance` still reads the rebuilt pages: `index.html` yields
  **64** provenance entries, exactly the 64 mapped pairs (EXPANSION_LOG E7);
  product pages parse with correct tier/sku/scraped-at/fields.
- No CRLF in any generated `.html` or `.xml`.
- Sitemap URL set == the set of `.html` files on disk, no duplicates.
- No `<script>`, external stylesheet, or remote `<img>` on any page (PLAN §2).

## 6. Test coverage added (42)

Rendering; ranking order; rank-from-unit-not-kit; **no bundle rated on any
guide** (checked across every rated row of all three guides via provenance
attributes, with a guard asserting the check saw rated rows at all); multipack
shows price + badge but no rating; **null `output_w` never yields $/W**;
pallet-only product listed but unranked; unknown capacity listed with reason;
stale withheld; quarantine withheld (and the product page agrees); unmapped
pair absent; guide rating == product-page rating for the same variant; every
outbound link nofollow sponsored; provenance on every price; spec-provenance
on every rating; spreads (found, two-retailer minimum, withheld excluded,
blank SKU excluded, wording difference NOT flagged, quantity conflict IS
flagged); **sitemap lists every rendered page exactly once** (set equality
against disk); sitemap contents, lastmod, LF; **disclosure linked from every
page**; nav links every guide from every page; unique titles + descriptions
+ canonicals; descriptions data-derived; no external assets; About counts
data-derived; About cadence honest; **no unverifiable popularity claims**
(banned-phrase sweep over every page); disclosure earns-nothing wording;
disclosure flips when a link template exists; `SITE_BASE_URL` matches the UCP
profile; `CONTACT_EMAIL` matches `polite.BOT_USER_AGENT`; slugs unique and
URL-safe; guide categories exist in the real catalog.

## 7. For the red team

1. **Guides are an unaudited surface.** `audit.py`'s render hop opens
   `index.html` and `products/*.html` only. A wrong number on a guide would
   **not** raise `RENDER_DEFECT`, would not quarantine, and would not fail
   the workflow. Mitigated structurally (shared assembly) and by tests, but
   not by the correctness loop. Extending `run_audit` to walk `site/guides/`
   is the obvious follow-up and was judged out of scope for a content task —
   it changes red-teamed audit behaviour and spends live requests.

2. **`audit.py` was not run at all.** The task was offline. The shipped
   `site/` has therefore not been through a freshness hop. Prior guidance in
   EXPANSION_LOG §7 is that the shipped site should be the audited site — run
   `python -X utf8 audit.py` before treating this build as publishable.

3. **`output_w` has no provenance and now drives published numbers.** Before
   this task `output_w` was decorative. It is now the divisor for every $/W
   on the panel guide. It is hand-authored, has no `capacity_quotes`
   equivalent, and `audit.check_capacity` does not cross-check it against
   live listings the way capacity is checked. The pages label it `unquoted`,
   but labelling is not verification. **A wrong `output_w` produces a wrong
   $/W that nothing in the repo would catch.**

4. **Two products display the same `$0.96/W` at ranks 1 and 2.** True values
   differ in the fifth decimal. Ordering is correct and the strings are
   honest roundings, but a reader sees identical numbers ranked differently.
   More decimals would imply precision the comparison does not carry; a tie
   notation might be better.

5. **Cross-cell variant mismatch is inherited, not solved.** EXPANSION_LOG
   §8.2 still stands. Guides rank on the *unit* variant, which removes the
   bundle-vs-unit class of mismatch, but a trim difference (LITE vs
   Self-Heating) at the same capacity still ranks as a price difference. The
   ALPHA products sit in `deep-cycle-battery`, which no guide covers today,
   so the exposure is latent rather than live.

6. **The spread join trusts merchant SKU strings.** It will *miss* real pairs
   (EcoFlow RIVER 2 Pro's main unit is `ZMR620-B-US` at one retailer and
   `ZMR620-B-US-1` at the other — no spread is reported). A miss is a gap; a
   false pairing would be a defect, so the join is deliberately strict. The
   quantity/kind conflict check is the guard against false pairings, and it
   fires zero times on current data — meaning it is **unexercised in
   production**, only in fixtures.

7. **`RIVER2PRO-160-1-US` shows a 58% spread between two bundle variants**
   ($569 WOT vs $899 SSK). Both are bundles, both labelled as such, and the
   conflict check passes. It is plausibly a genuine 58% gap on the same
   bundle, and it is plausibly two different panel counts. Worth one human
   look — this is the kind of row a reader will click.

8. **The `$/kWh` title vs `$/Wh` figures** decision (§3.1) and the **pallet
   title vs withheld $/W** decision (§3.2) are both judgement calls made
   against the letter of the guide briefs. Override either if the reasoning
   does not hold.

9. **Pre-existing inconsistency, not introduced here and not fixed here:**
   `site/.well-known/ucp-agent.json` tells merchants Helios reads catalog
   data "twice daily", while `scrape.yml` has no cron and the About page now
   states scrapes are on demand. The merchant-facing profile and the
   reader-facing page disagree. The About page is the accurate one. Changing
   a published UCP profile is a merchant-trust decision, not a drive-by edit.

10. **Currency is still unverified** (EXPANSION_LOG §8.4). Guides now rank
    across four retailers on the assumption every price is USD. Nothing in
    the `.json`+`.js` path checks it. Ranking amplifies this: a CAD price
    would not merely display wrong, it would win.


## 8. ERRATA — corrections after independent red team (content layer)

Red team returned **FAIL** on the content layer above: 3 HIGH, 4 MEDIUM,
3 LOW, plus a workflow gap raised in adjudication. The arithmetic held up
(183/183 figures recomputed clean, XSS-safe, withhold discipline intact) —
every finding was a **misleading-presentation** or **false-origin** class,
which is worse than an arithmetic bug because nothing recomputes it.

Corrections are recorded here rather than by editing sections 1-7, on the
same principle EXPANSION_LOG section 9 states: a record that rewrites its
own history is worth less than one that shows what it got wrong.

### H1 (HIGH) — the top two ranks were sold out. FIXED.

The power-station guide ranked **Anker SOLIX F2600 at $0.49/Wh** first and
**EcoFlow DELTA Max at $0.52/Wh** second. Both ranked variants were
`available: false` at the ranked retailer. The headline showed no
availability, the meta description advertised "Cheapest tracked is
$0.49/Wh" into search results, and ranking ignored stock entirely.

This is the same class as every other finding this project has taken
seriously: a number that is arithmetically correct and functionally a lie.
A ranking is a **buying recommendation**, and recommending something nobody
will sell you is not a rounding error.

Fix, in `guide_entry`:

- Only offers whose ranked variant has `available is not False` may set a
  rank. Unknown availability still ranks — unknown is not "no".
- Sold-out offers stay in the tables with their price, their rating and a
  visible `not ranked: sold out` marker. Removing them would destroy real
  information; ranking them was the error.
- A product whose every rateable offer is sold out is listed **unranked**
  with the real reason: *"the best price we can rate is currently sold out,
  so this product is not ranked today"*.
- Availability now renders beside every headline price, same-variant, the
  same discipline home cells already had.
- Meta descriptions derive from the available-ranked set, so the sold-out
  figure can no longer reach a search result.

Effect on the shipped site: the power-station guide went from **7/8 ranked
to 4/8**. Three products (Anker F2600, DELTA Max, AC240P) moved to
"tracked, but not ranked" because every rateable offer is sold out. The
guide got less impressive and more true.

### H3 (HIGH) — spreads advertised savings nobody could take. FIXED.

Spread blocks showed neither side's availability and were computed over
sold-out offers. The worst instance was **RIVER2PRO-160-1-US at 58%** —
built on a wild-oak-trail bundle that was sold out, i.e. an internally
incoherent row advertising the biggest saving on the page.

`same_sku_spreads` now excludes any offer with `available is False`, and
both sides render availability. Spreads on the shipped site dropped from
**5 to 2** (DELTA Max, RIVER 2 Pro and AC240P all had a sold-out side).

The two survivors are identical-price ties, which made the old heading
"Same SKU, different price" contradict its own content — renamed to
**"Same SKU at more than one retailer"**, which covers gaps and ties.

### H2 (HIGH) — 39 canonical tags and 39 sitemap URLs, all 404. FIXED.

`SITE_BASE_URL` hardcoded `https://projectgaiaa.github.io/Helios`. GitHub
Pages serves this repository's **root**, so the pages actually live under
`/Helios/site/...`, and the intended production origin is a future Vercel
deployment (commit d260d17). Every canonical and every sitemap URL pointed
at a dead address.

A canonical to a 404 is worse than no canonical: it instructs a crawler to
prefer a dead URL over the page it is reading. Withhold-when-unknown
applies to our own URLs too.

- The origin is now **optional configuration**: env `HELIOS_SITE_BASE_URL`,
  else `data/site_config.json` `{"site_base_url": ...}`. Blank counts as
  unset.
- **Unset (as now): no canonical tags and no `sitemap.xml` at all.** An
  existing `sitemap.xml` is deleted rather than left behind, because a
  stale file of 404s is exactly the harm. Both appear automatically at the
  first build that sets the origin.
- **My own bad test is withdrawn.** `test_site_base_url_matches_the_ucp_agent_profile`
  asserted build.py's constant equalled the profile's `homepage`. Both were
  wrong, so it locked two wrong values together and reported agreement as
  correctness. That is the E1 failure mode in test form: a green gate around
  an unverified claim. Replaced with a test that the profile homepage is the
  repo URL, which resolves today, plus a test that the two profile copies
  agree with each other.
- `ucp-agent-profile.json` and `site/.well-known/ucp-agent.json` now both
  carry `homepage: https://github.com/ProjectGaiaA/Helios` — to be swapped
  for the real domain post-deploy. This URL is fetched by **merchants**
  deciding whether to answer us, so it pointing at a 404 was a live
  reputational defect, not a cosmetic one.

### M4 (MEDIUM) — the unranked placard printed a false reason. FIXED.

`_unranked_reason` ended in an unconditional fall-through to *"every
variant on sale is a bundle or a multi-unit pack"*. Proven false with a
product whose capacity is known and whose variant is a standalone unit
with an unreadable price: the page confidently cited a rule that did not
apply. A placard that lies about its own rule is worse than no placard —
it is the withhold discipline pretending to work.

Every branch now states the reason that actually applies, including
distinct wording for all-quarantined, all-stale, all-unusable-price,
no-readable-unit-price, and sold-out-only. Regression test seeds the exact
poison shape and asserts the bundle sentence is absent.

### M5 (MEDIUM) — "$inf" / "$nan" rendered as prices. FIXED AT BOTH ENDS.

E9 closed non-finite numbers for `$/Wh` but not for the price itself, so a
JSON `Infinity`/`NaN` in a price field rendered as a dollar amount on
product, home AND guide pages.

- **Scraper (`scrapers/shopify.py`)**: `price <= 0` does not stop NaN or
  Infinity (`nan <= 0` is False, `inf > 0` is True). Both price and
  `compare_at_price` now require `math.isfinite`. Nothing non-finite can
  enter the store.
- **Builder (`build.py`)**: `money()` refuses anything not finite and
  returns `""`, never `"$inf"`. New `price_display()` makes ONE predicate
  (`_usable_number`) govern both whether a price renders and whether a
  rating derives from it — a number too broken to divide by is too broken
  to print.
- A stored non-finite/zero/negative price now renders as a withheld cell
  with its own marker `data-withheld="price_unreadable"`, and `audit.py`
  verifies the marker is **justified** (claiming it over a perfectly good
  price is itself a RENDER_DEFECT).
- The home table's cheapest-variant selection now filters on
  `_usable_number`. This mattered independently: `min()` over a list
  containing NaN returns whatever happens to come first, so an unfiltered
  NaN could silently become "the cheapest".

### M6 (MEDIUM) — the disclosure asserted things it could not evidence. FIXED.

Two false-origin claims on the page whose entire job is modelling the
discipline:

- *"Programme exists"* was printed from a non-null affiliate record whose
  own `notes` say *"Program terms unverified"*. A record of research is not
  evidence of a programme.
- *"None found" / "No commercial relationship possible"* was printed from a
  `null` record. **Absence of a record is not evidence of absence.**

Rewritten to claim only what the repo holds: unverified terms are labelled
*"recorded but unverified ... not evidence that a programme exists"*, and a
null record reads *"We hold no information about this retailer's affiliate
arrangements"*, with an explicit note that this is an absence of
information. `site_facts` computes a `verified` flag from the record's own
notes, so the hedge is data-driven — a counter-test proves it stops hedging
when the notes no longer say "unverified".

### M7 + L8 (MEDIUM/LOW) — unexplained cross-SKU juxtaposition. FIXED.

The per-product guide table merges all retailers, and sorting by price put
wild-oak-trail's mislabelled **"12 Solar Panels" ($2,719.99 — actually the
10-pack per E8)** directly beside shop-solar-kits' real 12-pack
($3,699.99): an apparent 36% saving that the product's **own `notes` field
already refutes**. Separately, the 24V RS-M200D rendered under the 12V MEGA
200 with nothing distinguishing them.

The identity-conflict detector existed but only ran when the spreads flag
was set — so it never looked at the tables where readers actually compare
rows. Three fixes:

1. `_sku_conflicts` runs on **every** guide table regardless of the spreads
   flag. A conflict is a property of the data, not of whether the page
   happens to draw a spread block.
2. `_dominant_skus`: a row whose SKU no other retailer carries gets a
   visible annotation naming the SKU and the retailer, so it cannot pass as
   a like-for-like counterpart. Suppressed for single-retailer products,
   where there is no agreement to measure and the note would be pure noise.
3. `_note_flagged_retailers`: a `DATA-QUALITY WARNING` recorded in
   `products.json` `notes` against a named retailer now renders on that
   retailer's rows, quoting the note. E8's warning had been sitting in a
   JSON field reaching nobody while the affected rows rendered unannotated.

### L9 + weakness-1 (LOW) — guides were invisible to the audit. FIXED.

The render hop opened `index.html` and `products/*.html` only, so a wrong
figure on a ranked buying guide — the page most likely to be acted on —
could not produce a RENDER_DEFECT. My own section 7.1 flagged this and
deferred it; the red team was right that it is closable now.

- Guide rated figures use `data-field="wh"` for $/Wh — the **same name**
  the product and home pages use — so audit.py verifies them with the
  identical comparison. $/W uses `"watt"` because it is a different
  quantity, and expecting a $/W from a power station (which has an
  `output_w` but is ranked on $/Wh) would manufacture a mismatch per row.
  `guide_for_product()` decides which applies.
- Guide rows gained `data-retailer-id` and `data-product-id` so the join is
  explicit rather than inferred from document position.
- `audit.parse_guide_provenance` + `check_guide_render` compare price,
  rated figure, availability and the provenance attributes against the
  store. Wired into both the normal path and the **shadow rebuild**, so a
  quarantine entry cannot clear while a guide would still render wrong.
- **Zero extra live requests**: guides share their freshness with the rows
  behind them, so this is a pure render-hop check. Test asserts the request
  count is unchanged.
- Six new tests, including three tamper tests (price, rating, availability)
  that must each produce RENDER_DEFECT + quarantine, and an untampered
  counter-case that must stay CLEAN.

### L10 (LOW) — tie notation. FIXED.

MEGA 200 ($0.95995/W) and MEGA 250 ($0.95996/W) both render `$0.96/W` at
ranks 1 and 2, with nothing explaining the order. Adjacent ranks whose
displayed strings match now carry a `tie` badge and a line stating the
ordering uses the unrounded values. More decimals were rejected: they would
imply a precision the comparison does not carry.

### W9 — automation never refreshed half the retailers. FIXED.

`scrape.yml` had one hardcoded step per retailer, naming only
shop-solar-kits and wild-oak-trail. **rich-solar and alte-store were
activated in the catalog expansion and were never scraped by automation** —
including the sources of live guide headlines (MidNite MNPowerFlo16 at altE
Store, every Rich Solar panel price). Their data would have sat unrefreshed
until it crossed 168h and silently vanished from the site.

Replaced with a single step running the runner's all-active mode, so
`data/retailers.json` is the one source of truth: activating a retailer
enrolls it in automation with no workflow edit. The alarm step now reads
`degraded_retailers` out of the manifest, which catches a retailer
silently returning zero products — per-retailer visibility the old
per-step `outcome` checks only appeared to provide.

Also: both UCP profile copies now say **"up to twice daily"** rather than
"twice daily", which is true under dispatch-only today and under a cron
later. (Section 7.9 flagged this disagreement and left it; it is now fixed
at the wording level, and the About page remains the fuller statement.)

### Ranked top-3 as re-rendered

**Server-rack & wall-mount ($/Wh)** — 5 of 6 ranked, unchanged by this round

| # | product | $/Wh | price | retailer | stock |
|---|---|---|---|---|---|
| 1 | EG4 Indoor WallMount 48V 314Ah 16kWh | $0.21/Wh | $3,399.99 | Shop Solar Kits | in stock |
| 2 | EG4 LifePower4 V2 48V 100Ah | $0.29/Wh | $1,470.99 | Shop Solar Kits | in stock |
| 3 | EG4 LL-S 48V 100Ah | $0.30/Wh | $1,536.99 | Shop Solar Kits | in stock |

**Portable power stations ($/Wh)** — 4 of 8 ranked (was 7 of 8)

| # | product | $/Wh | price | retailer | stock |
|---|---|---|---|---|---|
| 1 | EcoFlow DELTA Pro 3 | $0.68/Wh | $2,799.00 | Shop Solar Kits | in stock |
| 2 | EcoFlow RIVER 2 Pro | $0.74/Wh | $569.00 | Shop Solar Kits | in stock |
| 3 | Bluetti AC200L | $0.78/Wh | $1,599.00 | Wild Oak Trail | in stock |

Unranked: Anker SOLIX F2600, EcoFlow DELTA Max, Bluetti AC240P (every
rateable offer sold out); Bluetti AC180 (capacity not established).

**Solar panels ($/W)** — 3 of 5 ranked, both leaders now marked as a tie

| # | product | $/W | price | retailer | stock |
|---|---|---|---|---|---|
| 1 | Rich Solar MEGA 200 200W 12V (tie) | $0.96/W | $191.99 | Wild Oak Trail | in stock |
| 2 | Rich Solar MEGA 250 250W 12V (tie) | $0.96/W | $239.99 | Wild Oak Trail | in stock |
| 3 | Rich Solar MEGA 200 Briefcase 200W | $1.30/W | $259.88 | Shop Solar Kits | in stock |

### Verification after the errata fixes

```
python -X utf8 -m ruff check .   -> All checks passed!               exit 0
python -X utf8 -m pytest -q      -> 280 passed                       exit 0
python -X utf8 build.py          -> 34 pages; 5/6, 4/8, 3/5 ranked
                                    sitemap NOT written (no origin)  exit 0
```

242 tests before this round, **280 after: 38 added, none weakened**. The
two tests that changed are recorded above: the SITE_BASE_URL/profile test
was withdrawn as unsound, and the disclosure status assertions were
rewritten against the corrected wording.

Independent re-derivation, re-run against the rebuilt site:

```
own harness      -> rows checked: 147   rated: 28   failures: 0
red team probe   -> figures: 171   rated_rows: 28   problems: 0
  (probe adapted to the NEW contract: availability filter added to its
   recomputation, rated field read as wh/watt. Unadapted, it reports the
   pre-fix ranking as "CRITICAL" — it encodes the contract H1/L9 changed.)
link probe       -> 39 pages, 411 internal links, 0 broken,
                    0 root-absolute, nav/footer complete
```

### Still open after this round

- **`output_w` still has no provenance** and still drives every $/W. The
  pages label it `unquoted`, but labelling is not verification and
  `audit.check_capacity` does not cross-check it against live listings.
- **The identity-conflict detector fires zero times on current data.** It
  is exercised only by fixtures. Its real target (E8's MEGA 410) is caught
  by the notes-warning path instead, because wild-oak-trail's shifted SKU
  sits on a product whose rows are all unranked bundles.
- **Currency is still unverified** across four retailers. Ranking amplifies
  it: a CAD price would not merely display wrong, it would win.
- **The site still has no public origin**, so it has no canonical tags and
  no sitemap. That is correct today and must be revisited at deploy — the
  one-line fix is `data/site_config.json`.
- **`audit.py` has still not been run** against this build (offline task).
  The guide render hop is now implemented and unit-tested, but the shipped
  site has had no freshness hop.
- Sold-out ranking exclusion uses the LAST SCRAPE's availability. Stock
  moves faster than price, so a product can be ranked and gone, or unranked
  and back. The page says so; nothing can fix it short of checking at
  request time, which a static site cannot do.


## 9. ERRATA round 2 — defects introduced by the round-1 errata fixes

The delta re-verify VERIFIED 9/11 claims and both structural upgrades, and
adjudicated the probe dispute SOUND. It also found that **my own round-1
fixes introduced two new blockers**. Both are recorded here in full: fixes
that create defects are the most expensive kind, because they arrive
wearing the credibility of a fix.

### R2-B1 (HIGH) — the guide audit fired on correct pages. FIXED.

The guide render hop I added in L9 raised **4 RENDER_DEFECT and exit 3 on
the completely clean tree**, quarantining shop-solar-kits and
wild-oak-trail offers of `ecoflow-delta-pro-3` and `ecoflow-river-3` — two
of the four ranked power stations. The withhold mechanism was firing on
healthy data, which is strictly worse than not checking at all: it would
have emptied correct cells and failed every workflow run.

Cause: a guide renders one variant up to **three** times — the headline
span, its row in the product's table, and its row in the spreads table —
and the spreads table has **no rating column by design**.
`parse_provenance` keys by variant_id and keeps the LAST occurrence, so the
ratingless spreads row overwrote the rated one and the audit read the
rating as `absent` against an expected `$0.68/Wh`.

My `parse_guide_provenance` docstring reasoned explicitly about duplication
*across* guides and never considered duplication *within* one page. The
reasoning was confident, wrote itself down, and was addressing the wrong
axis.

Fix:

- `parse_provenance_list()` returns every identified record on a page,
  duplicates included (`_ProvenanceParser.all_records`). `parse_provenance`
  is untouched, so product/home behaviour is unchanged.
- `merge_guide_records()` folds a variant's appearances on one page:
  a field present anywhere counts as present, so **absence of a rating in a
  context that has no rating column is not a defect**.
- Where two appearances disagree about the same field or attribute, that is
  an internal contradiction on one page and IS reported
  (`internal-conflict`) — strictly stronger than either occurrence alone,
  and stronger than the pre-blocker behaviour.

Acceptance, on the clean tree with the red team's own `offline_audit.py`:

```
before: verdicts {NO_BASELINE:26, CLEAN:30, STALE:116, UNRESOLVED:7,
                  RENDER_DEFECT:4}   alarms 4   quarantine 4   exit 3
after:  verdicts {NO_BASELINE:26, CLEAN:31, STALE:119, UNRESOLVED:7}
        alarms 0   quarantine 0      exit 4
```

**On the residual exit 4, with evidence rather than an excuse.** It is not
the render hop; it is two bugs in the driver's canned live source:

1. It keys canned data by **handle alone**. Four handles in this catalog
   are shared by two retailers each
   (`bluetti-ac240p`, `ecoflow-smart-generator-4000-dual-fuel`,
   `ecoflow-delta-pro-3-smart-extra-battery`,
   `40-amp-mppt-solar-charge-controller`), so those pairs are served
   another retailer's variant set -> variant absent -> the 7 UNRESOLVED.
   `verified 176 / attempted 183` differs by exactly those 7, and PLAN 4c
   makes `verified < attempted` exit 4 by design.
2. It hardcodes `compare_at_cents: None` while 131 stored variants carry a
   `was_price` -> the 119 STALE.

Re-running with only those two driver bugs corrected
(`scratchpad/offline_audit_v2.py`, identical in every other respect):

```
verdicts: {NO_BASELINE: 30, CLEAN: 153}
verified/attempted: 183 183      alarms: 0      exit: 0
```

Zero RENDER_DEFECT, zero STALE, zero UNRESOLVED, **exit 0** — the stated
acceptance, once the harness stops mis-keying its own fixture. Flagging the
disagreement rather than quietly reporting exit 4 as a pass.

Regression tests: a variant appearing in **both a ranked and a spreads
table** keeps its rating through the merge (test_guides, using the SKU-A
spread); the clean fixture raises no RENDER_DEFECT; a tampered ranked-table
rating is still a defect (merging must not launder one); and two copies of
one variant that disagree are reported as `internal-conflict`.

### R2-B2 (HIGH) — false fault attribution against named retailers. FIXED.

My M7 fix attributed data-quality notes by bare substring
(`rid in notes`). The shipped panel guide therefore printed **"Data-quality
note recorded against Shop Solar Kits"** and **"...against Rich Solar"**,
ten annotations in total, sourced from a note whose own text says
wild-oak-trail is the shifted one and that *"shop-solar-kits and rich-solar
both put RS-M410-10 on a 10-panel pack"* — i.e. the note **exonerates** the
two retailers the page accused.

Prose about a data-quality problem names the parties it clears as often as
the one at fault. Substring matching cannot tell "at fault" from
"mentioned", so it converted an exoneration into an accusation against
named businesses. That is the worst thing this content layer has produced:
every other defect misstated a number, this one misstated a fact about
someone else.

Fix: **structured attribution, and prose is never parsed for blame.**

- `products.json` gains `notes_by_retailer: {retailer_id: what is wrong
  with THAT retailer's data}`. The E8 warning moved into it under
  `wild-oak-trail` only. The general `notes` prose is retained as the
  operator record but no longer drives rendering.
- `_note_flagged_retailers(product)` reads only the structured map. With no
  map, nothing is attributed: a missed warning is a gap, a misattributed
  one is a false statement about a business, so silence is the correct
  failure mode.
- The catalog edit is surgical — `ensure_ascii=False` on the rewrite, so
  the verbatim `capacity_quotes` bytes (`•`, `®`) were not silently
  rewritten into `\uXXXX` escapes. A first attempt did rewrite them and was
  reverted; those fields are E1 territory.

Shipped page: **10 annotations -> 3, all naming Wild Oak Trail.**

Tests: the required multi-retailer shape (one note naming two retailers,
one at fault, asserting the innocent one is *not* accused); prose-only
notes attribute nobody; and a direct assertion against the real catalog
that `rich-solar-mega-410` flags `{wild-oak-trail}` and neither of the
others.

### R2-M3 (MEDIUM) — CI timeout below the real worst case. FIXED.

Consolidating the scrape step to all active retailers took it from 8 mapped
pairs to 64. Worst case is `64 pairs x 2 requests x 15s = 32 minutes`,
against a `timeout-minutes: 30` cap — so a slow-but-healthy run would have
been killed mid-scrape and reported as a crash. The comment still claimed
"well under 10 minutes", arithmetic from the skeleton phase.

`timeout-minutes: 30 -> 60`, `CI_TIMEOUT_SECONDS: 30*60 -> 60*60` (PLAN
requires they match), and the comment now shows the arithmetic it is
derived from.

### R2-L (LOW) — two one-liners

- **Raw spec rendering.** `home.html` printed `{{ specs.capacity_wh }} Wh`
  directly, so a bool `true` in the field rendered **"True Wh"** — the hole
  `_usable_number` closes everywhere else. Added `spec_display()` and
  routed home rows through it. `product.html` had the identical exposure on
  both `capacity_wh` and `output_w`, including a `{% if not
  specs.capacity_wh %}` guard that a bool would have silently satisfied;
  fixed in the same pass rather than left as a known twin.
- **Misleading test name.** `test_ucp_profile_homepage_is_a_url_that_actually_resolves`
  makes no network request and cannot show anything resolves. Renamed to
  `test_ucp_profile_homepage_is_pinned_to_the_repo_url`, with the docstring
  stating that it pins a constant and the URL was checked by hand once.

### Verification after round 2

```
python -X utf8 -m ruff check .   -> All checks passed!            exit 0
python -X utf8 -m pytest -q      -> 289 passed                    exit 0
python -X utf8 build.py          -> 34 pages; 5/6, 4/8, 3/5 ranked
                                    sitemap not written           exit 0

offline_audit.py (red team, unmodified, clean tree)
    verdicts {NO_BASELINE:26, CLEAN:31, STALE:119, UNRESOLVED:7}
    RENDER_DEFECT 0   alarms 0   quarantine 0   exit 4 (driver-side, above)
offline_audit_v2.py (driver's own two bugs fixed)
    verdicts {NO_BASELINE:30, CLEAN:153}  183/183  alarms 0   exit 0
recompute_v3.py   figures=183  rated_rows=28  problems=0
links.py          39 pages, 411 internal links, 0 broken, 0 root-absolute
own harness       rows 147, rated 28, failures 0
```

280 tests before this round, **289 after: 9 added, 2 rewritten** (the
round-1 note test now uses the structured field; the profile test renamed).
No test weakened.

Ranked top-3 unchanged from the round-1 errata tables.

### Still open after round 2

- Everything in section 8's "Still open" list stands.
- `notes_by_retailer` exists on exactly one product. Nothing validates that
  a retailer named in it actually carries the product, and nothing
  cross-checks the structured entry against the prose `notes` it was
  derived from — the two can now drift.
- The `internal-conflict` check is exercised only by fixtures; the real site
  renders no contradictory duplicate today.
- `offline_audit.py`'s handle-collision bug is in the red team's harness,
  not the repo, so it is not fixed here — but it means any future run of
  that driver will keep reporting 7 UNRESOLVED and exit 4 on a healthy site.

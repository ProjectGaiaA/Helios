"""
Static Site Builder — renders home + product pages from the price store.

Deliberately minimal (the plant tracker's 91KB build.py is NOT ported).
Loads products.json + retailers.json + data/prices/*.jsonl, takes the
latest row per product x retailer, and renders templates/ into site/.

$/Wh discipline (PLAN section 2b — withhold-when-unknown):
- computed ONLY when the product's specs.capacity_wh is non-null AND the
  variant is classified "unit" (not "bundle").
- Bundles render with price + a "bundle" badge and NO $/Wh: a kit's price
  divided by only the battery's capacity is wrong by construction
  (verified on real DELTA Max data, C15).

Usage:
    python -X utf8 build.py
"""

import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
PRICES_DIR = DATA_DIR / "prices"
TEMPLATES_DIR = BASE_DIR / "templates"
SITE_DIR = BASE_DIR / "site"

# ---------------------------------------------------------------------------
# Site identity (SEO surface)
# ---------------------------------------------------------------------------
# The public origin is CONFIGURATION, not a constant, and it is UNSET by
# default. Helios is not deployed: GitHub Pages serves this repository's
# root (so the pages would live under /Helios/site/, not /Helios/), and the
# intended production origin is a future Vercel deployment (commit d260d17).
# A hardcoded origin therefore produced 39 canonical tags and 39 sitemap
# URLs that all 404.
#
# Withhold-when-unknown applies to our own URLs too. With no origin
# configured, build.py emits NO <link rel="canonical"> and writes NO
# sitemap.xml: a canonical pointing at a 404 actively instructs a crawler
# to prefer a dead URL over the page it is reading, which is worse than
# saying nothing. Both appear automatically at the first deploy that sets
# the origin.
#
# Set via env HELIOS_SITE_BASE_URL, or data/site_config.json:
#     {"site_base_url": "https://helios.example"}
SITE_CONFIG_FILENAME = "site_config.json"
SITE_BASE_URL_ENV = "HELIOS_SITE_BASE_URL"
SITE_NAME = "Helios"
# The same contact advertised to every retailer in polite.BOT_USER_AGENT.
# Publishing a different one on the About page than the one in the bot UA
# would make the bot unidentifiable to the people it scrapes; a test pins
# them together.
CONTACT_EMAIL = "brandon.william.hall@gmail.com"
CONTACT_REPO = "https://github.com/ProjectGaiaA/Helios"

# Search engines truncate around 155-160 characters. Longer text is not an
# error, it is just invisible, so descriptions are clipped on a word
# boundary rather than mid-word.
META_DESCRIPTION_MAX = 155

# ---------------------------------------------------------------------------
# Variant classification (PLAN section 2b, extended per red team #2)
# ---------------------------------------------------------------------------
# `bundle` if the raw title matches this pattern OR contains a second
# wattage token; else `unit`. Conservative: unknown -> bundle (withhold).
#
# The v2 plan regex (kit|bundle|\+|\bwith\b|panel-x|x-N-w) treated absence
# of a bundle signal as evidence of unit: "AC200L & D40", "AC180 w/ 200W
# Solar Panel", "and"-joined titles and N-Pack forms all slipped through
# and would have rendered a bundle price as a per-unit $/Wh. Red team #2
# proved 7/13 adversarial titles misclassified, so the signal list grew:
# &, w/, and, N-Pack(s), expansion, extra/spare battery. Multi-pack IS a
# bundle here — the capacity multiplier is unknown, and unknown withholds.
# CHANGED 2026-08-13 (red team #5). Two corrections, both evidenced:
#
# (1) The bare `\band\b` signal was a FALSE POSITIVE on descriptive titles.
#     "ALPHA 4 - Self Heating and Bluetooth" (a single 24V/100Ah battery)
#     classified as a bundle, so its honest $0.35/Wh was suppressed and the
#     home cell fell back to a DIFFERENT trim, rendering a 44% cross-retailer
#     gap where the same-SKU truth is 11%. The signal is now qualified: "and"
#     only counts when the token after it CONTAINS A DIGIT ("AC200L and D40",
#     "DELTA and 220W Panel"). That keeps every red team #2 "and"-joined case
#     while dropping prose. (Red team #5 asked for the bare signal to be
#     deleted outright; qualifying it preserves red team #2's MAJOR-2 catch
#     instead of trading one regression for another. Both cases are tested.)
#
# (2) The multi-pack rule did not implement itself. PLAN 2b says "Multi-pack
#     = bundle (capacity multiplier unknown -> withhold)", but the only
#     signal was the literal word "pack". "2 Batteries Only", "8 Solar
#     Panels", "Set of 4", "x2" all read as `unit` -- and EG4 LL-S really did
#     render $0.60/Wh and $0.90/Wh for 2- and 3-battery packs whose true
#     figure is $0.30/Wh. Quantity forms are now enumerated.
_BUNDLE_RE = re.compile(
    r'(kit|bundle|\+|&|\bw/|\bwith\b|'
    r'\band\s+\S*\d|'                                  # "and" + digit token
    r'\d+\s*-?\s*packs?\b|'
    r'expansion|extra\s+batter|spare\s+batter|'
    r'panel(s)?\b.*\bx\b|x\s*\d+\s*w|'
    # --- quantity forms (red team #5) ---
    r'\b\d+\s*x\b|\bx\s*\d+\b|'                        # "2x", "x 2"
    r'\b\d+\s+(?:\w+\s+)?'                             # "8 Solar Panels",
    r'(?:batteries|panels|modules|units|cells)\b|'     # "2 Batteries", "12 Panels"
    r'\bpair\s+of\b|\btwin\b|\bset\s+of\s+\d+\b|'
    # "Dual Battery" is a quantity; "Dual Fuel" is a fuel type, not a pack.
    r'\bdual\b(?!\s+fuel\b))',
    re.IGNORECASE,
)
# A wattage token is watts, not watt-hours: "200W" / "2.4kW" yes, "2016Wh"
# no (the \b after w fails when an h follows). Two of them in one title
# means a station plus a panel — a bundle even without a "+" or "kit".
_WATTAGE_TOKEN_RE = re.compile(r'\b\d+(?:[.,]\d+)*\s*k?w\b', re.IGNORECASE)


def classify_variant(raw_variant: str, product_title: str = "") -> str:
    """Classify a variant title as "unit" or "bundle".

    Shopify's placeholder "Default Title" carries no information, so the
    product title stands in for it — that is where "...Kit" lives for
    single-variant kit products. No usable text at all is UNKNOWN, and
    unknown must withhold, so it classifies as bundle.
    """
    text = (raw_variant or "").strip()
    if not text or text.lower() == "default title":
        text = (product_title or "").strip()
    if not text:
        return "bundle"
    if _BUNDLE_RE.search(text):
        return "bundle"
    if len(_WATTAGE_TOKEN_RE.findall(text)) >= 2:
        return "bundle"
    return "unit"


def _usable_number(value) -> bool:
    """A real, finite, positive number.

    bool is excluded deliberately (it is an int subclass, and True would
    otherwise sail through as the number 1). NaN/Inf are excluded because
    the ordinary `<= 0` guard does NOT stop them: `nan <= 0` is False and
    `inf > 0` is True, so JSON `NaN`/`Infinity` in a price or a capacity
    would have produced "$nan/Wh" or "$inf/Wh" on the page (red team #5).
    """
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def dollars_per_wh(price, capacity_wh, classification: str):
    """$/Wh, or None when it must be withheld (PLAN section 2b).

    None when: the variant is not a unit, capacity is unknown/non-positive,
    or the price is unusable. Never guess.
    """
    if classification != "unit":
        return None
    if not _usable_number(capacity_wh) or not _usable_number(price):
        return None
    return price / capacity_wh


def money(value) -> str:
    """Two-decimal money formatting: 1408.1 -> "$1,408.10".

    Refuses anything that is not a real, finite number, returning "" —
    never "$inf" or "$nan". E9 closed the non-finite hole for $/Wh but
    not for the price itself, so a JSON `Infinity` or `NaN` in a price
    field still rendered as a dollar amount on the product, home AND
    guide pages. bool is excluded for the same reason as _usable_number.
    """
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value)):
        return ""
    return f"${value:,.2f}"


def price_display(value) -> str:
    """The price string to render, or "" when nothing safe can be shown.

    ONE predicate decides both whether a price renders and whether a
    rating may derive from it (_usable_number): a number too broken to
    divide by is too broken to print. Without this the page could show a
    confident "$0.00" or "$-500.00" beside a withheld rating and give two
    contradictory accounts of the same variant.
    """
    return money(value) if _usable_number(value) else ""


def format_dollars_per_wh(value) -> str:
    return f"${value:,.2f}/Wh"


def format_dollars_per_watt(value) -> str:
    """$/W for panels — same two-decimal shape as $/Wh, different unit.

    Watts are NOT watt-hours: this rates a panel's purchase price against
    its nameplate output, never against stored energy.
    """
    return f"${value:,.2f}/W"


def format_percent(value) -> str:
    """Whole-percent gap between two observed prices.

    Deliberately coarse: the two prices either side of it are the real
    data and are always shown, so extra decimals here would imply a
    precision the comparison does not carry.
    """
    return f"{value:,.0f}%"


def format_spec(value, unit: str) -> str:
    """"5120 Wh" / "410 W" — a catalog spec, trimmed of trailing zeros."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,} {unit}"


def spec_display(value, unit: str) -> str | None:
    """A catalog spec to render, or None when there is nothing safe to show.

    The templates used to test `{% if specs.capacity_wh %}` and print the
    raw value, so a bool `true` in the field rendered the literal string
    "True Wh" — the same class of hole `_usable_number` closes everywhere
    else. Routing every spec through the shared guard keeps the displayed
    spec and the rating that divides by it agreeing about whether the
    figure exists at all.
    """
    return format_spec(value, unit) if _usable_number(value) else None


def clip_text(text: str, limit: int = META_DESCRIPTION_MAX) -> str:
    """Collapse whitespace and clip on a word boundary for meta tags."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rsplit(" ", 1)[0].rstrip(",;:.-") + "..."


# ---------------------------------------------------------------------------
# Clock, staleness, quarantine (PLAN 4c.4)
# ---------------------------------------------------------------------------
# All ages and staleness derive from ONE injected clock (build_site(now=...)).
# A bare datetime.now() sprinkled through view assembly makes tests
# calendar-red: fixtures with absolute timestamps rot past the threshold
# the day the calendar moves. The default is real time; tests pin it.

STALE_MAX_HOURS = 168  # 7 days; boundary-tested at 167h/169h


def parse_iso(ts):
    """ISO timestamp -> aware datetime, or None when unparseable."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def age_hours(now, ts) -> float | None:
    dt = parse_iso(ts)
    if dt is None:
        return None
    return (now - dt).total_seconds() / 3600.0


def age_display(hours) -> str:
    """Visible provenance: "as of 3h ago" / "as of 2d ago" on every price."""
    if hours is None:
        return "as of unknown time"
    if hours < 1:
        return "as of <1h ago"
    if hours < 48:
        return f"as of {int(hours)}h ago"
    return f"as of {int(hours // 24)}d ago"


def is_stale(hours) -> bool:
    """Withhold-on-doubt: an unparseable timestamp is stale, not fresh."""
    return hours is None or hours > STALE_MAX_HOURS


def quarantine_key(retailer_id, product_id, variant_id) -> str:
    return f"{retailer_id}:{product_id}:{variant_id}"


def load_quarantine(data_dir: Path) -> dict:
    """data/quarantine.json — a keyed map (PLAN 4c.3). Missing file = {}.

    A malformed file raises: quarantine is the withhold mechanism, and
    silently ignoring a broken one would re-publish exactly the numbers
    an audit pulled off the page.
    """
    path = data_dir / "quarantine.json"
    if not path.exists():
        return {}
    quarantine = load_json(path)
    validate_quarantine(quarantine)
    return quarantine


def validate_quarantine(quarantine) -> None:
    """Shape-check the quarantine map; raise ValueError with a message.

    Keys are "{retailer_id}:{product_id}:{variant_id}" with EVERY part
    non-empty — an empty variant_id key like "r:p:" would match nothing
    or, worse, everything that also lost its id (red team #4, MAJOR-7).
    Validation runs BEFORE any live request in audit.py (MINOR-13).
    """
    if not isinstance(quarantine, dict):
        raise ValueError(
            f"quarantine must be a keyed map, got {type(quarantine).__name__}")
    for key, entry in quarantine.items():
        parts = key.split(":", 2) if isinstance(key, str) else []
        if len(parts) != 3 or not all(parts):
            raise ValueError(
                f"quarantine key must be 'retailer:product:variant_id' "
                f"with non-empty parts: {key!r}")
        if not isinstance(entry, dict):
            raise ValueError(f"quarantine entry for {key!r} must be an object")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_latest_prices(prices_dir: Path, product_ids: list[str]) -> dict:
    """{product_id: {retailer_id: latest_row}} from the JSONL price store.

    Rows are append-only and chronological, so the last row per retailer
    wins. Malformed lines are skipped, not fatal: one bad append must not
    take down the whole build.
    """
    latest: dict[str, dict[str, dict]] = {}
    for product_id in product_ids:
        path = prices_dir / f"{product_id}.jsonl"
        if not path.exists():
            continue
        per_retailer: dict[str, dict] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = row.get("retailer_id")
            if rid:
                per_retailer[rid] = row
        if per_retailer:
            latest[product_id] = per_retailer
    return latest


def filter_to_mapped_pairs(latest: dict, handle_maps: dict | None) -> dict:
    """Drop rows for (product, retailer) pairs the catalog no longer carries.

    handle_maps is the CARRIAGE CONTRACT: it says which retailer sells which
    product. Until now it governed only scraping, so un-mapping a pair
    stopped future scrapes but left its last stored price rendering on the
    site indefinitely — the operator's decision had no effect on what
    readers saw. That mattered the moment a carriage had to be withdrawn
    because its cell was misleading (red team #5): withdrawing it did
    nothing.

    `handle_maps is None` (no file) means "no contract recorded" and
    filters nothing, matching load_handle_maps()'s missing-file behaviour
    and keeping fixtures that predate the file working.
    """
    if handle_maps is None:
        return latest
    out = {}
    for product_id, by_retailer in latest.items():
        kept = {
            rid: row for rid, row in by_retailer.items()
            if product_id in (handle_maps.get(rid) or {})
        }
        if kept:
            out[product_id] = kept
    return out


def _strip_query(url: str) -> str:
    return (url or "").split("?")[0].split("#")[0]


def _buy_url(row_url: str, variant_id) -> str:
    """Affiliate deep link: retailer product URL + ?variant={id} when known.

    Built from the row's own product URL with any existing query stripped
    (the scraper's default URL already deep-links the cheapest variant;
    each rendered row must link to ITS variant, not the cheapest one).
    """
    base = _strip_query(row_url)
    if variant_id:
        return f"{base}?variant={variant_id}"
    return base


# ---------------------------------------------------------------------------
# View-model assembly
# ---------------------------------------------------------------------------

def _avail_value(available) -> str:
    if available is True:
        return "true"
    if available is False:
        return "false"
    return "unknown"


def _normalize_vid(variant_id):
    """None for missing/empty variant ids — "" is not an identity."""
    if variant_id is None or variant_id == "":
        return None
    return variant_id


def _quarantine_key_for(quarantine: dict, retailer_id, product_id, variant_id):
    """The quarantine key for this variant, or None when not quarantined.

    Matching REQUIRES a non-empty variant_id on both sides (MAJOR-7): an
    id-less variant can never be quarantined, and a malformed key can
    never match anything.
    """
    vid = _normalize_vid(variant_id)
    if vid is None:
        return None
    key = quarantine_key(retailer_id, product_id, vid)
    return key if key in quarantine else None


def _variant_view(tier: str, data: dict, capacity_wh, product_title: str,
                  withheld: str | None = None, withheld_reason: str | None = None,
                  asof: str = "") -> dict:
    raw_variant = data.get("raw_variant", "")
    classification = classify_variant(raw_variant, product_title)
    price = data.get("price")
    per_wh = dollars_per_wh(price, capacity_wh, classification)
    was = data.get("was_price")
    return {
        "tier": tier,
        "raw_variant": raw_variant,
        "classification": classification,
        "price": price,
        "price_display": price_display(price),
        "was_price_display": money(was) if _usable_number(was) else None,
        "dollars_per_wh": per_wh,
        "dollars_per_wh_display": format_dollars_per_wh(per_wh) if per_wh is not None else None,
        "available": data.get("available"),
        "avail_value": _avail_value(data.get("available")),
        # Empty/missing variant_id normalizes to None: a "" id stamped
        # into data-variant-id collapses identity across variants and
        # mis-joins the audit (red team #4, MAJOR-7). The template only
        # emits the attribute when the id is real.
        "variant_id": _normalize_vid(data.get("variant_id")),
        "sku": data.get("sku"),
        "buy_url": _buy_url(data.get("_row_url", ""), data.get("variant_id")),
        # Withhold markers (PLAN 4c.4): distinct values so an auditor —
        # human or audit.py — can tell "too old" from "under verification".
        "withheld": withheld,
        "withheld_reason": withheld_reason,
        "asof": asof,
    }


def _retailer_name(row: dict, retailers_by_id: dict, retailer_id: str) -> str:
    """retailer_name is OPTIONAL on price rows (C4) — never KeyError.

    Preference order: retailers.json (canonical), then the row's own
    retailer_name, then a titleized id as last resort.
    """
    retailer = retailers_by_id.get(retailer_id)
    if retailer and retailer.get("name"):
        return retailer["name"]
    if row.get("retailer_name"):
        return row["retailer_name"]
    return retailer_id.replace("-", " ").title()


def build_product_page(product: dict, price_rows: dict, retailers_by_id: dict,
                       quarantine: dict | None = None, now=None) -> dict:
    """View model for one product page."""
    quarantine = quarantine or {}
    now = now or datetime.now(timezone.utc)
    capacity_wh = (product.get("specs") or {}).get("capacity_wh")
    product_id = product.get("id", "")
    sections = []
    for retailer_id, row in sorted(price_rows.items()):
        scraped_at = row.get("timestamp") or ""
        hours = age_hours(now, scraped_at)
        row_stale = is_stale(hours)
        asof = age_display(hours)
        variants = []
        for tier, data in (row.get("variants") or {}).items():
            if not isinstance(data, dict):
                continue
            data = dict(data)
            data["_row_url"] = row.get("url", "")
            qkey = _quarantine_key_for(
                quarantine, retailer_id, product_id, data.get("variant_id"))
            # Quarantine outranks stale: "under verification" is the more
            # specific fact and must not be masked by age.
            if qkey is not None:
                withheld, reason = "quarantine", "under verification"
            elif row_stale:
                withheld, reason = "stale", f"data too old ({asof})"
            elif (isinstance(data.get("price"), (int, float))
                    and not _usable_number(data.get("price"))):
                # A stored NaN/Infinity/zero/negative is corruption, not a
                # price. It used to render as "$inf"/"$nan"; now the cell
                # withholds with its own marker so the state is visible to
                # a reader and to audit.py.
                withheld, reason = ("price_unreadable",
                                    "stored price is not a usable number")
            else:
                withheld, reason = None, None
            variants.append(_variant_view(
                tier, data, capacity_wh, product.get("name", ""),
                withheld=withheld, withheld_reason=reason, asof=asof,
            ))
        # A malformed price (string, null) must sort as "unknown", not
        # TypeError the whole build. The isinstance guards elsewhere
        # already exclude such rows from $/Wh; this is the same rule.
        variants.sort(key=lambda v: (
            not isinstance(v["price"], (int, float)),
            v["price"] if isinstance(v["price"], (int, float)) else 0,
        ))
        sections.append({
            "retailer_id": retailer_id,
            "retailer_name": _retailer_name(row, retailers_by_id, retailer_id),
            "scraped_at": scraped_at,
            "timestamp": scraped_at[:10],
            "asof": asof,
            "in_stock": row.get("in_stock"),
            "variants": variants,
        })
    specs = product.get("specs") or {}
    return {
        "product": product,
        "capacity_display": spec_display(capacity_wh, "Wh"),
        "output_display": spec_display(specs.get("output_w"), "W"),
        "retailer_sections": sections,
    }


def build_home_rows(products: list[dict], latest: dict,
                    active_retailers: list[dict],
                    quarantine: dict | None = None, now=None) -> list[dict]:
    """View model rows for the home table (products x retailers)."""
    quarantine = quarantine or {}
    now = now or datetime.now(timezone.utc)
    rows = []
    for product in products:
        capacity_wh = (product.get("specs") or {}).get("capacity_wh")
        cells = []
        for retailer in active_retailers:
            row = latest.get(product["id"], {}).get(retailer["id"])
            if not row or not row.get("variants"):
                cells.append(None)
                continue
            # _usable_number, not isinstance: a stored NaN would poison
            # min() (every comparison against NaN is False, so the
            # "cheapest" becomes an artefact of dict order) and an
            # Infinity would render as "$inf".
            priced = [
                (tier, d) for tier, d in row["variants"].items()
                if isinstance(d, dict) and _usable_number(d.get("price"))
            ]
            if not priced:
                cells.append(None)
                continue
            cheapest_tier, cheapest = min(priced, key=lambda item: item[1]["price"])
            scraped_at = row.get("timestamp") or ""
            hours = age_hours(now, scraped_at)
            vid = _normalize_vid(cheapest.get("variant_id"))
            base_cell = {"variant_id": vid, "scraped_at": scraped_at}
            if is_stale(hours):
                cells.append({**base_cell, "withheld": "stale",
                              "withheld_reason": f"data too old ({age_display(hours)})"})
                continue
            # Quarantine applies BEFORE cheapest-variant selection: when
            # the true cheapest is under verification the WHOLE cell
            # withholds — silently substituting the next-cheapest under a
            # "lowest price" heading would present a false lowest price
            # (PLAN 4c.3). Matching requires a real variant_id (MAJOR-7).
            if _quarantine_key_for(quarantine, retailer["id"], product["id"], vid):
                cells.append({**base_cell, "withheld": "quarantine",
                              "withheld_reason": "under verification"})
                continue
            # The cell's price and $/Wh MUST come from the SAME variant
            # (red team #2, MAJOR-1) — and so must its AVAILABILITY: the
            # row-level in_stock aggregate can say True while the cheapest
            # variant is sold out, the residual mixed-variant defect
            # (PLAN 4c.4).
            cls = classify_variant(
                cheapest.get("raw_variant", ""), product.get("name", "")
            )
            per_wh = dollars_per_wh(cheapest["price"], capacity_wh, cls)
            cells.append({
                **base_cell,
                "withheld": None,
                "price": cheapest["price"],
                "price_display": price_display(cheapest["price"]),
                "is_bundle": cls == "bundle",
                "dollars_per_wh_display": (
                    format_dollars_per_wh(per_wh) if per_wh is not None else None
                ),
                "available": cheapest.get("available"),
                "avail_value": _avail_value(cheapest.get("available")),
                "asof": age_display(hours),
            })
        rows.append({
            "product": product,
            # Guarded: a bool/NaN capacity used to render as "True Wh".
            "capacity_display": spec_display(capacity_wh, "Wh"),
            "cells": cells,
        })
    return rows


# ---------------------------------------------------------------------------
# Guides (the content layer)
# ---------------------------------------------------------------------------
# A guide is a RANKING VIEW over exactly the same data the product pages
# render, assembled with exactly the same functions: classify_variant for
# unit-vs-bundle, dollars_per_wh for the rating, the quarantine map and
# STALE_MAX_HOURS for withholding. Nothing here re-implements a rule — a
# guide that computed its own $/Wh could disagree with the product page
# for the same variant, and two different numbers for one fact is the
# defect class this project exists to avoid.
#
# Guides are additive surfaces: audit.py's render hop reads index.html and
# products/*.html only, so a guide number is NOT independently audited
# today. That is the reason the assembly is shared rather than parallel,
# and the reason test_guides.py asserts guide strings equal product-page
# strings for the same variant.

# The rating metric. `spec_key` names the products.json field the rating
# divides by; both metrics run through dollars_per_wh(), which is a
# price/quantity division with the unit-and-usable-number guards — the
# guards are the valuable part and must not be duplicated.
# `field` is the data-field name the rated figure renders under. For $/Wh
# it is "wh" — the SAME name the product and home pages use — so audit.py
# can verify a guide's rating with the identical comparison it already runs
# on product pages (LOW-9). $/W gets its own name because it is a different
# quantity: reusing "wh" for it would make the audit compare a per-watt
# figure against a per-watt-hour expectation.
_METRICS = {
    "wh": {
        "spec_key": "capacity_wh",
        "spec_label": "usable capacity",
        "spec_unit": "Wh",
        "label": "$/Wh",
        "field": "wh",
        "format": format_dollars_per_wh,
    },
    "w": {
        "spec_key": "output_w",
        "spec_label": "rated output",
        "spec_unit": "W",
        "label": "$/W",
        "field": "watt",
        "format": format_dollars_per_watt,
    },
}

# Guide definitions. `categories` selects from products.json's own
# `category` field, so scope is a data query rather than a hand-kept list
# of ids: a product added to a tracked category joins its guide on the
# next build, and one removed leaves it.
GUIDES = [
    {
        "slug": "server-rack-and-wall-mount-battery-cost-per-kwh",
        "nav_label": "Rack & wall batteries",
        "h1": "Server-rack & wall-mount battery cost per kWh",
        "categories": ("server-rack-battery", "home-battery"),
        "metric": "wh",
        "spreads": False,
        "subject": "server-rack and wall-mount batteries",
        "lede": (
            "Every server-rack and wall-mount battery Helios tracks, ranked by "
            "cost per watt-hour of the cheapest standalone battery on offer. "
            "Cost per kWh is the same quantity expressed in thousands: divide "
            "a $/Wh figure by 1,000 to read it as $/kWh."
        ),
    },
    {
        "slug": "portable-power-stations-compared-by-real-prices",
        "nav_label": "Power stations",
        "h1": "Portable power stations compared by real prices",
        "categories": ("portable-power-station",),
        "metric": "wh",
        "spreads": True,
        "subject": "portable power stations",
        "lede": (
            "Portable power stations ranked by cost per watt-hour of the "
            "main unit, with every tracked retailer's own price beside it. "
            "Where two retailers publish the same SKU, the gap between them "
            "is shown as they published it."
        ),
    },
    {
        "slug": "solar-panel-pallets-cheapest-cost-per-watt",
        "nav_label": "Solar panels",
        "h1": "Solar panel pallets: cheapest cost per watt",
        "categories": ("solar-panel",),
        "metric": "w",
        "spreads": False,
        "subject": "solar panels",
        "lede": (
            "Solar panels ranked by cost per watt. Most of these products "
            "sell only as pallets of 8, 10, 12 or 36 panels; a pallet's "
            "price is listed exactly as the retailer published it, but it "
            "carries no $/W, because deriving one means trusting a panel "
            "count read off a variant label."
        ),
    },
]


def guide_by_slug(slug: str) -> dict | None:
    for spec in GUIDES:
        if spec["slug"] == slug:
            return spec
    return None


def guide_for_product(product: dict) -> dict | None:
    """The guide spec whose scope covers this product, or None.

    audit.py needs this to know WHICH rated figure a guide should be
    showing for a variant: a power station has an output_w, but it is
    ranked on $/Wh, so expecting a $/W from it would manufacture a
    mismatch on every station row.
    """
    category = product.get("category")
    for spec in GUIDES:
        if category in spec["categories"]:
            return spec
    return None


def expected_rating_display(variant_data: dict, product: dict, metric_key: str):
    """The rated string a guide should render for this variant, or None.

    Uses the site's own classifier, rating function and formatter, so the
    audit compares strings produced by the same code path rather than
    re-deriving the rule and testing the arithmetic twice.
    """
    metric = _METRICS[metric_key]
    cls = classify_variant(
        variant_data.get("raw_variant", ""), product.get("name", ""))
    rating = dollars_per_wh(
        variant_data.get("price"),
        (product.get("specs") or {}).get(metric["spec_key"]), cls)
    return metric["format"](rating) if rating is not None else None


def _spec_provenance(product: dict, metric_key: str, retailer_id: str) -> str:
    """How well-sourced is the divisor, at THIS retailer?

    - "quoted": specs.capacity_quotes holds a verbatim substring of THIS
      retailer's own listing stating the figure (the E1 fix: quotes are
      extracted from merchant bytes, never typed).
    - "cross-retailer": the figure is quoted, but only from a different
      retailer's listing. The rating is still computed — the capacity of a
      battery does not change per storefront — but the reader is told the
      evidence came from elsewhere. EXPANSION_LOG section 8.6 flagged
      exactly this gap; the guide surfaces it instead of hiding it.
    - "unquoted": no verbatim evidence recorded. TODAY this is every
      output_w figure: output_w is hand-authored from the nameplate and
      has no capacity_quotes-style provenance field, so $/W is stated
      conservatively and labelled as such on the page.
    """
    if metric_key != "wh":
        return "unquoted"
    quotes = (product.get("specs") or {}).get("capacity_quotes") or {}
    if not quotes:
        return "unquoted"
    return "quoted" if retailer_id in quotes else "cross-retailer"


def _rating_withheld_reason(classification, spec_value, price, metric) -> str | None:
    """Why this offer carries no rating — in the reader's words, not a code."""
    if classification != "unit":
        return f"bundle or multi-unit pack, so {metric['label']} is withheld"
    if not _usable_number(spec_value):
        return f"{metric['spec_label']} not established, so {metric['label']} is withheld"
    if not _usable_number(price):
        return "price could not be read on the last scrape"
    return None


def guide_offers(product: dict, price_rows: dict, retailers_by_id: dict,
                 metric_key: str, quarantine: dict | None = None,
                 now=None) -> list[dict]:
    """Every (retailer, variant) offer for one product, rated for a guide.

    The withhold order matches build_product_page exactly: quarantine
    outranks stale, and a withheld offer shows neither a price nor a
    rating. Reusing the order matters — a guide that showed a price the
    product page withholds would republish precisely what an audit pulled.
    """
    metric = _METRICS[metric_key]
    quarantine = quarantine or {}
    now = now or datetime.now(timezone.utc)
    spec_value = (product.get("specs") or {}).get(metric["spec_key"])
    product_id = product.get("id", "")
    product_name = product.get("name", "")

    offers = []
    for retailer_id, row in sorted(price_rows.items()):
        scraped_at = row.get("timestamp") or ""
        hours = age_hours(now, scraped_at)
        row_stale = is_stale(hours)
        asof = age_display(hours)
        for tier, data in (row.get("variants") or {}).items():
            if not isinstance(data, dict):
                continue
            vid = _normalize_vid(data.get("variant_id"))
            price = data.get("price")
            if _quarantine_key_for(quarantine, retailer_id, product_id, vid):
                withheld, withheld_reason = "quarantine", "under verification"
            elif row_stale:
                withheld, withheld_reason = "stale", f"data too old ({asof})"
            elif isinstance(price, (int, float)) and not _usable_number(price):
                # A stored number that is not finite and positive is
                # corruption, not a price. Marked distinctly so it reads
                # as a withheld cell rather than a blank one.
                withheld, withheld_reason = ("price_unreadable",
                                             "stored price is not a usable number")
            else:
                withheld, withheld_reason = None, None

            raw_variant = data.get("raw_variant", "")
            classification = classify_variant(raw_variant, product_name)
            # dollars_per_wh is the shared price/quantity rating with the
            # unit-only, finite, positive guards. For metric "w" the
            # divisor is output_w; the arithmetic and the guards are the
            # same, which is the point of reusing it.
            rating = (None if withheld
                      else dollars_per_wh(price, spec_value, classification))
            offers.append({
                "product_id": product_id,
                "retailer_id": retailer_id,
                "retailer_name": _retailer_name(row, retailers_by_id, retailer_id),
                "tier": tier,
                "raw_variant": raw_variant,
                "classification": classification,
                "price": price,
                "price_display": price_display(price),
                "was_price_display": (money(data["was_price"])
                                      if _usable_number(data.get("was_price"))
                                      else None),
                "rating": rating,
                "rating_display": (metric["format"](rating)
                                   if rating is not None else None),
                "rating_withheld_reason": (
                    None if rating is not None or withheld
                    else _rating_withheld_reason(
                        classification, spec_value, price, metric)),
                "spec_provenance": _spec_provenance(
                    product, metric_key, retailer_id),
                "available": data.get("available"),
                "avail_value": _avail_value(data.get("available")),
                "variant_id": vid,
                "sku": data.get("sku"),
                "buy_url": _buy_url(row.get("url", ""), data.get("variant_id")),
                "scraped_at": scraped_at,
                "age_hours": hours,
                "asof": asof,
                "withheld": withheld,
                "withheld_reason": withheld_reason,
            })

    # Rated offers first (cheapest rating wins), then priced-but-unrated,
    # then anything withheld. Malformed prices sort last rather than
    # raising, the same rule build_product_page's sort applies.
    offers.sort(key=lambda o: (
        o["withheld"] is not None,
        o["rating"] is None,
        o["rating"] if o["rating"] is not None else 0.0,
        not isinstance(o["price"], (int, float)),
        o["price"] if isinstance(o["price"], (int, float)) else 0.0,
        o["retailer_name"],
    ))
    return offers


# A quantity claim inside a variant label: "8 Solar Panels", "3 Batteries",
# "2 x Modules". Nouns match the quantity forms _BUNDLE_RE already
# enumerates, so the two stay in step.
_QUANTITY_RE = re.compile(
    r'\b(\d+)\s*(?:x\s+)?(?:solar\s+)?'
    r'(?:panels?|batteries|battery|modules?|units?|cells?|packs?)\b',
    re.IGNORECASE,
)


def _quantity_tokens(title: str) -> frozenset:
    """The quantities a variant label claims, e.g. "10 Solar Panels" -> {10}."""
    return frozenset(int(m.group(1)) for m in _QUANTITY_RE.finditer(title or ""))


def _identity_conflict(group: list[dict]) -> str | None:
    """Do two retailers disagree about WHAT this shared SKU is?

    Plain string inequality is the wrong test: merchants word the same item
    differently as a matter of course ("DELTA MAX [Unit Only]" vs "EcoFlow
    DELTA Max Portable Power Station(Main Unit ONLY)"), so flagging every
    wording difference would put a scary warning on every row and bury the
    one case that matters.

    Two signals are real disagreements about the item itself:
      - the labels claim different QUANTITIES. This is EXPANSION_LOG E8:
        wild-oak-trail lists RS-M410-10 as "12 Solar Panels" while two
        other retailers put the same SKU on a 10-panel pack.
      - the shared classifier reads one as a standalone unit and the other
        as a bundle.
    """
    quantities = {_quantity_tokens(o["raw_variant"]) for o in group}
    if len(quantities) > 1:
        return "quantity"
    if len({o["classification"] for o in group}) > 1:
        return "kind"
    return None


def same_sku_spreads(offers: list[dict]) -> list[dict]:
    """Cross-retailer price gaps on one retailer-reported SKU.

    Only offers that are actually displayed take part: withheld ones are
    excluded, so a quarantined price can never re-enter through a spread.

    A spread is a BUYING claim — "this retailer is cheaper for the same
    thing" — so it renders only when BOTH sides can actually be bought:
    an offer whose variant is sold out is excluded. Otherwise the page
    advertises a saving nobody can take, and the cheaper side is often
    cheap precisely because it is gone. This also drops the RIVER 2 Pro
    58% "gap" that rested on a sold-out wild-oak-trail bundle.

    SKU is the retailer's OWN string and is not independently verified.
    Two known hazards are surfaced rather than smoothed over:
    `identity_conflict` fires when the retailers disagree about the
    quantity or kind behind the shared SKU (EXPANSION_LOG E8:
    wild-oak-trail's RS-M410 pack SKUs are shifted one step against its
    own labels), and the identical main unit of one product can carry two
    different SKU strings at two retailers (ZMR620-B-US vs
    ZMR620-B-US-1), which this join simply misses. A miss is a gap; a
    false pairing would be a defect.
    """
    by_sku: dict[str, list[dict]] = {}
    for offer in offers:
        sku = offer.get("sku")
        sku = sku.strip() if isinstance(sku, str) else ""
        if not sku or offer["withheld"] or not _usable_number(offer["price"]):
            continue
        if offer["available"] is False:
            continue
        by_sku.setdefault(sku, []).append(offer)

    spreads = []
    for sku, group in sorted(by_sku.items()):
        if len({o["retailer_id"] for o in group}) < 2:
            continue
        ordered = sorted(group, key=lambda o: o["price"])
        low, high = ordered[0], ordered[-1]
        gap = high["price"] - low["price"]
        spreads.append({
            "sku": sku,
            "offers": ordered,
            "low": low,
            "high": high,
            "gap": gap,
            "gap_display": money(gap),
            "gap_pct_display": format_percent(gap / low["price"] * 100.0),
            "same_price": gap == 0,
            "identity_conflict": _identity_conflict(ordered),
        })
    spreads.sort(key=lambda s: (-s["gap"], s["sku"]))
    return spreads


def _unranked_reason(offers: list[dict], spec_value, metric) -> str:
    """Why a product in scope carries no rating at all.

    Every branch states the reason that ACTUALLY applies. The previous
    version fell through to "every variant on sale is a bundle" whenever
    no earlier branch matched, which printed a confident falsehood for a
    product whose standalone unit merely had an unreadable price — a
    placard that lies about its own rule is worse than no placard.
    """
    if not offers:
        return "no price data has been collected for this product yet"
    shown = [o for o in offers if not o["withheld"]]
    if not shown:
        kinds = {o["withheld"] for o in offers}
        if kinds == {"price_unreadable"}:
            return ("no stored price for this product is a usable number, so "
                    "nothing here can be rated or shown")
        if kinds == {"quarantine"}:
            return "every stored price is withheld pending verification"
        if kinds == {"stale"}:
            return "every stored price is too old to publish"
        return ("every stored price is withheld (too old, under verification, "
                "or not a usable number)")
    if not _usable_number(spec_value):
        return (f"{metric['spec_label']} is not established for this product, "
                f"so {metric['label']} is withheld at every retailer")
    units = [o for o in shown if o["classification"] == "unit"]
    if not units:
        return (f"every variant on sale is a bundle or a multi-unit pack, so "
                f"{metric['label']} is withheld")
    rated = [o for o in units if o["rating"] is not None]
    if not rated:
        return ("no standalone unit on offer has a readable price, so "
                f"{metric['label']} cannot be computed")
    # Rated offers exist, so the only way to get here is HIGH-1's rule:
    # everything we can rate is sold out.
    return ("the best price we can rate is currently sold out, so this "
            "product is not ranked today")


def _dominant_skus(offers: list[dict]) -> set:
    """SKUs that at least two retailers agree on for this product.

    Used to spot a row whose SKU nobody else carries. With fewer than two
    retailers there is no agreement to measure, so every SKU counts as
    dominant and nothing is annotated.
    """
    retailers_by_sku: dict[str, set] = {}
    for offer in offers:
        sku = offer.get("sku")
        sku = sku.strip() if isinstance(sku, str) else ""
        if sku:
            retailers_by_sku.setdefault(sku, set()).add(offer["retailer_id"])
    if len({o["retailer_id"] for o in offers}) < 2:
        return set(retailers_by_sku)
    return {sku for sku, rids in retailers_by_sku.items() if len(rids) > 1}


def _sku_conflicts(offers: list[dict]) -> dict:
    """{sku: "quantity"|"kind"} for SKUs two retailers describe differently.

    Runs on EVERY guide table, not only the ones that render a spreads
    section. The conflict is a property of the data, not of whether this
    page happens to draw a spread block — and the merged per-product
    table is exactly where a reader compares two rows side by side.
    """
    by_sku: dict[str, list[dict]] = {}
    for offer in offers:
        sku = offer.get("sku")
        sku = sku.strip() if isinstance(sku, str) else ""
        if sku and not offer["withheld"]:
            by_sku.setdefault(sku, []).append(offer)
    conflicts = {}
    for sku, group in by_sku.items():
        if len({o["retailer_id"] for o in group}) < 2:
            continue
        kind = _identity_conflict(group)
        if kind:
            conflicts[sku] = kind
    return conflicts


def _note_flagged_retailers(product: dict) -> dict:
    """Per-retailer data-quality warnings recorded in the catalog.

    Reads the STRUCTURED `notes_by_retailer` map on the product:
    {retailer_id: what is wrong with THAT retailer's data}.

    The first implementation searched the free-prose `notes` field for any
    retailer id and attributed the warning to every id it matched. Prose
    about a data-quality problem names the retailers it EXONERATES as
    often as the one at fault — E8's note says wild-oak-trail's pack SKUs
    are shifted "while shop-solar-kits and rich-solar both put RS-M410-10
    on a 10-panel pack" — so the shipped panel guide printed
    "Data-quality note recorded against Shop Solar Kits" and "...against
    Rich Solar", publishing a fault accusation against named businesses
    that the note's own text refutes. Substring matching cannot tell "at
    fault" from "mentioned", so attribution is structured and prose is
    never parsed for blame.
    """
    by_retailer = product.get("notes_by_retailer")
    if not isinstance(by_retailer, dict):
        return {}
    return {rid: text for rid, text in by_retailer.items()
            if isinstance(rid, str) and rid and isinstance(text, str) and text}


def guide_entry(product: dict, price_rows: dict, retailers_by_id: dict,
                metric_key: str, quarantine: dict | None = None,
                now=None, with_spreads: bool = False) -> dict:
    """One product's block on a guide page."""
    metric = _METRICS[metric_key]
    spec_value = (product.get("specs") or {}).get(metric["spec_key"])
    offers = guide_offers(product, price_rows, retailers_by_id, metric_key,
                          quarantine=quarantine, now=now)
    shown = [o for o in offers if not o["withheld"]]

    # --- HIGH-1: a ranking is a BUYING recommendation ---------------------
    # Ranking on a sold-out variant told readers the cheapest tracked
    # power station was an Anker F2600 at $0.49/Wh that no tracked
    # retailer would sell them. Availability is part of the claim, so an
    # offer that cannot be bought cannot set a rank. It still appears in
    # the table, marked sold out, because the price is real information.
    rated = [o for o in offers if o["rating"] is not None]
    rankable = [o for o in rated if o["available"] is not False]
    best = min(rankable, key=lambda o: o["rating"]) if rankable else None

    # --- MEDIUM-7 / LOW-8: identity annotations on every table -----------
    conflicts = _sku_conflicts(offers)
    dominant = _dominant_skus(shown)
    flagged = _note_flagged_retailers(product)
    multi_retailer = len({o["retailer_id"] for o in shown}) > 1
    for offer in offers:
        sku = offer.get("sku")
        sku = sku.strip() if isinstance(sku, str) else ""
        offer["sku_conflict"] = conflicts.get(sku)
        offer["sku_only_here"] = bool(
            sku and multi_retailer and sku not in dominant
            and not offer["withheld"])
        offer["note_warning"] = flagged.get(offer["retailer_id"])

    return {
        "product": product,
        "metric": metric,
        "spec_value": spec_value,
        "spec_display": spec_display(spec_value, metric["spec_unit"]),
        "spec_provenance": _spec_provenance(
            product, metric_key, best["retailer_id"] if best else ""),
        "offers": offers,
        "best": best,
        "rated": best is not None,
        "tied": False,
        "sold_out_only": bool(rated and not rankable),
        "retailer_count": len({o["retailer_id"] for o in shown}),
        "has_conflicts": bool(conflicts),
        "note_warning": next(iter(flagged.values()), None),
        "unranked_reason": (None if best is not None
                            else _unranked_reason(offers, spec_value, metric)),
        "spreads": same_sku_spreads(offers) if with_spreads else [],
    }


def _guide_stats(entries: list[dict], metric: dict) -> dict:
    """The numbers the methodology footer states about itself."""
    offers = [o for e in entries for o in e["offers"]]
    shown = [o for o in offers if not o["withheld"]]
    ages = [o["age_hours"] for o in shown if o["age_hours"] is not None]
    return {
        "metric_label": metric["label"],
        "spec_label": metric["spec_label"],
        "products_in_scope": len(entries),
        "products_ranked": sum(1 for e in entries if e["rated"]),
        "products_unranked": sum(1 for e in entries if not e["rated"]),
        "retailers": sorted({o["retailer_name"] for o in shown}),
        "offers_shown": len(shown),
        "offers_rated": sum(1 for o in shown if o["rating"] is not None),
        "offers_unrated": sum(1 for o in shown if o["rating"] is None),
        "offers_bundle": sum(1 for o in shown if o["classification"] != "unit"),
        "withheld_stale": sum(1 for o in offers if o["withheld"] == "stale"),
        "withheld_quarantine": sum(
            1 for o in offers if o["withheld"] == "quarantine"),
        "withheld_unreadable": sum(
            1 for o in offers if o["withheld"] == "price_unreadable"),
        "offers_sold_out": sum(1 for o in shown if o["available"] is False),
        "products_sold_out_only": sum(1 for e in entries if e["sold_out_only"]),
        "products_with_conflicts": sum(1 for e in entries if e["has_conflicts"]),
        "provenance_cross_retailer": sum(
            1 for o in shown
            if o["rating"] is not None and o["spec_provenance"] == "cross-retailer"),
        "provenance_unquoted": sum(
            1 for o in shown
            if o["rating"] is not None and o["spec_provenance"] == "unquoted"),
        "newest": age_display(min(ages)) if ages else None,
        "oldest": age_display(max(ages)) if ages else None,
        "stale_max_hours": STALE_MAX_HOURS,
    }


def build_guide(spec: dict, products: list[dict], latest: dict,
                retailers_by_id: dict, quarantine: dict | None = None,
                now=None) -> dict:
    """View model for one guide page."""
    metric = _METRICS[spec["metric"]]
    in_scope = [p for p in products if p.get("category") in spec["categories"]]
    entries = [
        guide_entry(p, latest.get(p["id"], {}), retailers_by_id, spec["metric"],
                    quarantine=quarantine, now=now,
                    with_spreads=spec.get("spreads", False))
        for p in in_scope
    ]
    ranked = sorted((e for e in entries if e["rated"]),
                    key=lambda e: (e["best"]["rating"], e["product"].get("name", "")))
    unranked = sorted((e for e in entries if not e["rated"]),
                      key=lambda e: e["product"].get("name", ""))
    # LOW-10: adjacent ranks can render the SAME string from different
    # underlying values ($0.95995/W and $0.95996/W both print "$0.96/W").
    # Ordering is correct but a reader sees identical numbers ranked 1 and
    # 2 with nothing explaining why. Mark them so the page can say "tie".
    for index, entry in enumerate(ranked):
        shown_rating = entry["best"]["rating_display"]
        neighbours = []
        if index:
            neighbours.append(ranked[index - 1]["best"]["rating_display"])
        if index + 1 < len(ranked):
            neighbours.append(ranked[index + 1]["best"]["rating_display"])
        entry["tied"] = shown_rating in neighbours
    spreads = [
        {"entry": e, "spread": s}
        for e in entries for s in e["spreads"]
    ]
    spreads.sort(key=lambda item: (-item["spread"]["gap"], item["spread"]["sku"]))
    return {
        "guide": spec,
        "metric": metric,
        "ranked": ranked,
        "unranked": unranked,
        "entries": entries,
        "spreads": spreads,
        "stats": _guide_stats(entries, metric),
    }


# ---------------------------------------------------------------------------
# Site-wide facts (About / disclosure / meta descriptions)
# ---------------------------------------------------------------------------
# Every claim on the About and disclosure pages that involves a number or a
# name is computed here from the repo's own data, so a stale sentence is
# impossible: change the catalog and the prose changes with it. Anything
# that CANNOT be computed (traffic, history, endorsements) is not claimed.

def site_facts(products: list[dict], retailers: list[dict], latest: dict,
               now=None) -> dict:
    """Counts and names for the About / disclosure pages, from live data."""
    now = now or datetime.now(timezone.utc)
    active_retailers = [r for r in retailers if r.get("active")]
    rendered_pairs = sum(len(by_retailer) for by_retailer in latest.values())

    with_capacity = [
        p for p in products
        if _usable_number((p.get("specs") or {}).get("capacity_wh"))
    ]
    quoted = [
        p for p in with_capacity
        if (p.get("specs") or {}).get("capacity_quotes")
    ]

    ages = []
    for by_retailer in latest.values():
        for row in by_retailer.values():
            hours = age_hours(now, row.get("timestamp") or "")
            if hours is not None:
                ages.append(hours)

    # An affiliate program is LIVE only when a link template exists to
    # rewrite outbound links with. Every retailer's template is empty
    # today, so the honest statement is that no commission is earned yet —
    # computed, not asserted, so it corrects itself the day one is joined.
    # MEDIUM-6: state only what the repo can evidence.
    # A non-null affiliate record whose own notes say "terms unverified"
    # does NOT establish that a programme exists — it records that someone
    # believed one might. And a null record is the ABSENCE of information,
    # not evidence that no programme exists. The disclosure page is where
    # the withhold-when-unknown discipline has to be modelled most
    # exactly, so both cases are labelled as what they are.
    def _verified(affiliate: dict) -> bool:
        notes = (affiliate.get("network") or "") + " " + (affiliate.get("notes") or "")
        return "unverified" not in notes.lower()

    programs = [
        {
            "name": r.get("name") or r["id"],
            "network": (r.get("affiliate") or {}).get("network") or "unknown",
            "commission": (r.get("affiliate") or {}).get("commission") or "unknown",
            "live": bool((r.get("affiliate") or {}).get("link_template")),
            "verified": _verified(r.get("affiliate") or {}),
            "notes": (r.get("affiliate") or {}).get("notes") or "",
        }
        for r in active_retailers if r.get("affiliate")
    ]
    no_record = [r.get("name") or r["id"]
                 for r in active_retailers if not r.get("affiliate")]

    return {
        "product_count": len(products),
        "retailer_count": len(active_retailers),
        "retailer_names": [r.get("name") or r["id"] for r in active_retailers],
        "retailers": active_retailers,
        "rendered_pairs": rendered_pairs,
        "products_with_prices": len(latest),
        "capacity_known": len(with_capacity),
        "capacity_withheld": len(products) - len(with_capacity),
        "capacity_quoted": len(quoted),
        "newest_scrape": age_display(min(ages)) if ages else None,
        "oldest_scrape": age_display(max(ages)) if ages else None,
        "stale_max_hours": STALE_MAX_HOURS,
        "stale_max_days": STALE_MAX_HOURS // 24,
        "affiliate_programs": programs,
        "affiliate_live": any(p["live"] for p in programs),
        "affiliate_verified_count": sum(1 for p in programs if p["verified"]),
        "no_record_retailers": no_record,
        "guides": GUIDES,
        "contact_email": CONTACT_EMAIL,
        "contact_repo": CONTACT_REPO,
        "generated_on": now.date().isoformat(),
    }


# ---------------------------------------------------------------------------
# Sitemap
# ---------------------------------------------------------------------------

def resolve_site_base_url(data_dir: Path) -> str | None:
    """The configured public origin, or None when the site has no home yet.

    Env wins over the committed file so a deploy pipeline can set it
    without a commit. A blank value counts as unset — an empty string in
    config must not become "https:///page.html".
    """
    from_env = os.environ.get(SITE_BASE_URL_ENV, "").strip()
    if from_env:
        return from_env.rstrip("/")
    path = data_dir / SITE_CONFIG_FILENAME
    if path.exists():
        configured = (load_json(path) or {}).get("site_base_url") or ""
        if isinstance(configured, str) and configured.strip():
            return configured.strip().rstrip("/")
    return None


def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


def render_sitemap(paths: list[str], now, base_url: str) -> str:
    """A sitemap over the pages this build actually wrote.

    `paths` is accumulated at write time, so the sitemap cannot list a page
    that was not rendered, and cannot omit one that was. lastmod comes from
    the injected clock, which keeps it deterministic under test.
    """
    base = base_url.rstrip("/")
    lastmod = now.date().isoformat()
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for path in paths:
        loc = _xml_escape(f"{base}/{path.lstrip('/')}")
        lines.append(f"  <url><loc>{loc}</loc><lastmod>{lastmod}</lastmod></url>")
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_site(data_dir: Path = DATA_DIR, site_dir: Path = SITE_DIR,
               templates_dir: Path = TEMPLATES_DIR, now=None,
               quarantine_override: dict | None = None) -> dict:
    """Render the whole site. Returns a small summary dict for callers/tests.

    `now` is the single clock for staleness and "as of" ages (PLAN 4c.4);
    tests pin it, production passes None for real time.
    `quarantine_override` replaces the on-disk quarantine map — the
    audit's shadow recheck (PLAN 4c.3 lifecycle, red team #4 MAJOR-3)
    rebuilds with the entry under test suppressed to prove the defect is
    actually gone before clearing it.
    """
    now = now or datetime.now(timezone.utc)
    products = [p for p in load_json(data_dir / "products.json") if p.get("active") is True]
    retailers = load_json(data_dir / "retailers.json")
    retailers_by_id = {r["id"]: r for r in retailers}
    active_retailers = [r for r in retailers if r.get("active")]
    if quarantine_override is not None:
        validate_quarantine(quarantine_override)
        quarantine = quarantine_override
    else:
        quarantine = load_quarantine(data_dir)

    latest = load_latest_prices(data_dir / "prices", [p["id"] for p in products])
    handle_maps_path = data_dir / "handle_maps.json"
    latest = filter_to_mapped_pairs(
        latest,
        load_json(handle_maps_path) if handle_maps_path.exists() else None,
    )

    env = Environment(
        loader=FileSystemLoader(templates_dir),
        autoescape=select_autoescape(["html"]),
    )

    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "products").mkdir(parents=True, exist_ok=True)
    (site_dir / "guides").mkdir(parents=True, exist_ok=True)

    facts = site_facts(products, retailers, latest, now=now)
    base_url = resolve_site_base_url(data_dir)
    rendered: list[str] = []

    def write_page(rel_path: str, template: str, page_title: str,
                   meta_description: str, **context) -> None:
        """Render one page and record it for the sitemap.

        Recording happens HERE rather than in a hand-kept list, so the
        sitemap is a by-product of writing rather than a parallel
        inventory that can drift out of step with the pages on disk.

        `root_prefix` makes every in-site link relative to the page's own
        depth. Absolute "/index.html" links break the moment the site is
        served from a subpath, which is exactly how it is published today
        (GitHub Pages project site under /Helios/).
        """
        depth = rel_path.count("/")
        html = env.get_template(template).render(
            page_title=page_title,
            meta_description=clip_text(meta_description),
            # None with no origin configured -> the template emits no
            # canonical at all. A canonical pointing at a 404 tells a
            # crawler to prefer a dead URL over the page in front of it.
            canonical_url=f"{base_url}/{rel_path}" if base_url else None,
            root_prefix="../" * depth,
            nav_guides=GUIDES,
            site_name=SITE_NAME,
            **context,
        )
        out = site_dir / rel_path
        out.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n": working-tree bytes stay LF on Windows regardless of
        # the machine's core.autocrlf — same rule as runner.py's JSONL writer.
        out.write_text(html, encoding="utf-8", newline="\n")
        rendered.append(rel_path)

    # --- home -------------------------------------------------------------
    home_rows = build_home_rows(products, latest, active_retailers,
                                quarantine=quarantine, now=now)
    write_page(
        "index.html", "home.html",
        f"Solar & home-energy price tracker — {SITE_NAME}",
        f"Side-by-side prices for {facts['product_count']} solar and "
        f"home-energy products across {facts['retailer_count']} retailers, "
        f"scraped from live listings. $/Wh only where capacity is published.",
        rows=home_rows, retailers=active_retailers, facts=facts,
    )

    # --- product pages ----------------------------------------------------
    for product in products:
        view = build_product_page(
            product, latest.get(product["id"], {}), retailers_by_id,
            quarantine=quarantine, now=now,
        )
        sections = view["retailer_sections"]
        capacity = (product.get("specs") or {}).get("capacity_wh")
        # Description built from what this page actually shows: how many
        # retailers were read, and whether it can rate the product at all.
        if sections:
            where = (f"Prices from {len(sections)} tracked "
                     f"retailer{'s' if len(sections) != 1 else ''}")
            when = f", checked {sections[0]['asof'].replace('as of ', '')}"
        else:
            where, when = "Tracked by Helios; no price collected yet", ""
        rating = ("$/Wh shown for standalone units." if _usable_number(capacity)
                  else "$/Wh withheld: capacity is not established.")
        write_page(
            f"products/{product['id']}.html", "product.html",
            f"{product.get('name', product['id'])} price comparison — {SITE_NAME}",
            f"{where} for the {product.get('name', product['id'])}{when}. "
            f"{rating}",
            **view,
        )

    # --- guides -----------------------------------------------------------
    guide_views = []
    for spec in GUIDES:
        view = build_guide(spec, products, latest, retailers_by_id,
                           quarantine=quarantine, now=now)
        guide_views.append(view)
        stats = view["stats"]
        cheapest = (view["ranked"][0]["best"]["rating_display"]
                    if view["ranked"] else None)
        headline = (f"Cheapest tracked is {cheapest}. " if cheapest else "")
        write_page(
            f"guides/{spec['slug']}.html", "guide.html",
            f"{spec['h1']} — {SITE_NAME}",
            f"{stats['products_ranked']} of {stats['products_in_scope']} "
            f"tracked {spec['subject']} ranked by {stats['metric_label']} from "
            f"scraped retailer prices. {headline}"
            f"Unrated items are listed with the reason.",
            facts=facts, **view,
        )

    # --- about / disclosure ----------------------------------------------
    write_page(
        "about.html", "about.html",
        f"About {SITE_NAME} — what it tracks and how",
        f"How Helios collects prices for {facts['product_count']} solar and "
        f"home-energy products from {facts['retailer_count']} retailers, and "
        f"why it withholds a number rather than guess one.",
        facts=facts,
    )
    write_page(
        "disclosure.html", "disclosure.html",
        f"Affiliate disclosure — {SITE_NAME}",
        "How Helios is funded, which outbound links are commercial, and why "
        "rankings are computed from price data alone.",
        facts=facts,
    )

    # --- sitemap ----------------------------------------------------------
    # Only when the site has a configured public origin. A sitemap is a
    # list of absolute URLs and there is no honest one to write until the
    # site is deployed; an existing file is REMOVED rather than left
    # behind, because a stale sitemap full of 404s is exactly what this
    # rule exists to prevent.
    sitemap_path = site_dir / "sitemap.xml"
    if base_url:
        sitemap_path.write_text(
            render_sitemap(rendered, now, base_url),
            encoding="utf-8", newline="\n")
        sitemap_urls = len(rendered)
    else:
        sitemap_path.unlink(missing_ok=True)
        sitemap_urls = 0

    product_pages = sum(1 for p in rendered if p.startswith("products/"))
    return {
        # Unchanged meaning: home + product pages. The content layer is
        # counted separately so this number stays comparable with the
        # build lines recorded in docs/.
        "pages_written": 1 + product_pages,
        "guide_pages": len(GUIDES),
        "info_pages": 2,
        "total_pages_written": len(rendered),
        "sitemap_urls": sitemap_urls,
        "site_base_url": base_url,
        "products": len(products),
        "products_with_prices": len(latest),
        "quarantined": len(quarantine),
        "guides": [
            {"slug": v["guide"]["slug"],
             "ranked": v["stats"]["products_ranked"],
             "in_scope": v["stats"]["products_in_scope"]}
            for v in guide_views
        ],
    }


if __name__ == "__main__":
    summary = build_site()
    print(
        f"Built {summary['pages_written']} page(s): "
        f"{summary['products']} products, "
        f"{summary['products_with_prices']} with price data -> {SITE_DIR}"
    )
    for guide in summary["guides"]:
        print(f"  guide {guide['slug']}: "
              f"{guide['ranked']}/{guide['in_scope']} ranked")
    print(
        f"Content layer: {summary['guide_pages']} guide(s) + "
        f"{summary['info_pages']} info page(s)"
    )
    if summary["site_base_url"]:
        print(f"Sitemap: {summary['sitemap_urls']} URL(s) at "
              f"{summary['site_base_url']}")
    else:
        print(f"Sitemap: not written, and no canonical tags emitted - no "
              f"public origin configured (set {SITE_BASE_URL_ENV} or "
              f"data/{SITE_CONFIG_FILENAME} at deploy).")
    sys.exit(0)

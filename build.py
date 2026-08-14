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
# Articles (the editorial layer)
# ---------------------------------------------------------------------------
# An article is EVERGREEN PROSE wrapped around LIVE DATA BLOCKS. The prose
# is authored once and says only things that stay true; every number comes
# out of the price store at build time through the same guide_entry()
# machinery the guides use, with the same provenance attributes, the same
# withhold rules and the same "as of" ages. Nothing here re-implements a
# rule, and no number is typed into prose.
#
# That split is the lesson the guides taught: prose ages badly and numbers
# age fast, so the only maintainable article is one where the numbers
# refresh themselves on every build and the words never claim a number.

AUTHOR = {
    "name": "Brandon Hall",
    # STRICT: this bio states only what is verifiable from this repository.
    # Brandon built the tracker and runs it; the code and the price history
    # behind every page are public. No credentials, no years of experience,
    # no hands-on testing — inventing any of that would be the same defect
    # class as a fabricated capacity quote (EXPANSION_LOG E1), just harder
    # to catch because nothing recomputes prose.
    "bio": (
        "Brandon Hall builds and runs Helios. He writes the scrapers that "
        "collect these prices, the audit that checks the site against its "
        "own price store, and the rules that decide when a number is too "
        "uncertain to publish."
    ),
    "disclaimer": (
        "Helios does not physically test equipment. Everything here comes "
        "from tracked prices, published specifications and the site's own "
        "scrape history."
    ),
}


def _article_entry(product_id: str, products_by_id: dict, latest: dict,
                   retailers_by_id: dict, metric_key: str,
                   quarantine: dict, now) -> dict | None:
    """One product's live offer table for an article.

    Straight through guide_entry(), so an article renders exactly what a
    guide would for the same product: same classifier, same rating, same
    quarantine/stale/unreadable withholding, same SKU annotations.
    """
    product = products_by_id.get(product_id)
    if product is None:
        return None
    return guide_entry(product, latest.get(product_id, {}), retailers_by_id,
                       metric_key, quarantine=quarantine, now=now,
                       with_spreads=True)


def _min_buyable_price(entry: dict):
    """Cheapest price a reader could actually act on, or None.

    Withheld offers and sold-out variants do not count: a price filter that
    admitted them would put a product in a "under $X" list on the strength
    of an offer nobody can buy — HIGH-1's mistake in a different costume.
    """
    prices = [o["price"] for o in entry["offers"]
              if not o["withheld"] and _usable_number(o["price"])
              and o["available"] is not False]
    return min(prices) if prices else None


def _article_ranking(block: dict, products: list[dict], latest: dict,
                     retailers_by_id: dict, quarantine: dict, now) -> dict:
    """A live ranking scoped by category / chemistry / price ceiling."""
    metric_key = block.get("metric", "wh")
    metric = _METRICS[metric_key]
    categories = set(block.get("categories") or ())
    chemistry = block.get("chemistry")
    max_price = block.get("max_price")

    in_scope = []
    for product in products:
        if categories and product.get("category") not in categories:
            continue
        if chemistry and (product.get("specs") or {}).get("chemistry") != chemistry:
            continue
        in_scope.append(product)

    entries, priced_out = [], 0
    for product in in_scope:
        entry = guide_entry(product, latest.get(product["id"], {}),
                            retailers_by_id, metric_key,
                            quarantine=quarantine, now=now)
        if max_price is not None:
            cheapest = _min_buyable_price(entry)
            if cheapest is None or cheapest > max_price:
                priced_out += 1
                continue
        entries.append(entry)

    ranked = sorted((e for e in entries if e["rated"]),
                    key=lambda e: (e["best"]["rating"],
                                   e["product"].get("name", "")))
    unranked = sorted((e for e in entries if not e["rated"]),
                      key=lambda e: e["product"].get("name", ""))
    for index, entry in enumerate(ranked):
        shown = entry["best"]["rating_display"]
        neighbours = []
        if index:
            neighbours.append(ranked[index - 1]["best"]["rating_display"])
        if index + 1 < len(ranked):
            neighbours.append(ranked[index + 1]["best"]["rating_display"])
        entry["tied"] = shown in neighbours
    return {
        "metric": metric,
        "ranked": ranked,
        "unranked": unranked,
        "in_scope": len(in_scope),
        "listed": len(entries),
        "priced_out": priced_out,
        "max_price": max_price,
        "max_price_display": money(max_price) if max_price else None,
        "categories": sorted(categories),
        "chemistry": chemistry,
        # Compact rankings render the headline row only. The headline still
        # carries variant id, scraped-at and the rated figure, so it stays
        # auditable; the per-retailer detail lives on the product page.
        "compact": bool(block.get("compact")),
    }


_SPEC_FIELDS = {
    "capacity_wh": ("Usable capacity", "Wh"),
    "output_w": ("Rated output", "W"),
    "weight_lb": ("Weight", "lb"),
    "chemistry": ("Cell chemistry", None),
}


def _article_specs(block: dict, products_by_id: dict) -> dict:
    """A spec comparison built ONLY from stored specs.

    A missing spec renders "not published" rather than a guess or a blank.
    The catalog stores a weight for only a minority of products (the exact
    count is whatever data/products.json holds today, which is why it is not
    written down here); a spec table that quietly omitted the empty rows
    would imply we checked and found nothing remarkable, when in fact we
    never recorded it.
    """
    fields = block.get("fields") or ["capacity_wh", "output_w", "chemistry",
                                     "weight_lb"]
    columns = [products_by_id[pid] for pid in block["ids"]
               if pid in products_by_id]
    rows = []
    for key in fields:
        label, unit = _SPEC_FIELDS.get(key, (key, None))
        cells = []
        for product in columns:
            value = (product.get("specs") or {}).get(key)
            if unit:
                cells.append(spec_display(value, unit))
            else:
                cells.append(value if isinstance(value, str) and value else None)
        rows.append({"label": label, "key": key, "cells": cells})
    return {"products": columns, "rows": rows}


# A verdict is EVIDENCE only where the audit actually re-read the pair.
# NOT_AUDITED means the sampler did not reach it inside its request budget
# this run, NO_ROW that nothing was ever scraped: neither says anything
# about a retailer's accuracy, and prose that reads them as a pass asserts
# a result the page's own panel does not show (red team HIGH-1).
#
# Mirrors audit._VERIFIED_VERDICTS, which cannot be imported here because
# audit.py imports THIS module. A test asserts the two stay identical.
EVIDENCE_VERDICTS = ("RENDER_DEFECT", "STALE", "CLEAN", "NO_BASELINE")


def _retailer_report(retailer_id: str, products: list[dict], latest: dict,
                     retailers_by_id: dict, handle_maps: dict | None,
                     quarantine: dict, now, data_dir: Path) -> dict:
    """Everything Helios can say about a retailer FROM ITS OWN OBSERVATIONS.

    Deliberately narrow. This is price-and-catalog telemetry, not a review:
    nothing here touches service, support, shipping speed, returns or
    warranty handling, because the project has never bought anything and
    has no data on any of it. BLOCKER-2's lesson applies double in prose —
    a claim about a named business must be traceable to a stored
    observation or it does not get made.
    """
    mapped = (handle_maps or {}).get(retailer_id) or {}
    products_by_id = {p["id"]: p for p in products}

    # Cross-retailer position, split strictly-vs-tied.
    #
    # The first version of this counted `mine == high` as "most expensive"
    # and only `mine == low == high` as a tie, which called a retailer
    # dearest on products where it merely SHARED the top price with someone
    # else (red team HIGH-2). It also had no else branch, so products that
    # sat between the low and the high fell into no bucket at all and the
    # rendered sentence did not add up to its own denominator (HIGH-3).
    #
    # The buckets below are mutually exclusive and exhaustive over
    # `compared`: every counted product lands in exactly one, so the numbers
    # on the page sum to the number of products the same sentence claims to
    # have compared. A test recomputes that independently.
    buckets = {"strictly_cheapest": 0, "tied_low": 0, "mid_pack": 0,
               "tied_top": 0, "strictly_dearest": 0, "same_everywhere": 0}
    compared = 0
    for product_id, by_retailer in latest.items():
        if retailer_id not in by_retailer or len(by_retailer) < 2:
            continue
        product = products_by_id.get(product_id)
        if product is None:
            continue
        entry = guide_entry(product, by_retailer, retailers_by_id, "wh",
                            quarantine=quarantine, now=now)
        per_retailer = {}
        for offer in entry["offers"]:
            if offer["withheld"] or not _usable_number(offer["price"]):
                continue
            if offer["available"] is False:
                continue
            current = per_retailer.get(offer["retailer_id"])
            if current is None or offer["price"] < current:
                per_retailer[offer["retailer_id"]] = offer["price"]
        if len(per_retailer) < 2 or retailer_id not in per_retailer:
            continue
        compared += 1
        mine = per_retailer[retailer_id]
        values = list(per_retailer.values())
        low, high = min(values), max(values)
        if low == high:
            # Nobody is cheaper or dearer than anybody. Calling this a low
            # or a high would be picking an end of a range with one point.
            buckets["same_everywhere"] += 1
        elif mine == low:
            buckets["strictly_cheapest" if values.count(low) == 1
                    else "tied_low"] += 1
        elif mine == high:
            buckets["strictly_dearest" if values.count(high) == 1
                    else "tied_top"] += 1
        else:
            buckets["mid_pack"] += 1

    # Audit observations for this retailer, if a report has been written.
    verdicts: dict[str, int] = {}
    audit_generated = None
    audit_path = data_dir / "audit_report.json"
    if audit_path.exists():
        try:
            report = load_json(audit_path)
        except (ValueError, OSError):
            report = {}
        # audit.py writes the run time as "timestamp"; "generated_at" is
        # accepted too so an older report still dates itself rather than
        # rendering an undated tally.
        stamp = report.get("timestamp") or report.get("generated_at") or ""
        audit_generated = stamp[:10] or None
        for result in report.get("results") or []:
            if result.get("retailer_id") == retailer_id:
                verdicts[result.get("verdict", "?")] = verdicts.get(
                    result.get("verdict", "?"), 0) + 1

    retailer = retailers_by_id.get(retailer_id) or {}
    return {
        "retailer_id": retailer_id,
        "name": retailer.get("name") or retailer_id,
        "url": retailer.get("url"),
        "mapped_products": len(mapped),
        "catalog_total": len(products),
        "compared": compared,
        **buckets,
        # Ordered for rendering: the panel prints every bucket including the
        # empty ones, the lede prints the non-empty ones, and both read the
        # SAME list, so the two surfaces cannot label a bucket differently.
        "position_parts": [
            {"key": key, "field": f"position-{key.replace('_', '-')}",
             "label": label, "count": buckets[key]}
            for key, label in (
                ("strictly_cheapest", "strictly cheapest"),
                ("tied_low", "tied for the lowest price"),
                ("mid_pack", "mid-pack"),
                ("tied_top", "tied for the highest price"),
                ("strictly_dearest", "strictly most expensive"),
                ("same_everywhere", "level with every other tracked retailer"),
            )
        ],
        "bucket_total": sum(buckets.values()),
        "verdicts": dict(sorted(verdicts.items())),
        "clean_verdicts": verdicts.get("CLEAN", 0),
        "defect_verdicts": verdicts.get("RENDER_DEFECT", 0),
        "verified_verdicts": sum(verdicts.get(v, 0)
                                 for v in EVIDENCE_VERDICTS),
        "audit_generated": audit_generated,
        "affiliate_record": bool(retailer.get("affiliate")),
        "affiliate_live": bool((retailer.get("affiliate") or {}).get("link_template")),
        # Nothing in retailers.json records shipping, thresholds, returns or
        # support. That is an absence of data, and the page says so rather
        # than sourcing it from anywhere else.
        "has_shipping_data": "shipping" in retailer,
    }


def _citation_span(sources: list[dict]) -> dict | None:
    """The dated spread of a citation list, measured from the dates.

    A cadence claim ("a window every six to eight weeks") is an arithmetic
    claim about the sources under it, and this article's own four dates
    disagreed with the one that used to be typed there. So the intervals
    are computed and printed, and the prose points at them rather than
    naming a number nothing recomputes.

    Returns None for fewer than two parseable YYYY-MM-DD dates: with one
    date there is no interval to report.
    """
    dates = []
    for source in sources or ():
        try:
            dates.append(datetime.strptime(source.get("date") or "",
                                           "%Y-%m-%d").date())
        except ValueError:
            continue
    if len(dates) < 2:
        return None
    dates.sort()
    gaps = [(later - earlier).days
            for earlier, later in zip(dates, dates[1:])]
    return {
        "count": len(dates),
        "first": dates[0].isoformat(),
        "last": dates[-1].isoformat(),
        "min_gap": min(gaps),
        "max_gap": max(gaps),
        "total_days": (dates[-1] - dates[0]).days,
    }


def _history_facts(data_dir: Path, products: list[dict]) -> dict:
    """How much price history actually exists. Usually the honest answer
    is 'not much yet', and the articles have to be able to say that."""
    dates, rows = set(), 0
    prices_dir = data_dir / "prices"
    if prices_dir.is_dir():
        for product in products:
            path = prices_dir / f"{product['id']}.jsonl"
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rows += 1
                stamp = (row.get("timestamp") or "")[:10]
                if stamp:
                    dates.add(stamp)
    ordered = sorted(dates)
    return {
        "rows": rows,
        "days": len(ordered),
        "first_date": ordered[0] if ordered else None,
        "last_date": ordered[-1] if ordered else None,
    }


class ArticleContext:
    """What an article's Direct-answer lede is allowed to read.

    Answer ledes are the one place prose and live data meet, so they are
    written as functions over resolved blocks rather than format strings
    with numbers pasted in. Every accessor returns None when the data is
    not there, and every lede has to handle that — withhold-when-unknown
    in sentence form.
    """

    def __init__(self, facts: dict, history: dict):
        self.entries: dict[str, dict] = {}
        self.rankings: list[dict] = []
        self.reports: dict[str, dict] = {}
        self.facts = facts
        self.history = history

    def entry(self, product_id: str):
        return self.entries.get(product_id)

    def best(self, product_id: str):
        """The best RANKABLE offer for a product, or None."""
        entry = self.entries.get(product_id)
        return entry["best"] if entry and entry.get("best") else None

    def rating(self, product_id: str):
        best = self.best(product_id)
        return best["rating_display"] if best else None

    def price(self, product_id: str):
        best = self.best(product_id)
        return best["price_display"] if best else None

    def retailer(self, product_id: str):
        best = self.best(product_id)
        return best["retailer_name"] if best else None

    def cheapest_priced(self, product_id: str):
        """Cheapest buyable price for a product regardless of rating."""
        entry = self.entries.get(product_id)
        if not entry:
            return None
        value = _min_buyable_price(entry)
        return money(value) if value is not None else None

    def top(self, index: int = 0, ranking: int = 0):
        try:
            return self.rankings[ranking]["ranked"][index]
        except (IndexError, KeyError):
            return None


def resolve_article(spec: dict, products: list[dict], latest: dict,
                    retailers_by_id: dict, handle_maps: dict | None,
                    quarantine: dict, facts: dict, history: dict,
                    data_dir: Path, now) -> dict:
    """Turn one article definition into a renderable view model."""
    products_by_id = {p["id"]: p for p in products}
    ctx = ArticleContext(facts, history)
    blocks = []

    for block in spec["blocks"]:
        kind = block["kind"]
        if kind in ("prose", "h2", "callout"):
            blocks.append(dict(block))
            continue

        if kind == "citations":
            # The spacing of the sources, computed from their own dates, so
            # prose can point at the intervals instead of characterising
            # them from memory (red team MEDIUM-8).
            blocks.append({**block, "span": _citation_span(block["sources"])})
            continue

        if kind == "history":
            # How thin the record actually is, computed rather than claimed.
            blocks.append({**block, "history": history})
            continue

        if kind == "products":
            metric_key = block.get("metric", "wh")
            resolved = []
            for product_id in block["ids"]:
                entry = _article_entry(product_id, products_by_id, latest,
                                       retailers_by_id, metric_key,
                                       quarantine, now)
                if entry is None:
                    continue
                ctx.entries[product_id] = entry
                resolved.append(entry)
            blocks.append({**block, "entries": resolved,
                           "metric": _METRICS[metric_key]})
            continue

        if kind == "ranking":
            view = _article_ranking(block, products, latest, retailers_by_id,
                                    quarantine, now)
            for entry in view["ranked"] + view["unranked"]:
                ctx.entries.setdefault(entry["product"]["id"], entry)
            ctx.rankings.append(view)
            blocks.append({**block, "ranking": view})
            continue

        if kind == "specs":
            blocks.append({**block, "table": _article_specs(block, products_by_id)})
            continue

        if kind == "retailer_report":
            report = _retailer_report(block["retailer_id"], products, latest,
                                      retailers_by_id, handle_maps,
                                      quarantine, now, data_dir)
            ctx.reports[block["retailer_id"]] = report
            blocks.append({**block, "report": report})
            continue

        raise ValueError(f"unknown article block kind: {kind!r}")

    answer = spec["answer"]
    if callable(answer):
        answer = answer(ctx)
    return {
        "article": spec,
        "blocks": blocks,
        "answer": answer,
        "author": AUTHOR,
        "history": history,
    }


# ---------------------------------------------------------------------------
# Direct-answer ledes (AI-retrieval pattern)
# ---------------------------------------------------------------------------
# Each returns ONE paragraph that answers the title question up front. Where
# a number appears it is read live out of the resolved blocks, and every
# accessor can return None, so each lede degrades to an honest sentence
# instead of an empty slot or a stale figure.

def _capacity_clause(ctx, a_id: str, b_id: str) -> str:
    """How the two capacities compare, from the stored specs.

    "stores twice the energy of" was typed into this lede. It happens to be
    true of today's catalog and would go on being asserted if a capacity
    were corrected tomorrow, which is the same defect class as a hardcoded
    price.
    """
    def capacity(pid):
        entry = ctx.entry(pid)
        value = ((entry or {}).get("product", {}).get("specs") or {}).get("capacity_wh")
        return value if _usable_number(value) else None

    a, b = capacity(a_id), capacity(b_id)
    if a is None or b is None:
        return "is compared here against"
    ratio = a / b
    if 0.95 <= ratio <= 1.05:
        return "stores about the same energy as"
    if ratio < 0.95:
        return "stores less energy than"
    if 1.9 <= ratio <= 2.1:
        return "stores roughly twice the energy of"
    return f"stores about {ratio:.1f} times the energy of"


def _answer_head_to_head(ctx):
    a_id, b_id = "ecoflow-delta-pro-3", "bluetti-ac200l"
    a_rate, a_price = ctx.rating(a_id), ctx.price(a_id)
    b_rate, b_price = ctx.rating(b_id), ctx.price(b_id)
    if a_rate and b_rate:
        # EVERY comparative below is read off the computed figures. The
        # direction used to be typed into the sentence ("currently costs
        # less per watt-hour", "the DELTA Pro 3 is the cheaper energy"), so
        # repricing the AC200L made the lede contradict the two numbers it
        # was quoting in the same breath (red team HIGH-4).
        a_rating = (ctx.best(a_id) or {}).get("rating")
        b_rating = (ctx.best(b_id) or {}).get("rating")
        a_paid, b_paid = _money_value(a_price), _money_value(b_price)
        if a_rating < b_rating:
            direction = "costs less per watt-hour"
            energy = "the DELTA Pro 3 is the cheaper energy"
        elif a_rating > b_rating:
            direction = "costs more per watt-hour"
            energy = "the AC200L is the cheaper energy"
        else:
            direction = "matches it on cost per watt-hour"
            energy = "neither is the cheaper energy today"
        if b_paid < a_paid:
            cheque = "The AC200L is the smaller cheque"
        elif a_paid < b_paid:
            cheque = "The DELTA Pro 3 is the smaller cheque"
        else:
            cheque = "The two carry the same entry price"
        return (
            f"The EcoFlow DELTA Pro 3 {_capacity_clause(ctx, a_id, b_id)} the "
            f"Bluetti AC200L and currently {direction}: {a_rate} against "
            f"{b_rate} at the cheapest in-stock offer we track "
            f"({a_price} and {b_price} respectively). {cheque}; {energy}. "
            f"Which matters depends on whether you are buying capacity or "
            f"buying an entry price."
        )
    if a_rate or b_rate:
        known = a_rate or b_rate
        return (
            f"Only one side of this comparison currently has a rateable "
            f"in-stock price ({known}), so Helios is not publishing a "
            f"per-watt-hour verdict today. Both live tables are below."
        )
    return ("Neither unit currently has an in-stock, rateable price at a "
            "tracked retailer, so there is no honest per-watt-hour verdict "
            "to give today. The live tables below show what we do have.")


def _money_value(display: str):
    try:
        return float(display.replace("$", "").replace(",", ""))
    except (AttributeError, ValueError):
        return float("inf")


def _answer_under_2000(ctx):
    top = ctx.top()
    ranking = ctx.rankings[0] if ctx.rankings else None
    if top:
        return (
            f"{top['product']['name']} is the cheapest stored energy under "
            f"$2,000 that we can currently price and rate: "
            f"{top['best']['rating_display']} at "
            f"{top['best']['price_display']} from "
            f"{top['best']['retailer_name']}. That is the answer on cost per "
            f"watt-hour alone — it is a bare rack battery, not a system, and "
            f"the section below sets out what else you still have to buy."
        )
    if ranking and ranking["listed"]:
        return ("Nothing under $2,000 currently has both a published capacity "
                "and an in-stock price, so Helios is not naming a winner "
                "today. Everything in scope is listed below with the reason "
                "it could not be rated.")
    return ("No tracked battery currently has an in-stock offer under $2,000. "
            "The list below is empty on purpose rather than padded.")


def _answer_camping(ctx):
    top = ctx.top()
    if not top:
        return ("No portable power station currently has both a published "
                "capacity and an in-stock price, so there is no ranked answer "
                "today. The full list and the reasons are below.")
    weight = spec_display((top["product"].get("specs") or {}).get("weight_lb"), "lb")
    tail = (f" It weighs {weight}." if weight else
            " Its weight is not published in our catalog, which is itself "
            "worth knowing before you carry it anywhere.")
    return (
        f"On cost per watt-hour, {top['product']['name']} leads the portable "
        f"power stations we track at {top['best']['rating_display']} "
        f"({top['best']['price_display']} from {top['best']['retailer_name']})."
        f"{tail} For camping the ranking is only half the question: the other "
        f"half is what you are willing to lift, and that trade-off is set out "
        f"below."
    )


def _answer_fridge(ctx):
    return (
        "Take the kWh-per-year figure off your fridge's energy label, multiply "
        "by 1,000 and divide by 365. That is the watt-hours it needs per day, "
        "already averaged across the compressor's on/off cycling. Multiply by "
        "the number of days you want to cover, then add headroom for inverter "
        "losses. A label reading 365 kWh/year works out to about 1,000 Wh a "
        "day, so a single day of fridge-only backup needs a battery in the "
        "1,000-1,500 Wh class once losses are allowed for. The worked "
        "arithmetic and live prices for tracked units in that range are below."
    )


def _answer_chemistry(ctx):
    lfp = ctx.rankings[0]["listed"] if ctx.rankings else 0
    ncm = ctx.rankings[1]["listed"] if len(ctx.rankings) > 1 else 0
    return (
        f"LiFePO4 (lithium iron phosphate) trades energy density for cycle "
        f"life and thermal stability; NCM (nickel-cobalt-manganese) trades the "
        f"opposite way, packing more watt-hours into less weight. For home "
        f"backup and most portable power stations the industry has settled on "
        f"LiFePO4, and our catalog reflects that: of the products where a "
        f"chemistry is recorded, {lfp} are LiFePO4 and {ncm} "
        f"{'is' if ncm == 1 else 'are'} NCM. Live examples of each are below, "
        f"with the caveat that we read chemistry off spec sheets rather than "
        f"cells."
    )


def _answer_dollars_per_wh(ctx):
    return (
        "Cost per watt-hour is a purchase price divided by a published "
        "capacity, and it is only meaningful when both halves describe the "
        "same standalone thing. It tells you which battery stores energy most "
        "cheaply. It hides cycle life, usable depth of discharge, inverter "
        "capability, warranty, and everything a kit bundles in. Helios "
        "computes it for one variant at a time and withholds it entirely for "
        "bundles and multi-packs, because a kit price over a battery's "
        "capacity produces a precise-looking number that is wrong by "
        "construction. This page is the full rule set."
    )


def _answer_sales(ctx):
    history = ctx.history
    days = history.get("days") or 0
    span = ""
    if history.get("first_date"):
        span = (f" Our own record currently spans {days} "
                f"day{'s' if days != 1 else ''} of scrapes "
                f"({history['first_date']} to {history['last_date']}), which "
                f"is nowhere near enough to call a cadence ourselves.")
    return (
        "Published deal coverage puts the big power-station discounts on the "
        "US retail-holiday calendar — Earth Day, Memorial Day, Amazon's Prime "
        "Day window, and the Fourth of July — with 48- and 72-hour "
        "manufacturer flash sales running inside those windows. That is the "
        "pattern to plan around, and it is sourced below to dated reporting "
        "rather than to us." + span +
        " What we can offer is the measurement going forward, twice a day, "
        "with every observation dated."
    )


def _audit_sentence(report: dict) -> str:
    """What this build's audit tallies actually license us to say.

    The sentence this replaced asserted that the retailer's published prices
    "match what our audit re-reads from its own product endpoints" — a
    favourable factual claim about a named business, hardcoded, while the
    evidence panel three inches below it showed NOT_AUDITED for every check
    (red team HIGH-1). The audit samples a rotation, so most builds carry no
    verdict for any given retailer, and on those builds the honest sentence
    is that we have nothing to report.
    """
    name = report["name"]
    when = (f" in the audit run of {report['audit_generated']}"
            if report.get("audit_generated") else " in the current audit report")
    if report["defect_verdicts"]:
        count = report["defect_verdicts"]
        return (
            f"{name} is a real Shopify storefront, and our audit currently "
            f"records {count} disagreement"
            f"{'s' if count != 1 else ''} between a figure this site "
            f"rendered and our own stored price for it{when}; those numbers "
            f"are pulled off the site until they verify clean."
        )
    if report["clean_verdicts"]:
        return (
            f"{name} is a real Shopify storefront. Our audit re-read "
            f"{report['clean_verdicts']} of its tracked listings directly "
            f"from the store's own product endpoints{when} and found the "
            f"published price agreeing with both what we stored and what "
            f"this page renders."
        )
    if report["verified_verdicts"]:
        return (
            f"{name} is a real Shopify storefront. Our audit re-read "
            f"{report['verified_verdicts']} of its tracked listings{when}, "
            f"but none of those checks produced a clean price comparison, so "
            f"we are not claiming a price-accuracy result for this retailer "
            f"today."
        )
    return (
        f"{name} is a real Shopify storefront. Our audit re-reads a rotating "
        f"sample of tracked listings rather than all of them on every run, "
        f"and it carries no verdict for this retailer{when} — so this build "
        f"has nothing to say about their price accuracy either way, and the "
        f"tally below says exactly that."
    )


def _position_sentence(report: dict) -> str:
    """The cross-retailer tally, in words, with the buckets that exist.

    Every non-empty bucket is named with the label the panel uses, so the
    numbers in the sentence sum to the number of products the same sentence
    says were compared.
    """
    parts = [f"{p['label']} on {p['count']}" for p in report["position_parts"]
             if p["count"]]
    if not parts:
        return ""
    if len(parts) > 1:
        listed = ", ".join(parts[:-1]) + f", and {parts[-1]}"
    else:
        listed = parts[0]
    noun = "product" if report["compared"] == 1 else "products"
    return (f" On the {report['compared']} {noun} where we can compare it "
            f"against another tracked retailer on an in-stock price, it was "
            f"{listed}.")


def _answer_ssk(ctx):
    report = ctx.reports.get("shop-solar-kits")
    if not report:
        return ("This page reports only what our tracker observes about Shop "
                "Solar Kits. No observations are available in this build.")
    line = (f" It is the broadest catalog we track: {report['mapped_products']} "
            f"of our {report['catalog_total']} products are mapped to it.")
    return (
        _audit_sentence(report) + line + _position_sentence(report) +
        " That is the whole of what we can attest to. We have never bought "
        "from them, so this page says nothing about shipping, support, "
        "returns or warranty handling."
    )


# ---------------------------------------------------------------------------
# The articles
# ---------------------------------------------------------------------------
# Prose blocks are authored HTML and are rendered with |safe. They are
# static strings in this file — no data is ever interpolated into prose, so
# there is nothing for a scraped value to escape into. Numbers live in data
# blocks only.

_NO_HANDS_ON = {
    "kind": "prose",
    "html": (
        '<p class="prov">Helios has not physically tested any product on this '
        'page. Our authority is price data, published specifications and our '
        'own scrape history — that is a narrower claim than a review site '
        'makes, and it is the one we can actually support.</p>'
    ),
}

ARTICLES = [
    {
        "slug": "ecoflow-delta-pro-3-vs-bluetti-ac200l",
        "h1": "EcoFlow DELTA Pro 3 vs Bluetti AC200L",
        "subject": "a head-to-head on tracked prices and published specs",
        "answer": _answer_head_to_head,
        "blocks": [
            {"kind": "prose", "html":
                "<p>These two land in the same shopping list surprisingly "
                "often, and they are not really the same class of machine. "
                "One is a 4 kWh home-backup-scale unit; the other is a 2 kWh "
                "portable. Comparing them is still useful, because the "
                "question most people are actually asking is <em>how much "
                "battery should I buy at this price point</em>.</p>"},
            _NO_HANDS_ON,
            {"kind": "h2", "text": "Published specifications, side by side"},
            {"kind": "prose", "html":
                "<p>Every figure in this table is the value stored in our "
                "catalog, taken from the retailer listings we track. Where a "
                "cell says the spec is not published, it means we have not "
                "recorded one — not that the number is zero.</p>"},
            {"kind": "specs", "ids": ["ecoflow-delta-pro-3", "bluetti-ac200l"]},
            {"kind": "h2", "text": "Live prices: EcoFlow DELTA Pro 3"},
            {"kind": "products", "ids": ["ecoflow-delta-pro-3"], "metric": "wh"},
            {"kind": "h2", "text": "Live prices: Bluetti AC200L"},
            {"kind": "products", "ids": ["bluetti-ac200l"], "metric": "wh"},
            {"kind": "h2", "text": "Reading the two tables together"},
            {"kind": "prose", "html":
                "<p>The comparison that survives scrutiny is cost per "
                "watt-hour on the main unit at each retailer, which is the "
                "figure in the $/Wh column above. Kit variants carry a bundle "
                "badge and no rating, because dividing a bundle price by the "
                "battery's capacity credits the panels and cables to the "
                "battery and produces a number that is wrong by "
                "construction.</p>"
                "<p>Capacity is the honest headline difference. The DELTA Pro "
                "3 stores roughly twice what the AC200L does, and the spec "
                "table above records a substantially higher <em>continuous</em> "
                "output for it, so it can hold up continuous loads the AC200L "
                "cannot. Whether it will <em>start</em> a load the AC200L "
                "cannot is a different question and we are not answering it: "
                "starting surge is a separate rating, our catalog does not "
                "store it, and inferring it from continuous output would be a "
                "guess dressed as a spec. If your constraint is the size of "
                "the cheque rather than the size of the load, that extra "
                "capability is money spent on headroom you may never "
                "use.</p>"},
            {"kind": "h2", "text": "What we cannot compare"},
            {"kind": "prose", "html":
                "<p>This is the part most head-to-heads skip, so here it is "
                "explicitly. We cannot tell you:</p>"
                "<ul>"
                "<li><strong>Real runtime under a real load.</strong> We have "
                "not run either unit. Rated capacity is a nameplate figure; "
                "what you get depends on the load, the temperature and the "
                "inverter's efficiency at that draw.</li>"
                "<li><strong>Cycle life in practice.</strong> Both are "
                "LiFePO4 and both manufacturers publish cycle ratings. We "
                "have not verified either, and we have not owned either long "
                "enough to have an opinion.</li>"
                "<li><strong>Noise, app quality, firmware, or support.</strong> "
                "No data. Not measured, not inferred.</li>"
                "<li><strong>Warranty handling.</strong> We have never filed "
                "a claim with either company.</li>"
                "</ul>"
                "<p>Retailer coverage is also uneven between these two, and "
                "the tables above show it: a product carried by one tracked "
                "retailer has no cross-retailer price check at all, so its "
                "price is a single observation rather than a corroborated "
                "one.</p>"},
        ],
    },
    {
        "slug": "best-home-backup-battery-under-2000",
        "h1": "Best home backup battery under $2,000",
        "subject": "tracked home-backup batteries with an in-stock price under $2,000",
        "answer": _answer_under_2000,
        "blocks": [
            {"kind": "prose", "html":
                "<p>Under $2,000 you are shopping for a battery, not a "
                "system. That distinction drives everything below: the "
                "cheapest stored energy at this budget comes from bare "
                "server-rack modules that assume you already own — or are "
                "about to buy — an inverter, a rack and the wiring to "
                "connect them.</p>"},
            _NO_HANDS_ON,
            {"kind": "h2", "text": "The ranking, live"},
            {"kind": "prose", "html":
                "<p>Scope is every tracked product in the rack, wall-mount "
                "and expansion-battery categories whose cheapest "
                "<em>in-stock</em> offer is at or under $2,000. Sold-out "
                "bargains do not qualify — a price you cannot act on is not "
                "a price.</p>"},
            {"kind": "ranking",
             "categories": ("server-rack-battery", "home-battery",
                            "expansion-battery"),
             "metric": "wh", "max_price": 2000},
            {"kind": "h2", "text": "What the ranking does not include"},
            {"kind": "prose", "html":
                "<p>A rack battery is a component. Budget for an inverter or "
                "hybrid charger, a rack or wall bracket, cabling of the right "
                "gauge, and in most jurisdictions an electrician and a "
                "permit. None of that is in the prices above, so the total "
                "installed cost of any battery here is meaningfully more "
                "than its sticker price.</p>"
                "<p>Expansion batteries are cheaper per watt-hour than the "
                "stations they attach to, and they are useless on their own. "
                "Where one appears above, it is priced as the accessory it "
                "is.</p>"
                "<p>Anything listed without a $/Wh figure is there because we "
                "could not honestly compute one — the reason is printed "
                "against each entry rather than hidden.</p>"},
        ],
    },
    {
        "slug": "best-power-station-for-camping",
        "h1": "Best power station for camping",
        "subject": "tracked portable power stations ranked on price per watt-hour",
        "answer": _answer_camping,
        "blocks": [
            {"kind": "prose", "html":
                "<p>Camping is the use case where the cheapest watt-hour is "
                "most often the wrong answer. Every extra watt-hour is extra "
                "mass, and mass is the constraint that actually bites when "
                "the thing has to come out of a car and go somewhere.</p>"},
            _NO_HANDS_ON,
            {"kind": "h2", "text": "Ranked on cost per watt-hour"},
            {"kind": "ranking", "categories": ("portable-power-station",),
             "metric": "wh"},
            {"kind": "h2", "text": "The weight trade-off"},
            {"kind": "prose", "html":
                "<p>Below is every weight our catalog actually stores for "
                "these products. It is not a complete set, and the gaps are "
                "shown as gaps: we record a weight when a tracked listing "
                "states one, and we do not estimate the rest.</p>"},
            {"kind": "specs",
             "ids": ["ecoflow-river-3", "ecoflow-river-2-pro", "bluetti-ac180",
                     "ecoflow-delta-max", "bluetti-ac200l", "anker-solix-f2600"],
             "fields": ["capacity_wh", "weight_lb", "output_w"]},
            {"kind": "prose", "html":
                "<p>Read that table against the ranking above and the shape "
                "of the decision appears: the cheapest energy per watt-hour "
                "generally sits in the larger, heavier units, because the "
                "fixed cost of the inverter, case and electronics is spread "
                "over more cells. A small station is nearly always worse "
                "value per watt-hour and better value per kilogram carried.</p>"
                "<p>What we cannot tell you is how any of these behave in a "
                "tent at 2&nbsp;a.m. — fan noise, cold-weather charge "
                "behaviour, how the app copes without signal, whether the "
                "handle is comfortable after two hundred metres. We have not "
                "used them. Where a review site would give you an opinion, we "
                "give you the price history and the published numbers, and "
                "you should read an owner's account for the rest.</p>"},
        ],
    },
    {
        "slug": "how-many-watt-hours-to-run-a-refrigerator",
        "h1": "How many watt-hours do you need to run a fridge?",
        "subject": "sizing a battery for refrigerator backup, with worked arithmetic",
        "answer": _answer_fridge,
        "blocks": [
            {"kind": "h2", "text": "Start with the label, not a rule of thumb"},
            {"kind": "prose", "html":
                "<p>A fridge does not draw its rated wattage continuously. "
                "The compressor cycles on and off, so the number that matters "
                "is energy over time, not power at an instant. Every fridge "
                "sold with an energy label carries that figure already: "
                "kilowatt-hours per year, measured over a standardised test "
                "cycle.</p>"
                "<p>That single number does the work. You do not need to "
                "guess a duty cycle, and you should be sceptical of any guide "
                "that hands you an average wattage for &ldquo;a "
                "fridge&rdquo; — the range across sizes and ages is enormous, "
                "and your own label is authoritative for your own "
                "appliance.</p>"},
            {"kind": "h2", "text": "The arithmetic"},
            {"kind": "prose", "html":
                "<p>Suppose the label reads <strong>365 kWh per year</strong>. "
                "That is an assumed example input, not a claim about your "
                "fridge:</p>"
                "<ol>"
                "<li>365 kWh/year &times; 1,000 = 365,000 Wh per year.</li>"
                "<li>365,000 &divide; 365 days = <strong>1,000 Wh per "
                "day</strong>.</li>"
                "<li>Two days of cover = 2,000 Wh of <em>delivered</em> "
                "energy.</li>"
                "<li>Inverter and conversion losses are real and vary by unit "
                "and load; add headroom rather than assuming a figure. Losses "
                "divide, they do not add: if you assume 20% of what the "
                "battery holds is lost on the way to the appliance, the "
                "nameplate capacity you need is 2,000 &divide; 0.8 = "
                "<strong>2,500 Wh</strong>, not 2,000 &times; 1.2.</li>"
                "</ol>"
                "<p>Run the same three lines with your own label figure and "
                "you have your answer. The reason this article does not "
                "publish a tidy &ldquo;fridges need X&rdquo; number is that "
                "the honest version of that number is a range so wide it "
                "would not help you.</p>"},
            {"kind": "h2", "text": "Two more things the arithmetic misses"},
            {"kind": "prose", "html":
                "<p><strong>Starting surge.</strong> A compressor draws far "
                "more at the moment it starts than while it runs. Energy "
                "capacity does not help here; the inverter's rated and surge "
                "output does. Check your appliance's requirement against the "
                "rated output figures in the table below.</p>"
                "<p><strong>Usable versus nameplate.</strong> Published "
                "capacity is not always the energy you can draw. We publish "
                "the capacity the retailer's listing states, which is the "
                "same figure the manufacturer markets.</p>"},
            _NO_HANDS_ON,
            {"kind": "h2", "text": "Tracked units in the relevant range"},
            {"kind": "prose", "html":
                "<p>Live prices, refreshed on every build. The smallest units "
                "here cover well under a day of the worked example above; the "
                "largest cover several.</p>"},
            {"kind": "products",
             "ids": ["ecoflow-river-2-pro", "bluetti-ac200l",
                     "ecoflow-delta-pro-3"],
             "metric": "wh"},
        ],
    },
    {
        "slug": "lifepo4-vs-ncm-plain-english",
        "h1": "LiFePO4 vs NCM, in plain English",
        "subject": "what the two cell chemistries trade against each other",
        "answer": _answer_chemistry,
        "blocks": [
            {"kind": "h2", "text": "The trade, in one paragraph"},
            {"kind": "prose", "html":
                "<p>Both are lithium-ion. The difference is what the cathode "
                "is made of, and that choice sets everything else. "
                "<strong>LiFePO4</strong> — lithium iron phosphate, sometimes "
                "written LFP — uses iron and phosphate: cheap, abundant, "
                "chemically stable, and comparatively heavy for the energy it "
                "stores. <strong>NCM</strong> — nickel, cobalt, manganese, "
                "sometimes NMC — packs more energy into less mass and volume, "
                "which is why it dominated the first generation of portable "
                "power stations and why it remains common in electric "
                "vehicles where range and pack density set the design, "
                "particularly in Europe and in premium long-range models. It "
                "is no longer the majority chemistry in EVs globally, though: "
                "the IEA puts LFP at more than half of all EV batteries "
                "deployed worldwide in 2025, overtaking the nickel-based "
                "chemistries.</p>"},
            {"kind": "citations", "sources": [
                {"title": "Electric vehicle batteries — Global EV Outlook 2026",
                 "publisher": "International Energy Agency", "date": "2026-05-20",
                 "url": "https://www.iea.org/reports/global-ev-outlook-2026/"
                        "electric-vehicle-batteries",
                 "note": "LFP accounted for over 55% of EV batteries deployed "
                         "globally in 2025, up from nearly 50% in 2024, "
                         "passing the nickel-based chemistries. We have not "
                         "independently verified these figures; we are citing "
                         "them."},
            ]},
            {"kind": "h2", "text": "What each one is better at"},
            {"kind": "prose", "html":
                "<ul>"
                "<li><strong>Cycle life</strong> favours LiFePO4, usually by a "
                "wide margin. Manufacturers typically rate LFP packs for "
                "several thousand cycles to 80% capacity, and NCM packs for "
                "something in the 800 to 2,000 range. Those are published "
                "ratings, not our measurements.</li>"
                "<li><strong>Energy density</strong> favours NCM. For the same "
                "watt-hours, an NCM pack is lighter and smaller.</li>"
                "<li><strong>Thermal behaviour</strong> favours LiFePO4, which "
                "is the main reason it has taken over stationary storage: it "
                "is the more forgiving chemistry when something goes "
                "wrong.</li>"
                "<li><strong>Cost per watt-hour</strong> currently favours "
                "LiFePO4 on the products we track, which is visible directly "
                "in the tables below rather than asserted here.</li>"
                "</ul>"
                "<p>For a battery that lives in a garage and cycles daily for "
                "a decade, the density penalty costs you floor space and the "
                "cycle life pays you back every day. For something you carry, "
                "the trade is genuinely closer.</p>"},
            _NO_HANDS_ON,
            {"kind": "h2", "text": "LiFePO4 in our catalog, live"},
            {"kind": "prose", "html":
                "<p>Every tracked product whose listing states LiFePO4 cells, "
                "ranked on cost per watt-hour.</p>"},
            {"kind": "ranking", "chemistry": "LiFePO4", "metric": "wh",
             "compact": True},
            {"kind": "h2", "text": "NCM in our catalog, live"},
            {"kind": "prose", "html":
                "<p>The other side of the comparison is thin, and that is "
                "itself the finding: the market has moved. Here is every "
                "tracked product recorded as NCM.</p>"},
            {"kind": "ranking", "chemistry": "NCM", "metric": "wh"},
            {"kind": "h2", "text": "How we know the chemistry"},
            {"kind": "prose", "html":
                "<p>From the retailer's own listing text, recorded in our "
                "catalog when the listing states it. We have not opened a "
                "pack or tested a cell, and where a listing does not state a "
                "chemistry we leave the field empty rather than infer one "
                "from the brand or the price. Products with no recorded "
                "chemistry appear in neither table above.</p>"},
        ],
    },
    {
        "slug": "what-dollars-per-wh-tells-you",
        "h1": "What $/Wh tells you, and what it hides",
        "subject": "the full rule set behind every cost-per-watt-hour figure on this site",
        "answer": _answer_dollars_per_wh,
        "blocks": [
            {"kind": "prose", "html":
                "<p>Cost per watt-hour is the most useful single number in "
                "battery shopping and the easiest one to compute wrongly. "
                "This page is both an explainer and our public specification: "
                "the rules below are the rules the site enforces in code, not "
                "aspirations.</p>"},
            {"kind": "h2", "text": "What it is"},
            {"kind": "prose", "html":
                "<p>A retailer's price for one standalone product, divided by "
                "that product's published capacity in watt-hours. Nothing "
                "else. It answers exactly one question — which battery stores "
                "energy most cheaply — and answers it well.</p>"},
            {"kind": "h2", "text": "The four rules we enforce"},
            {"kind": "prose", "html":
                "<ol>"
                "<li><strong>One variant at a time.</strong> The price and the "
                "rating in any row always describe the same variant. Pairing "
                "a discounted bundle's price with a bare unit's rating is a "
                "defect we have shipped once and now test against.</li>"
                "<li><strong>Bundles get no rating.</strong> If the listing "
                "sells a kit, a multi-pack or a battery-plus-panels bundle, "
                "we show the price and withhold the $/Wh. A kit price over a "
                "battery's capacity is wrong by construction, and the wrongness "
                "scales with how good the kit is.</li>"
                "<li><strong>Unknown capacity withholds.</strong> Capacity is "
                "used only where a tracked listing states it, backed by a "
                "verbatim quote from that listing. Where two retailers "
                "disagree about a capacity, we publish no rating for that "
                "product at any retailer.</li>"
                "<li><strong>Stale and disputed numbers come off the "
                "page.</strong> Prices older than our staleness threshold are "
                "withheld rather than shown with a caveat, and any figure our "
                "audit finds disagreeing with its own source is quarantined "
                "until it verifies clean.</li>"
                "</ol>"},
            {"kind": "h2", "text": "The rule, visible"},
            {"kind": "prose", "html":
                "<p>Here is the discipline working on a real product. The "
                "single-battery variant carries a rating; the multi-battery "
                "variants of the <em>same</em> product carry prices and a "
                "bundle badge, and no rating at all — because we do not know "
                "from the listing alone that a &ldquo;2 Batteries&rdquo; "
                "variant is exactly twice the capacity.</p>"},
            {"kind": "products", "ids": ["eg4-ll-s-48v-100ah"], "metric": "wh"},
            {"kind": "h2", "text": "What it hides"},
            {"kind": "prose", "html":
                "<ul>"
                "<li><strong>Cycle life.</strong> Two batteries at the same "
                "$/Wh are not the same purchase if one lasts three times as "
                "long. Divide by rated cycles and the ranking can invert.</li>"
                "<li><strong>Usable depth of discharge.</strong> Nameplate "
                "capacity is not always drawable capacity.</li>"
                "<li><strong>Everything that is not the battery.</strong> "
                "Inverter capability, outlets, solar input, app, warranty, and "
                "whether the thing needs an electrician.</li>"
                "<li><strong>Total installed cost.</strong> Especially for "
                "rack batteries, where the battery is a component of a system "
                "you have to finish buying.</li>"
                "<li><strong>Shipping and tax.</strong> Not in any price on "
                "this site.</li>"
                "</ul>"
                "<p>Use $/Wh to shortlist and to catch a bad price. Do not use "
                "it to pick a winner on its own.</p>"},
            {"kind": "h2", "text": "Why we would rather show nothing"},
            {"kind": "prose", "html":
                "<p>A missing number is a gap a reader can see and work "
                "around. A wrong number looks exactly like a right one. Every "
                "withheld rating on this site prints the rule that withheld "
                "it, so you can judge whether the gap matters to you rather "
                "than wondering whether we simply failed to look.</p>"},
        ],
    },
    {
        "slug": "when-do-power-stations-go-on-sale",
        "h1": "When do power stations actually go on sale?",
        "subject": "the discount calendar, from dated external reporting plus our own measurements",
        "answer": _answer_sales,
        "blocks": [
            {"kind": "h2", "text": "The pattern, from published coverage"},
            {"kind": "prose", "html":
                "<p>Power-station pricing is not a smooth market. It runs on "
                "manufacturer-led promotional windows anchored to the US "
                "retail calendar, with short flash sales inside them. The "
                "deal-coverage record is the best public evidence of that "
                "cadence, so here it is, dated and linked rather than "
                "summarised from memory.</p>"},
            {"kind": "citations", "sources": [
                {"title": "Electrified Weekly — Earth Day power station sales "
                          "from EcoFlow + Bluetti, 72-hour Anker SOLIX flash sale",
                 "publisher": "9to5Toys", "date": "2026-04-11",
                 "url": "https://9to5toys.com/2026/04/11/electrified-weekly-earth-day-power-station-sales-ecoflow-bluetti-anker-solix-exclusive-new-lows-more/",
                 "note": "Earth Day promotions from two manufacturers running "
                         "alongside a 72-hour Anker SOLIX weekend flash sale."},
                {"title": "Bluetti exclusive Memorial Day power station lows, "
                          "EcoFlow 48-hour flash sale",
                 "publisher": "Electrek", "date": "2026-05-25",
                 "url": "https://electrek.co/2026/05/25/bluetti-exclusive-memorial-day-power-station-lows-ecoflow-48-hour-flash-sale-power-stations-more/",
                 "note": "A 48-hour EcoFlow flash sale running through May 26, "
                         "nested inside a longer seasonal sale."},
                {"title": "EcoFlow Early Prime Day power station deals up to "
                          "62% off",
                 "publisher": "Electrek", "date": "2026-06-08",
                 "url": "https://electrek.co/2026/06/08/ecoflow-early-prime-day-sale-48-hour-flash-sale-power-station-from-149-more/",
                 "note": "A 48-hour flash sale inside an 'early Prime Day' "
                         "window, weeks ahead of Amazon's own event."},
                {"title": "July 4th Green Deals: Bluetti Summer Power Sale, "
                          "EcoFlow flash sale",
                 "publisher": "Electrek", "date": "2026-07-03",
                 "url": "https://electrek.co/2026/07/03/july-4th-bluetti-summer-power-sale-hiboy-ex11-full-suspension-e-bike-ecoflow-more/",
                 "note": "Independence Day promotions, with one manufacturer "
                         "running a season-long sale in place of a holiday "
                         "event."},
            ]},
            {"kind": "prose", "html":
                "<p>Read together, those reports describe anchors rather than "
                "a cadence. The promotional windows they cover sit on the US "
                "retail calendar — Earth Day, Memorial Day, the Prime Day "
                "period and the Fourth of July — with 48- to 72-hour flash "
                "sales used as the sharpest price point inside each window, "
                "and with off-cycle events (an &ldquo;early Prime Day&rdquo; "
                "weeks ahead of Amazon's own) sitting between them. The "
                "intervals between the dated reports above are uneven, as "
                "the line under them shows, so what this evidence supports "
                "is several promotional windows inside any given quarter, "
                "not a clock you can set. The flash sales are where the "
                "genuine lows appear, and they are short enough that you "
                "have to already be watching.</p>"
                "<p>Note what those citations are and are not. They are "
                "evidence that promotional windows exist and roughly when. "
                "They are not evidence that any particular product hits its "
                "lowest price in them, and we have not verified the "
                "individual prices quoted in that coverage.</p>"},
            {"kind": "h2", "text": "What our own tracker has seen so far"},
            {"kind": "callout", "html":
                "<p><strong>Our price history is very short.</strong> Helios "
                "began recording on 2026-08-13. A discount calendar needs "
                "months of observations to say anything defensible, and we do "
                "not have them yet. Rather than dress up a few days of data "
                "as a trend, or quietly reprint someone else's history as "
                "though it were ours, we are stating the gap.</p>"},
            {"kind": "history"},
            {"kind": "prose", "html":
                "<p>What that record <em>can</em> already do is catch a price "
                "move between two scrapes and show it with a timestamp. What "
                "it cannot yet do is tell you whether today's price is high "
                "or low by this product's own standards, because it has no "
                "standard to compare against.</p>"},
            {"kind": "h2", "text": "What the tracked stations cost right now"},
            {"kind": "prose", "html":
                "<p>Live prices, re-read on every build. If a promotional "
                "window is open as you read this, it shows up here first — "
                "and if these prices look ordinary, that is the honest "
                "answer to whether there is a sale on today.</p>"},
            {"kind": "ranking", "categories": ("portable-power-station",),
             "metric": "wh", "compact": True},
            {"kind": "h2", "text": "How to actually catch a flash sale"},
            {"kind": "prose", "html":
                "<ul>"
                "<li>Decide your target price before the window opens. A 48-hour "
                "sale is designed to prevent deliberation.</li>"
                "<li>Watch the manufacturer's own store as well as the "
                "retailers — several of the sales in the coverage above were "
                "manufacturer-direct.</li>"
                "<li>Check the compare-at price against a real history, not "
                "against the retailer's own struck-through figure.</li>"
                "<li>Remember that the sharpest discounts in that coverage "
                "attach to specific SKUs, not to whole ranges.</li>"
                "</ul>"},
            {"kind": "h2", "text": "Price alerts: planned, not built"},
            {"kind": "prose", "html":
                "<p>The obvious thing to build on a twice-daily price record "
                "is an alert when a tracked product drops below a threshold "
                "you set. That is <strong>planned and does not exist "
                "today</strong> — there is no signup, no notification, and no "
                "waiting list, and this paragraph exists so nobody reads the "
                "rest of the page as implying otherwise. When it ships it "
                "will be announced on this site.</p>"},
        ],
    },
    {
        "slug": "is-shop-solar-kits-legit",
        "h1": "Is Shop Solar Kits legit? What our tracker observes",
        "subject": "observed price-accuracy, catalog breadth and cross-retailer position",
        "answer": _answer_ssk,
        "blocks": [
            {"kind": "callout", "html":
                "<p><strong>Scope of this page.</strong> This is not a review. "
                "Helios has never bought anything from Shop Solar Kits, "
                "contacted their support, returned an item or filed a warranty "
                "claim. Everything below is an observation our own tracker "
                "made about their published data. If you want to know whether "
                "their service is good, this page cannot tell you, and any "
                "page that answers that question from price data alone is "
                "guessing.</p>"},
            {"kind": "h2", "text": "What we observe"},
            {"kind": "retailer_report", "retailer_id": "shop-solar-kits"},
            {"kind": "h2", "text": "Price accuracy"},
            {"kind": "prose", "html":
                "<p>The strongest thing we can say about a retailer is "
                "boring: the prices they publish are the prices they publish "
                "consistently. Our audit re-reads a sample of tracked "
                "products directly from the store's own product endpoints on "
                "a schedule and compares them against both the price we "
                "stored and the figure rendered on this site. A disagreement "
                "between the live store and our record is expected and "
                "harmless — prices move. A disagreement between our record "
                "and our own page is a defect, and it pulls the number off "
                "the site until it verifies clean.</p>"
                "<p>The verdict tallies above are that check, restricted to "
                "this retailer. They speak to whether the storefront's "
                "published data is stable and machine-readable. They say "
                "nothing about what happens after you press buy.</p>"},
            {"kind": "h2", "text": "Cross-retailer position"},
            {"kind": "prose", "html":
                "<p>Where two or more tracked retailers carry the same "
                "product and both have it in stock, we can say which was "
                "cheaper at the moment we looked. That tally is above. It is "
                "a snapshot of a small sample, not a claim that any retailer "
                "is generally cheapest — with a catalog this size and a price "
                "history this short, anyone claiming otherwise from this data "
                "is overreaching.</p>"},
            {"kind": "h2", "text": "Where the same product diverges"},
            {"kind": "prose", "html":
                "<p>These are variants that more than one tracked retailer "
                "publishes under the same SKU, with both sides in stock. Same "
                "SKU, different price, shown with each retailer's own "
                "label.</p>"},
            {"kind": "products",
             "ids": ["ecoflow-delta-pro-3", "ecoflow-river-3",
                     "ecoflow-delta-max"],
             "metric": "wh"},
            {"kind": "h2", "text": "What we deliberately do not know"},
            {"kind": "prose", "html":
                "<ul>"
                "<li><strong>Shipping cost, free-shipping thresholds and "
                "delivery times.</strong> Our retailer records contain no "
                "shipping data at all. We are not going to source it from "
                "somewhere unverifiable and present it as tracked.</li>"
                "<li><strong>Support, returns and warranty handling.</strong> "
                "Never used. No data.</li>"
                "<li><strong>Order accuracy or fulfilment reliability.</strong> "
                "Never ordered. No data.</li>"
                "<li><strong>Whether the discount framing is fair.</strong> We "
                "record a retailer's struck-through compare-at price when "
                "there is one, but we have not tracked long enough to say "
                "whether those reference prices are real.</li>"
                "</ul>"
                "<p>Our commercial position is disclosed in full on the "
                "affiliate disclosure page, and it currently amounts to: we "
                "earn nothing from this retailer or any other.</p>"},
        ],
    },
]


def article_by_slug(slug: str) -> dict | None:
    for spec in ARTICLES:
        if spec["slug"] == slug:
            return spec
    return None


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
    handle_maps = load_json(handle_maps_path) if handle_maps_path.exists() else None
    latest = filter_to_mapped_pairs(latest, handle_maps)

    env = Environment(
        loader=FileSystemLoader(templates_dir),
        autoescape=select_autoescape(["html"]),
    )

    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "products").mkdir(parents=True, exist_ok=True)
    (site_dir / "guides").mkdir(parents=True, exist_ok=True)
    (site_dir / "articles").mkdir(parents=True, exist_ok=True)

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

    # --- articles ----------------------------------------------------------
    history = _history_facts(data_dir, products)
    for spec in ARTICLES:
        view = resolve_article(spec, products, latest, retailers_by_id,
                               handle_maps, quarantine, facts, history,
                               data_dir, now)
        # Meta description leads with the article's own live answer, so a
        # search result carries a current number rather than a slogan.
        write_page(
            f"articles/{spec['slug']}.html", "article.html",
            f"{spec['h1']} — {SITE_NAME}",
            view["answer"],
            facts=facts, **view,
        )

    write_page(
        "articles/index.html", "articles_index.html",
        f"Articles — {SITE_NAME}",
        f"{len(ARTICLES)} explainers and comparisons on solar and home-energy "
        f"gear, with live prices from {facts['retailer_count']} tracked "
        f"retailers re-rendered on every build. No physical product reviews.",
        articles=ARTICLES, author=AUTHOR, facts=facts,
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
        "article_pages": len(ARTICLES),
        # articles index + about + disclosure
        "info_pages": 3,
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

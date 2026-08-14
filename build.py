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
_BUNDLE_RE = re.compile(
    r'(kit|bundle|\+|&|\bw/|\bwith\b|\band\b|\d+\s*-?\s*packs?\b|'
    r'expansion|extra\s+batter|spare\s+batter|'
    r'panel(s)?\b.*\bx\b|x\s*\d+\s*w)',
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


def dollars_per_wh(price, capacity_wh, classification: str):
    """$/Wh, or None when it must be withheld (PLAN section 2b).

    None when: the variant is not a unit, capacity is unknown/non-positive,
    or the price is unusable. Never guess.
    """
    if classification != "unit":
        return None
    if not isinstance(capacity_wh, (int, float)) or capacity_wh <= 0:
        return None
    if not isinstance(price, (int, float)) or price <= 0:
        return None
    return price / capacity_wh


def money(value) -> str:
    """Two-decimal money formatting: 1408.1 -> "$1,408.10"."""
    return f"${value:,.2f}"


def format_dollars_per_wh(value) -> str:
    return f"${value:,.2f}/Wh"


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
        "price_display": money(price) if isinstance(price, (int, float)) else "",
        "was_price_display": money(was) if isinstance(was, (int, float)) else None,
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
    return {"product": product, "retailer_sections": sections}


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
            priced = [
                (tier, d) for tier, d in row["variants"].items()
                if isinstance(d, dict) and isinstance(d.get("price"), (int, float))
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
                "price_display": money(cheapest["price"]),
                "is_bundle": cls == "bundle",
                "dollars_per_wh_display": (
                    format_dollars_per_wh(per_wh) if per_wh is not None else None
                ),
                "available": cheapest.get("available"),
                "avail_value": _avail_value(cheapest.get("available")),
                "asof": age_display(hours),
            })
        rows.append({"product": product, "cells": cells})
    return rows


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

    env = Environment(
        loader=FileSystemLoader(templates_dir),
        autoescape=select_autoescape(["html"]),
    )

    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "products").mkdir(parents=True, exist_ok=True)

    home_rows = build_home_rows(products, latest, active_retailers,
                                quarantine=quarantine, now=now)
    home_html = env.get_template("home.html").render(
        rows=home_rows, retailers=active_retailers
    )
    # newline="\n": working-tree bytes stay LF on Windows regardless of the
    # machine's core.autocrlf — same rule as the JSONL writer in runner.py.
    (site_dir / "index.html").write_text(home_html, encoding="utf-8", newline="\n")

    pages_written = 1
    for product in products:
        view = build_product_page(
            product, latest.get(product["id"], {}), retailers_by_id,
            quarantine=quarantine, now=now,
        )
        html = env.get_template("product.html").render(**view)
        (site_dir / "products" / f"{product['id']}.html").write_text(
            html, encoding="utf-8", newline="\n"
        )
        pages_written += 1

    return {
        "pages_written": pages_written,
        "products": len(products),
        "products_with_prices": len(latest),
        "quarantined": len(quarantine),
    }


if __name__ == "__main__":
    summary = build_site()
    print(
        f"Built {summary['pages_written']} page(s): "
        f"{summary['products']} products, "
        f"{summary['products_with_prices']} with price data -> {SITE_DIR}"
    )
    sys.exit(0)

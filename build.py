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

def _variant_view(tier: str, data: dict, capacity_wh, product_title: str) -> dict:
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
        "buy_url": _buy_url(data.get("_row_url", ""), data.get("variant_id")),
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


def build_product_page(product: dict, price_rows: dict, retailers_by_id: dict) -> dict:
    """View model for one product page."""
    capacity_wh = (product.get("specs") or {}).get("capacity_wh")
    sections = []
    for retailer_id, row in sorted(price_rows.items()):
        variants = []
        for tier, data in (row.get("variants") or {}).items():
            if not isinstance(data, dict):
                continue
            data = dict(data)
            data["_row_url"] = row.get("url", "")
            variants.append(_variant_view(tier, data, capacity_wh, product.get("name", "")))
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
            "timestamp": (row.get("timestamp") or "")[:10],
            "in_stock": row.get("in_stock"),
            "variants": variants,
        })
    return {"product": product, "retailer_sections": sections}


def build_home_rows(products: list[dict], latest: dict,
                    active_retailers: list[dict]) -> list[dict]:
    """View model rows for the home table (products x retailers)."""
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
            # The cell's price and $/Wh MUST come from the SAME variant
            # (red team #2, MAJOR-1). The first version paired the cheapest
            # variant's price with the cheapest UNIT variant's $/Wh — on
            # real wild-oak-trail data that renders a $509 "+110W Panel"
            # bundle price beside the $569 unit's $0.74/Wh, a number that
            # describes a purchase the cell price does not buy. If the
            # cheapest variant is a bundle, the cell shows a bundle badge
            # and no $/Wh.
            cls = classify_variant(
                cheapest.get("raw_variant", ""), product.get("name", "")
            )
            per_wh = dollars_per_wh(cheapest["price"], capacity_wh, cls)
            cells.append({
                "price": cheapest["price"],
                "price_display": money(cheapest["price"]),
                "is_bundle": cls == "bundle",
                "dollars_per_wh_display": (
                    format_dollars_per_wh(per_wh) if per_wh is not None else None
                ),
                "in_stock": row.get("in_stock"),
            })
        rows.append({"product": product, "cells": cells})
    return rows


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_site(data_dir: Path = DATA_DIR, site_dir: Path = SITE_DIR,
               templates_dir: Path = TEMPLATES_DIR) -> dict:
    """Render the whole site. Returns a small summary dict for callers/tests."""
    products = [p for p in load_json(data_dir / "products.json") if p.get("active") is True]
    retailers = load_json(data_dir / "retailers.json")
    retailers_by_id = {r["id"]: r for r in retailers}
    active_retailers = [r for r in retailers if r.get("active")]

    latest = load_latest_prices(data_dir / "prices", [p["id"] for p in products])

    env = Environment(
        loader=FileSystemLoader(templates_dir),
        autoescape=select_autoescape(["html"]),
    )

    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "products").mkdir(parents=True, exist_ok=True)

    home_rows = build_home_rows(products, latest, active_retailers)
    home_html = env.get_template("home.html").render(
        rows=home_rows, retailers=active_retailers
    )
    # newline="\n": working-tree bytes stay LF on Windows regardless of the
    # machine's core.autocrlf — same rule as the JSONL writer in runner.py.
    (site_dir / "index.html").write_text(home_html, encoding="utf-8", newline="\n")

    pages_written = 1
    for product in products:
        view = build_product_page(
            product, latest.get(product["id"], {}), retailers_by_id
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
    }


if __name__ == "__main__":
    summary = build_site()
    print(
        f"Built {summary['pages_written']} page(s): "
        f"{summary['products']} products, "
        f"{summary['products_with_prices']} with price data -> {SITE_DIR}"
    )
    sys.exit(0)

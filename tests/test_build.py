"""Tests for the minimal static site builder (build.py).

Edge cases per PLAN section 2: missing capacity, missing retailer_name,
sold-out product — plus empty-variant rows and the optional price_anomaly
key, which consumers must tolerate (C4)."""

import json
from pathlib import Path

from build import build_site

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


def _write_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _write_jsonl(path: Path, rows):
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _seed_data(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    prices = data_dir / "prices"
    prices.mkdir(parents=True)

    _write_json(data_dir / "products.json", [
        {
            "id": "product-a", "name": "Test Station 1000", "brand": "TestCo",
            "category": "portable-power-station",
            "specs": {"capacity_wh": 1000, "output_w": 1200,
                      "chemistry": "LiFePO4", "weight_lb": 25.0},
            "active": True, "notes": None,
        },
        {
            "id": "product-b", "name": "Mystery Station", "brand": "TestCo",
            "category": "portable-power-station",
            # capacity unknown -> every $/Wh withheld (PLAN 2b)
            "specs": {"capacity_wh": None, "output_w": 1800,
                      "chemistry": "LiFePO4", "weight_lb": None},
            "active": True, "notes": None,
        },
        {
            "id": "product-c", "name": "Unreadable Station", "brand": "TestCo",
            "category": "portable-power-station",
            "specs": {"capacity_wh": 500, "output_w": 500,
                      "chemistry": "LiFePO4", "weight_lb": 10.0},
            "active": True, "notes": None,
        },
        {
            "id": "product-d", "name": "Never Scraped", "brand": "TestCo",
            "category": "portable-power-station",
            "specs": {"capacity_wh": 500, "output_w": 500,
                      "chemistry": "LiFePO4", "weight_lb": 10.0},
            "active": True, "notes": None,
        },
        {
            "id": "product-inactive", "name": "Retired Station", "brand": "TestCo",
            "category": "portable-power-station",
            "specs": {"capacity_wh": 500, "output_w": 500,
                      "chemistry": "LiFePO4", "weight_lb": 10.0},
            "active": False, "notes": None,
        },
    ])

    _write_json(data_dir / "retailers.json", [
        {"id": "r1", "name": "Retailer One", "url": "https://r1.example",
         "scraper_type": "shopify", "affiliate": None, "active": True,
         "priority": 1},
    ])

    _write_jsonl(prices / "product-a.jsonl", [
        # older row: must lose to the later one
        {"retailer_id": "r1", "retailer_name": "Retailer One",
         "timestamp": "2026-08-10T00:00:00+00:00",
         "url": "https://r1.example/products/test-station",
         "variants": {"main-unit-only": {
             "price": 480.00, "was_price": None, "available": True,
             "raw_variant": "Station [Main Unit Only]", "variant_id": 111}},
         "in_stock": True},
        {"retailer_id": "r1", "retailer_name": "Retailer One",
         "timestamp": "2026-08-13T00:00:00+00:00",
         "url": "https://r1.example/products/test-station?variant=111",
         "variants": {
             "main-unit-only": {
                 "price": 500.00, "was_price": 600.00, "available": True,
                 "raw_variant": "Station [Main Unit Only]", "variant_id": 111},
             "kit-1-x-200w-panel": {
                 "price": 800.00, "was_price": None, "available": False,
                 "raw_variant": "Station Kit [1 x 200W Panel]", "variant_id": 222},
         },
         "in_stock": True,
         # optional key (C4): consumers must not choke on it
         "price_anomaly": ["ANOMALY: product-a at r1 tier=main-unit-only"]},
    ])

    _write_jsonl(prices / "product-b.jsonl", [
        # Hostile row: retailer_name missing (optional, C4), retailer id not
        # in retailers.json, product sold out.
        {"retailer_id": "mystery-solar",
         "timestamp": "2026-08-13T00:00:00+00:00",
         "url": "https://mystery.example/products/mystery-station",
         "variants": {"main-unit-only": {
             "price": 700.00, "was_price": None, "available": False,
             "raw_variant": "Mystery Station [Main Unit Only]", "variant_id": 333}},
         "in_stock": False},
    ])

    _write_jsonl(prices / "product-c.jsonl", [
        {"retailer_id": "r1", "retailer_name": "Retailer One",
         "timestamp": "2026-08-13T00:00:00+00:00",
         "url": "https://r1.example/products/unreadable-station",
         "variants": {}, "in_stock": None, "no_sizes_readable": True},
    ])

    return data_dir


def _build(tmp_path):
    data_dir = _seed_data(tmp_path)
    site_dir = tmp_path / "site"
    summary = build_site(data_dir=data_dir, site_dir=site_dir,
                         templates_dir=TEMPLATES_DIR)
    return site_dir, summary


def test_build_writes_expected_pages(tmp_path):
    site_dir, summary = _build(tmp_path)
    assert (site_dir / "index.html").exists()
    assert (site_dir / "products" / "product-a.html").exists()
    assert (site_dir / "products" / "product-b.html").exists()
    assert (site_dir / "products" / "product-c.html").exists()
    assert (site_dir / "products" / "product-d.html").exists()
    # inactive products are not rendered
    assert not (site_dir / "products" / "product-inactive.html").exists()
    assert summary["pages_written"] == 5  # home + 4 active products


def test_unit_variant_gets_dollars_per_wh_and_bundle_does_not(tmp_path):
    site_dir, _ = _build(tmp_path)
    html = (site_dir / "products" / "product-a.html").read_text(encoding="utf-8")
    # unit: $500 / 1000 Wh = $0.50/Wh, exactly one rated variant on the page
    assert '<span class="wh">$0.50/Wh</span>' in html
    assert html.count('<span class="wh">') == 1
    # bundle: badge shown, price shown, no $/Wh
    assert 'class="badge">bundle</span>' in html
    assert "$800.00" in html
    # money is two-decimal
    assert "$500.00" in html
    assert "$600.00" in html  # was_price


def test_affiliate_links_deep_link_each_variant(tmp_path):
    site_dir, _ = _build(tmp_path)
    html = (site_dir / "products" / "product-a.html").read_text(encoding="utf-8")
    assert 'href="https://r1.example/products/test-station?variant=111"' in html
    assert 'href="https://r1.example/products/test-station?variant=222"' in html
    assert 'rel="nofollow sponsored"' in html


def test_latest_row_wins(tmp_path):
    site_dir, _ = _build(tmp_path)
    html = (site_dir / "products" / "product-a.html").read_text(encoding="utf-8")
    assert "$480.00" not in html, "older JSONL row leaked into the render"


def test_missing_capacity_withholds_every_dollars_per_wh(tmp_path):
    site_dir, _ = _build(tmp_path)
    html = (site_dir / "products" / "product-b.html").read_text(encoding="utf-8")
    assert '<span class="wh">' not in html
    assert "withheld" in html
    assert "$700.00" in html  # price still shown


def test_missing_retailer_name_and_unknown_retailer_falls_back(tmp_path):
    """retailer_name is optional on rows (C4); an id absent from
    retailers.json renders as a titleized id, never a KeyError."""
    site_dir, _ = _build(tmp_path)
    html = (site_dir / "products" / "product-b.html").read_text(encoding="utf-8")
    assert "Mystery Solar" in html


def test_sold_out_product_renders_sold_out(tmp_path):
    site_dir, _ = _build(tmp_path)
    html = (site_dir / "products" / "product-b.html").read_text(encoding="utf-8")
    assert "Sold out" in html


def test_empty_variant_row_renders_no_priced_variants(tmp_path):
    site_dir, _ = _build(tmp_path)
    html = (site_dir / "products" / "product-c.html").read_text(encoding="utf-8")
    assert "No priced variants could be read" in html


def test_never_scraped_product_renders_placeholder(tmp_path):
    site_dir, _ = _build(tmp_path)
    html = (site_dir / "products" / "product-d.html").read_text(encoding="utf-8")
    assert "No price data collected yet" in html


def test_home_table_shows_price_and_unit_dollars_per_wh(tmp_path):
    site_dir, _ = _build(tmp_path)
    html = (site_dir / "index.html").read_text(encoding="utf-8")
    # product-a cell: cheapest variant is the unit -> its price AND its $/Wh
    assert "$500.00" in html
    assert "$0.50/Wh" in html
    # product-b has no row for retailer r1 -> dash cell renders, not a crash
    assert "Mystery Station" in html


# --- red team #2 MAJOR-1 regression: home cell price and $/Wh must come
# --- from the SAME variant. Exact captured wild-oak-trail scenario: the
# --- "+ 110W Panel" bundle at $509 undercuts the $569 main unit; the old
# --- code rendered $509.00 beside the unit's $0.74/Wh.

def _seed_509_scenario(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "prices").mkdir(parents=True)
    _write_json(data_dir / "products.json", [{
        "id": "ecoflow-river-2-pro", "name": "EcoFlow RIVER 2 Pro",
        "brand": "EcoFlow", "category": "portable-power-station",
        "specs": {"capacity_wh": 768, "output_w": 800,
                  "chemistry": "LiFePO4", "weight_lb": 17.2},
        "active": True, "notes": None,
    }])
    _write_json(data_dir / "retailers.json", [
        {"id": "wild-oak-trail", "name": "Wild Oak Trail",
         "url": "https://www.wildoaktrail.com", "scraper_type": "shopify",
         "affiliate": None, "active": True, "priority": 1},
    ])
    _write_jsonl(data_dir / "prices" / "ecoflow-river-2-pro.jsonl", [{
        "retailer_id": "wild-oak-trail", "retailer_name": "Wild Oak Trail",
        "timestamp": "2026-08-13T00:00:00+00:00",
        "url": "https://www.wildoaktrail.com/products/ecoflow-river-2-pro-portable-power-station",
        "variants": {
            "ecoflow-river-2-pro-1-110w-portable-solar-panel": {
                "price": 509.00, "was_price": 998.00, "available": True,
                "raw_variant": "EcoFlow RIVER 2 Pro + 1 110W Portable Solar Panel",
                "variant_id": 44532078936300},
            "ecoflow-river-2-pro-portable-power-station-main-unit-only": {
                "price": 569.00, "was_price": 599.00, "available": True,
                "raw_variant": "EcoFlow RIVER 2 Pro Portable Power Station(Main Unit Only)",
                "variant_id": 44532078903532},
        },
        "in_stock": True,
    }])
    return data_dir


def test_home_cell_price_and_dollars_per_wh_come_from_same_variant(tmp_path):
    data_dir = _seed_509_scenario(tmp_path)
    site_dir = tmp_path / "site"
    build_site(data_dir=data_dir, site_dir=site_dir, templates_dir=TEMPLATES_DIR)
    html = (site_dir / "index.html").read_text(encoding="utf-8")

    # Cheapest variant is the $509 bundle: its price shows...
    assert "$509.00" in html
    # ...with a bundle badge, and NOT the $569 unit's $/Wh next to it.
    assert 'class="badge">bundle</span>' in html
    assert "$0.74/Wh" not in html
    assert '<div class="wh">' not in html
    # The unit's price must not be presented as the cell price either.
    assert "$569.00" not in html


def test_home_cell_shows_dollars_per_wh_when_cheapest_is_the_unit(tmp_path):
    """Counter-case: drop the discounted bundle and the unit's own $/Wh
    appears with the unit's own price."""
    data_dir = _seed_509_scenario(tmp_path)
    # Rewrite the price file with only the main-unit variant
    price_file = data_dir / "prices" / "ecoflow-river-2-pro.jsonl"
    row = json.loads(price_file.read_text(encoding="utf-8").splitlines()[0])
    row["variants"] = {
        "ecoflow-river-2-pro-portable-power-station-main-unit-only":
            row["variants"]["ecoflow-river-2-pro-portable-power-station-main-unit-only"]
    }
    price_file.write_text(json.dumps(row) + "\n", encoding="utf-8", newline="\n")

    site_dir = tmp_path / "site"
    build_site(data_dir=data_dir, site_dir=site_dir, templates_dir=TEMPLATES_DIR)
    html = (site_dir / "index.html").read_text(encoding="utf-8")

    assert "$569.00" in html
    assert "$0.74/Wh" in html  # 569 / 768 = 0.7409


# --- red team #2 MINOR-5: a malformed (string) price must not crash the build ---

def test_string_price_row_does_not_crash_build(tmp_path):
    data_dir = _seed_509_scenario(tmp_path)
    price_file = data_dir / "prices" / "ecoflow-river-2-pro.jsonl"
    row = json.loads(price_file.read_text(encoding="utf-8").splitlines()[0])
    row["variants"]["corrupt-variant"] = {
        "price": "N/A", "was_price": None, "available": None,
        "raw_variant": "Corrupt Variant", "variant_id": None,
    }
    price_file.write_text(json.dumps(row) + "\n", encoding="utf-8", newline="\n")

    site_dir = tmp_path / "site"
    summary = build_site(data_dir=data_dir, site_dir=site_dir,
                         templates_dir=TEMPLATES_DIR)
    assert summary["pages_written"] == 2  # no TypeError from the sort key

    html = (site_dir / "products" / "ecoflow-river-2-pro.html").read_text(encoding="utf-8")
    # The corrupt variant renders without a price or $/Wh; real ones survive.
    assert "Corrupt Variant" in html
    assert "$509.00" in html
    assert "$569.00" in html

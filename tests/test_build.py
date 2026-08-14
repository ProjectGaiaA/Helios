"""Tests for the static site builder (build.py).

Edge cases per PLAN section 2 (missing capacity, missing retailer_name,
sold-out product, optional keys) plus the A2 provenance/withhold layer
(PLAN 4c.4): injected clock, staleness boundaries, quarantine markers.

All fixture timestamps are NOW-RELATIVE against a pinned clock — absolute
timestamps made the suite calendar-red the day they aged past the
staleness threshold.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from build import STALE_MAX_HOURS, build_site

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"

# The single injected clock for every test in this file.
NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)


def _ts(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


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
                      "chemistry": "LiFePO4", "weight_lb": 25.0,
                      "capacity_source": "vendor-spec (test)"},
            "active": True, "notes": None,
        },
        {
            "id": "product-b", "name": "Mystery Station", "brand": "TestCo",
            "category": "portable-power-station",
            # capacity unknown -> every $/Wh withheld (PLAN 2b)
            "specs": {"capacity_wh": None, "output_w": 1800,
                      "chemistry": "LiFePO4", "weight_lb": None,
                      "capacity_source": None},
            "active": True, "notes": None,
        },
        {
            "id": "product-c", "name": "Unreadable Station", "brand": "TestCo",
            "category": "portable-power-station",
            "specs": {"capacity_wh": 500, "output_w": 500,
                      "chemistry": "LiFePO4", "weight_lb": 10.0,
                      "capacity_source": "vendor-spec (test)"},
            "active": True, "notes": None,
        },
        {
            "id": "product-d", "name": "Never Scraped", "brand": "TestCo",
            "category": "portable-power-station",
            "specs": {"capacity_wh": 500, "output_w": 500,
                      "chemistry": "LiFePO4", "weight_lb": 10.0,
                      "capacity_source": "vendor-spec (test)"},
            "active": True, "notes": None,
        },
        {
            "id": "product-inactive", "name": "Retired Station", "brand": "TestCo",
            "category": "portable-power-station",
            "specs": {"capacity_wh": 500, "output_w": 500,
                      "chemistry": "LiFePO4", "weight_lb": 10.0,
                      "capacity_source": "vendor-spec (test)"},
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
         "timestamp": _ts(72),
         "url": "https://r1.example/products/test-station",
         "variants": {"main-unit-only": {
             "price": 480.00, "was_price": None, "available": True,
             "raw_variant": "Station [Main Unit Only]", "variant_id": 111,
             "sku": "TS-1000"}},
         "in_stock": True},
        {"retailer_id": "r1", "retailer_name": "Retailer One",
         "timestamp": _ts(2),
         "url": "https://r1.example/products/test-station?variant=111",
         "variants": {
             "main-unit-only": {
                 "price": 500.00, "was_price": 600.00, "available": True,
                 "raw_variant": "Station [Main Unit Only]", "variant_id": 111,
                 "sku": "TS-1000"},
             "kit-1-x-200w-panel": {
                 "price": 800.00, "was_price": None, "available": False,
                 "raw_variant": "Station Kit [1 x 200W Panel]", "variant_id": 222,
                 "sku": "TS-1000-KIT"},
         },
         "in_stock": True,
         # optional key (C4): consumers must not choke on it
         "price_anomaly": ["ANOMALY: product-a at r1 tier=main-unit-only"]},
    ])

    _write_jsonl(prices / "product-b.jsonl", [
        # Hostile row: retailer_name missing (optional, C4), retailer id not
        # in retailers.json, product sold out, sku predates the field.
        {"retailer_id": "mystery-solar",
         "timestamp": _ts(3),
         "url": "https://mystery.example/products/mystery-station",
         "variants": {"main-unit-only": {
             "price": 700.00, "was_price": None, "available": False,
             "raw_variant": "Mystery Station [Main Unit Only]", "variant_id": 333}},
         "in_stock": False},
    ])

    _write_jsonl(prices / "product-c.jsonl", [
        {"retailer_id": "r1", "retailer_name": "Retailer One",
         "timestamp": _ts(4),
         "url": "https://r1.example/products/unreadable-station",
         "variants": {}, "in_stock": None, "no_sizes_readable": True},
    ])

    return data_dir


def _build(tmp_path):
    data_dir = _seed_data(tmp_path)
    site_dir = tmp_path / "site"
    summary = build_site(data_dir=data_dir, site_dir=site_dir,
                         templates_dir=TEMPLATES_DIR, now=NOW)
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
    assert '<span class="wh" data-field="wh">$0.50/Wh</span>' in html
    assert html.count('<span class="wh"') == 1
    # bundle: badge shown, price shown, no $/Wh
    assert 'class="badge">bundle</span>' in html
    assert "$800.00" in html
    # money is two-decimal
    assert "$500.00" in html
    assert "$600.00" in html  # was_price


def test_provenance_attributes_on_every_rendered_price(tmp_path):
    """PLAN 4c.4: product rows carry variant_id/tier/sku/scraped-at; home
    cells carry variant_id/scraped-at; ages are visible."""
    site_dir, _ = _build(tmp_path)
    page = (site_dir / "products" / "product-a.html").read_text(encoding="utf-8")
    assert 'data-variant-id="111"' in page
    assert 'data-tier="main-unit-only"' in page
    assert 'data-sku="TS-1000"' in page
    assert f'data-scraped-at="{_ts(2)}"' in page
    assert 'data-field="price"' in page
    assert 'data-field="availability" data-value="true"' in page
    assert "as of 2h ago" in page

    home = (site_dir / "index.html").read_text(encoding="utf-8")
    assert 'data-variant-id="111"' in home
    assert f'data-scraped-at="{_ts(2)}"' in home
    assert "as of 2h ago" in home


def test_missing_sku_renders_empty_data_sku(tmp_path):
    """Rows predating the sku field (v2.2) must render, with an empty
    attr, not KeyError."""
    site_dir, _ = _build(tmp_path)
    page = (site_dir / "products" / "product-b.html").read_text(encoding="utf-8")
    assert 'data-sku=""' in page


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
    assert '<span class="wh"' not in html
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

def _seed_509_scenario(tmp_path, bundle_available=True):
    data_dir = tmp_path / "data"
    (data_dir / "prices").mkdir(parents=True)
    _write_json(data_dir / "products.json", [{
        "id": "ecoflow-river-2-pro", "name": "EcoFlow RIVER 2 Pro",
        "brand": "EcoFlow", "category": "portable-power-station",
        "specs": {"capacity_wh": 768, "output_w": 800,
                  "chemistry": "LiFePO4", "weight_lb": 17.2,
                  "capacity_source": "listing-title (test)"},
        "active": True, "notes": None,
    }])
    _write_json(data_dir / "retailers.json", [
        {"id": "wild-oak-trail", "name": "Wild Oak Trail",
         "url": "https://www.wildoaktrail.com", "scraper_type": "shopify",
         "affiliate": None, "active": True, "priority": 1},
    ])
    _write_jsonl(data_dir / "prices" / "ecoflow-river-2-pro.jsonl", [{
        "retailer_id": "wild-oak-trail", "retailer_name": "Wild Oak Trail",
        "timestamp": _ts(5),
        "url": "https://www.wildoaktrail.com/products/ecoflow-river-2-pro-portable-power-station",
        "variants": {
            "ecoflow-river-2-pro-1-110w-portable-solar-panel": {
                "price": 509.00, "was_price": 998.00,
                "available": bundle_available,
                "raw_variant": "EcoFlow RIVER 2 Pro + 1 110W Portable Solar Panel",
                "variant_id": 44532078936300, "sku": "RIVER2PRO-110-1-US"},
            "ecoflow-river-2-pro-portable-power-station-main-unit-only": {
                "price": 569.00, "was_price": 599.00, "available": True,
                "raw_variant": "EcoFlow RIVER 2 Pro Portable Power Station(Main Unit Only)",
                "variant_id": 44532078903532, "sku": "ZMR620-B-US-1"},
        },
        "in_stock": True,
    }])
    return data_dir


def _build_509(tmp_path, data_dir=None):
    data_dir = data_dir or _seed_509_scenario(tmp_path)
    site_dir = tmp_path / "site"
    build_site(data_dir=data_dir, site_dir=site_dir,
               templates_dir=TEMPLATES_DIR, now=NOW)
    return data_dir, site_dir


def test_home_cell_price_and_dollars_per_wh_come_from_same_variant(tmp_path):
    _, site_dir = _build_509(tmp_path)
    html = (site_dir / "index.html").read_text(encoding="utf-8")

    # Cheapest variant is the $509 bundle: its price shows...
    assert "$509.00" in html
    # ...with a bundle badge, and NOT the $569 unit's $/Wh next to it.
    assert 'class="badge">bundle</span>' in html
    assert "$0.74/Wh" not in html
    assert '<div class="wh"' not in html
    # The unit's price must not be presented as the cell price either.
    assert "$569.00" not in html


def test_home_cell_shows_dollars_per_wh_when_cheapest_is_the_unit(tmp_path):
    """Counter-case: drop the discounted bundle and the unit's own $/Wh
    appears with the unit's own price."""
    data_dir = _seed_509_scenario(tmp_path)
    price_file = data_dir / "prices" / "ecoflow-river-2-pro.jsonl"
    row = json.loads(price_file.read_text(encoding="utf-8").splitlines()[0])
    row["variants"] = {
        "ecoflow-river-2-pro-portable-power-station-main-unit-only":
            row["variants"]["ecoflow-river-2-pro-portable-power-station-main-unit-only"]
    }
    price_file.write_text(json.dumps(row) + "\n", encoding="utf-8", newline="\n")

    _, site_dir = _build_509(tmp_path, data_dir)
    html = (site_dir / "index.html").read_text(encoding="utf-8")

    assert "$569.00" in html
    assert "$0.74/Wh" in html  # 569 / 768 = 0.7409


def test_home_cell_availability_comes_from_the_same_cheapest_variant(tmp_path):
    """PLAN 4c.4 residual fix: the cheapest variant is sold out while the
    ROW aggregate in_stock is True — the cell must say sold out."""
    data_dir = _seed_509_scenario(tmp_path, bundle_available=False)
    _, site_dir = _build_509(tmp_path, data_dir)
    html = (site_dir / "index.html").read_text(encoding="utf-8")
    assert 'data-field="availability" data-value="false"' in html
    assert "Sold out" in html


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
                         templates_dir=TEMPLATES_DIR, now=NOW)
    assert summary["pages_written"] == 2  # no TypeError from the sort key

    html = (site_dir / "products" / "ecoflow-river-2-pro.html").read_text(encoding="utf-8")
    # The corrupt variant renders without a price or $/Wh; real ones survive.
    assert "Corrupt Variant" in html
    assert "$509.00" in html
    assert "$569.00" in html


# --- staleness boundaries (PLAN 4c.4: STALE_MAX_HOURS=168, injected clock) ---

def _reseed_timestamp(data_dir, hours_ago):
    price_file = data_dir / "prices" / "ecoflow-river-2-pro.jsonl"
    row = json.loads(price_file.read_text(encoding="utf-8").splitlines()[0])
    row["timestamp"] = _ts(hours_ago)
    price_file.write_text(json.dumps(row) + "\n", encoding="utf-8", newline="\n")


def test_row_at_167h_still_renders_prices(tmp_path):
    assert STALE_MAX_HOURS == 168
    data_dir = _seed_509_scenario(tmp_path)
    _reseed_timestamp(data_dir, 167)
    _, site_dir = _build_509(tmp_path, data_dir)
    home = (site_dir / "index.html").read_text(encoding="utf-8")
    page = (site_dir / "products" / "ecoflow-river-2-pro.html").read_text(encoding="utf-8")
    assert "$509.00" in home and "$509.00" in page
    assert 'data-withheld="stale"' not in home
    assert 'data-withheld="stale"' not in page


def test_row_at_169h_withholds_with_stale_marker(tmp_path):
    data_dir = _seed_509_scenario(tmp_path)
    _reseed_timestamp(data_dir, 169)
    _, site_dir = _build_509(tmp_path, data_dir)
    home = (site_dir / "index.html").read_text(encoding="utf-8")
    page = (site_dir / "products" / "ecoflow-river-2-pro.html").read_text(encoding="utf-8")
    for html in (home, page):
        assert 'data-withheld="stale"' in html
        assert "$509.00" not in html
        assert "$569.00" not in html
    # the stale marker is distinct from quarantine (PLAN 4c.4)
    assert 'data-withheld="quarantine"' not in home
    assert 'data-withheld="quarantine"' not in page


def test_unparseable_timestamp_withholds_as_stale(tmp_path):
    """Withhold-on-doubt: a row whose timestamp cannot be parsed must not
    render as fresh."""
    data_dir = _seed_509_scenario(tmp_path)
    price_file = data_dir / "prices" / "ecoflow-river-2-pro.jsonl"
    row = json.loads(price_file.read_text(encoding="utf-8").splitlines()[0])
    row["timestamp"] = "not-a-timestamp"
    price_file.write_text(json.dumps(row) + "\n", encoding="utf-8", newline="\n")
    _, site_dir = _build_509(tmp_path, data_dir)
    page = (site_dir / "products" / "ecoflow-river-2-pro.html").read_text(encoding="utf-8")
    assert 'data-withheld="stale"' in page
    assert "$509.00" not in page


# --- quarantine markers (PLAN 4c.3/4c.4 + acceptance 4) ---

QKEY = "wild-oak-trail:ecoflow-river-2-pro:44532078936300"


def _quarantine_entry():
    return {QKEY: {
        "sku": "RIVER2PRO-110-1-US",
        "tier_last_seen": "ecoflow-river-2-pro-1-110w-portable-solar-panel",
        "reason": "render_defect", "observed": "$555.00", "expected": "$509.00",
        "first_seen": _ts(24), "last_seen": _ts(1),
        "consecutive_failures": 1, "unobserved_audits": 0,
    }}


def test_quarantined_cheapest_variant_withholds_cell_and_row(tmp_path):
    """Quarantine applies BEFORE cheapest selection: the $509 bundle is
    quarantined, so the home cell withholds ENTIRELY — the $569 unit must
    NOT be silently promoted to 'lowest price'. The product page keeps
    the unit row but withholds the quarantined one, marker-distinct."""
    data_dir = _seed_509_scenario(tmp_path)
    _write_json(data_dir / "quarantine.json", _quarantine_entry())
    _, site_dir = _build_509(tmp_path, data_dir)

    home = (site_dir / "index.html").read_text(encoding="utf-8")
    assert 'data-withheld="quarantine"' in home
    assert "$509.00" not in home
    assert "$569.00" not in home, "next-cheapest silently substituted"

    page = (site_dir / "products" / "ecoflow-river-2-pro.html").read_text(encoding="utf-8")
    assert 'data-withheld="quarantine"' in page
    assert "$509.00" not in page
    assert "$569.00" in page  # the healthy variant still renders
    assert "under verification" in page


def test_quarantine_removed_renders_again(tmp_path):
    data_dir = _seed_509_scenario(tmp_path)
    _write_json(data_dir / "quarantine.json", _quarantine_entry())
    _, site_dir = _build_509(tmp_path, data_dir)
    assert "$509.00" not in (site_dir / "index.html").read_text(encoding="utf-8")

    _write_json(data_dir / "quarantine.json", {})
    _, site_dir = _build_509(tmp_path, data_dir)
    home = (site_dir / "index.html").read_text(encoding="utf-8")
    assert "$509.00" in home
    assert 'data-withheld="quarantine"' not in home


# --- carriage contract: handle_maps governs rendering (red team #5) ---
# handle_maps says which retailer sells which product. It used to govern
# only SCRAPING, so withdrawing a carriage left its last stored price on
# the site forever — which made "exclude the misleading cell" a no-op.


def test_unmapped_pair_is_not_rendered(tmp_path):
    data_dir = _seed_509_scenario(tmp_path)
    _write_json(data_dir / "handle_maps.json", {"wild-oak-trail": {}})
    _, site_dir = _build_509(tmp_path, data_dir)
    home = (site_dir / "index.html").read_text(encoding="utf-8")
    assert "$509.00" not in home
    assert "$569.00" not in home


def test_mapped_pair_is_rendered(tmp_path):
    data_dir = _seed_509_scenario(tmp_path)
    _write_json(data_dir / "handle_maps.json",
                {"wild-oak-trail": {"ecoflow-river-2-pro": "any-handle"}})
    _, site_dir = _build_509(tmp_path, data_dir)
    assert "$509.00" in (site_dir / "index.html").read_text(encoding="utf-8")


def test_absent_handle_maps_file_filters_nothing(tmp_path):
    """No file means no contract recorded — matching load_handle_maps()'s
    missing-file behaviour, so a fresh checkout still renders."""
    data_dir = _seed_509_scenario(tmp_path)
    assert not (data_dir / "handle_maps.json").exists()
    _, site_dir = _build_509(tmp_path, data_dir)
    assert "$509.00" in (site_dir / "index.html").read_text(encoding="utf-8")


def test_filter_to_mapped_pairs_is_a_noop_without_maps():
    from build import filter_to_mapped_pairs
    latest = {"p": {"r": {"variants": {}}}}
    assert filter_to_mapped_pairs(latest, None) == latest
    assert filter_to_mapped_pairs(latest, {"r": {"p": "h"}}) == latest
    assert filter_to_mapped_pairs(latest, {"r": {}}) == {}

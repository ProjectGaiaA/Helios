"""Tests for the Shopify scraper (scrapers/shopify.py).

Fixtures are authored from real shop-solar-kits /products.json data
(2026-08-13), trimmed but structurally untouched: dollar-string prices,
no `available` key (C1), real variant ids and bundle-style titles.
"""

from unittest.mock import patch

import responses

from tests.conftest import load_fixture
from scrapers.shopify import ShopifyScraper

BASE = "https://shopsolarkits.com"


def _add_product(handle, fixture_name, js_status=404, js_body=None):
    """Register .json (fixture) and .js (availability) endpoints."""
    fixture = load_fixture("shop-solar-kits", fixture_name)
    responses.add(
        responses.GET, f"{BASE}/products/{handle}.json", json=fixture, status=200
    )
    if js_body is not None:
        responses.add(
            responses.GET, f"{BASE}/products/{handle}.js", json=js_body, status=200
        )
    else:
        responses.add(
            responses.GET, f"{BASE}/products/{handle}.js", status=js_status
        )
    return fixture


def test_sku_missing_or_blank_normalizes_to_none(no_sleep):
    """Absent, empty, and whitespace-only SKUs must all store as None.

    A "" SKU is indistinguishable from real data downstream; the drift
    tripwire needs a clean None to know it has nothing to compare.
    """
    scraper = ShopifyScraper("shop-solar-kits", BASE)
    parsed = scraper._parse_product(
        {
            "title": "Probe Product",
            "handle": "probe-product",
            "variants": [
                {"id": 1, "title": "No Sku Key", "price": "100.00"},
                {"id": 2, "title": "Empty Sku", "price": "100.00", "sku": ""},
                {"id": 3, "title": "Padded Sku", "price": "100.00", "sku": " X-1 "},
            ],
        }
    )
    by_id = {v["variant_id"]: v for v in parsed["variants"].values()}
    assert by_id[1]["sku"] is None
    assert by_id[2]["sku"] is None
    assert by_id[3]["sku"] == "X-1"


# --- JSON parsing: structure, variant_id passthrough, deep link ---


@responses.activate
def test_json_product_parsing_returns_correct_structure(no_sleep):
    _add_product("anker-solix-f2600", "anker-solix-f2600-product.json")

    scraper = ShopifyScraper("shop-solar-kits", BASE)
    result = scraper.scrape_product("anker-solix-f2600")

    assert result is not None
    assert result["retailer_id"] == "shop-solar-kits"
    assert result["handle"] == "anker-solix-f2600"
    assert result["title"].startswith("Anker Solix F2600")
    assert "variants" in result
    assert len(result["variants"]) == 5
    # A row with readable variants must NOT carry the health flag
    assert "no_sizes_readable" not in result

    unit = result["variants"]["anker-solix-f2600-main-unit-only"]
    assert unit["price"] == 1254.00
    assert unit["was_price"] == 2499.00
    assert unit["raw_variant"] == "Anker Solix F2600 [Main Unit Only]"
    # variant_id passthrough (C4): needed for ?variant= affiliate deep links
    assert unit["variant_id"] == 41836471582860
    # sku passthrough: cross-retailer identity + the SKU-drift tripwire
    assert unit["sku"] == "A1781111"

    bundle = result["variants"]["double-kit-2-x-200w-rigid-panels"]
    assert bundle["price"] == 1618.49
    assert bundle["variant_id"] == 41837963411596
    assert bundle["sku"] == "ANKER-F2600-DOUBLE-KIT"

    # Deep link points at the cheapest variant (the bare unit here)
    assert result["url"] == f"{BASE}/products/anker-solix-f2600?variant=41836471582860"

    # .js gave no availability -> stock unknown, never assumed
    assert result["in_stock"] is None
    assert unit["available"] is None


# --- Pack variants are KEPT (the plant tracker's pack-skip regex is gone) ---


@responses.activate
def test_pack_variants_are_kept(no_sleep):
    """"2-Pack" is a legitimate solar variant, not per-unit noise.

    The plant tracker skipped variants matching (?:[2-9]|1\\d)[\\s-]*(?:plant|pack).
    Helios deleted that regex: multi-packs are real products here, and
    whether they earn a $/Wh is a build-time classification question.
    """
    _add_product("lion-20k-pbx", "pack-variant-product.json")

    scraper = ShopifyScraper("shop-solar-kits", BASE)
    result = scraper.scrape_product("lion-20k-pbx")

    assert result is not None
    assert len(result["variants"]) == 4
    assert result["variants"]["2-pack"]["price"] == 89.00
    assert result["variants"]["3-pack"]["price"] == 129.00
    assert result["variants"]["4-pack"]["price"] == 169.00
    assert result["variants"]["single-charger"]["price"] == 49.00


# --- Slug normalization and collision suffixing ---


def test_normalize_variant_slugifies():
    scraper = ShopifyScraper("test", "http://test.com")
    assert scraper._normalize_variant("Anker Solix F2600 [Main Unit Only]") == \
        "anker-solix-f2600-main-unit-only"
    assert scraper._normalize_variant("48V / 100Ah") == "48v-100ah"
    assert scraper._normalize_variant("2-Pack") == "2-pack"
    assert scraper._normalize_variant("Default Title") == "default"
    assert scraper._normalize_variant("") == "default"
    assert scraper._normalize_variant("!!!") == "default"


@responses.activate
def test_slug_collision_appends_variant_id(no_sleep):
    """Two titles that slugify identically must both survive.

    The plant tracker's tier map was last-write-wins: the second variant
    silently replaced the first, and a row could wear its neighbour's
    price. On collision the second key gets a -{variant_id} suffix.
    """
    _add_product("bluetti-ac200l-collision", "slug-collision-product.json")

    scraper = ShopifyScraper("shop-solar-kits", BASE)
    result = scraper.scrape_product("bluetti-ac200l-collision")

    assert result is not None
    assert len(result["variants"]) == 2
    assert result["variants"]["ac200l-200w-solar-panel"]["price"] == 1899.00
    suffixed = result["variants"]["ac200l-200w-solar-panel-46535814578499"]
    assert suffixed["price"] == 1949.00
    assert suffixed["variant_id"] == 46535814578499


@responses.activate
def test_slug_collision_without_variant_ids_never_loses_a_price(no_sleep):
    """Red team #2 MINOR-4: three same-titled variants with MISSING ids
    collapsed onto two keys under the single id-suffix `if` (both
    collisions produced the same "{tier}-" key) and one price was silently
    overwritten. Uniqueness must be guaranteed, not attempted once."""
    fixture = {
        "product": {
            "id": 9, "title": "Triple Same", "handle": "triple-same",
            "variants": [
                {"title": "AC200L + 200W Solar Panel", "price": "100.00"},
                {"title": "AC200L + 200W Solar Panel", "price": "200.00"},
                {"title": "AC200L + 200W Solar Panel", "price": "300.00"},
            ],
        }
    }
    responses.add(
        responses.GET, f"{BASE}/products/triple-same.json", json=fixture, status=200
    )
    responses.add(responses.GET, f"{BASE}/products/triple-same.js", status=404)

    scraper = ShopifyScraper("shop-solar-kits", BASE)
    result = scraper.scrape_product("triple-same")

    assert len(result["variants"]) == 3, "a collision overwrote a variant"
    prices = sorted(v["price"] for v in result["variants"].values())
    assert prices == [100.00, 200.00, 300.00]
    assert sorted(result["variants"].keys()) == [
        "ac200l-200w-solar-panel",
        "ac200l-200w-solar-panel-2",
        "ac200l-200w-solar-panel-3",
    ]


# --- Empty variants -> no_sizes_readable (health flag on the JSON path) ---


@responses.activate
def test_empty_variants_sets_no_sizes_readable(no_sleep):
    """A page that answers with zero readable variants is published as a
    row (true fact) but flagged, so runner.py's hit-rate health does not
    count it as a successful price read. In the plant tracker the flag
    only existed on the HTML path, so a JSON-path breakage reported 100%
    healthy."""
    _add_product("empty-variants-product", "empty-variants-product.json")

    scraper = ShopifyScraper("shop-solar-kits", BASE)
    result = scraper.scrape_product("empty-variants-product")

    assert result is not None
    assert result["variants"] == {}
    assert result["no_sizes_readable"] is True
    assert result["in_stock"] is None  # unknown, not asserted


# --- 404: record broken, return None, NO HTML fallback ---


@responses.activate
def test_404_returns_none_without_html_fallback(no_sleep):
    responses.add(
        responses.GET, f"{BASE}/products/gone-product.json", status=404
    )

    with patch("scrapers.common.record_broken") as record_broken:
        scraper = ShopifyScraper("shop-solar-kits", BASE)
        result = scraper.scrape_product("gone-product", product_id="gone-product")

    assert result is None
    record_broken.assert_called_once_with(
        "shop-solar-kits", "gone-product", "gone-product"
    )
    # Exactly one request: the .json endpoint. No HTML page fetch — the
    # fallback was deleted, not deferred (PLAN section 5).
    urls_called = [c.request.url for c in responses.calls]
    assert urls_called == [f"{BASE}/products/gone-product.json"]


# --- Redirect: record candidate, follow for data ---


@responses.activate
def test_redirect_records_candidate_and_follows(no_sleep):
    fixture = load_fixture("shop-solar-kits", "eg4-lifepower4-product.json")
    responses.add(
        responses.GET,
        f"{BASE}/products/old-eg4-handle.json",
        status=301,
        headers={"Location": f"{BASE}/products/eg4-lifepower4-lithium-battery.json"},
    )
    responses.add(
        responses.GET,
        f"{BASE}/products/eg4-lifepower4-lithium-battery.json",
        json=fixture,
        status=200,
    )
    responses.add(
        responses.GET,
        f"{BASE}/products/eg4-lifepower4-lithium-battery.js",
        status=404,
    )

    with patch("scrapers.common.record_redirect_candidate") as record_redirect:
        scraper = ShopifyScraper("shop-solar-kits", BASE)
        result = scraper.scrape_product("old-eg4-handle", product_id="eg4-lifepower4")

    assert result is not None
    assert result["handle"] == "eg4-lifepower4-lithium-battery"
    record_redirect.assert_called_once_with(
        "shop-solar-kits",
        "eg4-lifepower4",
        "old-eg4-handle",
        "eg4-lifepower4-lithium-battery",
        f"{BASE}/products/eg4-lifepower4-lithium-battery.json",
    )


# --- Availability via .js (the only source of stock truth) ---


@responses.activate
def test_js_availability_joins_by_variant_id_not_position(no_sleep):
    """Guards against the positional-mapping class of bug: reversing the
    .js variant order must not flip which variant is marked available."""
    _add_product(
        "ecoflow-river-2-pro",
        "ecoflow-river-2-pro-product.json",
        js_body={
            "variants": [
                # Deliberately reversed relative to the .json order.
                {"id": 41679254454412, "available": True},   # main unit
                {"id": 41737132769420, "available": False},  # +160W bundle
            ]
        },
    )

    scraper = ShopifyScraper("shop-solar-kits", BASE)
    result = scraper.scrape_product("ecoflow-river-2-pro")

    assert result["variants"]["ecoflow-river-2-pro-main-unit-only"]["available"] is True
    assert result["variants"]["ecoflow-river-2-pro-160w-solar-panel"]["available"] is False
    assert result["in_stock"] is True


@responses.activate
def test_js_failure_degrades_to_unknown_never_to_available(no_sleep):
    """A failed availability fetch must not be read as "in stock"."""
    _add_product("eg4-lifepower4-lithium-battery", "eg4-lifepower4-product.json",
                 js_status=500)

    scraper = ShopifyScraper("shop-solar-kits", BASE)
    result = scraper.scrape_product("eg4-lifepower4-lithium-battery")

    assert list(result["variants"].values())[0]["available"] is None
    assert result["in_stock"] is None


@responses.activate
def test_js_non_boolean_availability_is_ignored(no_sleep):
    """Only a real bool counts. A string or null must not become True."""
    _add_product(
        "eg4-lifepower4-lithium-battery", "eg4-lifepower4-product.json",
        js_body={"variants": [{"id": 43287020535948, "available": "yes"}]},
    )

    scraper = ShopifyScraper("shop-solar-kits", BASE)
    result = scraper.scrape_product("eg4-lifepower4-lithium-battery")
    assert list(result["variants"].values())[0]["available"] is None


@responses.activate
def test_prices_are_not_read_from_the_js_endpoint(no_sleep):
    """.js returns price in CENTS; .json returns dollar strings.

    Taking price from .js would multiply every price on the site by 100.
    Only the availability boolean is read from there.
    """
    _add_product(
        "eg4-lifepower4-lithium-battery", "eg4-lifepower4-product.json",
        js_body={"variants": [{"id": 43287020535948, "available": True, "price": 147099}]},
    )

    scraper = ShopifyScraper("shop-solar-kits", BASE)
    result = scraper.scrape_product("eg4-lifepower4-lithium-battery")
    tier = list(result["variants"].values())[0]
    assert tier["price"] == 1470.99
    assert tier["available"] is True


# --- Default Title, zero prices, 429 ---


@responses.activate
def test_default_title_maps_to_default_tier(no_sleep):
    _add_product("eg4-lifepower4-lithium-battery", "eg4-lifepower4-product.json")

    scraper = ShopifyScraper("shop-solar-kits", BASE)
    result = scraper.scrape_product("eg4-lifepower4-lithium-battery")

    assert list(result["variants"].keys()) == ["default"]
    assert result["variants"]["default"]["raw_variant"] == "Default Title"
    assert result["variants"]["default"]["price"] == 1470.99


@responses.activate
def test_zero_price_variants_excluded(no_sleep):
    fixture = {
        "product": {
            "id": 1,
            "title": "Test Station",
            "handle": "test-station",
            "variants": [
                {"id": 100, "title": "Main Unit", "price": "0", "compare_at_price": None},
                {"id": 101, "title": "Main Unit + Panel", "price": "499.00", "compare_at_price": None},
            ],
        }
    }
    responses.add(
        responses.GET, f"{BASE}/products/test-station.json", json=fixture, status=200
    )
    responses.add(responses.GET, f"{BASE}/products/test-station.js", status=404)

    scraper = ShopifyScraper("shop-solar-kits", BASE)
    result = scraper.scrape_product("test-station")

    assert result is not None
    assert len(result["variants"]) == 1
    assert "main-unit-panel" in result["variants"]


@responses.activate
def test_429_rate_limit_retries_after_delay(no_sleep):
    fixture = load_fixture("shop-solar-kits", "eg4-lifepower4-product.json")
    responses.add(
        responses.GET,
        f"{BASE}/products/eg4-lifepower4-lithium-battery.json",
        status=429,
        headers={"Retry-After": "5"},
    )
    responses.add(
        responses.GET,
        f"{BASE}/products/eg4-lifepower4-lithium-battery.json",
        json=fixture,
        status=200,
    )
    responses.add(
        responses.GET, f"{BASE}/products/eg4-lifepower4-lithium-battery.js", status=404
    )

    scraper = ShopifyScraper("shop-solar-kits", BASE)
    result = scraper.scrape_product("eg4-lifepower4-lithium-battery")

    assert result is not None
    assert result["title"].startswith("EG4 LifePower4")

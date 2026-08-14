"""Tests for the content layer: guides, about, disclosure, sitemap, SEO.

The guides are a second rendering surface over the same price store the
product pages use, and audit.py's render hop does NOT read them (it opens
index.html and products/*.html only). So the withhold rules have to be
proven here directly: a bundle must never carry a $/Wh on a guide, a
product with no output_w must never carry a $/W, and a quarantined or
stale price must not reappear on a guide after being pulled from the
product page.

Fixture timestamps are NOW-RELATIVE against a pinned clock, matching
test_build.py — absolute timestamps make the suite calendar-red once they
age past STALE_MAX_HOURS.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from html import escape
from html.parser import HTMLParser
from pathlib import Path

import pytest

from build import (
    GUIDES,
    STALE_MAX_HOURS,
    build_site,
    clip_text,
    format_dollars_per_watt,
    guide_by_slug,
    money,
    price_display,
    same_sku_spreads,
)

REPO_ROOT = Path(__file__).parent.parent
TEMPLATES_DIR = REPO_ROOT / "templates"

NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)

# The site has no public origin by default (it is not deployed), so the
# tests that need canonical tags or a sitemap configure one explicitly.
TEST_BASE_URL = "https://helios.test"

RACK_GUIDE = "server-rack-and-wall-mount-battery-cost-per-kwh"
STATION_GUIDE = "portable-power-stations-compared-by-real-prices"
PANEL_GUIDE = "solar-panel-pallets-cheapest-cost-per-watt"


def _ts(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def _write_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _write_jsonl(path: Path, rows):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _product(pid, name, category, capacity_wh=None, output_w=None, quotes=None):
    specs = {"capacity_wh": capacity_wh, "output_w": output_w,
             "chemistry": "LiFePO4", "weight_lb": None,
             "capacity_source": "test" if capacity_wh else None}
    if quotes:
        specs["capacity_quotes"] = quotes
    return {"id": pid, "name": name, "brand": "TestCo", "category": category,
            "specs": specs, "active": True, "notes": None}


def _variant(price, raw, vid, sku=None, available=True, was=None):
    return {"price": price, "was_price": was, "available": available,
            "raw_variant": raw, "variant_id": vid, "sku": sku}


def seed_data(tmp_path: Path) -> Path:
    """A catalog exercising every guide branch."""
    data_dir = tmp_path / "data"
    prices = data_dir / "prices"
    prices.mkdir(parents=True)

    _write_json(data_dir / "products.json", [
        # --- rack guide -------------------------------------------------
        _product("rack-rated", "Rack Battery 5kWh", "server-rack-battery",
                 capacity_wh=5000, quotes={"r1": "5.0 kWh server rack"}),
        _product("wall-rated", "Wall Battery 10kWh", "home-battery",
                 capacity_wh=10000, quotes={"r2": "10 kWh wall mount"}),
        # capacity unknown -> ranked nowhere, listed with a reason
        _product("rack-unrated", "Rack Battery Mystery", "server-rack-battery",
                 capacity_wh=None),
        # --- station guide ----------------------------------------------
        _product("station-a", "Station A", "portable-power-station",
                 capacity_wh=1000),
        _product("station-b", "Station B", "portable-power-station",
                 capacity_wh=2000),
        # E8 shape: one SKU, two retailers, two different pack quantities
        _product("station-c", "Station C", "portable-power-station",
                 capacity_wh=3000),
        # HIGH-1: would rank FIRST on price alone, but it is sold out
        _product("station-soldout", "Station Sold Out", "portable-power-station",
                 capacity_wh=1000),
        # MEDIUM-4 poison: a standalone unit whose stored price is not a
        # usable number. Capacity is known and the variant is NOT a bundle,
        # so any "everything here is a bundle" explanation would be false.
        _product("station-poison", "Station Poison Price",
                 "portable-power-station", capacity_wh=1000),
        # --- panel guide --------------------------------------------------
        _product("panel-rated", "Panel 200W", "solar-panel", output_w=200),
        # output_w null -> $/W must never appear for this product
        _product("panel-no-output", "Panel Unknown Output", "solar-panel",
                 output_w=None),
        # pallet-only -> every variant is a bundle
        _product("panel-pallet", "Panel Pallet 400W", "solar-panel",
                 output_w=400),
    ])

    _write_json(data_dir / "retailers.json", [
        {"id": "r1", "name": "Retailer One", "url": "https://r1.example",
         "scraper_type": "shopify", "active": True, "priority": 1,
         "affiliate": {"network": "test", "commission": "5%",
                       "cookie_days": None, "link_template": "",
                       "notes": "unverified"}},
        {"id": "r2", "name": "Retailer Two", "url": "https://r2.example",
         "scraper_type": "shopify", "active": True, "priority": 2,
         "affiliate": None},
    ])

    _write_jsonl(prices / "rack-rated.jsonl", [{
        "retailer_id": "r1", "retailer_name": "Retailer One",
        "timestamp": _ts(2), "url": "https://r1.example/products/rack",
        "variants": {
            "one-battery": _variant(1500.00, "1 Battery Only", 11, "RACK-1"),
            # multi-pack: classifier says bundle -> NO $/Wh even though the
            # product has a known capacity (the EG4 LL-S defect class)
            "two-batteries": _variant(3000.00, "2 Batteries Only", 12, "RACK-2"),
        },
        "in_stock": True,
    }])

    _write_jsonl(prices / "wall-rated.jsonl", [{
        "retailer_id": "r2", "retailer_name": "Retailer Two",
        "timestamp": _ts(3), "url": "https://r2.example/products/wall",
        "variants": {"default": _variant(2000.00, "Default Title", 21, "WALL-1")},
        "in_stock": True,
    }])

    _write_jsonl(prices / "rack-unrated.jsonl", [{
        "retailer_id": "r1", "retailer_name": "Retailer One",
        "timestamp": _ts(2), "url": "https://r1.example/products/mystery-rack",
        "variants": {"default": _variant(999.00, "Default Title", 31, "MYST-1")},
        "in_stock": True,
    }])

    # station-a: same SKU at both retailers at different prices -> a spread
    _write_jsonl(prices / "station-a.jsonl", [
        {"retailer_id": "r1", "retailer_name": "Retailer One",
         "timestamp": _ts(4), "url": "https://r1.example/products/station-a",
         "variants": {
             "main": _variant(500.00, "Station A [Main Unit Only]", 41, "SKU-A"),
         },
         "in_stock": True},
        {"retailer_id": "r2", "retailer_name": "Retailer Two",
         "timestamp": _ts(4), "url": "https://r2.example/products/station-a",
         "variants": {
             "main": _variant(600.00, "Station A Main Unit", 42, "SKU-A"),
         },
         "in_stock": True},
    ])

    # station-b: the cheapest variant is a KIT — it must not be rated, and
    # the product's rank must come from the unit, not the kit.
    _write_jsonl(prices / "station-b.jsonl", [{
        "retailer_id": "r1", "retailer_name": "Retailer One",
        "timestamp": _ts(5), "url": "https://r1.example/products/station-b",
        "variants": {
            "kit": _variant(900.00, "Station B Kit + 200W Panel", 51, "SKU-BK"),
            "unit": _variant(1400.00, "Station B [Main Unit Only]", 52, "SKU-B"),
        },
        "in_stock": True,
    }])

    # station-c: the SAME SKU carries a different pack quantity at each
    # retailer — EXPANSION_LOG E8's shifted RS-M410 SKUs, in miniature.
    _write_jsonl(prices / "station-c.jsonl", [
        {"retailer_id": "r1", "retailer_name": "Retailer One",
         "timestamp": _ts(4), "url": "https://r1.example/products/station-c",
         "variants": {"pack": _variant(2000.00, "2 Batteries Only", 91, "SKU-C")},
         "in_stock": True},
        {"retailer_id": "r2", "retailer_name": "Retailer Two",
         "timestamp": _ts(4), "url": "https://r2.example/products/station-c",
         "variants": {"pack": _variant(2400.00, "3 Batteries Only", 92, "SKU-C")},
         "in_stock": True},
    ])

    # station-soldout: cheapest rateable $/Wh on the whole guide ($0.30/Wh)
    # but the variant cannot be bought. It must not take rank #1; it must
    # be listed, marked sold out, with the real reason.
    _write_jsonl(prices / "station-soldout.jsonl", [{
        "retailer_id": "r1", "retailer_name": "Retailer One",
        "timestamp": _ts(4), "url": "https://r1.example/products/station-soldout",
        "variants": {"main": _variant(300.00, "Station [Main Unit Only]", 101,
                                      "SKU-SO", available=False)},
        "in_stock": False,
    }])

    # station-poison: NaN in a price field. Nothing may render "$nan".
    _write_jsonl(prices / "station-poison.jsonl", [{
        "retailer_id": "r1", "retailer_name": "Retailer One",
        "timestamp": _ts(4), "url": "https://r1.example/products/station-poison",
        "variants": {
            "main": _variant(float("nan"), "Station [Main Unit Only]", 111,
                             "SKU-NAN"),
            "second": _variant(float("inf"), "Station [Main Unit Only] B", 112,
                               "SKU-INF"),
        },
        "in_stock": True,
    }])

    _write_jsonl(prices / "panel-rated.jsonl", [{
        "retailer_id": "r1", "retailer_name": "Retailer One",
        "timestamp": _ts(6), "url": "https://r1.example/products/panel",
        "variants": {"default": _variant(180.00, "Default Title", 61, "PAN-1")},
        "in_stock": True,
    }])

    _write_jsonl(prices / "panel-no-output.jsonl", [{
        "retailer_id": "r1", "retailer_name": "Retailer One",
        "timestamp": _ts(6), "url": "https://r1.example/products/panel-unknown",
        "variants": {"default": _variant(250.00, "Default Title", 71, "PAN-2")},
        "in_stock": True,
    }])

    _write_jsonl(prices / "panel-pallet.jsonl", [{
        "retailer_id": "r1", "retailer_name": "Retailer One",
        "timestamp": _ts(6), "url": "https://r1.example/products/pallet",
        "variants": {
            "eight": _variant(2400.00, "8 Solar Panels", 81, "PAL-8"),
            "twelve": _variant(3600.00, "12 Solar Panels", 82, "PAL-12"),
        },
        "in_stock": True,
    }])

    return data_dir


def set_origin(data_dir: Path, base_url: str = TEST_BASE_URL) -> None:
    """Give the fixture site a public origin (as a deploy would)."""
    _write_json(data_dir / "site_config.json", {"site_base_url": base_url})


@pytest.fixture
def built(tmp_path):
    data_dir = seed_data(tmp_path)
    site_dir = tmp_path / "site"
    summary = build_site(data_dir=data_dir, site_dir=site_dir,
                         templates_dir=TEMPLATES_DIR, now=NOW)
    return data_dir, site_dir, summary


@pytest.fixture
def built_deployed(tmp_path):
    """The same site as `built`, but with an origin configured."""
    data_dir = seed_data(tmp_path)
    set_origin(data_dir)
    site_dir = tmp_path / "site"
    summary = build_site(data_dir=data_dir, site_dir=site_dir,
                         templates_dir=TEMPLATES_DIR, now=NOW)
    return data_dir, site_dir, summary


def guide_html(site_dir: Path, slug: str) -> str:
    return (site_dir / "guides" / f"{slug}.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# A tiny provenance parser — the same discipline audit.py uses: read the
# data-* attributes, never a prose regex.
# ---------------------------------------------------------------------------

class RowParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self._cur = None
        self._field = None
        self._product = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("article", "div") and a.get("data-product-id"):
            self._product = a["data-product-id"]
        if tag == "tr":
            self._cur = {"attrs": a, "fields": {}, "product": self._product,
                         "links": []}
        elif self._cur is not None and tag == "a":
            self._cur["links"].append(a)
        elif self._cur is not None and a.get("data-field"):
            self._field = a["data-field"]
            self._cur.setdefault("field_attrs", {})[self._field] = a

    def handle_endtag(self, tag):
        if tag == "tr" and self._cur is not None:
            self.rows.append(self._cur)
            self._cur = None
        if tag in ("span", "div"):
            self._field = None

    def handle_data(self, data):
        if self._cur is not None and self._field and data.strip():
            self._cur["fields"][self._field] = data.strip()


def parse_rows(html: str) -> list[dict]:
    parser = RowParser()
    parser.feed(html)
    return [r for r in parser.rows if "data-tier" in r["attrs"]]


def rating_of(row: dict):
    """The rated figure on a guide row, whichever metric it uses.

    $/Wh renders under data-field="wh" — the same name the product and
    home pages use, so audit.py verifies it with its existing comparison —
    and $/W under "watt", because it is a different quantity.
    """
    fields = row["fields"]
    return fields.get("wh") or fields.get("watt")


def rating_attrs(row: dict):
    attrs = row.get("field_attrs") or {}
    return attrs.get("wh") or attrs.get("watt")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_every_guide_renders(built):
    _, site_dir, summary = built
    assert len(GUIDES) == 3
    for spec in GUIDES:
        path = site_dir / "guides" / f"{spec['slug']}.html"
        assert path.exists(), f"{spec['slug']} not rendered"
        html = path.read_text(encoding="utf-8")
        # escaped: the rack guide's h1 contains "&", which Jinja escapes
        assert escape(spec["h1"]) in html
        assert "Methodology" in html
    assert summary["guide_pages"] == 3
    assert summary["info_pages"] == 2


def test_guides_rank_by_metric_ascending(built):
    _, site_dir, _ = built
    html = guide_html(site_dir, RACK_GUIDE)
    # wall-rated: 2000/10000 = $0.20/Wh ; rack-rated: 1500/5000 = $0.30/Wh
    ranked = re.findall(r'<span class="rank">(\d+)\.</span>\s*\n?\s*'
                        r'<a href="[^"]*products/([^."]+)\.html"', html)
    assert [pid for _, pid in ranked] == ["wall-rated", "rack-rated"]
    assert "$0.20/Wh" in html
    assert "$0.30/Wh" in html


def test_guide_ranks_product_by_its_unit_not_a_cheaper_kit(built):
    """station-b's cheapest variant is a $900 kit; its rank must come from
    the $1,400 unit (1400/2000 = $0.70/Wh), and the kit must carry no
    rating at all."""
    _, site_dir, _ = built
    html = guide_html(site_dir, STATION_GUIDE)
    assert "$0.70/Wh" in html
    assert "$0.45/Wh" not in html, "kit price rated against unit capacity"
    for row in parse_rows(html):
        if row["attrs"].get("data-sku") == "SKU-BK":
            assert rating_of(row) is None


# ---------------------------------------------------------------------------
# Withhold rules on guides
# ---------------------------------------------------------------------------

def test_no_bundle_variant_is_ever_rated_on_any_guide(built):
    """THE rule (PLAN 2b). Checked on every rated row of every guide by
    reading the provenance attributes, not by eyeballing one product."""
    _, site_dir, _ = built
    bundle_skus = {"RACK-2", "SKU-BK", "PAL-8", "PAL-12"}
    rated_any = 0
    for spec in GUIDES:
        html = guide_html(site_dir, spec["slug"])
        for row in parse_rows(html):
            has_rating = rating_of(row) is not None
            rated_any += int(has_rating)
            if row["attrs"].get("data-sku") in bundle_skus:
                assert not has_rating, (
                    f"{spec['slug']}: bundle {row['attrs']['data-sku']} rated "
                    f"{rating_of(row)}")
    assert rated_any > 0, "no rated rows at all — the assertion proved nothing"


def test_multipack_shows_price_and_badge_but_no_dollars_per_wh(built):
    _, site_dir, _ = built
    html = guide_html(site_dir, RACK_GUIDE)
    assert "$3,000.00" in html            # the pack price is still published
    assert 'class="badge">bundle</span>' in html
    assert "$0.60/Wh" not in html         # 3000 / 5000 — the wrong number


def test_null_output_w_never_shows_dollars_per_watt(built):
    """A panel with no output_w is listed, priced, and never rated."""
    _, site_dir, _ = built
    html = guide_html(site_dir, PANEL_GUIDE)
    assert "Panel Unknown Output" in html
    assert "$250.00" in html
    for row in parse_rows(html):
        if row["product"] == "panel-no-output":
            assert rating_of(row) is None
    # no $/W string anywhere may equal a rating derived for that product
    assert "rated output is not established" in html


def test_pallet_only_product_is_listed_but_unranked(built):
    _, site_dir, _ = built
    html = guide_html(site_dir, PANEL_GUIDE)
    assert "Panel Pallet 400W" in html
    assert "every variant on sale is a bundle or a multi-unit pack" in html
    assert "$2,400.00" in html            # pallet price still published
    assert "$0.75/W" not in html          # 2400 / (400*8) — never derived


def test_rated_panel_uses_output_w(built):
    _, site_dir, _ = built
    html = guide_html(site_dir, PANEL_GUIDE)
    assert format_dollars_per_watt(180.0 / 200) == "$0.90/W"
    assert "$0.90/W" in html


def test_unknown_capacity_product_is_listed_with_its_reason(built):
    _, site_dir, _ = built
    html = guide_html(site_dir, RACK_GUIDE)
    assert "Rack Battery Mystery" in html
    assert "usable capacity is not established" in html
    assert "$999.00" in html              # price still shown


def test_stale_row_is_withheld_from_guides(tmp_path):
    data_dir = seed_data(tmp_path)
    path = data_dir / "prices" / "wall-rated.jsonl"
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    row["timestamp"] = _ts(STALE_MAX_HOURS + 1)
    _write_jsonl(path, [row])
    site_dir = tmp_path / "site"
    build_site(data_dir=data_dir, site_dir=site_dir,
               templates_dir=TEMPLATES_DIR, now=NOW)
    html = guide_html(site_dir, RACK_GUIDE)
    assert 'data-withheld="stale"' in html
    assert "$2,000.00" not in html
    assert "$0.20/Wh" not in html


def test_quarantined_variant_is_withheld_from_guides(tmp_path):
    """A price pulled off the product page must not reappear on a guide."""
    data_dir = seed_data(tmp_path)
    _write_json(data_dir / "quarantine.json", {
        "r1:rack-rated:11": {
            "sku": "RACK-1", "tier_last_seen": "one-battery",
            "reason": "render_defect", "observed": "$1,111.00",
            "expected": "$1,500.00", "first_seen": _ts(24),
            "last_seen": _ts(1), "consecutive_failures": 1,
            "unobserved_audits": 0,
        }
    })
    site_dir = tmp_path / "site"
    build_site(data_dir=data_dir, site_dir=site_dir,
               templates_dir=TEMPLATES_DIR, now=NOW)
    html = guide_html(site_dir, RACK_GUIDE)
    assert 'data-withheld="quarantine"' in html
    assert "$1,500.00" not in html
    assert "$0.30/Wh" not in html
    assert "under verification" in html
    # and the product page agrees — the two surfaces cannot disagree
    page = (site_dir / "products" / "rack-rated.html").read_text(encoding="utf-8")
    assert "$1,500.00" not in page


def test_unmapped_pair_is_absent_from_guides(tmp_path):
    """handle_maps is the carriage contract for guides too (red team #5)."""
    data_dir = seed_data(tmp_path)
    _write_json(data_dir / "handle_maps.json", {"r1": {}, "r2": {}})
    site_dir = tmp_path / "site"
    build_site(data_dir=data_dir, site_dir=site_dir,
               templates_dir=TEMPLATES_DIR, now=NOW)
    html = guide_html(site_dir, RACK_GUIDE)
    assert "$1,500.00" not in html
    assert "$2,000.00" not in html


# ---------------------------------------------------------------------------
# Cross-surface consistency: a guide may not disagree with a product page
# ---------------------------------------------------------------------------

def test_guide_rating_equals_product_page_rating_for_same_variant(built):
    _, site_dir, _ = built
    compared = 0
    for slug in (RACK_GUIDE, STATION_GUIDE):
        for grow in parse_rows(guide_html(site_dir, slug)):
            if rating_of(grow) is None:
                continue
            page = (site_dir / "products" / f"{grow['product']}.html")
            for prow in parse_rows(page.read_text(encoding="utf-8")):
                if (prow["attrs"].get("data-tier") == grow["attrs"]["data-tier"]
                        and prow["attrs"].get("data-variant-id")
                        == grow["attrs"].get("data-variant-id")):
                    assert prow["fields"].get("wh") == rating_of(grow)
                    compared += 1
    assert compared >= 3


def test_every_outbound_guide_link_is_nofollow_sponsored(built):
    _, site_dir, _ = built
    checked = 0
    for spec in GUIDES:
        html = guide_html(site_dir, spec["slug"])
        for row in parse_rows(html):
            for link in row["links"]:
                if link.get("href", "").startswith("http"):
                    assert link.get("rel") == "nofollow sponsored"
                    checked += 1
    assert checked > 0


def test_every_rendered_price_carries_provenance(built):
    _, site_dir, _ = built
    for spec in GUIDES:
        for row in parse_rows(guide_html(site_dir, spec["slug"])):
            if "price" not in row["fields"]:
                continue
            assert row["attrs"].get("data-scraped-at")
            assert "data-tier" in row["attrs"]
            assert "data-sku" in row["attrs"]


def test_rating_carries_spec_provenance(built):
    """Every rating says which spec it divided by and how well sourced it
    is — quoted from this retailer, quoted elsewhere, or unquoted."""
    _, site_dir, _ = built
    seen = set()
    for spec in GUIDES:
        for row in parse_rows(guide_html(site_dir, spec["slug"])):
            attrs = rating_attrs(row)
            if not attrs:
                continue
            assert attrs["data-spec-key"] in ("capacity_wh", "output_w")
            assert float(attrs["data-spec-value"]) > 0
            seen.add(attrs["data-spec-provenance"])
    assert seen <= {"quoted", "cross-retailer", "unquoted"}
    assert "quoted" in seen and "unquoted" in seen


# ---------------------------------------------------------------------------
# Same-SKU spreads
# ---------------------------------------------------------------------------

def test_same_sku_spread_is_reported(built):
    _, site_dir, _ = built
    html = guide_html(site_dir, STATION_GUIDE)
    assert "Same SKU at more than one retailer" in html
    assert "SKU-A" in html
    assert "$100.00" in html   # 600 - 500
    assert "20%" in html       # 100 / 500
    # both retailers' own labels are shown verbatim
    assert "Station A [Main Unit Only]" in html
    assert "Station A Main Unit" in html


def test_differently_worded_labels_are_not_flagged_as_a_conflict():
    """Merchants word the same item differently as a matter of course.
    Flagging that would put a warning on nearly every spread and bury the
    ones that mean something."""
    offers = [
        {"sku": "X", "retailer_id": "r1", "price": 1049.0, "withheld": None,
         "raw_variant": "DELTA MAX [Unit Only]", "classification": "unit",
         "available": True},
        {"sku": "X", "retailer_id": "r2", "price": 1599.0, "withheld": None,
         "raw_variant": "EcoFlow DELTA Max Portable Power Station(Main Unit ONLY)",
         "classification": "unit", "available": True},
    ]
    assert same_sku_spreads(offers)[0]["identity_conflict"] is None


def test_quantity_conflict_on_a_shared_sku_is_flagged(built):
    """EXPANSION_LOG E8: one SKU, two different pack sizes."""
    _, site_dir, _ = built
    html = guide_html(site_dir, STATION_GUIDE)
    assert "SKU-C" in html
    assert "different quantities" in html
    assert "not a like-for-like price comparison" in html


def test_quantity_conflict_detection_is_exact():
    from build import _identity_conflict, _quantity_tokens
    assert _quantity_tokens("10 Solar Panels") == frozenset({10})
    assert _quantity_tokens("12 Solar Panels") == frozenset({12})
    # a wattage is not a quantity
    assert _quantity_tokens("EcoFlow River 2 Pro + 160W Solar Panel") \
        == frozenset()
    def offer(raw, cls="bundle"):
        return {"raw_variant": raw, "classification": cls}
    assert _identity_conflict(
        [offer("10 Solar Panels"), offer("12 Solar Panels")]) == "quantity"
    assert _identity_conflict(
        [offer("10 Solar Panels"), offer("10 Solar Panels")]) is None
    assert _identity_conflict(
        [offer("Main Unit", "unit"), offer("Main Unit Kit")]) == "kind"


def test_spread_needs_two_retailers():
    one = [{"sku": "X", "retailer_id": "r1", "price": 10.0, "withheld": None,
            "raw_variant": "a", "classification": "unit",
            "available": True}]
    assert same_sku_spreads(one) == []


def test_withheld_offer_never_enters_a_spread():
    offers = [
        {"sku": "X", "retailer_id": "r1", "price": 10.0, "withheld": None,
         "raw_variant": "a", "classification": "unit", "available": True},
        {"sku": "X", "retailer_id": "r2", "price": 99.0, "available": True,
         "withheld": "quarantine", "raw_variant": "a",
         "classification": "unit"},
    ]
    assert same_sku_spreads(offers) == []


def test_blank_sku_never_joins():
    offers = [
        {"sku": "  ", "retailer_id": "r1", "price": 10.0, "withheld": None,
         "raw_variant": "a", "classification": "unit", "available": True},
        {"sku": None, "retailer_id": "r2", "price": 20.0, "withheld": None,
         "raw_variant": "a", "classification": "unit", "available": True},
    ]
    assert same_sku_spreads(offers) == []


# ---------------------------------------------------------------------------
# Sitemap
# ---------------------------------------------------------------------------

def test_no_sitemap_and_no_canonicals_without_a_configured_origin(built):
    """The site is not deployed and has no public origin. Every absolute
    URL we could write would 404, so we write none: a canonical pointing
    at a dead URL tells crawlers to prefer it over the live page."""
    _, site_dir, summary = built
    assert not (site_dir / "sitemap.xml").exists()
    assert summary["sitemap_urls"] == 0
    assert summary["site_base_url"] is None
    for page in site_dir.rglob("*.html"):
        html = page.read_text(encoding="utf-8")
        assert 'rel="canonical"' not in html, page.name


def test_a_stale_sitemap_is_removed_when_the_origin_goes_away(tmp_path):
    """A sitemap left on disk from a configured build is a file full of
    404s; the next unconfigured build must delete it, not ignore it."""
    data_dir = seed_data(tmp_path)
    set_origin(data_dir)
    site_dir = tmp_path / "site"
    build_site(data_dir=data_dir, site_dir=site_dir,
               templates_dir=TEMPLATES_DIR, now=NOW)
    assert (site_dir / "sitemap.xml").exists()

    (data_dir / "site_config.json").unlink()
    build_site(data_dir=data_dir, site_dir=site_dir,
               templates_dir=TEMPLATES_DIR, now=NOW)
    assert not (site_dir / "sitemap.xml").exists()


def test_origin_can_come_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_SITE_BASE_URL", "https://env.example/")
    data_dir = seed_data(tmp_path)
    site_dir = tmp_path / "site"
    summary = build_site(data_dir=data_dir, site_dir=site_dir,
                         templates_dir=TEMPLATES_DIR, now=NOW)
    # trailing slash normalized away, no doubled separator in URLs
    assert summary["site_base_url"] == "https://env.example"
    sitemap = (site_dir / "sitemap.xml").read_text(encoding="utf-8")
    assert "https://env.example/index.html" in sitemap
    assert "//index.html" not in sitemap


def test_blank_origin_counts_as_unset(tmp_path):
    data_dir = seed_data(tmp_path)
    _write_json(data_dir / "site_config.json", {"site_base_url": "   "})
    site_dir = tmp_path / "site"
    summary = build_site(data_dir=data_dir, site_dir=site_dir,
                         templates_dir=TEMPLATES_DIR, now=NOW)
    assert summary["site_base_url"] is None
    assert not (site_dir / "sitemap.xml").exists()


def test_sitemap_lists_every_rendered_page_exactly_once(built_deployed):
    _, site_dir, summary = built_deployed
    sitemap = (site_dir / "sitemap.xml").read_text(encoding="utf-8")
    locs = re.findall(r"<loc>([^<]+)</loc>", sitemap)
    assert len(locs) == len(set(locs)), "a URL is listed more than once"

    on_disk = {
        p.relative_to(site_dir).as_posix()
        for p in site_dir.rglob("*.html")
    }
    base = TEST_BASE_URL.rstrip("/") + "/"
    listed = set()
    for loc in locs:
        assert loc.startswith(base), loc
        listed.add(loc[len(base):])
    assert listed == on_disk
    assert len(locs) == summary["sitemap_urls"] == summary["total_pages_written"]


def test_sitemap_includes_products_guides_and_info_pages(built_deployed):
    _, site_dir, _ = built_deployed
    sitemap = (site_dir / "sitemap.xml").read_text(encoding="utf-8")
    assert "/index.html" in sitemap
    assert "/about.html" in sitemap
    assert "/disclosure.html" in sitemap
    assert "/products/rack-rated.html" in sitemap
    for spec in GUIDES:
        assert f"/guides/{spec['slug']}.html" in sitemap


def test_sitemap_lastmod_uses_the_injected_clock(built_deployed):
    _, site_dir, _ = built_deployed
    sitemap = (site_dir / "sitemap.xml").read_text(encoding="utf-8")
    assert f"<lastmod>{NOW.date().isoformat()}</lastmod>" in sitemap


def test_sitemap_is_lf_only(built_deployed):
    _, site_dir, _ = built_deployed
    raw = (site_dir / "sitemap.xml").read_bytes()
    assert b"\r\n" not in raw


def test_canonicals_appear_once_an_origin_is_configured(built_deployed):
    _, site_dir, _ = built_deployed
    for page in site_dir.rglob("*.html"):
        html = page.read_text(encoding="utf-8")
        canonical = re.search(r'<link rel="canonical" href="([^"]+)"', html)
        assert canonical, page.name
        rel = page.relative_to(site_dir).as_posix()
        assert canonical.group(1) == f"{TEST_BASE_URL}/{rel}"


# ---------------------------------------------------------------------------
# Navigation / SEO
# ---------------------------------------------------------------------------

def test_disclosure_is_linked_from_every_page(built):
    _, site_dir, _ = built
    pages = list(site_dir.rglob("*.html"))
    assert len(pages) > 5
    for page in pages:
        html = page.read_text(encoding="utf-8")
        depth = len(page.relative_to(site_dir).parts) - 1
        href = "../" * depth + "disclosure.html"
        assert f'href="{href}"' in html, f"{page.name} does not link disclosure"


def test_nav_links_every_guide_from_every_page(built):
    _, site_dir, _ = built
    for page in site_dir.rglob("*.html"):
        html = page.read_text(encoding="utf-8")
        depth = len(page.relative_to(site_dir).parts) - 1
        prefix = "../" * depth
        for spec in GUIDES:
            assert f'href="{prefix}guides/{spec["slug"]}.html"' in html
        assert f'href="{prefix}about.html"' in html


def test_every_page_has_a_unique_title_and_a_description(built):
    _, site_dir, _ = built
    titles = []
    for page in site_dir.rglob("*.html"):
        html = page.read_text(encoding="utf-8")
        title = re.search(r"<title>(.*?)</title>", html, re.S)
        assert title, page
        titles.append(title.group(1))
        desc = re.search(r'<meta name="description" content="([^"]*)"', html)
        assert desc, f"{page.name} has no meta description"
        assert 0 < len(desc.group(1)) <= 160
    assert len(titles) == len(set(titles)), "duplicate <title> across pages"


def test_titles_and_descriptions_are_data_derived(built):
    _, site_dir, _ = built
    html = guide_html(site_dir, PANEL_GUIDE)
    desc = re.search(r'<meta name="description" content="([^"]*)"', html).group(1)
    # the fixture has 3 solar-panel products; only panel-rated can be rated
    assert "1 of 3 tracked solar panels ranked by $/W" in desc
    assert "Cheapest tracked is $0.90/W" in desc
    home = (site_dir / "index.html").read_text(encoding="utf-8")
    hdesc = re.search(r'<meta name="description" content="([^"]*)"',
                      home).group(1)
    assert "11 solar and home-energy products across 2 retailers" in hdesc


def test_clip_text_clips_on_a_word_boundary():
    assert clip_text("short text", 50) == "short text"
    out = clip_text("word " * 60, 40)
    assert len(out) <= 40
    assert out.endswith("...")
    assert "  " not in out


def test_no_external_assets_anywhere(built):
    """PLAN section 2: pages are self-contained. Outbound links to
    retailers are fine; loading a remote script/font/stylesheet is not."""
    _, site_dir, _ = built
    for page in site_dir.rglob("*.html"):
        html = page.read_text(encoding="utf-8")
        assert "<script" not in html
        assert not re.search(r'<link[^>]+rel="stylesheet"', html)
        assert not re.search(r'<img[^>]+src="https?:', html)


# ---------------------------------------------------------------------------
# About / disclosure honesty
# ---------------------------------------------------------------------------

def test_about_page_counts_come_from_the_catalog(built):
    _, site_dir, _ = built
    html = (site_dir / "about.html").read_text(encoding="utf-8")
    flat = " ".join(html.split())
    assert "11 products" in flat
    assert "2 retailers" in flat
    assert "Retailer One" in flat and "Retailer Two" in flat
    # capacity discipline: 7 of the 11 fixture products carry a capacity,
    # and 2 of those 7 have a verbatim capacity_quotes entry.
    assert "Of 11 tracked products, 7 carry a capacity figure and 4 do not" in flat
    assert "2 of the 7 have that evidence stored" in flat
    assert f"older than {STALE_MAX_HOURS} hours" in flat


def test_about_page_states_the_real_cadence(built):
    """Pins the cadence claim to reality. Reality as of 2026-08-14: the
    scrape cron is enabled (11:00 + 21:30 UTC in scrape.yml), so the page
    states the twice-daily schedule. If the cron is ever removed, this
    test must fail until the page tells the truth again."""
    _, site_dir, _ = built
    html = (site_dir / "about.html").read_text(encoding="utf-8")
    assert "collected on a schedule, twice daily" in html
    # the reader-verifiable check stays advertised
    assert "displays its own age" in html
    # no invented refresh promise beyond the real schedule
    assert "updated daily" not in html.lower()
    assert "hourly" not in html.lower()
    # the schedule this pins must actually exist where CI reads it
    workflow = (REPO_ROOT / ".github" / "workflows" / "scrape.yml").read_text(
        encoding="utf-8")
    assert "schedule:" in workflow and "cron:" in workflow


def test_pages_make_no_unverifiable_popularity_claims(built):
    _, site_dir, _ = built
    banned = ["trusted by", "thousands of", "millions of", "#1 ",
              "best in class", "award-winning", "readers rely",
              "industry-leading", "years of experience"]
    for page in site_dir.rglob("*.html"):
        low = page.read_text(encoding="utf-8").lower()
        for phrase in banned:
            assert phrase not in low, f"{page.name}: {phrase!r}"


def test_disclosure_says_nothing_is_earned_while_no_program_is_live(built):
    """affiliate_live is computed from link_template, so this sentence
    flips by itself the day a program is actually wired up."""
    _, site_dir, _ = built
    html = (site_dir / "disclosure.html").read_text(encoding="utf-8")
    assert "Helios earns nothing today" in html
    assert "Not joined" in html
    assert "Retailer Two" in html


def test_disclosure_claims_only_what_the_records_evidence(built):
    """MEDIUM-6. A record whose own notes say the terms are unverified is
    not evidence that a programme exists, and a NULL record is an absence
    of information, not evidence that no programme exists. The page that
    exists to be trusted has to model that distinction exactly."""
    _, site_dir, _ = built
    flat = " ".join((site_dir / "disclosure.html")
                    .read_text(encoding="utf-8").split())

    # r1's affiliate notes say "unverified" -> must not be asserted as fact
    assert "Programme exists" not in flat
    assert "Affiliate terms recorded but <strong>unverified</strong>" in flat
    assert "not evidence that a programme exists" in flat

    # r2 has affiliate: null -> absence of information, not absence of fact
    assert "None found" not in flat
    assert "No commercial relationship possible" not in flat
    assert ("We hold no information about this retailer's affiliate "
            "arrangements") in flat
    assert "absence of information</em>, not evidence that no programme exists" \
        in flat


def test_disclosure_marks_verified_terms_as_verified(tmp_path):
    """Counter-case: drop the "unverified" marker and the page stops
    hedging — the hedge is data-driven, not boilerplate."""
    data_dir = seed_data(tmp_path)
    retailers = json.loads(
        (data_dir / "retailers.json").read_text(encoding="utf-8"))
    retailers[0]["affiliate"]["notes"] = "Terms confirmed with the retailer."
    retailers[0]["affiliate"]["network"] = "impact"
    _write_json(data_dir / "retailers.json", retailers)
    site_dir = tmp_path / "site"
    build_site(data_dir=data_dir, site_dir=site_dir,
               templates_dir=TEMPLATES_DIR, now=NOW)
    flat = " ".join((site_dir / "disclosure.html")
                    .read_text(encoding="utf-8").split())
    assert "Affiliate terms recorded and verified" in flat
    assert "<strong>unverified</strong>" not in flat


def test_disclosure_flips_when_a_link_template_exists(tmp_path):
    data_dir = seed_data(tmp_path)
    retailers = json.loads(
        (data_dir / "retailers.json").read_text(encoding="utf-8"))
    retailers[0]["affiliate"]["link_template"] = "https://track.example/?u={url}"
    _write_json(data_dir / "retailers.json", retailers)
    site_dir = tmp_path / "site"
    build_site(data_dir=data_dir, site_dir=site_dir,
               templates_dir=TEMPLATES_DIR, now=NOW)
    html = (site_dir / "disclosure.html").read_text(encoding="utf-8")
    assert "Helios earns nothing today" not in html
    assert "Helios earns a commission" in html
    assert "Active — Helios may earn a commission" in html


# ---------------------------------------------------------------------------
# Constants that must not drift from the rest of the repo
# ---------------------------------------------------------------------------

def test_ucp_profile_homepage_is_pinned_to_the_repo_url():
    """Pins a CONSTANT. This test makes no network request and therefore
    cannot show that the URL resolves — the previous name claimed it could.

    The homepage is fetched by MERCHANTS deciding whether to answer us, so
    it has to point at something real. It used to point at a GitHub Pages
    URL that 404s (Pages serves the repo root, so the site lives under
    /Helios/site/), and an earlier test pinned build.py's SITE_BASE_URL to
    it — locking two wrong values together and reporting agreement as
    correctness. Now pins the LIVE Vercel deployment (verified serving
    HTTP 200 with the site content on 2026-08-14, the same origin that
    passed the merchant-side UCP profile fetch). Swap for the custom
    domain when one exists."""
    expected = "https://helios-projectgaiaas-projects.vercel.app/"
    for rel in ("ucp-agent-profile.json", "site/.well-known/ucp-agent.json"):
        profile = json.loads((REPO_ROOT / rel).read_text(encoding="utf-8"))
        assert profile["homepage"] == expected, rel
        # dispatch-only today, cron later: true under both
        assert "up to twice daily" in profile["description"], rel


def test_both_ucp_profile_copies_agree():
    """Two copies of one document drift. They are served to the same
    audience and must not disagree about what Helios is."""
    root = json.loads(
        (REPO_ROOT / "ucp-agent-profile.json").read_text(encoding="utf-8"))
    served = json.loads(
        (REPO_ROOT / "site/.well-known/ucp-agent.json").read_text(encoding="utf-8"))
    for key in ("homepage", "description", "name", "contact"):
        assert root.get(key) == served.get(key), key


def test_contact_email_matches_the_scraper_user_agent():
    from build import CONTACT_EMAIL
    from scrapers.polite import BOT_USER_AGENT
    assert CONTACT_EMAIL in BOT_USER_AGENT


def test_guide_slugs_are_unique_and_url_safe():
    slugs = [g["slug"] for g in GUIDES]
    assert len(slugs) == len(set(slugs))
    for slug in slugs:
        assert re.fullmatch(r"[a-z0-9-]+", slug), slug
        assert guide_by_slug(slug)["slug"] == slug
    assert guide_by_slug("nope") is None


def test_guide_categories_exist_in_the_real_catalog():
    """A guide scoped to a category nothing uses would render an empty
    page forever without anyone noticing."""
    products = json.loads(
        (REPO_ROOT / "data" / "products.json").read_text(encoding="utf-8"))
    known = {p.get("category") for p in products}
    for spec in GUIDES:
        assert set(spec["categories"]) & known, spec["slug"]


# ---------------------------------------------------------------------------
# HIGH-1: availability is part of a ranking claim
# ---------------------------------------------------------------------------

def test_sold_out_offer_never_takes_a_rank(built):
    """station-soldout is the cheapest rateable $/Wh in the fixture
    ($300/1000Wh = $0.30/Wh) and is sold out. Ranking it would tell a
    reader the best buy is something no tracked retailer will sell."""
    _, site_dir, _ = built
    html = guide_html(site_dir, STATION_GUIDE)
    ranked = re.findall(r'<span class="rank">(\d+)\.</span>\s*\n?\s*'
                        r'<a href="[^"]*products/([^."]+)\.html"', html)
    ranked_ids = [pid for _, pid in ranked]
    assert "station-soldout" not in ranked_ids
    assert ranked_ids, "nothing ranked at all - the assertion proves nothing"
    # it is still listed, still priced, and the reason is the real one
    assert "Station Sold Out" in html
    assert "$300.00" in html
    assert "the best price we can rate is currently sold out" in html


def test_sold_out_variant_keeps_its_price_and_rating_in_the_table(built):
    """Sold out removes a rank, not the information."""
    _, site_dir, _ = built
    html = guide_html(site_dir, STATION_GUIDE)
    for row in parse_rows(html):
        if row["attrs"].get("data-sku") == "SKU-SO":
            assert row["fields"].get("price") == "$300.00"
            assert row["fields"].get("wh") == "$0.30/Wh"
            attrs = (row.get("field_attrs") or {})["availability"]
            assert attrs["data-value"] == "false"
            break
    else:
        raise AssertionError("sold-out row not rendered at all")
    assert "not ranked: sold out" in html


def test_headline_shows_availability_of_the_ranked_variant(built):
    """The headline price is a same-variant claim, so its availability has
    to travel with it, exactly like a home cell."""
    _, site_dir, _ = built
    for slug in (RACK_GUIDE, STATION_GUIDE, PANEL_GUIDE):
        html = guide_html(site_dir, slug)
        blocks = re.findall(r'data-field="best-rating".*?</p>', html, re.S)
        assert blocks, slug
        for block in blocks:
            assert 'data-field="availability"' in block, slug


def test_meta_description_headline_uses_the_available_ranked_set(built):
    """The "cheapest tracked is" figure must come from what can be bought:
    $0.30/Wh (sold out) must not be advertised in search results."""
    _, site_dir, _ = built
    html = guide_html(site_dir, STATION_GUIDE)
    desc = re.search(r'<meta name="description" content="([^"]*)"', html).group(1)
    assert "$0.30/Wh" not in desc
    # station-a's in-stock unit at $500/1000Wh is the real cheapest
    assert "Cheapest tracked is $0.50/Wh" in desc


def test_spread_requires_both_sides_in_stock(tmp_path):
    """HIGH-3: a spread is a buying claim. A gap whose cheap side is sold
    out advertises a saving nobody can take."""
    data_dir = seed_data(tmp_path)
    path = data_dir / "prices" / "station-a.jsonl"
    rows = [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows[0]["variants"]["main"]["available"] = False   # the cheap side
    _write_jsonl(path, rows)
    site_dir = tmp_path / "site"
    build_site(data_dir=data_dir, site_dir=site_dir,
               templates_dir=TEMPLATES_DIR, now=NOW)
    html = guide_html(site_dir, STATION_GUIDE)
    assert "$100.00" not in html, "spread rendered on a sold-out cheap side"


def test_spread_tables_show_availability_on_both_sides(built):
    _, site_dir, _ = built
    html = guide_html(site_dir, STATION_GUIDE)
    start = html.find('id="spreads"')
    assert start != -1
    block = html[start:html.find('id="unranked"')]
    assert block.count('data-field="availability"') >= 2


def test_same_sku_spreads_drops_sold_out_offers():
    offers = [
        {"sku": "X", "retailer_id": "r1", "price": 10.0, "withheld": None,
         "raw_variant": "a", "classification": "unit", "available": False},
        {"sku": "X", "retailer_id": "r2", "price": 20.0, "withheld": None,
         "raw_variant": "a", "classification": "unit", "available": True},
    ]
    assert same_sku_spreads(offers) == []


# ---------------------------------------------------------------------------
# MEDIUM-4: the unranked placard must state the reason that applies
# ---------------------------------------------------------------------------

def test_unreadable_price_does_not_print_a_false_bundle_reason(built):
    """station-poison has a KNOWN capacity and a NON-bundle variant whose
    stored price is NaN. The old fall-through printed "every variant on
    sale is a bundle or a multi-unit pack", which is simply untrue."""
    _, site_dir, _ = built
    html = guide_html(site_dir, STATION_GUIDE)
    assert "Station Poison Price" in html
    block = html[html.find("Station Poison Price"):]
    block = block[:block.find("</article>")]
    assert "no stored price for this product is a usable number" in block
    assert "bundle or a multi-unit pack" not in block


def test_unranked_reasons_are_each_the_real_rule(built):
    _, site_dir, _ = built
    reasons = {}
    for slug in (RACK_GUIDE, STATION_GUIDE, PANEL_GUIDE):
        html = guide_html(site_dir, slug)
        for name, reason in re.findall(
                r'<h2><a href="[^"]*products/([^."]+)\.html">[^<]*</a></h2>\s*\n'
                r'\s*<p class="withheld">No [^:]+: ([^<]+)</p>', html):
            reasons[name] = reason
    assert "capacity is not established" in reasons["rack-unrated"]
    assert "bundle or a multi-unit pack" in reasons["panel-pallet"]
    assert "rated output is not established" in reasons["panel-no-output"]
    assert "currently sold out" in reasons["station-soldout"]
    assert ("no stored price for this product is a usable number"
            in reasons["station-poison"])


# ---------------------------------------------------------------------------
# MEDIUM-5: non-finite prices never render as money
# ---------------------------------------------------------------------------

def test_money_refuses_non_finite_and_non_numbers():
    assert money(1408.1) == "$1,408.10"
    assert money(float("inf")) == ""
    assert money(float("-inf")) == ""
    assert money(float("nan")) == ""
    assert money(True) == ""
    assert money("199.00") == ""
    assert money(None) == ""


def test_price_display_withholds_anything_not_usable():
    assert price_display(10.0) == "$10.00"
    assert price_display(0) == ""
    assert price_display(-5.0) == ""
    assert price_display(float("nan")) == ""
    assert price_display(float("inf")) == ""


def test_no_page_ever_renders_inf_or_nan(built):
    _, site_dir, _ = built
    for page in site_dir.rglob("*.html"):
        text = page.read_text(encoding="utf-8")
        for poison in ("$inf", "$nan", "$-inf", "inf/Wh", "nan/Wh"):
            assert poison not in text, f"{page.name}: {poison}"


def test_corrupt_price_is_withheld_with_its_own_marker(built):
    _, site_dir, _ = built
    page = (site_dir / "products" / "station-poison.html").read_text(
        encoding="utf-8")
    assert 'data-withheld="price_unreadable"' in page
    assert "stored price is not a usable number" in page
    assert 'data-withheld="price_unreadable"' in guide_html(
        site_dir, STATION_GUIDE)


def test_corrupt_price_never_wins_the_home_cheapest_cell(built):
    """min() over a list containing NaN returns whatever happens to come
    first, so an unfiltered NaN can silently become the cheapest."""
    _, site_dir, _ = built
    home = (site_dir / "index.html").read_text(encoding="utf-8")
    assert "$inf" not in home and "$nan" not in home


def test_scraper_rejects_non_finite_prices():
    """The other end of the guard: nothing non-finite enters the store."""
    from scrapers.shopify import ShopifyScraper
    scraper = ShopifyScraper("r1", "https://r1.example")
    product = {
        "title": "Poison", "handle": "poison",
        "variants": [
            {"id": 1, "title": "Inf", "price": "Infinity", "sku": "A"},
            {"id": 2, "title": "NaN", "price": "NaN", "sku": "B"},
            {"id": 3, "title": "Good", "price": "10.00", "sku": "C",
             "compare_at_price": "NaN"},
        ],
    }
    parsed = scraper._parse_product(product)
    prices = [v["price"] for v in parsed["variants"].values()]
    assert prices == [10.0], prices
    assert all(v["was_price"] is None for v in parsed["variants"].values())


# ---------------------------------------------------------------------------
# MEDIUM-7 / LOW-8: no unexplained cross-SKU juxtaposition
# ---------------------------------------------------------------------------

def test_quantity_conflict_annotated_in_product_table_not_only_spreads(built):
    """The merged per-product table is exactly where a reader compares two
    rows. station-c's SKU-C carries "2 Batteries Only" at one retailer and
    "3 Batteries Only" at the other."""
    _, site_dir, _ = built
    html = guide_html(site_dir, STATION_GUIDE)
    block = html[html.find("Station C"):]
    block = block[:block.find("</article>")]
    assert "Retailers disagree on the quantity behind SKU" in block
    assert "SKU-C" in block


def test_conflict_annotation_runs_on_guides_without_a_spreads_section(tmp_path):
    """The detector is a property of the data, not of the spreads flag.
    The panel guide renders no spreads block at all."""
    data_dir = seed_data(tmp_path)
    _write_jsonl(data_dir / "prices" / "panel-pallet.jsonl", [
        {"retailer_id": "r1", "retailer_name": "Retailer One",
         "timestamp": _ts(6), "url": "https://r1.example/products/pallet",
         "variants": {"a": _variant(2400.00, "8 Solar Panels", 81, "PAL-X")},
         "in_stock": True},
        {"retailer_id": "r2", "retailer_name": "Retailer Two",
         "timestamp": _ts(6), "url": "https://r2.example/products/pallet",
         "variants": {"a": _variant(2200.00, "12 Solar Panels", 83, "PAL-X")},
         "in_stock": True},
    ])
    site_dir = tmp_path / "site"
    build_site(data_dir=data_dir, site_dir=site_dir,
               templates_dir=TEMPLATES_DIR, now=NOW)
    html = guide_html(site_dir, PANEL_GUIDE)
    assert "Same SKU at more than one retailer" not in html  # no spreads here
    assert "Retailers disagree on the quantity behind SKU" in html
    assert "PAL-X" in html


def test_sku_carried_by_only_one_retailer_is_annotated(tmp_path):
    """E8's other half: a SKU nobody else carries has no like-for-like
    counterpart, so it must not sit unlabelled beside ones that do."""
    data_dir = seed_data(tmp_path)
    path = data_dir / "prices" / "station-a.jsonl"
    rows = [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows[1]["variants"]["extra"] = _variant(
        700.00, "Station A Regional Edition", 43, "SKU-ONLY-R2")
    _write_jsonl(path, rows)
    site_dir = tmp_path / "site"
    build_site(data_dir=data_dir, site_dir=site_dir,
               templates_dir=TEMPLATES_DIR, now=NOW)
    flat = " ".join(guide_html(site_dir, STATION_GUIDE).split())
    assert "SKU-ONLY-R2" in flat
    assert "carried only by Retailer Two among tracked retailers" in flat


def test_single_retailer_product_gets_no_only_here_noise(built):
    """With one retailer there is no agreement to measure, so every SKU
    would read as "only here" and the annotation would be pure noise."""
    _, site_dir, _ = built
    html = guide_html(site_dir, RACK_GUIDE)
    block = html[html.find("Rack Battery 5kWh"):]
    block = " ".join(block[:block.find("</article>")].split())
    assert "carried only by" not in block


def test_products_json_note_warning_reaches_the_page(tmp_path):
    """A DATA-QUALITY WARNING recorded against a retailer in the catalog
    was reaching nobody: it sat in JSON while the affected rows rendered
    unannotated."""
    data_dir = seed_data(tmp_path)
    products = json.loads(
        (data_dir / "products.json").read_text(encoding="utf-8"))
    for product in products:
        if product["id"] == "station-a":
            product["notes"] = (
                "DATA-QUALITY WARNING: r2's pack SKUs are shifted one step "
                "against its own labels; do not trust them as identity.")
            product["notes_by_retailer"] = {
                "r2": "pack SKUs are shifted one step against its own labels"
            }
    _write_json(data_dir / "products.json", products)
    site_dir = tmp_path / "site"
    build_site(data_dir=data_dir, site_dir=site_dir,
               templates_dir=TEMPLATES_DIR, now=NOW)
    html = guide_html(site_dir, STATION_GUIDE)
    flat = " ".join(html.split())
    assert "Data-quality note recorded against Retailer Two" in flat
    assert "pack SKUs are shifted one step" in flat
    # and NOT against the retailer the note does not name
    block = html[html.find("Station A"):]
    block = block[:block.find("</article>")]
    assert block.count("Data-quality note recorded against") == 1


def test_ordinary_notes_do_not_trigger_a_warning(built):
    _, site_dir, _ = built
    for slug in (RACK_GUIDE, STATION_GUIDE, PANEL_GUIDE):
        assert "Data-quality note recorded" not in guide_html(site_dir, slug)


# ---------------------------------------------------------------------------
# LOW-10: ties
# ---------------------------------------------------------------------------

def test_adjacent_equal_ratings_are_marked_as_a_tie(tmp_path):
    """Two products whose ratings round to the same displayed string sit
    at ranks N and N+1 with nothing explaining the ordering."""
    data_dir = seed_data(tmp_path)
    # 191.99/200 = 0.95995 and 239.99/250 = 0.95996 -> both "$0.96/W"
    products = json.loads(
        (data_dir / "products.json").read_text(encoding="utf-8"))
    products.append(_product("panel-twin", "Panel Beta 250W", "solar-panel",
                             output_w=250))
    _write_json(data_dir / "products.json", products)
    _write_jsonl(data_dir / "prices" / "panel-rated.jsonl", [{
        "retailer_id": "r1", "retailer_name": "Retailer One",
        "timestamp": _ts(6), "url": "https://r1.example/products/panel",
        "variants": {"default": _variant(191.99, "Default Title", 61, "PAN-1")},
        "in_stock": True}])
    _write_jsonl(data_dir / "prices" / "panel-twin.jsonl", [{
        "retailer_id": "r1", "retailer_name": "Retailer One",
        "timestamp": _ts(6), "url": "https://r1.example/products/twin",
        "variants": {"default": _variant(239.99, "Default Title", 62, "PAN-T")},
        "in_stock": True}])
    site_dir = tmp_path / "site"
    build_site(data_dir=data_dir, site_dir=site_dir,
               templates_dir=TEMPLATES_DIR, now=NOW)
    html = guide_html(site_dir, PANEL_GUIDE)
    assert html.count("$0.96/W") >= 2
    assert html.count('class="badge">tie</span>') == 2
    flat = " ".join(html.split())
    assert "rounds to the same displayed value as an adjacent rank" in flat


def test_distinct_ratings_are_not_marked_as_ties(built):
    _, site_dir, _ = built
    assert 'class="badge">tie</span>' not in guide_html(site_dir, RACK_GUIDE)


# ---------------------------------------------------------------------------
# BLOCKER-2 regression: fault attribution must be structured
# ---------------------------------------------------------------------------

def test_note_warning_attributes_only_the_retailer_at_fault(tmp_path):
    """The real catalog shape: ONE note that NAMES THREE retailers, of
    which only one is at fault — the other two are named because the note
    exonerates them.

    Substring attribution printed "Data-quality note recorded against Shop
    Solar Kits" on the shipped panel guide for a note whose own text says
    shop-solar-kits got it RIGHT. Publishing a fault accusation against a
    named business, sourced from text that refutes it, is the worst thing
    this content layer has done. Structured attribution makes the shape
    unrepresentable.
    """
    data_dir = seed_data(tmp_path)
    products = json.loads(
        (data_dir / "products.json").read_text(encoding="utf-8"))
    for product in products:
        if product["id"] == "station-a":
            # prose mentions BOTH retailers; only r2 is at fault
            product["notes"] = (
                "DATA-QUALITY WARNING: r2's pack SKUs are shifted one step "
                "against its own labels, while r1 puts the same SKU on the "
                "correct pack. Do not trust r2's codes as identity.")
            product["notes_by_retailer"] = {
                "r2": "This retailer's pack SKUs are shifted one step "
                      "against its own labels."
            }
    _write_json(data_dir / "products.json", products)
    site_dir = tmp_path / "site"
    build_site(data_dir=data_dir, site_dir=site_dir,
               templates_dir=TEMPLATES_DIR, now=NOW)
    flat = " ".join(guide_html(site_dir, STATION_GUIDE).split())

    assert "Data-quality note recorded against Retailer Two" in flat
    # r1 is named in the prose but is NOT at fault and must not be accused
    assert "Data-quality note recorded against Retailer One" not in flat
    assert flat.count("Data-quality note recorded against") == 1


def test_prose_notes_alone_never_attribute_fault(tmp_path):
    """A DATA-QUALITY WARNING with no structured map accuses nobody.
    Silence is the correct failure mode: a missed warning is a gap, a
    misattributed one is a false statement about a business."""
    data_dir = seed_data(tmp_path)
    products = json.loads(
        (data_dir / "products.json").read_text(encoding="utf-8"))
    for product in products:
        if product["id"] == "station-a":
            product["notes"] = (
                "DATA-QUALITY WARNING: r1 and r2 disagree about something.")
    _write_json(data_dir / "products.json", products)
    site_dir = tmp_path / "site"
    build_site(data_dir=data_dir, site_dir=site_dir,
               templates_dir=TEMPLATES_DIR, now=NOW)
    assert "Data-quality note recorded" not in guide_html(site_dir, STATION_GUIDE)


def test_real_catalog_note_accuses_only_wild_oak_trail():
    """The shipped page: E8's warning names three retailers and exonerates
    two of them."""
    from build import _note_flagged_retailers
    products = json.loads(
        (REPO_ROOT / "data" / "products.json").read_text(encoding="utf-8"))
    mega410 = next(p for p in products if p["id"] == "rich-solar-mega-410")
    flagged = _note_flagged_retailers(mega410)
    assert set(flagged) == {"wild-oak-trail"}
    assert "shop-solar-kits" not in flagged
    assert "rich-solar" not in flagged


def test_shipped_panel_guide_accuses_only_wild_oak_trail():
    """End to end against the real build output, if it exists."""
    guide = (REPO_ROOT / "site" / "guides"
             / f"{PANEL_GUIDE}.html")
    if not guide.exists():
        pytest.skip("site not built")
    flat = " ".join(guide.read_text(encoding="utf-8").split())
    assert "Data-quality note recorded against Shop Solar Kits" not in flat
    assert "Data-quality note recorded against Rich Solar" not in flat


def test_variant_in_both_ranked_and_spread_tables_keeps_its_rating(built):
    """BLOCKER-1, at the source. station-a's SKU-A appears in the ranked
    table (which has a $/Wh column) AND in the spreads table (which has
    none, by design). Keying provenance by variant_id alone let the
    ratingless spreads row overwrite the rated one, so audit.py read the
    rating as absent and raised RENDER_DEFECT against a correct page."""
    from audit import parse_guide_provenance, parse_provenance_list

    _, site_dir, _ = built
    guide = site_dir / "guides" / f"{STATION_GUIDE}.html"
    per_vid = {}
    for record in parse_provenance_list(guide.read_text(encoding="utf-8")):
        per_vid.setdefault(record["_vid"], []).append(record)

    twice = {vid: recs for vid, recs in per_vid.items() if len(recs) > 1}
    assert twice, "fixture renders no variant more than once on a guide"

    # SKU-A is in a spread, so both its variants render twice or more
    spread_vids = [vid for vid, recs in twice.items()
                   if any(r.get("sku") == "SKU-A" for r in recs)]
    assert spread_vids, "SKU-A not duplicated - the spread stopped rendering"

    merged = parse_guide_provenance(site_dir)
    for vid in spread_vids:
        records = twice[vid]
        assert any("wh" not in r["fields"] for r in records), vid
        assert any("wh" in r["fields"] for r in records), vid
        # the merged view keeps the rating and reports no contradiction
        assert merged[vid]["fields"]["wh"]["text"].endswith("/Wh"), vid
        assert not merged[vid]["internal_conflicts"], vid

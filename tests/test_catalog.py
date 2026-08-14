"""Schema validation for the data seeds (retailers, products, handle maps).

The consumers (runner.py, build.py) rely on these shapes; the optional
fields are contractual too (C6/C12): a consumer that KeyErrors on a
missing `shipping` or `affiliate: null` is the bug these tests catch.
"""

import json
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"


def _load(name):
    with open(DATA_DIR / name, encoding="utf-8") as f:
        return json.load(f)


# --- retailers.json ---


def test_retailers_is_a_list_of_dicts():
    retailers = _load("retailers.json")
    assert isinstance(retailers, list)
    assert retailers, "retailers.json must not be empty"
    assert all(isinstance(r, dict) for r in retailers)


def test_retailers_required_fields():
    for r in _load("retailers.json"):
        assert isinstance(r.get("id"), str) and r["id"]
        assert isinstance(r.get("name"), str) and r["name"]
        assert isinstance(r.get("url"), str) and r["url"].startswith("https://")
        assert r.get("scraper_type") in ("shopify", "custom")
        assert isinstance(r.get("active"), bool)


def test_retailers_optional_fields_have_valid_shapes():
    """affiliate may be null (C6); inactive entries carry inactive_reason;
    trust_builder / notes / shipping are optional."""
    for r in _load("retailers.json"):
        affiliate = r.get("affiliate")
        assert affiliate is None or isinstance(affiliate, dict)
        if isinstance(affiliate, dict):
            assert "commission" in affiliate
            assert "link_template" in affiliate
        if not r["active"]:
            assert isinstance(r.get("inactive_reason"), str) and r["inactive_reason"], (
                f"{r['id']}: inactive retailers must say why"
            )
        if "trust_builder" in r:
            assert isinstance(r["trust_builder"], bool)


def test_retailer_ids_unique():
    ids = [r["id"] for r in _load("retailers.json")]
    assert len(ids) == len(set(ids))


def test_active_retailers_are_the_planned_seeds():
    active = sorted(r["id"] for r in _load("retailers.json") if r["active"])
    assert active == ["shop-solar-kits", "wild-oak-trail"]


# --- products.json ---


def test_products_is_a_list():
    """C12: the catalog must be a LIST of {id, ...} dicts — runner.py
    iterates it directly."""
    products = _load("products.json")
    assert isinstance(products, list)
    assert products
    assert all(isinstance(p, dict) for p in products)


def test_products_required_fields_and_specs():
    for p in _load("products.json"):
        assert isinstance(p.get("id"), str) and p["id"]
        assert isinstance(p.get("name"), str) and p["name"]
        assert isinstance(p.get("brand"), str) and p["brand"]
        assert isinstance(p.get("category"), str) and p["category"]
        assert isinstance(p.get("active"), bool)
        specs = p.get("specs")
        assert isinstance(specs, dict)
        for key in ("capacity_wh", "output_w", "chemistry", "weight_lb",
                    "capacity_source"):
            assert key in specs, f"{p['id']}: specs.{key} missing"
        # capacity_wh is nullable (withhold-when-unknown, PLAN 2b) but
        # never a bogus zero/negative
        cap = specs["capacity_wh"]
        assert cap is None or (isinstance(cap, (int, float)) and cap > 0)
        # capacity is hand-authored, so its provenance is contractual
        # (PLAN 4c.2: the audit cross-checks it against live text)
        source = specs["capacity_source"]
        assert source is None or (isinstance(source, str) and source)
        if cap is not None:
            assert source, f"{p['id']}: non-null capacity needs capacity_source"


def test_product_ids_unique():
    ids = [p["id"] for p in _load("products.json")]
    assert len(ids) == len(set(ids))


# --- handle_maps.json ---


def test_handle_maps_shape_and_referential_integrity():
    """C12 shape: {retailer_id: {product_id: handle}}. Every key must
    resolve against the seeds, or the runner silently scrapes nothing."""
    maps = _load("handle_maps.json")
    retailer_ids = {r["id"] for r in _load("retailers.json")}
    product_ids = {p["id"] for p in _load("products.json")}

    assert isinstance(maps, dict)
    for rid, mapping in maps.items():
        assert rid in retailer_ids, f"unknown retailer in handle_maps: {rid}"
        assert isinstance(mapping, dict)
        for pid, handle in mapping.items():
            assert pid in product_ids, f"unknown product in handle_maps: {pid}"
            assert isinstance(handle, str) and handle


def test_every_active_product_is_mapped_somewhere():
    """An active product with no handle anywhere can never get a price."""
    maps = _load("handle_maps.json")
    mapped = {pid for mapping in maps.values() for pid in mapping}
    for p in _load("products.json"):
        if p["active"]:
            assert p["id"] in mapped, f"active product {p['id']} has no handle mapping"

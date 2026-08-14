"""Tests for the end-to-end audit (audit.py) — verdict taxonomy (PLAN 4c).

Live fetches are `responses` mocks; site HTML is built by the real
build_site against the pinned clock from test_build. The injection tests
mirror acceptance 3a/3b: 3a tampers the RENDERED page (RENDER_DEFECT,
exit 3, quarantine), 3b tampers the STORED row and rebuilds (STALE, no
quarantine) — a run that quarantines 3b has the taxonomy bug.
"""

import json

import responses

import build as build_mod
from audit import (
    CLEAN,
    NO_BASELINE,
    NO_ROW,
    NOT_AUDITED,
    RENDER_DEFECT,
    STALE,
    UNRESOLVED,
    check_capacity_quote,
    display_price_to_cents,
    main,
    parse_provenance,
    parse_provenance_full,
    run_audit,
)
from build import build_site
from tests.test_build import (
    NOW,
    TEMPLATES_DIR,
    _seed_509_scenario,
    _ts,
    _write_json,
)

RETAILER_URL = "https://www.wildoaktrail.com"
HANDLE = "ecoflow-river-2-pro-portable-power-station"
BUNDLE_VID = 44532078936300
UNIT_VID = 44532078903532
QKEY = f"wild-oak-trail:ecoflow-river-2-pro:{BUNDLE_VID}"


def _seed_audit(tmp_path):
    data_dir = _seed_509_scenario(tmp_path)
    _write_json(data_dir / "handle_maps.json",
                {"wild-oak-trail": {"ecoflow-river-2-pro": HANDLE}})
    return data_dir


def _rebuild(tmp_path, data_dir):
    site_dir = tmp_path / "site"
    build_site(data_dir=data_dir, site_dir=site_dir,
               templates_dir=TEMPLATES_DIR, now=NOW)
    return site_dir


def _add_live(bundle_price="509.00", unit_price="569.00",
              bundle_compare="998.00", unit_compare="599.00",
              bundle_sku="RIVER2PRO-110-1-US", unit_sku="ZMR620-B-US-1",
              include_bundle=True, title_wh="768Wh", js_availability=True):
    variants = []
    if include_bundle:
        variants.append({"id": BUNDLE_VID,
                         "title": "EcoFlow RIVER 2 Pro + 1 110W Portable Solar Panel",
                         "price": bundle_price, "compare_at_price": bundle_compare,
                         "sku": bundle_sku})
    variants.append({"id": UNIT_VID,
                     "title": "EcoFlow RIVER 2 Pro Portable Power Station(Main Unit Only)",
                     "price": unit_price, "compare_at_price": unit_compare,
                     "sku": unit_sku})
    responses.add(
        responses.GET, f"{RETAILER_URL}/products/{HANDLE}.json",
        json={"product": {"id": 1,
                          "title": f"EcoFlow RIVER 2 Pro Portable Power Station {title_wh}",
                          "body_html": "<p>Portable power station.</p>",
                          "variants": variants}},
        status=200,
    )
    responses.add(
        responses.GET, f"{RETAILER_URL}/products/{HANDLE}.js",
        json={"variants": [{"id": v["id"], "available": True} for v in variants]}
        if js_availability else {"variants": []},
        status=200,
    )


def _run(tmp_path, data_dir, site_dir, **kw):
    return run_audit(
        data_dir=data_dir, site_dir=site_dir,
        report_out=tmp_path / "report.json",
        quarantine_out=tmp_path / "quarantine_out.json",
        audit_all=True, now=NOW, **kw,
    )


def _quarantine_out(tmp_path):
    return json.loads((tmp_path / "quarantine_out.json").read_text(encoding="utf-8"))


# --- parser + money helpers ---


def test_parse_provenance_reads_attrs_and_unescapes():
    html = (
        '<table><tr data-variant-id="42" data-tier="a-b" data-sku="S&amp;1" '
        'data-scraped-at="2026-08-14T00:00:00+00:00">'
        '<td><span data-field="price">$1,408.12</span>'
        '<span data-field="availability" data-value="false">Sold out</span></td>'
        '</tr></table>'
    )
    records = parse_provenance(html)
    assert records["42"]["tier"] == "a-b"
    assert records["42"]["sku"] == "S&1"  # html.unescape via convert_charrefs
    assert records["42"]["fields"]["price"]["text"] == "$1,408.12"
    assert records["42"]["fields"]["availability"]["value"] == "false"
    assert records["42"]["withheld"] is None


def test_display_price_to_cents():
    assert display_price_to_cents("$1,408.12") == 140812
    assert display_price_to_cents("$569.00") == 56900
    assert display_price_to_cents("") is None
    assert display_price_to_cents("price withheld (stale)") is None


# --- clean run ---


@responses.activate
def test_all_clean_exits_zero(tmp_path, no_sleep):
    data_dir = _seed_audit(tmp_path)
    site_dir = _rebuild(tmp_path, data_dir)
    _add_live()
    report, exit_code = _run(tmp_path, data_dir, site_dir)

    assert exit_code == 0
    assert report["verified"] == 2 and report["attempted"] == 2
    assert report["verdict_counts"] == {CLEAN: 2}
    assert report["live_requests_used"] == 2
    assert _quarantine_out(tmp_path) == {}
    # report file is LF-only
    raw = (tmp_path / "report.json").read_bytes()
    assert raw.count(b"\r") == 0


# --- acceptance 3a: render-hop injection ---


@responses.activate
def test_3a_tampered_display_price_is_render_defect_exit_3(tmp_path, no_sleep):
    data_dir = _seed_audit(tmp_path)
    site_dir = _rebuild(tmp_path, data_dir)
    # Tamper the DISPLAYED price on the product page only
    page = site_dir / "products" / "ecoflow-river-2-pro.html"
    page.write_text(
        page.read_text(encoding="utf-8").replace("$509.00", "$555.00"),
        encoding="utf-8", newline="\n",
    )
    _add_live()
    report, exit_code = _run(tmp_path, data_dir, site_dir)

    assert exit_code == 3
    assert report["verdict_counts"][RENDER_DEFECT] == 1
    assert report["verdict_counts"][CLEAN] == 1
    defect = next(e for e in report["results"] if e["verdict"] == RENDER_DEFECT)
    assert defect["variant_id"] == str(BUNDLE_VID)
    # quarantines exactly that variant
    quarantine = _quarantine_out(tmp_path)
    assert list(quarantine) == [QKEY]
    assert quarantine[QKEY]["reason"] == "render_defect"
    assert quarantine[QKEY]["consecutive_failures"] == 1


# --- acceptance 3b: freshness-hop injection ---


@responses.activate
def test_3b_tampered_row_rebuilt_is_stale_not_quarantined(tmp_path, no_sleep):
    data_dir = _seed_audit(tmp_path)
    # Tamper the STORED row, then rebuild so site and store AGREE
    price_file = data_dir / "prices" / "ecoflow-river-2-pro.jsonl"
    row = json.loads(price_file.read_text(encoding="utf-8").splitlines()[0])
    row["variants"]["ecoflow-river-2-pro-1-110w-portable-solar-panel"]["price"] = 499.00
    price_file.write_text(json.dumps(row) + "\n", encoding="utf-8", newline="\n")
    site_dir = _rebuild(tmp_path, data_dir)
    _add_live()  # live still says 509.00
    report, exit_code = _run(tmp_path, data_dir, site_dir)

    # STALE is not a defect: no quarantine, no exit 3.
    assert exit_code == 0
    assert report["verdict_counts"][STALE] == 1
    assert report["verdict_counts"][CLEAN] == 1
    assert _quarantine_out(tmp_path) == {}
    stale = next(e for e in report["results"] if e["verdict"] == STALE)
    assert stale["freshness_diffs"][0]["field"] == "price"
    assert stale["freshness_diffs"][0] == {
        "field": "price", "stored": 49900, "live": 50900}


# --- sku drift ---


@responses.activate
def test_sku_drift_raises_alarm_without_quarantine(tmp_path, no_sleep):
    data_dir = _seed_audit(tmp_path)
    price_file = data_dir / "prices" / "ecoflow-river-2-pro.jsonl"
    row = json.loads(price_file.read_text(encoding="utf-8").splitlines()[0])
    row["variants"]["ecoflow-river-2-pro-1-110w-portable-solar-panel"]["sku"] = "WRONG-SKU"
    price_file.write_text(json.dumps(row) + "\n", encoding="utf-8", newline="\n")
    site_dir = _rebuild(tmp_path, data_dir)
    _add_live()
    report, exit_code = _run(tmp_path, data_dir, site_dir)

    assert any("SKU DRIFT" in a for a in report["alarms"])
    drifted = next(e for e in report["results"] if e["variant_id"] == str(BUNDLE_VID))
    assert drifted["sku_drift"] is True
    assert _quarantine_out(tmp_path) == {}
    assert exit_code == 0  # drift is an alarm, not a taxonomy defect


# --- non-verdicts ---


@responses.activate
def test_no_row_pairs_need_no_live_requests(tmp_path, no_sleep):
    data_dir = _seed_audit(tmp_path)
    # Map a second product that has no scraped row
    maps = json.loads((data_dir / "handle_maps.json").read_text(encoding="utf-8"))
    maps["wild-oak-trail"]["never-scraped"] = "never-scraped-handle"
    _write_json(data_dir / "handle_maps.json", maps)
    products = json.loads((data_dir / "products.json").read_text(encoding="utf-8"))
    products.append({**products[0], "id": "never-scraped", "name": "Never Scraped"})
    _write_json(data_dir / "products.json", products)

    site_dir = _rebuild(tmp_path, data_dir)
    _add_live()
    report, exit_code = _run(tmp_path, data_dir, site_dir)

    assert report["verdict_counts"][NO_ROW] == 1
    assert report["live_requests_used"] == 2  # no fetch for the NO_ROW pair
    assert exit_code == 0  # a coverage gap is not a mismatch


@responses.activate
def test_missing_stored_sku_is_no_baseline(tmp_path, no_sleep):
    data_dir = _seed_audit(tmp_path)
    price_file = data_dir / "prices" / "ecoflow-river-2-pro.jsonl"
    row = json.loads(price_file.read_text(encoding="utf-8").splitlines()[0])
    del row["variants"]["ecoflow-river-2-pro-1-110w-portable-solar-panel"]["sku"]
    price_file.write_text(json.dumps(row) + "\n", encoding="utf-8", newline="\n")
    site_dir = _rebuild(tmp_path, data_dir)
    _add_live()
    report, exit_code = _run(tmp_path, data_dir, site_dir)

    assert report["verdict_counts"][NO_BASELINE] == 1
    assert report["verdict_counts"][CLEAN] == 1
    assert exit_code == 0  # hops verified; only drift was not evaluable


@responses.activate
def test_variant_absent_from_live_is_unresolved_exit_4(tmp_path, no_sleep):
    """Absence can mean hidden, not gone (UCP filters.available defaults
    true, C21) — never classify absence as drift or defect."""
    data_dir = _seed_audit(tmp_path)
    site_dir = _rebuild(tmp_path, data_dir)
    _add_live(include_bundle=False)
    report, exit_code = _run(tmp_path, data_dir, site_dir)

    assert report["verdict_counts"][UNRESOLVED] == 1
    assert exit_code == 4  # incomplete must never read as success
    assert _quarantine_out(tmp_path) == {}


@responses.activate
def test_budget_exhaustion_marks_not_audited_exit_4(tmp_path, no_sleep):
    data_dir = _seed_audit(tmp_path)
    site_dir = _rebuild(tmp_path, data_dir)
    report, exit_code = _run(tmp_path, data_dir, site_dir, budget=0)

    assert report["verdict_counts"][NOT_AUDITED] == 2
    assert report["live_requests_used"] == 0
    assert exit_code == 4


# --- quarantine lifecycle ---


@responses.activate
def test_quarantine_recheck_clean_clears_entry(tmp_path, no_sleep):
    data_dir = _seed_audit(tmp_path)
    entry = {QKEY: {"sku": "RIVER2PRO-110-1-US",
                    "tier_last_seen": "ecoflow-river-2-pro-1-110w-portable-solar-panel",
                    "reason": "render_defect", "observed": "$555.00",
                    "expected": "$509.00", "first_seen": _ts(24),
                    "last_seen": _ts(1), "consecutive_failures": 1,
                    "unobserved_audits": 0}}
    _write_json(data_dir / "quarantine.json", entry)
    site_dir = _rebuild(tmp_path, data_dir)  # renders the withheld marker
    _add_live()
    report, exit_code = _run(tmp_path, data_dir, site_dir)

    assert exit_code == 0
    assert _quarantine_out(tmp_path) == {}, "clean recheck must clear the entry"
    assert any("quarantine cleared" in n for n in report["notices"])


@responses.activate
def test_quarantine_ttl_expires_unobservable_entry(tmp_path, no_sleep):
    data_dir = _seed_audit(tmp_path)
    ghost_key = "wild-oak-trail:ecoflow-river-2-pro:99999"
    _write_json(data_dir / "quarantine.json", {ghost_key: {
        "sku": None, "tier_last_seen": "gone", "reason": "render_defect",
        "observed": "x", "expected": "y", "first_seen": _ts(120),
        "last_seen": _ts(24), "consecutive_failures": 1,
        "unobserved_audits": 4}})
    site_dir = _rebuild(tmp_path, data_dir)
    _add_live()
    report, _ = _run(tmp_path, data_dir, site_dir)

    assert ghost_key not in _quarantine_out(tmp_path)
    assert any("TTL-expired" in n for n in report["notices"])


# --- capacity cross-check ---


@responses.activate
def test_capacity_contradicted_raises_alarm(tmp_path, no_sleep):
    data_dir = _seed_audit(tmp_path)
    site_dir = _rebuild(tmp_path, data_dir)
    _add_live(title_wh="999Wh")  # product claims 768 Wh
    report, _ = _run(tmp_path, data_dir, site_dir)
    assert any("CAPACITY CONTRADICTED" in a for a in report["alarms"])


@responses.activate
def test_capacity_confirmed_and_kwh_form(tmp_path, no_sleep):
    data_dir = _seed_audit(tmp_path)
    products = json.loads((data_dir / "products.json").read_text(encoding="utf-8"))
    products[0]["specs"]["capacity_wh"] = 5120
    _write_json(data_dir / "products.json", products)
    site_dir = _rebuild(tmp_path, data_dir)
    _add_live(title_wh="5.12kWh")
    report, _ = _run(tmp_path, data_dir, site_dir)
    assert not any("CAPACITY" in a for a in report["alarms"])


# --- red team #5: was-price normalization + quote provenance ---


def _drop_stored_was_prices(data_dir):
    """Mirror shopify.py: a compare_at that is not a real discount is
    stored as `was_price: None`, not as the raw compare_at."""
    path = data_dir / "prices" / "ecoflow-river-2-pro.jsonl"
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        for v in row["variants"].values():
            v["was_price"] = None
        lines.append(json.dumps(row))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


@responses.activate
def test_compare_at_below_price_is_clean_not_stale(tmp_path, no_sleep):
    """A compare_at that is NOT a discount must not read as a price move.

    shopify.py stores `was_price: None` when compare_at <= price ("not
    actually a discount"). The freshness hop compared that None against
    the RAW live compare_at and reported a move that never happened. Real
    triple: EcoFlow DELTA Pro 3 @ shop-solar-kits, price $2,799.00,
    compare_at $2,644.09 — a permanent STALE no re-scrape could clear.
    """
    data_dir = _seed_audit(tmp_path)
    _drop_stored_was_prices(data_dir)   # what shopify.py actually writes
    site_dir = _rebuild(tmp_path, data_dir)
    _add_live(unit_compare="500.00", bundle_compare="400.00")  # both < price
    report, code = _run(tmp_path, data_dir, site_dir)
    assert all(r["verdict"] == CLEAN for r in report["results"]), report["results"]
    assert code == 0


@responses.activate
def test_compare_at_equal_to_price_is_clean_not_stale(tmp_path, no_sleep):
    """Real triple: Rich Solar MEGA 410 @ rich-solar, compare_at == price."""
    data_dir = _seed_audit(tmp_path)
    _drop_stored_was_prices(data_dir)
    site_dir = _rebuild(tmp_path, data_dir)
    _add_live(unit_compare="569.00", bundle_compare="509.00")  # == price
    report, code = _run(tmp_path, data_dir, site_dir)
    assert all(r["verdict"] == CLEAN for r in report["results"]), report["results"]
    assert code == 0


@responses.activate
def test_real_discount_still_reports_stale(tmp_path, no_sleep):
    """The normalization must not blind the hop to a genuine was-price
    move: a compare_at ABOVE price is a real discount and still compares."""
    data_dir = _seed_audit(tmp_path)
    site_dir = _rebuild(tmp_path, data_dir)
    _add_live(unit_compare="1299.00")  # stored row holds 599.00
    report, _ = _run(tmp_path, data_dir, site_dir)
    stale = [r for r in report["results"] if r["verdict"] == STALE]
    assert stale, report["results"]
    assert any(d["field"] == "was_price" for d in stale[0]["freshness_diffs"])


def test_check_capacity_quote_detects_a_fabricated_quote():
    """A right number with an invented quotation is still a provenance
    failure — and it is what shipped: enphase-iq-battery-5p cited
    listing-body '5000Wh' when both merchants wrote "total usable energy
    capacity of 5.0 kWh". Every numeric check passed it."""
    product = {"specs": {"capacity_wh": 5000,
                         "capacity_quotes": {"alte-store": "5000Wh"}}}
    body = "<p>It has a total usable energy capacity of 5.0 kWh and more.</p>"
    assert check_capacity_quote(product, "alte-store", "IQ Battery 5P",
                                body) == "QUOTE_NOT_FOUND"


def test_check_capacity_quote_accepts_a_verbatim_quote():
    product = {"specs": {"capacity_wh": 5000, "capacity_quotes": {
        "alte-store": "total usable energy capacity of 5.0 kWh"}}}
    body = "<p>It has a  total usable energy\n capacity of 5.0 kWh and more.</p>"
    # tags stripped and whitespace collapsed on BOTH sides before comparing
    assert check_capacity_quote(product, "alte-store", "IQ Battery 5P",
                                body) == "FOUND"


def test_check_capacity_quote_is_scoped_to_the_quoting_retailer():
    """A quote recorded for one retailer must not be asserted against a
    different retailer's listing — merchants word things differently."""
    product = {"specs": {"capacity_wh": 5000,
                         "capacity_quotes": {"alte-store": "5.0 kWh"}}}
    assert check_capacity_quote(product, "shop-solar-kits", "t", "b") is None


@responses.activate
def test_fabricated_quote_surfaces_as_a_notice_not_an_alarm(tmp_path, no_sleep):
    data_dir = _seed_audit(tmp_path)
    products = json.loads((data_dir / "products.json").read_text(encoding="utf-8"))
    products[0]["specs"]["capacity_quotes"] = {"wild-oak-trail": "768Wh of pure fiction"}
    _write_json(data_dir / "products.json", products)
    site_dir = _rebuild(tmp_path, data_dir)
    _add_live()
    report, code = _run(tmp_path, data_dir, site_dir)
    assert any("QUOTE_NOT_FOUND" in n for n in report["notices"]), report["notices"]
    assert not report["alarms"]      # a reworded listing must not fail a run
    assert code == 0


# --- usage/config errors exit 1 ---


def test_missing_data_dir_exits_1(tmp_path):
    code = main(["--data-dir", str(tmp_path / "nope"),
                 "--site-dir", str(tmp_path / "nope-site"),
                 "--report-out", str(tmp_path / "r.json"),
                 "--quarantine-out", str(tmp_path / "q.json")])
    assert code == 1


# ===========================================================================
# Red team #4 regressions
# ===========================================================================

def _seed_quarantined(tmp_path, entry_overrides=None):
    """Seed + write a quarantine entry for the $509 bundle (the cheapest)."""
    data_dir = _seed_audit(tmp_path)
    entry = {
        "sku": "RIVER2PRO-110-1-US",
        "tier_last_seen": "ecoflow-river-2-pro-1-110w-portable-solar-panel",
        "reason": "render_defect", "observed": "$555.00",
        "expected": "$509.00", "first_seen": _ts(24), "last_seen": _ts(1),
        "consecutive_failures": 1, "unobserved_audits": 0,
    }
    entry.update(entry_overrides or {})
    _write_json(data_dir / "quarantine.json", {QKEY: entry})
    return data_dir


# --- CRITICAL-1: leaks on EITHER surface are RENDER_DEFECT, entry retained ---


@responses.activate
def test_c1_price_leak_on_both_surfaces_is_render_defect(tmp_path, no_sleep):
    """Site built WITHOUT honoring the quarantine (price everywhere, no
    markers): the old recheck cleared the entry; now it is a leak."""
    data_dir = _seed_audit(tmp_path)
    site_dir = _rebuild(tmp_path, data_dir)  # built BEFORE quarantine exists
    _write_json(data_dir / "quarantine.json", {
        QKEY: {"sku": "RIVER2PRO-110-1-US", "tier_last_seen": "t",
               "reason": "render_defect", "observed": "$555.00",
               "expected": "$509.00", "first_seen": _ts(24),
               "last_seen": _ts(1), "consecutive_failures": 1,
               "unobserved_audits": 0}})
    _add_live()
    report, exit_code = _run(tmp_path, data_dir, site_dir)

    assert exit_code == 3
    defect = next(e for e in report["results"] if e["verdict"] == RENDER_DEFECT)
    assert defect["variant_id"] == str(BUNDLE_VID)
    assert "without the quarantine marker" in defect["detail"]
    q = _quarantine_out(tmp_path)
    assert QKEY in q, "leak must RETAIN the entry, not clear it"
    assert q[QKEY]["consecutive_failures"] == 2
    assert q[QKEY]["first_seen"] == _ts(24), "entry recreated instead of updated"


@responses.activate
def test_c1_home_only_leak_is_render_defect(tmp_path, no_sleep):
    """Product page withholds correctly; the HOME cell leaks the price.
    The old recheck never looked at home_prov at all."""
    data_dir = _seed_quarantined(tmp_path)
    site_dir = _rebuild(tmp_path, data_dir)  # markers on both surfaces
    index = site_dir / "index.html"
    html = index.read_text(encoding="utf-8")
    assert "price withheld (under verification)" in html
    html = html.replace(
        '<span class="muted">price withheld (under verification)</span>',
        '<span data-field="price">$509.00</span>', 1)
    index.write_text(html, encoding="utf-8", newline="\n")
    _add_live()
    report, exit_code = _run(tmp_path, data_dir, site_dir)

    assert exit_code == 3
    defect = next(e for e in report["results"] if e["verdict"] == RENDER_DEFECT)
    assert "home cell" in defect["detail"]
    q = _quarantine_out(tmp_path)
    assert QKEY in q and q[QKEY]["consecutive_failures"] == 2


# --- CRITICAL-2: absence is never a clean recheck ---


@responses.activate
def test_c2_marker_absence_is_unresolved_and_counts_toward_ttl(tmp_path, no_sleep):
    data_dir = _seed_quarantined(tmp_path)
    site_dir = _rebuild(tmp_path, data_dir)
    page = site_dir / "products" / "ecoflow-river-2-pro.html"
    html = page.read_text(encoding="utf-8")
    assert ' data-withheld="quarantine"' in html
    # Strip the marker attribute: the row is now neither withheld nor
    # priced -- POSITIVE evidence is gone, which must never read clean.
    page.write_text(html.replace(' data-withheld="quarantine"', "", 1),
                    encoding="utf-8", newline="\n")
    _add_live()
    report, exit_code = _run(tmp_path, data_dir, site_dir)

    assert exit_code == 4
    entry = next(e for e in report["results"] if e["variant_id"] == str(BUNDLE_VID))
    assert entry["verdict"] == UNRESOLVED
    assert "positive marker" in entry["detail"]
    q = _quarantine_out(tmp_path)
    assert QKEY in q, "absence must RETAIN the entry"
    assert q[QKEY]["unobserved_audits"] == 1


@responses.activate
def test_c2_stale_recheck_increments_ttl_counter(tmp_path, no_sleep):
    """Any recheck that is not CLEAN feeds the TTL -- red team proved
    STALE rechecks previously never incremented it."""
    data_dir = _seed_quarantined(tmp_path)
    site_dir = _rebuild(tmp_path, data_dir)
    _add_live(bundle_price="519.00")  # live moved; markers + shadow are fine
    report, exit_code = _run(tmp_path, data_dir, site_dir)

    entry = next(e for e in report["results"] if e["variant_id"] == str(BUNDLE_VID))
    assert entry["verdict"] == STALE
    q = _quarantine_out(tmp_path)
    assert QKEY in q
    assert q[QKEY]["unobserved_audits"] == 1
    assert exit_code == 0  # STALE is verified, still not a defect


# --- MAJOR-3: oscillation -- shadow recheck keeps a persistent defect down ---


@responses.activate
def test_m3_persistent_build_defect_never_republishes_wrong_price(
        tmp_path, no_sleep, monkeypatch):
    """Red team's oscillation table, 6 cycles. A persistent build defect
    (formatter renders 509.00 as $555.00 every build) previously
    alternated wrong-price/withheld forever with consecutive_failures
    stuck at 1. With the shadow recheck: the wrong price appears only in
    cycle 0, the entry never clears, consecutive_failures climbs."""
    data_dir = _seed_audit(tmp_path)
    real_money = build_mod.money
    monkeypatch.setattr(
        build_mod, "money",
        lambda v: "$555.00" if v == 509.00 else real_money(v))
    _add_live()

    quarantine_path = data_dir / "quarantine.json"
    first_seen = None
    failures = []
    for cycle in range(6):
        site_dir = tmp_path / f"site-cycle{cycle}"
        build_site(data_dir=data_dir, site_dir=site_dir,
                   templates_dir=TEMPLATES_DIR, now=NOW)
        published = (
            (site_dir / "index.html").read_text(encoding="utf-8")
            + (site_dir / "products" / "ecoflow-river-2-pro.html").read_text(
                encoding="utf-8"))
        if cycle >= 1:
            assert "$555.00" not in published, (
                f"cycle {cycle}: the wrong price was republished")
        report, exit_code = run_audit(
            data_dir=data_dir, site_dir=site_dir,
            report_out=tmp_path / f"report-cycle{cycle}.json",
            quarantine_out=quarantine_path,
            audit_all=True, now=NOW)
        assert exit_code == 3, f"cycle {cycle}: defect must stay visible"
        q = json.loads(quarantine_path.read_text(encoding="utf-8"))
        assert QKEY in q, f"cycle {cycle}: entry cleared while defect persists"
        failures.append(q[QKEY]["consecutive_failures"])
        if first_seen is None:
            first_seen = q[QKEY]["first_seen"]
        assert q[QKEY]["first_seen"] == first_seen, "entry was delete+recreated"

    assert failures == sorted(failures), "failure count must be monotonic"
    assert failures[-1] >= 3, f"consecutive_failures stuck: {failures}"


# --- MAJOR-4: zero attempted is not success ---


@responses.activate
def test_m4_nothing_audited_exits_4(tmp_path, no_sleep, capsys):
    data_dir = _seed_audit(tmp_path)
    site_dir = _rebuild(tmp_path, data_dir)
    _write_json(data_dir / "handle_maps.json", {})  # empty maps
    report, exit_code = _run(tmp_path, data_dir, site_dir)

    assert report["attempted"] == 0 and report["verified"] == 0
    assert exit_code == 4
    assert "nothing audited" in capsys.readouterr().out


# --- MAJOR-6: no .js availability answer = not verified ---


@responses.activate
def test_m6_missing_js_availability_is_unresolved(tmp_path, no_sleep):
    data_dir = _seed_audit(tmp_path)
    site_dir = _rebuild(tmp_path, data_dir)
    _add_live(js_availability=False)
    report, exit_code = _run(tmp_path, data_dir, site_dir)

    assert exit_code == 4
    assert report["verdict_counts"][UNRESOLVED] == 2
    for e in report["results"]:
        if e["verdict"] == UNRESOLVED:
            assert ".js gave no answer" in e["detail"]
    assert any("no .js availability" in err for err in report["errors"])


# --- MAJOR-7: empty variant_id is non-joinable everywhere ---


@responses.activate
def test_m7_two_variants_with_empty_ids_are_unresolved_never_quarantined(
        tmp_path, no_sleep):
    data_dir = _seed_audit(tmp_path)
    price_file = data_dir / "prices" / "ecoflow-river-2-pro.jsonl"
    row = json.loads(price_file.read_text(encoding="utf-8").splitlines()[0])
    row["variants"] = {
        "station-a": {"price": 100.00, "was_price": None, "available": True,
                      "raw_variant": "Station A [Main Unit Only]",
                      "variant_id": "", "sku": "SKU-A"},
        "station-b": {"price": 200.00, "was_price": None, "available": True,
                      "raw_variant": "Station B [Main Unit Only]",
                      "sku": "SKU-B"},  # variant_id key entirely absent
    }
    price_file.write_text(json.dumps(row) + "\n", encoding="utf-8", newline="\n")
    site_dir = _rebuild(tmp_path, data_dir)

    # The FIXED build stamps NO data-variant-id at all for id-less
    # variants — the attribute is absent, not empty.
    page_html = (site_dir / "products" / "ecoflow-river-2-pro.html").read_text(
        encoding="utf-8")
    assert 'data-variant-id=""' not in page_html
    records, unidentified = parse_provenance_full(page_html)
    assert "" not in records and "None" not in records

    # Defense in depth: a LEGACY page that did stamp an empty id is
    # segregated by the parser, never keyed into records.
    legacy_records, legacy_unidentified = parse_provenance_full(
        '<tr data-variant-id="" data-tier="x" data-scraped-at="t">'
        '<td><span data-field="price">$100.00</span></td></tr>'
        '<tr data-variant-id="" data-tier="y" data-scraped-at="t">'
        '<td><span data-field="price">$200.00</span></td></tr>'
    )
    assert legacy_records == {}
    assert len(legacy_unidentified) == 2
    assert legacy_unidentified[0]["fields"]["price"]["text"] == "$100.00"
    assert legacy_unidentified[1]["fields"]["price"]["text"] == "$200.00"

    report, exit_code = _run(tmp_path, data_dir, site_dir)
    assert report["verdict_counts"][UNRESOLVED] == 2
    for e in report["results"]:
        assert e["verdict"] == UNRESOLVED
        assert "not joinable" in e["detail"]
    assert report["live_requests_used"] == 0, "no live spend on unjoinable triples"
    assert _quarantine_out(tmp_path) == {}
    assert exit_code == 4


# --- MAJOR-8: home-cell $/Wh compared ---


@responses.activate
def test_m8_tampered_home_dollars_per_wh_is_render_defect(tmp_path, no_sleep):
    data_dir = _seed_audit(tmp_path)
    # unit-only row so the home cell carries a $/Wh
    price_file = data_dir / "prices" / "ecoflow-river-2-pro.jsonl"
    row = json.loads(price_file.read_text(encoding="utf-8").splitlines()[0])
    row["variants"] = {
        "ecoflow-river-2-pro-portable-power-station-main-unit-only":
            row["variants"]["ecoflow-river-2-pro-portable-power-station-main-unit-only"]}
    price_file.write_text(json.dumps(row) + "\n", encoding="utf-8", newline="\n")
    site_dir = _rebuild(tmp_path, data_dir)

    index = site_dir / "index.html"
    html = index.read_text(encoding="utf-8")
    assert "$0.74/Wh" in html
    index.write_text(html.replace("$0.74/Wh", "$9.99/Wh"),
                     encoding="utf-8", newline="\n")
    _add_live()
    report, exit_code = _run(tmp_path, data_dir, site_dir)

    assert exit_code == 3
    defect = next(e for e in report["results"] if e["verdict"] == RENDER_DEFECT)
    assert any(m["where"] == "home" and m["field"] == "wh"
               for m in defect["mismatches"])


# --- MINOR-10: provenance attributes must not lie ---


@responses.activate
def test_minor10_tampered_data_sku_attr_is_render_defect(tmp_path, no_sleep):
    data_dir = _seed_audit(tmp_path)
    site_dir = _rebuild(tmp_path, data_dir)
    page = site_dir / "products" / "ecoflow-river-2-pro.html"
    html = page.read_text(encoding="utf-8")
    assert 'data-sku="RIVER2PRO-110-1-US"' in html
    page.write_text(html.replace('data-sku="RIVER2PRO-110-1-US"',
                                 'data-sku="LIAR-SKU"'),
                    encoding="utf-8", newline="\n")
    _add_live()
    report, exit_code = _run(tmp_path, data_dir, site_dir)

    assert exit_code == 3
    defect = next(e for e in report["results"] if e["verdict"] == RENDER_DEFECT)
    assert any(m["field"] == "sku-attr" for m in defect["mismatches"])


def test_minor10_asof_is_locatable_by_data_field(tmp_path):
    data_dir = _seed_audit(tmp_path)
    site_dir = _rebuild(tmp_path, data_dir)
    page_html = (site_dir / "products" / "ecoflow-river-2-pro.html").read_text(
        encoding="utf-8")
    records = parse_provenance(page_html)
    assert records[str(BUNDLE_VID)]["fields"]["asof"]["text"].startswith("as of")


# --- MINOR-13: malformed quarantine exits 1 before any live spend ---


@responses.activate
def test_minor13_malformed_quarantine_exits_1_without_live_requests(tmp_path):
    data_dir = _seed_audit(tmp_path)
    site_dir = _rebuild(tmp_path, data_dir)
    args = ["--all", "--data-dir", str(data_dir), "--site-dir", str(site_dir),
            "--report-out", str(tmp_path / "r.json"),
            "--quarantine-out", str(tmp_path / "q.json")]

    (data_dir / "quarantine.json").write_text("[]\n", encoding="utf-8")
    assert main(args) == 1

    _write_json(data_dir / "quarantine.json", {"badkey-no-colons": {}})
    assert main(args) == 1

    _write_json(data_dir / "quarantine.json",
                {"wild-oak-trail:ecoflow-river-2-pro:": {}})  # empty vid
    assert main(args) == 1

    # No responses were registered: zero recorded calls proves zero spend.
    assert len(responses.calls) == 0


# --- MINOR-14: non-finite stored prices classify UNRESOLVED ---


@responses.activate
def test_minor14_infinity_price_is_unresolved(tmp_path, no_sleep):
    data_dir = _seed_audit(tmp_path)
    price_file = data_dir / "prices" / "ecoflow-river-2-pro.jsonl"
    row = json.loads(price_file.read_text(encoding="utf-8").splitlines()[0])
    row["variants"]["ecoflow-river-2-pro-1-110w-portable-solar-panel"]["price"] = (
        float("inf"))
    price_file.write_text(json.dumps(row) + "\n", encoding="utf-8", newline="\n")
    site_dir = _rebuild(tmp_path, data_dir)
    _add_live()
    report, exit_code = _run(tmp_path, data_dir, site_dir)

    assert exit_code == 4
    bad = next(e for e in report["results"] if e["variant_id"] == str(BUNDLE_VID))
    assert bad["verdict"] == UNRESOLVED
    assert "finite money" in bad["detail"]
    good = next(e for e in report["results"] if e["variant_id"] == str(UNIT_VID))
    assert good["verdict"] == CLEAN

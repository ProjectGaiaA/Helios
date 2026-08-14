"""Tests for the $/Wh discipline (PLAN section 2b).

The variant titles below are REAL rows captured from shopsolarkits.com and
wildoaktrail.com on 2026-08-13 (C15: the DELTA Max is one 2,016Wh battery
sold as eight bundle permutations — naive per-variant $/Wh is wrong by
construction).
"""

import pytest

from build import classify_variant, dollars_per_wh, format_dollars_per_wh, money


# --- classification: real DELTA Max titles (C15) ---

DELTA_MAX_BUNDLES = [
    "DELTA MAX Eclipse Kit [1 x 200W Folding Panel]",
    "DELTA MAX Double Kit [2 x 200W Rigid Panels]",
    "DELTA MAX Nomad Kit [2 x 200W Folding Panels]",
    "DELTA MAX Quad Kit [4 x 200W Rigid Panels]",
    "DELTA MAX Hex Kit [6 x 100W Rigid Panels]",
    "DELTA MAX Base Camp Kit [4 x 200W Panels + EMP Bag]",
    "DELTA MAX Double Quad Kit [8 x 200W Rigid Panels]",
    # wild-oak-trail phrasings of the same product
    "EcoFlow DELTA Max 2000 + 1*220W Solar Panel",
    "EcoFlow DELTA Max  with 100w 12v Solar Panel Bundle",
    "EcoFlow DELTA Max + Smart Generator (Dual Fuel)",
]


@pytest.mark.parametrize("title", DELTA_MAX_BUNDLES)
def test_real_delta_max_bundle_titles_classify_as_bundle(title):
    assert classify_variant(title) == "bundle"


@pytest.mark.parametrize("title", [
    "DELTA MAX [Unit Only]",
    "EcoFlow DELTA Max Portable Power Station(Main Unit ONLY)",
    "Anker Solix F2600 [Main Unit Only]",
    "AC200L Only",
    "AC180 Only",
])
def test_real_unit_titles_classify_as_unit(title):
    assert classify_variant(title) == "unit"


def test_second_wattage_token_means_bundle():
    """A station wattage plus a panel wattage in one title is a bundle
    even without "kit"/"bundle"/"+"."""
    assert classify_variant("RIVER 2 Pro 800W 160W Solar Panel") == "bundle"


# --- red team #2 MAJOR-2: adversarial titles the v2 regex misread ---
# The v2 rule treated absence of a bundle signal as evidence of unit; all
# seven of these classified "unit" and would have rendered a bundle or
# multi-pack price as a per-unit $/Wh (2-Pack: $1.20/Wh shown, $0.60 true).

@pytest.mark.parametrize("title", [
    "AC200L & D40 Expansion",                 # "&" joiner, no "+"
    "AC180 w/ 200W Solar Panel",              # "w/" abbreviation of "with"
    "AC180 and 200W Solar Panel",             # "and" joiner
    "2-Pack",                                 # multi-pack: multiplier unknown
    "4 Pack",                                 # multi-pack, spaced form
    "EcoFlow DELTA Max Smart Extra Battery",  # extra battery, not the unit
    "RIVER 2 Pro Spare Battery",              # spare battery, not the unit
])
def test_adversarial_titles_classify_as_bundle(title):
    assert classify_variant(title) == "bundle"


def test_plain_model_title_still_classifies_as_unit():
    """The extended signals must not swallow a bare model-name title."""
    assert classify_variant("AC180") == "unit"
    assert classify_variant("Bluetti AC200L Portable Power Station") == "unit"


def test_wh_tokens_are_not_wattage_tokens():
    """"2,560Wh / 2,400W" is ONE wattage token (Wh is capacity), so a bare
    spec-style unit title must not be misread as a bundle."""
    assert classify_variant("Anker Solix F2600 2,560Wh 2,400W Station") == "unit"


def test_default_title_falls_back_to_product_title():
    # Single-variant server-rack battery: product title is the signal
    assert classify_variant(
        "Default Title",
        "EG4 LifePower4 V2 Lithium Battery | 48V 100Ah | 5.12kWh Server Rack",
    ) == "unit"
    # Single-variant kit product: the product title says Kit -> bundle
    assert classify_variant(
        "Default Title",
        "EcoFlow DELTA [MAX] Solar Kits - 2,400W / 2,016Wh Portable Power Station",
    ) == "bundle"


def test_unknown_classifies_as_bundle_and_withholds():
    """Conservative default: no signal at all -> bundle -> no $/Wh."""
    assert classify_variant("", "") == "bundle"
    assert classify_variant(None, None) == "bundle"


# --- $/Wh computation and withholding ---


def test_dollars_per_wh_unit_with_capacity():
    # Hand-check: RIVER 2 Pro main unit $569 / 768 Wh
    value = dollars_per_wh(569.00, 768, "unit")
    assert value == pytest.approx(0.7409, abs=0.0001)
    assert format_dollars_per_wh(value) == "$0.74/Wh"


def test_dollars_per_wh_withheld_for_bundles():
    """C15: $1,408 Eclipse Kit / 2,016 Wh would claim $0.70/Wh for a
    battery-plus-panel price. Withheld, not computed."""
    assert dollars_per_wh(1408.12, 2016, "bundle") is None


def test_dollars_per_wh_withheld_when_capacity_null():
    assert dollars_per_wh(699.00, None, "unit") is None


def test_dollars_per_wh_withheld_when_capacity_nonpositive():
    assert dollars_per_wh(699.00, 0, "unit") is None
    assert dollars_per_wh(699.00, -5, "unit") is None


def test_dollars_per_wh_withheld_when_price_unusable():
    assert dollars_per_wh(0, 768, "unit") is None
    assert dollars_per_wh(None, 768, "unit") is None


# --- quantity forms (red team #5) -------------------------------------
# PLAN 2b has always said "Multi-pack = bundle (capacity multiplier
# unknown -> withhold)", but the only signal implementing it was the
# literal word "pack". Everything below read as `unit`, and EG4 LL-S
# really did render $0.60/Wh and $0.90/Wh for 2- and 3-battery packs
# whose true per-battery figure is $0.30/Wh.

@pytest.mark.parametrize("title", [
    "2 Batteries Only",                            # EG4 LL-S, real row
    "3 Batteries Only",                            # EG4 LL-S, real row
    "ALPHA 5 PRO [Standard] / 2 Batteries",        # Rich Solar, real row
    "6 Batteries",
    "8 Solar Panels",                              # Rich Solar MEGA, real row
    "12 Panels",                                   # rich-solar phrasing, real row
    "10 Solar Panels",
    "4 Modules",
    "2 Units",
    "Pair of Batteries",
    "Dual Battery",
    "Twin Pack",
    "Twin",
    "Set of 4",
    "x2",
    "2x",
    "2 x 200W Rigid Panels",
])
def test_quantity_form_titles_classify_as_bundle(title):
    assert classify_variant(title) == "bundle"


@pytest.mark.parametrize("title", [
    # The false positive that started this: a SINGLE 24V/100Ah battery
    # whose descriptive title merely contains the word "and". Classifying
    # it as a bundle suppressed its honest $0.35/Wh, and the home cell
    # fell back to a different trim, implying a 44% cross-retailer gap
    # where the same-SKU truth is 11%.
    "ALPHA 4 - Self Heating and Bluetooth",
    "1 Battery Only",              # singular: one battery is a unit
    "ALPHA 5 PRO [Standard] / 1 Battery Only",
    "DELTA PRO 3 [Main Unit Only]",
    "Rich Solar - 200 Watt 12V Briefcase Solar Panel",
    "Only Battery (51.2V 100Ah)",
    # "Dual Fuel" is a fuel type, not a quantity — real EcoFlow row.
    "EcoFlow Smart Generator 4000 (Dual Fuel) Only",
])
def test_singular_and_descriptive_titles_stay_unit(title):
    assert classify_variant(title) == "unit"


def test_and_signal_is_kept_for_product_joiners():
    """Dropping "and" outright would re-open red team #2's MAJOR-2. It is
    qualified instead: "and" counts only when the next token has a digit."""
    assert classify_variant("AC200L and D40") == "bundle"
    assert classify_variant("DELTA Max and 220W Panel") == "bundle"
    assert classify_variant("Self Heating and Bluetooth") == "unit"


# --- non-finite guards (red team #5) ----------------------------------
# `nan <= 0` is False and `inf > 0` is True, so the ordinary positivity
# guard passes both straight through and the page renders "$nan/Wh".

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_dollars_per_wh_withholds_non_finite_price(bad):
    assert dollars_per_wh(bad, 768, "unit") is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_dollars_per_wh_withholds_non_finite_capacity(bad):
    assert dollars_per_wh(569.0, bad, "unit") is None


def test_dollars_per_wh_rejects_bool_masquerading_as_number():
    assert dollars_per_wh(True, 768, "unit") is None
    assert dollars_per_wh(569.0, True, "unit") is None


# --- money formatting: two decimals, thousands separators ---


def test_money_two_decimals():
    assert money(1408.1) == "$1,408.10"
    assert money(569) == "$569.00"
    assert money(1254.00) == "$1,254.00"

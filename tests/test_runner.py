"""Tests for runner CLI argument handling (red team #2, MINOR-6)."""

import pytest

from scrapers.runner import parse_products_arg


def test_no_flag_means_no_filter():
    assert parse_products_arg(None) is None


def test_valid_list_parses_and_strips():
    assert parse_products_arg("a, b,,c ") == ["a", "b", "c"]


def test_single_product_parses():
    assert parse_products_arg("ecoflow-river-2-pro") == ["ecoflow-river-2-pro"]


def test_empty_value_exits_1_instead_of_full_crawl():
    """`--products ""` used to parse to a falsy list, which the run loop
    read as "no filter" — turning a limiting flag into a FULL crawl."""
    with pytest.raises(SystemExit) as excinfo:
        parse_products_arg("")
    assert excinfo.value.code == 1


def test_whitespace_and_commas_only_exits_1():
    with pytest.raises(SystemExit) as excinfo:
        parse_products_arg(" , ,  ")
    assert excinfo.value.code == 1

"""Shared test fixtures for Helios scraper tests."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir():
    """Return the path to the test fixtures directory."""
    return FIXTURES_DIR


def load_fixture(retailer: str, filename: str) -> str | dict:
    """Load a fixture file by retailer and filename.

    Returns parsed JSON for .json files, raw string for anything else.
    """
    path = FIXTURES_DIR / retailer / filename
    text = path.read_text(encoding="utf-8")
    if filename.endswith(".json"):
        return json.loads(text)
    return text


@pytest.fixture
def no_sleep():
    """Patch time.sleep to no-op so tests don't actually wait."""
    with patch("time.sleep"):
        yield


@pytest.fixture(autouse=True)
def stub_robots():
    """Stub is_allowed_by_robots to always return True in scraper modules.

    RobotFileParser uses urllib (not requests), so the `responses`
    library can't mock it. This patches the function where it's imported
    in the modules that EXIST in Helios (shopify, runner), preventing any
    real network call to fetch robots.txt. Patching a module that does
    not exist makes every test error at setup — the ported patch list
    from the plant tracker named five modules, three of which have no
    Helios counterpart. Tests in test_polite.py exercise the real
    function via _robots_cache directly, which this stub does not touch.
    """
    with (
        patch("scrapers.shopify.is_allowed_by_robots", return_value=True),
        patch("scrapers.runner.is_allowed_by_robots", return_value=True),
    ):
        yield


@pytest.fixture
def tmp_data_dir(tmp_path):
    """Provide a temporary data directory for tests that write files."""
    prices_dir = tmp_path / "prices"
    prices_dir.mkdir()
    return tmp_path

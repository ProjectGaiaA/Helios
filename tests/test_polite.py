"""Tests for the polite scraping module (scrapers/polite.py)."""

from urllib.robotparser import RobotFileParser

from scrapers.polite import (
    random_ua,
    polite_headers,
    is_allowed_by_robots,
    polite_delay,
    make_polite_session,
    USER_AGENTS,
    _robots_cache,
)


def test_random_ua_returns_string_from_list():
    ua = random_ua()
    assert ua in USER_AGENTS
    assert isinstance(ua, str)
    assert len(ua) > 20


def test_polite_headers_includes_required_fields():
    headers = polite_headers()
    assert "User-Agent" in headers
    assert "Accept" in headers
    assert "DNT" in headers
    assert headers["DNT"] == "1"


def test_polite_headers_accepts_custom_ua():
    headers = polite_headers(ua="CustomAgent/1.0")
    assert headers["User-Agent"] == "CustomAgent/1.0"


def _make_robots_parser(rules_text: str) -> RobotFileParser:
    """Create a RobotFileParser from a rules string without fetching."""
    rp = RobotFileParser()
    rp.parse(rules_text.splitlines())
    return rp


def test_robots_allows_when_permitted():
    """robots.txt allowing all should return True."""
    _robots_cache.clear()
    parser = _make_robots_parser("User-agent: *\nAllow: /")
    _robots_cache["example.com"] = parser

    result = is_allowed_by_robots("https://example.com/products/test")
    assert result is True


def test_robots_denies_when_disallowed():
    """robots.txt disallowing path should return False."""
    _robots_cache.clear()
    parser = _make_robots_parser("User-agent: *\nDisallow: /products/")
    _robots_cache["blocked.com"] = parser

    result = is_allowed_by_robots("https://blocked.com/products/test")
    assert result is False


def test_robots_fail_open_when_unreachable():
    """Unreachable robots.txt should fail-open (return True)."""
    _robots_cache.clear()
    # None in cache means fetch failed — triggers fail-open
    _robots_cache["down.com"] = None

    result = is_allowed_by_robots("https://down.com/products/test")
    assert result is True


def test_polite_delay_returns_value_in_range(no_sleep):
    """polite_delay should return a float between min and max."""
    result = polite_delay(5.0, 15.0)
    assert 5.0 <= result <= 15.0


def test_make_polite_session_sets_headers():
    session = make_polite_session()
    assert "User-Agent" in session.headers
    assert "Accept" in session.headers


# --- robots.txt fetch must not use Python-urllib's default UA ---

from unittest.mock import patch as _patch  # noqa: E402

import scrapers.polite as _polite  # noqa: E402


class _Resp:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text


def _clear_cache():
    _polite._robots_cache.clear()


def test_robots_fetched_with_identifying_bot_ua():
    """A WAF that 403s Python-urllib must not silently disable the scraper.

    RobotFileParser.read() swallows the 403 and sets disallow_all=True —
    in the plant tracker that made a permitted retailer disappear for 40+
    runs. We fetch it ourselves with an honest identifier instead.
    """
    _clear_cache()
    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen["url"] = url
        seen["ua"] = (headers or {}).get("User-Agent")
        return _Resp(200, "User-agent: *\nAllow: /\n")

    with _patch.object(_polite.requests, "get", fake_get):
        allowed = _polite.is_allowed_by_robots("https://example.com/products/x.json")

    assert allowed is True
    assert seen["url"] == "https://example.com/robots.txt"
    assert seen["ua"] == _polite.BOT_USER_AGENT
    assert "HeliosPriceBot" in seen["ua"]
    assert "Mozilla" not in seen["ua"], "robots.txt must not be fetched with a spoofed browser UA"


def test_bot_ua_has_reachable_contact():
    """The bot UA must carry a real contact, not a dead domain.

    The plant tracker's bot advertised a URL; Helios has no domain yet, so
    the contact is an email (PLAN v2 CHANGELOG: bot UA decision).
    """
    assert "brandon.william.hall@gmail.com" in _polite.BOT_USER_AGENT
    assert "plantpricetracker" not in _polite.BOT_USER_AGENT.lower()


def test_robots_403_fails_open_not_closed():
    """A 403 on robots.txt must fail OPEN with a warning, never block everything."""
    _clear_cache()
    with _patch.object(_polite.requests, "get", lambda *a, **k: _Resp(403)):
        assert _polite.is_allowed_by_robots("https://example.com/products/x.json") is True


def test_robots_disallow_is_still_honored():
    """Failing open on fetch errors must not mean ignoring real rules."""
    _clear_cache()
    body = "User-agent: *\nDisallow: /private/\n"
    with _patch.object(_polite.requests, "get", lambda *a, **k: _Resp(200, body)):
        assert _polite.is_allowed_by_robots("https://example.com/private/x") is False
        assert _polite.is_allowed_by_robots("https://example.com/products/x") is True

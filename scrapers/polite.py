"""
Polite Scraping Utilities

Shared infrastructure for careful, respectful web scraping:
- Real browser user-agent rotation
- robots.txt compliance
- Polite request headers that look like a real browser
- Minimum 5-second randomized delays between requests
- Request logging with timestamps and delays
"""

import logging
import random
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bot identity
# ---------------------------------------------------------------------------
# Used for robots.txt fetches. An honest identifier with a real contact,
# because some WAFs reject Python-urllib's default UA with a 403 and the
# fallout is silent (see _get_robots_parser). Contact is an email rather
# than a URL: the project has no domain yet, and a dead link in a bot UA
# reads as evasion to a retailer reviewing their logs.
BOT_USER_AGENT = (
    "HeliosPriceBot/0.1 (+contact: brandon.william.hall@gmail.com)"
)

# ---------------------------------------------------------------------------
# 10 real browser user-agent strings (updated 2026-04)
# ---------------------------------------------------------------------------
USER_AGENTS = [
    # Chrome (Windows)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Chrome (Mac)
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Firefox (Windows)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    # Firefox (Mac)
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) Gecko/20100101 Firefox/128.0",
    # Safari (Mac)
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    # Edge (Windows)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    # Chrome (Linux)
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Firefox (Linux)
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    # Chrome 123 (Windows)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Safari (iPhone — useful as secondary rotation)
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
]


def random_ua() -> str:
    """Return a random real browser user-agent string."""
    return random.choice(USER_AGENTS)


def polite_headers(ua: str = None) -> dict:
    """Return request headers that look like a real browser."""
    return {
        "User-Agent": ua or random_ua(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
    }


# ---------------------------------------------------------------------------
# robots.txt compliance
# ---------------------------------------------------------------------------
_robots_cache: dict[str, RobotFileParser] = {}


def _get_robots_parser(domain: str) -> RobotFileParser | None:
    """Fetch and cache robots.txt for a domain. Returns None on failure.

    Fetches the file ourselves rather than calling RobotFileParser.read().
    read() uses Python-urllib's default User-Agent, which some WAFs reject
    with a 403, and CPython's read() swallows that HTTPError and sets
    disallow_all=True. The result is a parser that silently forbids
    everything, with no exception for the caller's fail-open branch to
    catch — in the plant tracker this module was ported from, that made a
    retailer whose robots.txt actually says "Allow: /" vanish from the
    site for 40+ consecutive runs.
    """
    if domain in _robots_cache:
        return _robots_cache[domain]

    robots_url = f"https://{domain}/robots.txt"
    try:
        resp = requests.get(
            robots_url, headers={"User-Agent": BOT_USER_AGENT}, timeout=20
        )
        if resp.status_code >= 400:
            # Genuine failure: fail open loudly (documented behavior).
            logger.warning(
                f"robots.txt for {domain} returned HTTP {resp.status_code} — "
                f"proceeding without robots rules (fail-open)"
            )
            _robots_cache[domain] = None
            return None
        rp = RobotFileParser()
        rp.parse(resp.text.splitlines())
        _robots_cache[domain] = rp
        return rp
    except Exception as e:
        logger.warning(f"Could not fetch robots.txt for {domain}: {e}")
        _robots_cache[domain] = None
        return None


def is_allowed_by_robots(url: str, user_agent: str = "*") -> bool:
    """Check if a URL is allowed by the domain's robots.txt.

    Returns True if allowed or if robots.txt cannot be fetched (fail-open
    but with a warning logged).
    """
    parsed = urlparse(url)
    domain = parsed.netloc
    if not domain:
        return True

    rp = _get_robots_parser(domain)
    if rp is None:
        # Could not fetch robots.txt — proceed but warn
        return True

    allowed = rp.can_fetch(user_agent, url)
    if not allowed:
        logger.warning(f"robots.txt DISALLOWS: {url} — skipping")
    return allowed


# ---------------------------------------------------------------------------
# Polite delay with logging
# ---------------------------------------------------------------------------

def polite_delay(min_seconds: float = 5.0, max_seconds: float = 15.0) -> float:
    """Sleep for a random duration between min and max seconds.

    Returns the actual delay used (for logging).
    """
    delay = random.uniform(min_seconds, max_seconds)
    time.sleep(delay)
    return delay


def log_request(url: str, delay_used: float = None, status_code: int = None):
    """Log a request with timestamp and optional delay info."""
    parts = [f"REQUEST: {url}"]
    if delay_used is not None:
        parts.append(f"delay={delay_used:.1f}s")
    if status_code is not None:
        parts.append(f"status={status_code}")
    logger.info("  ".join(parts))


def make_polite_session(ua: str = None) -> requests.Session:
    """Create a requests.Session with polite browser-like headers."""
    s = requests.Session()
    chosen_ua = ua or random_ua()
    s.headers.update(polite_headers(chosen_ua))
    return s

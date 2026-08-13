"""
Shopify Product Scraper

Most solar/home-energy retailers use Shopify, which exposes structured
JSON endpoints:
  - /products/{handle}.json — single product with variants and prices.
    NOTE: .json does NOT carry stock. It has no `available` key at all.
    Per-variant availability comes from /products/{handle}.js, fetched
    separately by fetch_availability(). See that method for why.
  - /products.json?limit=250 — paginated product listing

This scraper uses the JSON endpoints instead of HTML scraping:
  - More robust (won't break on theme changes)
  - Less likely to trigger bot detection
  - Structured data, no parsing needed

There is deliberately NO HTML fallback. The plant tracker this module was
ported from grew a ~500-line HTML path for one retailer's theme, and it
was the source of every wrong-price defect in that project's history. A
future retailer that disables its JSON endpoints gets its own scraper
module, not a fallback here.

Usage:
    from scrapers.shopify import ShopifyScraper
    scraper = ShopifyScraper("shop-solar-kits", "https://shopsolarkits.com")
    results = scraper.scrape_products(["ecoflow-delta-max"])
"""

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from scrapers.polite import (
    polite_delay,
    log_request, is_allowed_by_robots, make_polite_session,
)
from scrapers.common import FetchResult

logger = logging.getLogger(__name__)


class ShopifyScraper:
    """Scrape product data from Shopify-based energy stores."""

    def __init__(self, retailer_id: str, base_url: str, delay_range: tuple = (5, 15)):
        """Initialize scraper with conservative defaults.

        delay_range is 5-15 seconds between requests by default.
        This is intentionally slow to be respectful — we're scraping
        once daily, not building a real-time feed. Being polite to
        retailer servers is both ethical and keeps us from getting blocked.
        """
        self.retailer_id = retailer_id
        self.base_url = base_url.rstrip("/")
        self.delay_range = delay_range
        self.session = make_polite_session()

    def _delay(self):
        """Random 5-15s delay between requests. Intentionally slow to be polite."""
        delay = polite_delay(self.delay_range[0], self.delay_range[1])
        return delay

    def _get_json(self, url: str, allow_redirects: bool = True) -> FetchResult:
        """Fetch JSON from URL with error handling and robots.txt compliance.

        Returns a FetchResult with data, status_code, and redirect_url.
        When allow_redirects=False, a 301/302 response returns the
        redirect URL without following it.
        """
        if not is_allowed_by_robots(url):
            return FetchResult(data=None, status_code=None, redirect_url=None)
        try:
            resp = self.session.get(url, timeout=20, allow_redirects=allow_redirects)
            log_request(url, status_code=resp.status_code)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 30))
                logger.warning(f"Rate limited by {self.retailer_id}, waiting {retry_after}s")
                time.sleep(retry_after)
                resp = self.session.get(url, timeout=20, allow_redirects=allow_redirects)
                log_request(url, status_code=resp.status_code)
            if resp.status_code in (301, 302) and not allow_redirects:
                redirect_url = resp.headers.get("Location")
                return FetchResult(data=None, status_code=resp.status_code, redirect_url=redirect_url)
            if resp.status_code == 404:
                logger.info(f"Product not found: {url}")
                return FetchResult(data=None, status_code=404, redirect_url=None)
            if resp.status_code >= 500:
                logger.warning(f"Server error {resp.status_code} for {url}")
                return FetchResult(data=None, status_code=resp.status_code, redirect_url=None)
            resp.raise_for_status()
            return FetchResult(data=resp.json(), status_code=resp.status_code, redirect_url=None)
        except requests.RequestException as e:
            logger.error(f"Request failed for {url}: {e}")
            return FetchResult(data=None, status_code=None, redirect_url=None)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON from {url}: {e}")
            return FetchResult(data=None, status_code=None, redirect_url=None)

    def scrape_product(self, handle: str, product_id: str = None) -> dict | None:
        """Scrape a single product by its Shopify handle.

        On 301/302: records a redirect candidate and follows the redirect.
        On 404: records a broken handle entry (if product_id provided)
                and returns None — there is no HTML fallback (see module
                docstring).
        On 5xx: skips silently (server problem, not a handle change).

        Args:
            handle: The Shopify product handle (URL slug), e.g. "ecoflow-delta-max"
            product_id: Optional product ID for recovery tracking.

        Returns:
            Structured dict with price data, or None on failure.
        """
        from scrapers.common import (
            record_broken,
            record_redirect_candidate,
            extract_handle_from_url,
        )

        # Try JSON endpoint — with redirect detection
        json_url = f"{self.base_url}/products/{handle}.json"
        result = self._get_json(json_url, allow_redirects=False)

        # Handle redirect: record candidate and follow for data
        if result.status_code in (301, 302) and result.redirect_url:
            new_handle = extract_handle_from_url(result.redirect_url)
            if product_id and new_handle:
                record_redirect_candidate(
                    self.retailer_id, product_id, handle,
                    new_handle, result.redirect_url,
                )
            # Follow the redirect to get data for this run
            follow_result = self._get_json(result.redirect_url)
            if follow_result.data and "product" in follow_result.data:
                return self._parse_product(
                    follow_result.data["product"],
                    self.fetch_availability(new_handle or handle),
                )
            return None

        # Handle 5xx: skip silently — server problem, not a handle change
        if result.status_code is not None and result.status_code >= 500:
            return None

        # Handle 404: record broken handle. No HTML fallback — the gap is
        # visible in the manifest as products_error, which is the alarm.
        if result.status_code == 404:
            if product_id:
                record_broken(self.retailer_id, product_id, handle)
            return None

        # Normal success path
        if result.data and "product" in result.data:
            return self._parse_product(
                result.data["product"], self.fetch_availability(handle)
            )

        return None

    def scrape_products(self, handles: list[str], product_ids: list[str] = None) -> list[dict]:
        """Scrape multiple products by handle. Returns list of result dicts.

        Args:
            handles: List of Shopify product handles to scrape.
            product_ids: Optional parallel list of product IDs for recovery tracking.
        """
        results = []
        for i, handle in enumerate(handles):
            pid = product_ids[i] if product_ids else None
            logger.info(f"  [{i+1}/{len(handles)}] {self.retailer_id}: {handle}")
            result = self.scrape_product(handle, product_id=pid)
            if result:
                results.append(result)
            else:
                results.append({
                    "retailer_id": self.retailer_id,
                    "handle": handle,
                    "error": "Product not found or request failed",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            if i < len(handles) - 1:
                self._delay()
        return results

    def fetch_availability(self, handle: str) -> dict:
        """Per-variant stock, keyed by variant id: {variant_id: bool}.

        WHY THIS EXISTS. The scraper reads /products/{handle}.json, and that
        endpoint HAS NO `available` FIELD. `variant.get("available")` therefore
        returns None for every variant of every Shopify retailer. The plant
        tracker this was ported from shipped that way for months — 170 rows of
        unknown stock — because its module docstring claimed .json carried
        availability, so nobody checked.

        /products/{handle}.js does carry it, per variant, and it matches what
        a shopper sees in the store's own variant selector.

        Deliberately a SEPARATE fetch rather than switching endpoints. The .js
        payload returns price in CENTS (140800) where .json returns dollar
        strings ("1408.00"); swapping wholesale would multiply every price on
        the site by 100. Only the boolean is taken from here.

        Returns {} on any failure, which leaves availability unknown rather
        than asserting stock we cannot confirm.
        """
        # This is a SECOND request to the same host for the same product, so it
        # gets its own delay. Without one, every product becomes a back-to-back
        # request pair, which contradicts the project's own stated rule of
        # 5-15s between requests and is exactly the behaviour that gets a
        # scraper blocked.
        self._delay()
        url = f"{self.base_url}/products/{handle}.js"
        result = self._get_json(url)
        data = result.data
        if not isinstance(data, dict):
            return {}
        out = {}
        for variant in data.get("variants") or []:
            if not isinstance(variant, dict):
                continue
            vid = variant.get("id")
            avail = variant.get("available")
            # Only a real boolean counts. A missing key means unknown, and
            # unknown must never be coerced to True.
            if vid is not None and isinstance(avail, bool):
                out[str(vid)] = avail
        return out

    def _parse_product(self, product: dict, availability: dict | None = None) -> dict:
        """Parse a Shopify product JSON into our canonical format."""
        title = product.get("title", "")
        handle = product.get("handle", "")
        raw_variants = product.get("variants", [])

        # Extract prices by variant. Multi-pack and bundle variants are KEPT:
        # "2 x 100W Panel" and "DELTA Max + 220W Panel" are legitimate solar
        # products, not inflated per-unit noise. Whether a variant earns a
        # $/Wh figure is decided at build time by bundle classification, not
        # here by exclusion.
        variants = {}
        any_available = False

        for variant in raw_variants:
            # Same guard as fetch_availability: one malformed entry in the
            # payload must skip, not crash the whole retailer run.
            if not isinstance(variant, dict):
                continue
            variant_title = (variant.get("title") or "").strip()
            price_str = variant.get("price", "0")
            compare_price_str = variant.get("compare_at_price")
            # If 'available' field is missing, set to None (unknown — display as "Check site")
            # If present, use the actual value
            available = variant.get("available")
            if available is None:
                available = None  # Unknown — don't assume in stock or out of stock

            try:
                price = float(price_str)
            except (ValueError, TypeError):
                continue

            if price <= 0:
                continue

            # Real per-variant stock, fetched from /products/{handle}.js because
            # the .json endpoint this product came from has no `available` field.
            if availability:
                looked_up = availability.get(str(variant.get("id", "")))
                if isinstance(looked_up, bool):
                    available = looked_up

            was_price = None
            if compare_price_str:
                try:
                    was_price = float(compare_price_str)
                    if was_price <= price:
                        was_price = None  # Not actually a discount
                except (ValueError, TypeError):
                    pass

            variant_id = variant.get("id", "")

            # Normalize the variant title to a tier key. On collision within
            # this product, suffix with the variant id instead of overwriting:
            # last-write-wins silently replaced an earlier variant's price
            # with a later, differently-priced one in the plant tracker.
            # The suffix must be GUARANTEED unique: with missing variant
            # ids, a single `if` collapsed three same-titled variants onto
            # two keys and still overwrote one (red team #2, MINOR-4) —
            # hence the numeric fallback loop.
            base_tier = self._normalize_variant(variant_title)
            tier = base_tier
            if tier in variants and variant_id:
                tier = f"{base_tier}-{variant_id}"
            n = 2
            while tier in variants:
                tier = f"{base_tier}-{n}"
                n += 1

            if available is True:
                any_available = True

            variants[tier] = {
                "price": price,
                "was_price": was_price,
                "available": available,
                "raw_variant": variant_title,
                "variant_id": variant_id,
            }

        # Product URL — use variant ID of the cheapest variant for deep linking
        product_url = f"{self.base_url}/products/{handle}"
        if variants:
            cheapest = min(variants.values(), key=lambda x: x["price"])
            if cheapest.get("variant_id"):
                product_url = f"{self.base_url}/products/{handle}?variant={cheapest['variant_id']}"

        # If NO variant had an explicit available field, stock is unknown.
        # Some stores return null for both in-stock AND sold-out products,
        # so we can't assume either way.
        has_any_explicit_availability = any(
            v.get("available") is not None for v in variants.values()
        )
        if not has_any_explicit_availability:
            any_available = None  # Unknown — show dash

        result = {
            "retailer_id": self.retailer_id,
            "retailer_name": self.retailer_id.replace("-", " ").title(),
            "handle": handle,
            "title": title,
            "url": product_url,
            "variants": variants,
            "in_stock": any_available,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if not variants:
            # A published row with zero readable variants is a true fact
            # ("the page answered, we could price nothing") but NOT a
            # successful price read. Without this flag, runner.py counted
            # such rows as hits and a completely broken retailer reported
            # 100% healthy. Key name kept as `no_sizes_readable` (not
            # `no_variants_readable`) because runner.py's hit-rate health
            # consumes it under that name, ported as-is from the plant
            # tracker.
            result["no_sizes_readable"] = True
        return result

    def _normalize_variant(self, variant_title: str) -> str:
        """Slugify a variant title into a stable tier key.

        Solar variant titles are free-form ("Delta Max 2000 + 2x 220W
        Panels", "48V / 100Ah"), so unlike the plant tracker's closed
        gallon/height vocabulary there is nothing to normalize INTO — the
        slug IS the tier. Collisions within one product are handled by the
        caller, which appends the variant id rather than overwriting.
        """
        raw = variant_title.strip().lower()
        if not raw or raw == "default title":
            return "default"
        slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
        return slug or "default"


# ---------------------------------------------------------------------------
# Handle mapping: maps canonical product IDs to Shopify product handles
# per retailer. Loaded from data/handle_maps.json at runtime.
# ---------------------------------------------------------------------------

_HANDLE_MAPS_PATH = Path(__file__).parent.parent / "data" / "handle_maps.json"
_handle_maps_cache: dict | None = None


def load_handle_maps() -> dict[str, dict[str, str]]:
    """Load handle maps from data/handle_maps.json. Cached after first call.

    A missing file means an empty mapping, not a crash: the plant tracker
    opened it unguarded, so a fresh checkout died with FileNotFoundError
    before the runner could even report which retailer was unmapped.
    """
    global _handle_maps_cache
    if _handle_maps_cache is None:
        if _HANDLE_MAPS_PATH.exists():
            with open(_HANDLE_MAPS_PATH, encoding="utf-8") as f:
                _handle_maps_cache = json.load(f)
        else:
            logger.warning(f"{_HANDLE_MAPS_PATH} does not exist — no handle mappings")
            _handle_maps_cache = {}
    return _handle_maps_cache


def get_handles_for_retailer(retailer_id: str, product_ids: list[str]) -> dict[str, str]:
    """Get the Shopify handle mapping for a retailer.

    Returns dict of {product_id: shopify_handle} for products this retailer carries.
    """
    mapping = load_handle_maps().get(retailer_id, {})
    return {pid: mapping[pid] for pid in product_ids if pid in mapping}


def save_handle_map_entry(retailer_id: str, product_id: str, new_handle: str) -> None:
    """Write a single handle update to data/handle_maps.json.

    Intentionally unused in the skeleton: this is the named Phase B surface
    the recovery system's candidate validation writes through (PLAN C2/C3).

    Creates the retailer key if it doesn't exist. Invalidates the
    in-memory cache so the next load_handle_maps() reads fresh data.
    """
    global _handle_maps_cache
    if _HANDLE_MAPS_PATH.exists():
        with open(_HANDLE_MAPS_PATH, encoding="utf-8") as f:
            maps = json.load(f)
    else:
        maps = {}
    if retailer_id not in maps:
        maps[retailer_id] = {}
    maps[retailer_id][product_id] = new_handle
    with open(_HANDLE_MAPS_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(maps, f, indent=2, ensure_ascii=False)
    _handle_maps_cache = None
    logger.info(f"Handle map updated: {retailer_id}/{product_id} -> {new_handle}")

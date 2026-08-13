"""
Scraper Runner — Orchestrates price collection across all retailers.

Runs each retailer's scraper, writes results to JSONL price files,
generates a monitoring manifest, and flags anomalies.

Usage:
    python -m scrapers.runner                    # Scrape all active retailers
    python -m scrapers.runner --retailer shop-solar-kits   # Single retailer
    python -m scrapers.runner --products ecoflow-delta-max # Subset of catalog
    python -m scrapers.runner --dry-run          # Show what would be scraped
"""

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add parent dir to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import requests

from scrapers.polite import (
    polite_delay,
    log_request, is_allowed_by_robots, make_polite_session,
)
from scrapers.shopify import ShopifyScraper, get_handles_for_retailer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
PRICES_DIR = DATA_DIR / "prices"
MANIFEST_PATH = DATA_DIR / "last_manifest.json"
PROMOS_PATH = DATA_DIR / "promos.json"

# Must match `timeout-minutes` on the scrape job in
# .github/workflows/scrape.yml. Not consumed by code since the recovery
# time-budget block was deleted (Phase B), but kept so the invariant has a
# named home on this side of the boundary when recovery returns.
CI_TIMEOUT_SECONDS = 30 * 60

# Patterns that indicate a discount banner or promo code on a retail page.
# Checked against the full page HTML (case-insensitive).
_PROMO_CODE_PATTERNS = [
    # Explicit code callouts: "Use code SAVE10", "Promo code: SOLAR20"
    re.compile(r'(?:use\s+(?:code|coupon|promo)\s*[:\-]?\s*)([A-Z0-9]{4,20})', re.IGNORECASE),
    # Discount percentages linked to a code: "Get 10% off with SUN10"
    re.compile(r'(?:get|save|extra)\s+\d+%\s+off\s+with\s+(?:code\s+)?([A-Z0-9]{4,20})', re.IGNORECASE),
    # Inline "code XXXX" shorthand
    re.compile(r'\bcode\s+([A-Z][A-Z0-9]{3,19})\b'),
]

# Patterns that signal a discount/sale banner (no explicit code needed).
_SALE_BANNER_PATTERNS = [
    re.compile(r'free\s+shipping\s+(?:on\s+orders?\s+over|when\s+you\s+spend)\s*\$?([\d,]+)', re.IGNORECASE),
    re.compile(r'(\d{1,2})\s*%\s*off\s+(?:your\s+(?:first|next|entire)\s+order|all\s+orders?|site-?wide)', re.IGNORECASE),
    re.compile(r'(?:save|extra)\s+\$(\d+)\s+(?:on\s+(?:your\s+(?:first|next)|orders?\s+over\s+\$[\d,]+))', re.IGNORECASE),
    re.compile(r'(?:buy\s+\d+\s*[,/]\s*get\s+\d+\s+free)', re.IGNORECASE),
    re.compile(r'flash\s+sale', re.IGNORECASE),
    re.compile(r'limited[\s-]time\s+offer', re.IGNORECASE),
]

_PROMO_SESSION = None  # Lazy-initialized polite session for promo scraping


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def append_price(product_id: str, price_entry: dict):
    """Append a price entry to the product's JSONL file.

    newline="\\n" forces LF even on Windows-local runs — belt to
    .gitattributes' suspenders. Without it, text mode writes CRLF and the
    first local scrape rewrites every committed row's line ending.
    """
    PRICES_DIR.mkdir(parents=True, exist_ok=True)
    filepath = PRICES_DIR / f"{product_id}.jsonl"
    with open(filepath, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(price_entry, ensure_ascii=False) + "\n")


def load_previous_manifest() -> dict:
    """Load the last scrape manifest for anomaly detection."""
    if MANIFEST_PATH.exists():
        try:
            with open(MANIFEST_PATH, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def merge_manifest(prev_manifest: dict, new_entries: list[dict]) -> dict:
    """Merge new per-retailer results into the previous manifest.

    CI runs each retailer as its own `python -m scrapers.runner --retailer X`
    step. Without merging, each invocation would overwrite the manifest with
    only that one retailer's data — losing everyone else. This helper takes
    the on-disk manifest and replaces (or adds) entries for the retailers in
    new_entries, leaving untouched retailers alone.

    Price records are keyed by ``f"{product_id}:{retailer_id}"``. We purge
    any keys matching a retailer we just scraped before overlaying the new
    records, so stale prices for that retailer cannot linger.

    The returned dict has fresh top-level totals, a timestamp set to "now",
    and a merged ``retailers`` list. Callers still need to set
    ``degraded_retailers`` / ``pipeline_status`` after they compute health.
    """
    new_retailer_ids = {e["retailer_id"] for e in new_entries}

    # Start from previous retailer entries, drop any being replaced this run
    merged_entries = [
        e for e in prev_manifest.get("retailers", [])
        if e.get("retailer_id") not in new_retailer_ids
    ] + list(new_entries)

    # Start from previous prices, drop any keys whose retailer is being
    # replaced this run, then overlay this run's new records
    merged_prices: dict = {}
    for key, val in prev_manifest.get("prices", {}).items():
        if ":" in key:
            rid = key.rsplit(":", 1)[1]
            if rid in new_retailer_ids:
                continue
        merged_prices[key] = val
    for entry in new_entries:
        for key, val in entry.get("price_records", {}).items():
            merged_prices[key] = val

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "retailers": merged_entries,
        "total_prices_collected": sum(e.get("prices_collected", 0) for e in merged_entries),
        "total_anomalies": sum(len(e.get("anomalies", [])) for e in merged_entries),
        "anomalies": [a for e in merged_entries for a in e.get("anomalies", [])],
        "prices": merged_prices,
    }


def check_price_anomaly(product_id: str, retailer_id: str, new_prices: dict, prev_manifest: dict) -> list[str]:
    """Check for suspicious price changes (>50% swing)."""
    warnings = []
    prev_key = f"{product_id}:{retailer_id}"
    prev_prices = prev_manifest.get("prices", {}).get(prev_key, {})

    for tier, new_data in new_prices.items():
        new_price = new_data.get("price", 0) if isinstance(new_data, dict) else 0
        old_price = prev_prices.get(tier, 0)

        if old_price > 0 and new_price > 0:
            change_pct = abs(new_price - old_price) / old_price * 100
            if change_pct > 50:
                warnings.append(
                    f"ANOMALY: {product_id} at {retailer_id} tier={tier}: "
                    f"${old_price:.2f} -> ${new_price:.2f} ({change_pct:.0f}% change)"
                )
    return warnings


def _get_promo_session() -> requests.Session:
    """Get or create a polite session for promo scraping."""
    global _PROMO_SESSION
    if _PROMO_SESSION is None:
        _PROMO_SESSION = make_polite_session()
    return _PROMO_SESSION


def _fetch_page_html(url: str, timeout: int = 15) -> str | None:
    """Fetch a URL and return the raw HTML text. Returns None on failure.

    Checks robots.txt before fetching. Logs every request.
    """
    if not is_allowed_by_robots(url):
        return None
    session = _get_promo_session()
    try:
        resp = session.get(url, timeout=timeout)
        log_request(url, status_code=resp.status_code)
        if resp.status_code == 200:
            return resp.text
    except requests.RequestException:
        pass
    return None


def _extract_promos_from_html(html: str) -> dict:
    """Scan HTML for promo codes and discount banners.

    Returns a dict with:
      - codes: list of found promo code strings (uppercase, deduplicated)
      - banners: list of human-readable discount banner strings found
    """
    codes = []
    banners = []

    # Work on a compact text version (collapse whitespace, strip tags)
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'\s+', ' ', text)

    # Extract explicit promo codes
    seen_codes = set()
    for pattern in _PROMO_CODE_PATTERNS:
        for m in pattern.finditer(text):
            code = m.group(1).upper()
            # Skip obvious false positives: all-digit, very short, looks like SKU
            if len(code) < 4 or code.isdigit():
                continue
            if code not in seen_codes:
                seen_codes.add(code)
                codes.append(code)

    # Extract sale/discount banners
    seen_banners = set()
    for pattern in _SALE_BANNER_PATTERNS:
        for m in pattern.finditer(text):
            banner = m.group(0).strip()
            banner_key = banner.lower()
            if banner_key not in seen_banners:
                seen_banners.add(banner_key)
                banners.append(banner)

    return {"codes": codes, "banners": banners}


# Sample product pages per retailer for promo scanning. Deliberately empty
# for the skeleton: homepage-only. The plant tracker hard-coded its
# most-visited product path per nursery here; Helios adds paths only once
# traffic data says which product page is worth a second polite request.
_SAMPLE_PRODUCT_PATHS: dict[str, str] = {}


def scrape_promos(retailers: list[dict], dry_run: bool = False) -> dict:
    """Check retailer homepages for promo codes/banners.

    Writes results to data/promos.json with a timestamp per retailer.
    Returns a summary dict of what was found.

    We fetch the retailer homepage (always has sitewide banners/popups in
    HTML), plus a sample product page for retailers listed in
    _SAMPLE_PRODUCT_PATHS (none in the skeleton — homepage-only).

    Politeness: 5-15 second delay between requests, max 2 pages per retailer.
    """
    logger.info("\nScraping promo codes / discount banners...")

    # Load existing promos to merge (preserve history)
    existing = {}
    if PROMOS_PATH.exists():
        try:
            with open(PROMOS_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass

    results = {}
    timestamp = datetime.now(timezone.utc).isoformat()

    shopify_retailers = [r for r in retailers if r.get("active") and r.get("scraper_type") == "shopify"]
    custom_retailers  = [r for r in retailers if r.get("active") and r.get("scraper_type") != "shopify"
                         and r["id"] in _SAMPLE_PRODUCT_PATHS]
    target_retailers  = shopify_retailers + custom_retailers

    for retailer in target_retailers:
        rid = retailer["id"]
        base_url = retailer["url"].rstrip("/")
        found_codes: list[str] = []
        found_banners: list[str] = []

        if dry_run:
            logger.info(f"  [dry-run] {rid}: would fetch {base_url}")
            results[rid] = {"retailer_id": rid, "dry_run": True}
            continue

        logger.info(f"  {rid}: checking homepage...")
        html = _fetch_page_html(base_url)
        if html:
            extracted = _extract_promos_from_html(html)
            found_codes.extend(extracted["codes"])
            found_banners.extend(extracted["banners"])

        polite_delay(5, 15)

        # Sample product page (skeleton: _SAMPLE_PRODUCT_PATHS is empty)
        sample_path = _SAMPLE_PRODUCT_PATHS.get(rid)
        if sample_path:
            logger.info(f"  {rid}: checking sample product page...")
            pdp_html = _fetch_page_html(f"{base_url}{sample_path}")
            if pdp_html:
                extracted = _extract_promos_from_html(pdp_html)
                for c in extracted["codes"]:
                    if c not in found_codes:
                        found_codes.append(c)
                for b in extracted["banners"]:
                    if b not in found_banners:
                        found_banners.append(b)
            polite_delay(5, 15)

        # Deduplicate and build result
        entry = {
            "retailer_id": rid,
            "retailer_name": retailer["name"],
            "timestamp": timestamp,
            "codes": found_codes,
            "banners": found_banners,
        }
        results[rid] = entry

        if found_codes:
            logger.info(f"    Codes found: {', '.join(found_codes)}")
        if found_banners:
            logger.info(f"    Banners: {len(found_banners)} found")
        if not found_codes and not found_banners:
            logger.info("    No active promos detected")

    # Merge with existing promos (keep history per retailer)
    for rid, entry in results.items():
        if not entry.get("dry_run"):
            existing[rid] = entry

    if not dry_run:
        with open(PROMOS_PATH, "w", encoding="utf-8", newline="\n") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
        logger.info(f"  Promos saved to {PROMOS_PATH}")

    return results


def scrape_retailer(retailer: dict, product_ids: list[str], prev_manifest: dict, dry_run: bool = False) -> dict:
    """Scrape a single retailer for all mapped products.

    Returns a manifest entry with stats.
    """
    retailer_id = retailer["id"]
    scraper_type = retailer.get("scraper_type", "")

    if scraper_type != "shopify":
        logger.info(f"  Skipping {retailer_id} — custom scraper not yet implemented")
        return {
            "retailer_id": retailer_id,
            "status": "skipped",
            "reason": "custom scraper not implemented",
        }

    # Get handle mappings for this retailer
    handle_map = get_handles_for_retailer(retailer_id, product_ids)
    if not handle_map:
        logger.info(f"  Skipping {retailer_id} — no product handle mappings defined")
        return {
            "retailer_id": retailer_id,
            "status": "skipped",
            "reason": "no handle mappings",
        }

    logger.info(f"  Scraping {retailer_id}: {len(handle_map)} products")

    if dry_run:
        for product_id, handle in handle_map.items():
            logger.info(f"    [dry-run] {product_id} -> {handle}")
        return {
            "retailer_id": retailer_id,
            "status": "dry_run",
            "products_mapped": len(handle_map),
        }

    # Create scraper and run
    scraper = ShopifyScraper(retailer_id, retailer["url"])
    handles_list = list(handle_map.values())
    product_ids_list = list(handle_map.keys())

    results = scraper.scrape_products(handles_list, product_ids=product_ids_list)

    # Process results
    products_found = 0
    products_error = 0
    products_no_sizes = 0
    prices_collected = 0
    anomalies = []
    price_records = {}

    for i, result in enumerate(results):
        product_id = product_ids_list[i]

        if result.get("error"):
            products_error += 1
            logger.warning(f"    Error: {product_id} — {result['error']}")
            continue

        products_found += 1
        # A product the scraper published but could read no variants from is
        # NOT a successful price read. It is published because "sold out" or
        # "page answered, nothing priceable" is a true fact worth recording,
        # but counting it as a hit let a totally broken retailer report 100%
        # healthy in the plant tracker: all 68 of one nursery's products went
        # through a drifted page and produced products_found=68,
        # prices_collected=0, pipeline_status "healthy".
        if result.get("no_sizes_readable"):
            products_no_sizes += 1
        variants = result.get("variants", {})
        prices_collected += len(variants)

        # Check for anomalies
        anomaly_warnings = check_price_anomaly(product_id, retailer_id, variants, prev_manifest)
        anomalies.extend(anomaly_warnings)
        for w in anomaly_warnings:
            logger.warning(f"    {w}")

        # A large price move is recorded and flagged, NOT discarded.
        #
        # The plant tracker used to gate the write on "no anomalies", which
        # freezes a defect in place: once a wrong price is written, the
        # correction back is itself a >50% move and gets blocked — forever.
        # A real sale also produces a single surprising number, so acting on
        # one is the wrong layer; systemic collapse is a build-time concern.
        price_entry = {
            "retailer_id": retailer_id,
            "retailer_name": result.get("retailer_name", retailer["name"]),
            "timestamp": result["timestamp"],
            "url": result.get("url", ""),
            "variants": variants,
            "in_stock": result.get("in_stock", None),
        }
        if anomaly_warnings:
            # Travels with the row so a reviewer can find it later; the run
            # also reports the count, and the warnings are already logged.
            price_entry["price_anomaly"] = anomaly_warnings
        append_price(product_id, price_entry)

        # Record for manifest
        for tier, data in variants.items():
            price_val = data.get("price", 0) if isinstance(data, dict) else 0
            price_records[f"{product_id}:{retailer_id}"] = price_records.get(
                f"{product_id}:{retailer_id}", {}
            )
            price_records[f"{product_id}:{retailer_id}"][tier] = price_val

    # Check if we got significantly fewer results than expected.
    # products_priced, not products_found: a published no-variant row proves
    # the page was reachable, not that we can still read it.
    expected = len(handle_map)
    products_priced = products_found - products_no_sizes
    if products_priced < expected * 0.8:
        logger.error(
            f"  {retailer_id}: Only {products_priced}/{expected} products yielded "
            f"prices ({products_priced/expected*100:.0f}%; {products_no_sizes} published "
            f"with no readable variants). Possible scraper breakage!"
        )

    return {
        "retailer_id": retailer_id,
        "status": "completed",
        "products_expected": expected,
        "products_found": products_found,
        # products_priced is the honest health input: a published no-variant
        # row proves the page was reachable, not that its variants are still
        # readable.
        "products_no_sizes": products_no_sizes,
        "products_priced": products_priced,
        "products_error": products_error,
        "prices_collected": prices_collected,
        "anomalies": anomalies,
        "price_records": price_records,
    }


def retailer_hit_rate(entry: dict) -> tuple[int, int, float]:
    """(products that yielded prices, expected, ratio) for one manifest entry.

    Counts products_priced, NOT products_found. A product published with no
    readable variants proves the page was reachable, not that we can still
    parse it. Counting those as hits let a completely broken retailer report
    perfect health in the plant tracker (68 empty rows, hit_rate 100%).

    Extracted from run() so it is reachable from tests: while this logic
    was inline over there, two mutants that reverted it survived a
    474-test suite.

    Falls back to products_found for manifest entries written before
    products_priced existed.
    """
    found = entry.get("products_priced")
    if found is None:
        found = entry.get("products_found", 0)
    expected = entry.get("products_expected", 1)
    return found, expected, (found / expected if expected else 1.0)


def find_dead_retailers(this_run_entries: list[dict], prev_manifest: dict) -> list[str]:
    """Retailers that returned ZERO products this run AND the previous run.

    One zero can be flakiness; two consecutive zeros means the retailer is
    dead — bot-blocked, redesigned, or handle-rotted — and silence is how
    the plant tracker served 20-day-old prices without anyone knowing.
    Callers treat a non-empty result as fatal for the run.
    """
    prev_by_rid = {
        e.get("retailer_id"): e
        for e in (prev_manifest or {}).get("retailers", [])
    }
    dead = []
    for entry in this_run_entries:
        if entry.get("status") != "completed":
            continue
        if entry.get("products_expected", 0) > 0 and entry.get("products_found", 0) == 0:
            prev = prev_by_rid.get(entry.get("retailer_id"))
            if prev is not None and prev.get("products_found", 1) == 0:
                dead.append(entry["retailer_id"])
    return dead


def run(retailer_filter: str = None, dry_run: bool = False, skip_promos: bool = False,
        product_filter: list[str] = None):
    """Main scraper orchestrator."""
    logger.info("=" * 60)
    logger.info("Helios — Scraper Run")
    logger.info(f"Time: {datetime.now(timezone.utc).isoformat()}")
    logger.info("=" * 60)

    # Load data. products.json is a LIST of product dicts; `active` is
    # ENFORCED here (unlike the plant tracker, which declared the flag and
    # then scraped inactive products anyway).
    products = load_json(DATA_DIR / "products.json")
    retailers = load_json(DATA_DIR / "retailers.json")
    product_ids = [p["id"] for p in products if p.get("active") is True]

    if product_filter:
        unknown = [pid for pid in product_filter if pid not in product_ids]
        if unknown:
            logger.error(
                f"Unknown or inactive product id(s) in --products: {', '.join(unknown)}"
            )
            sys.exit(1)
        product_ids = [pid for pid in product_ids if pid in product_filter]

    # Filter to active retailers (or specific retailer)
    if retailer_filter:
        active_retailers = [r for r in retailers if r["id"] == retailer_filter]
        if not active_retailers:
            logger.error(f"Retailer '{retailer_filter}' not found in retailers.json")
            sys.exit(1)
    else:
        active_retailers = [r for r in retailers if r.get("active", False)]

    logger.info(f"\n{len(product_ids)} active products, {len(active_retailers)} retailers to scrape\n")

    # Load previous manifest for anomaly detection
    prev_manifest = load_previous_manifest()

    # Scrape each retailer (this run's results)
    this_run_entries = []
    this_run_anomalies = []

    for retailer in active_retailers:
        entry = scrape_retailer(retailer, product_ids, prev_manifest, dry_run)
        this_run_entries.append(entry)
        this_run_anomalies.extend(entry.get("anomalies", []))

    # Merge this run's results into the existing manifest.
    # CI runs each retailer in its own `python -m scrapers.runner --retailer X`
    # invocation, so we must preserve other retailers' entries from the previous
    # manifest rather than overwriting them. On a full run (no --retailer), every
    # active retailer is in this_run_entries and the merge effectively replaces
    # everything anyway.
    # Dry-run entries carry no price_records, so merging them would replace
    # real manifest entries with empties in the printed summary while the
    # disk manifest still holds real prices (red team #2, MINOR-9). Dry runs
    # never write, so merging nothing keeps the summary honest.
    manifest = merge_manifest(prev_manifest, [] if dry_run else this_run_entries)

    if not dry_run:
        with open(MANIFEST_PATH, "w", encoding="utf-8", newline="\n") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        logger.info(f"\nManifest saved to {MANIFEST_PATH}")

    # Summary — log only the retailers this invocation actually scraped
    logger.info(f"\n{'=' * 60}")
    logger.info("SCRAPE SUMMARY")
    logger.info(f"{'=' * 60}")
    this_run_total_prices = 0
    for entry in this_run_entries:
        status = entry.get("status", "unknown")
        rid = entry["retailer_id"]
        if status == "completed":
            found = entry.get("products_found", 0)
            expected = entry.get("products_expected", 0)
            prices = entry.get("prices_collected", 0)
            this_run_total_prices += prices
            errors = entry.get("products_error", 0)
            pct = (found / expected * 100) if expected else 0
            flag = " !!!" if pct < 80 else ""
            logger.info(f"  {rid}: {found}/{expected} products ({pct:.0f}%), {prices} prices, {errors} errors{flag}")
        elif status == "skipped":
            logger.info(f"  {rid}: SKIPPED — {entry.get('reason', '')}")
        elif status == "dry_run":
            logger.info(f"  {rid}: DRY RUN — {entry.get('products_mapped', 0)} mapped")

    if this_run_anomalies:
        logger.warning(f"\n{len(this_run_anomalies)} PRICE ANOMALIES DETECTED:")
        for a in this_run_anomalies:
            logger.warning(f"  {a}")

    logger.info(f"\nTotal prices collected (this run): {this_run_total_prices}")
    logger.info(f"Total prices in manifest (all retailers): {manifest['total_prices_collected']}")

    # Promo code scraping — runs after price scraping, only on full runs
    if not retailer_filter and not skip_promos:
        scrape_promos(retailers, dry_run=dry_run)
    elif retailer_filter:
        logger.info("\nPromo scraping skipped (single-retailer run)")

    # Check per-retailer health — degraded retailers are flagged, not fatal.
    # Compute fresh health only for retailers we scraped this invocation;
    # retailers already in the merged manifest from a previous run keep
    # whatever health status they had. This matters for partial (CI) runs
    # where only one retailer is scraped at a time.
    for entry in this_run_entries:
        if entry.get("status") == "completed":
            found, expected, hit_rate = retailer_hit_rate(entry)
            if hit_rate < 0.8:
                entry["health"] = "degraded"
                logger.warning(
                    f"  DEGRADED: {entry['retailer_id']} at {hit_rate:.0%} hit rate "
                    f"— flagged for manual handle review"
                )
            else:
                entry["health"] = "healthy"

    # Pipeline-level health status reflects ALL retailers in the merged manifest,
    # not just the ones scraped this run.
    degraded_retailers = [
        e["retailer_id"] for e in manifest["retailers"]
        if e.get("health") == "degraded"
    ]
    manifest["degraded_retailers"] = degraded_retailers
    manifest["pipeline_status"] = "degraded" if degraded_retailers else "healthy"

    if not dry_run:
        # Always re-save so pipeline_status/degraded_retailers reflect current state.
        # The earlier save (before health computation) is kept as a safety net in
        # case health computation crashes — we still want something on disk.
        with open(MANIFEST_PATH, "w", encoding="utf-8", newline="\n") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)

    if degraded_retailers:
        logger.warning(
            f"\nPIPELINE DEGRADED: {len(degraded_retailers)} retailer(s) below 80% threshold: "
            f"{', '.join(degraded_retailers)}"
        )
        logger.warning("Degraded retailers need manual handle review — pipeline continues.")
    else:
        logger.info("\nAll retailers healthy.")

    # A retailer at zero products for two consecutive runs is dead, not
    # flaky. Exit non-zero so the CI step goes red and someone is told —
    # the manifest above is already saved, so state is not lost.
    dead = find_dead_retailers(this_run_entries, prev_manifest)
    if dead and not dry_run:
        logger.error(
            f"\nFATAL: retailer(s) returned 0 products for 2+ consecutive runs: "
            f"{', '.join(dead)} — bot-block, redesign, or handle rot. "
            f"Fix or deactivate; this exit is the alarm."
        )
        sys.exit(2)

    return manifest


def parse_products_arg(value: str | None) -> list[str] | None:
    """Parse the --products flag. None means "no filter".

    A provided-but-empty value (`--products ""` or `--products " , "`) is
    an operator error, and treating a falsy filter as "no filter" turned
    it into a FULL crawl — the opposite of what a limiting flag was asked
    to do (red team #2, MINOR-6). Exit 1 instead of guessing.
    """
    if value is None:
        return None
    product_filter = [p.strip() for p in value.split(",") if p.strip()]
    if not product_filter:
        logger.error(
            "--products was provided but contains no product ids — "
            "refusing to fall back to a full crawl"
        )
        sys.exit(1)
    return product_filter


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Helios price scrapers")
    parser.add_argument("--retailer", type=str, help="Scrape a specific retailer only")
    parser.add_argument("--products", type=str,
                        help="Comma-separated product ids to scrape (subset of the active catalog)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be scraped without scraping")
    parser.add_argument("--skip-promos", action="store_true", help="Skip promo code scraping this run")
    args = parser.parse_args()

    run(retailer_filter=args.retailer, dry_run=args.dry_run,
        skip_promos=args.skip_promos,
        product_filter=parse_products_arg(args.products))

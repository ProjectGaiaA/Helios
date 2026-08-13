"""
Shared scraper types and helpers.

Holds the pieces of the plant tracker's recovery system that shopify.py
cannot run without: the FetchResult return type, the pure URL-to-handle
helper, and log-only stubs for the two recovery recorders. The full
recovery system (recovery.json state, candidate review, discovery) is
Phase B — the stubs keep the 301/404 branches importable and observable
without writing any state.
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class FetchResult:
    """Result of an HTTP fetch with status code and redirect info.

    Replaces the raw dict|None return of _get_json() so callers can
    distinguish 404 (handle changed) from 5xx (server hiccup) from
    redirect (handle renamed with a redirect in place).
    """

    data: dict | None
    status_code: int | None
    redirect_url: str | None


def record_broken(retailer_id: str, product_id: str, old_handle: str) -> None:
    """Log-only stub: a handle returned 404.

    Phase B replaces this with recovery.json state tracking. Logging now
    means the manifest's error counts have a paper trail to grep when a
    retailer degrades.
    """
    logger.info(
        f"Recovery stub: broken handle {retailer_id}/{product_id} ({old_handle})"
    )


def record_redirect_candidate(
    retailer_id: str,
    product_id: str,
    old_handle: str,
    new_handle: str,
    redirect_url: str,
) -> None:
    """Log-only stub: a handle 301/302'd to a new one.

    Phase B replaces this with recovery.json candidate tracking. The
    scraper already follows the redirect for this run's data; the log
    line is the only record that the mapping should be updated.
    """
    logger.info(
        f"Recovery stub: redirect candidate {retailer_id}/{product_id} "
        f"{old_handle} -> {new_handle} ({redirect_url})"
    )


def extract_handle_from_url(url: str) -> str | None:
    """Extract a Shopify product handle from a URL.

    Examples:
        https://shop.com/products/new-handle.json -> new-handle
        https://shop.com/products/new-handle -> new-handle
        /products/new-handle.json -> new-handle
    """
    # Strip query params and fragment
    path = url.split("?")[0].split("#")[0]
    # Find /products/HANDLE pattern
    parts = path.split("/products/")
    if len(parts) < 2:
        return None
    handle = parts[-1].strip("/")
    # Remove .json suffix
    if handle.endswith(".json"):
        handle = handle[:-5]
    return handle if handle else None

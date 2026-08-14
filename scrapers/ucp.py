"""
UCP (Universal Commerce Protocol) catalog client — fixture-tested, O5-gated.

Shopify stores expose an unauthenticated UCP/MCP JSON-RPC endpoint at
`{store}/api/ucp/mcp`, advertised via `/.well-known/ucp` (which may point at
a different canonical host, e.g. wild-oak-trail's endpoint lives on its
`.myshopify.com` host — robots must be checked against the host actually
called). `tools/list` is a plain POST with no initialize, no session, no
meta (C18).

This module wraps CATALOG READS ONLY: search_catalog, lookup_catalog,
get_product. The same endpoint hosts ten checkout/cart/order tools; those
are NEVER wrapped (section 0: checkout is for humans), and `checkout_url`
values inside responses are never followed.

O5 GATE. `tools/call` requires the agent profile in BOTH places at once
(gaia's UCP_API_RUNBOOK.md section 3, verified live against merchants):
the JSON field `params.arguments.meta["ucp-agent"].profile` — NOT
params._meta, NOT params.meta, NOT arguments._meta; eight wrong
placements all fail with "Missing profile uri" — AND the HTTP header
`UCP-Agent: profile="<url>"`. Meta without the header is a bare HTTP 422
with NO diagnostic body. Without a hosted profile the store answers 422,
so a profile-less client is uncallable BY DESIGN: every tools/call
wrapper raises UcpNotActivated before any HTTP. tools/list succeeding
does NOT imply tools/call will (C18) — never infer activation from it.

PROFILE IDENTITY. Helios will host its OWN profile (O5) — never borrow
gaia's plantpricetracker.com profile URL: reputation and merchant
blocking attach to the profile, and the two projects must not share
fate. UCP_AGENT_PROFILE stays unset until the Helios profile is live.

RATE. The runbook's original "ceiling unknown, no throttling observed"
was RETRACTED (UCP_API_RUNBOOK.md "Rate limits — CORRECTED 2026-08-13"):
planting-tree refuses after ~93 calls with HTTP 429 / -32000 "Too many
requests, please retry after 1933 seconds" — a 32-minute lockout — and a
naive retry policy burned 42 refused requests into it. This client is
sequential with a hard >=1.5s inter-call gap, and on 429/503 it raises
UcpRateLimited carrying the advertised retry-after and NEVER retries:
abandon the store for the session.

MONEY. UCP amounts are integer MINOR UNITS with ISO 4217 currency (C19):
{"amount": 600, "currency": "USD"} is $6.00. All comparisons happen in
integer cents; dollars from JSONL/site convert through Decimal, never
float equality. Non-USD is not converted here — callers classify those
triples UNRESOLVED and withhold.

OUTPUT SHAPE. The response parsing in variants_from_result() is
EXAMPLE-DERIVED: input schemas were captured live by red team #3, but the
profile gate blocked live output capture, so the shape comes from
Shopify's published example (C21). Re-verify against a live response the
day O5 unblocks; a schema surprise raises UcpSchemaError, which callers
must classify UNRESOLVED.
"""

import json
import logging
import os
import re
import time
from decimal import Decimal, InvalidOperation

import requests

from scrapers.polite import BOT_USER_AGENT, is_allowed_by_robots, log_request

logger = logging.getLogger(__name__)

UCP_API_VERSION = "2026-04-08"
UCP_PROFILE_ENV = "UCP_AGENT_PROFILE"

# The three catalog tools are the ONLY tools this module may call.
CATALOG_TOOLS = ("search_catalog", "lookup_catalog", "get_product")

# Always sent: an unpinned context lets the store guess buyer locale and
# currency, and a silent currency guess is exactly the kind of ambient
# input that makes two runs incomparable.
DEFAULT_CONTEXT = {"address_country": "US", "currency": "USD"}


class UcpError(Exception):
    """Base for all UCP client errors."""


class UcpNotActivated(UcpError):
    """tools/call attempted without a hosted agent profile (O5 open)."""


class UcpTransportError(UcpError):
    """Network failure, or a non-200 without a parseable JSON-RPC body."""


class UcpRpcError(UcpError):
    """Non-success WITH a JSON-RPC error body.

    The profile gate returns HTTP 422 + a diagnostic body — parse BEFORE
    any status check, never raise_for_status first (C18): the body's
    data.code (e.g. "invalid_profile_url") is the actionable part.
    """

    def __init__(self, http_status, code, message, data_code=None):
        super().__init__(f"JSON-RPC error {code} (HTTP {http_status}): {message}"
                         + (f" [{data_code}]" if data_code else ""))
        self.http_status = http_status
        self.code = code
        self.message = message
        self.data_code = data_code


class UcpToolError(UcpError):
    """HTTP 200 but result.isError is true (tool-level failure)."""


class UcpSchemaError(UcpError):
    """Response shape violates the (example-derived) expected schema."""


class UcpCurrencyError(UcpError):
    """Amount in a currency this client does not convert (non-USD)."""

    def __init__(self, currency):
        super().__init__(f"non-USD currency: {currency!r} — classify UNRESOLVED")
        self.currency = currency


class UcpRateLimited(UcpError):
    """HTTP 429/503 — hard stop, NEVER retried (runbook rate correction).

    Carries the store's advertised retry-after seconds when present so a
    caller can schedule a future session; retrying now burns requests
    into a lockout (measured: 42 wasted requests on planting-tree).
    """

    def __init__(self, http_status, retry_after=None):
        super().__init__(
            f"HTTP {http_status} rate limited"
            + (f", retry after {retry_after}s" if retry_after else "")
            + " — abandoning this store for the session (no retry)"
        )
        self.http_status = http_status
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# Money — integer cents everywhere, Decimal for dollar conversion
# ---------------------------------------------------------------------------

def dollars_to_cents(value) -> int:
    """569.0 / "1470.99" / 6 -> integer cents, exactly.

    Decimal(str(...)) sidesteps float representation error: 1470.99 * 100
    in floats is 147098.99999999997, and int() of that loses a cent.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError(f"not a money value: {value!r}")
    try:
        cents = Decimal(str(value)) * 100
    except InvalidOperation as e:
        raise ValueError(f"not a money value: {value!r}") from e
    # Infinity round-trips through json.loads (non-standard but accepted
    # by the default parser), and Decimal('Infinity') survives to int()
    # as an OverflowError deep in a caller (red team #4, MINOR-14).
    # NaN never equals anything, so it needs the same explicit gate.
    if not cents.is_finite():
        raise ValueError(f"non-finite money value: {value!r}")
    if cents != cents.to_integral_value():
        raise ValueError(f"sub-cent money value: {value!r}")
    return int(cents)


def minor_units_to_cents(amount, currency) -> int:
    """UCP minor units -> cents. USD only; USD minor units ARE cents.

    {"amount": 600, "currency": "USD"} is $6.00 -> 600 cents (C19). The
    amount must be an int — a float or string amount is a schema surprise,
    not something to coerce. Non-USD raises UcpCurrencyError: minor-unit
    exponents differ per currency and guessing one fabricates a price.
    """
    if (currency or "").upper() != "USD":
        raise UcpCurrencyError(currency)
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise UcpSchemaError(f"minor-unit amount must be int, got {amount!r}")
    return amount


# ---------------------------------------------------------------------------
# Shopify gids
# ---------------------------------------------------------------------------

def gid_to_id(gid: str) -> str:
    """'gid://shopify/ProductVariant/41679254454412' -> '41679254454412'."""
    if not isinstance(gid, str) or "gid://shopify/" not in gid:
        raise UcpSchemaError(f"not a shopify gid: {gid!r}")
    tail = gid.rsplit("/", 1)[-1]
    if not tail.isdigit():
        raise UcpSchemaError(f"gid has non-numeric id: {gid!r}")
    return tail


def variant_gid(variant_id) -> str:
    return f"gid://shopify/ProductVariant/{variant_id}"


def product_gid(product_id) -> str:
    return f"gid://shopify/Product/{product_id}"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_UNSET = object()


class UcpClient:
    """JSON-RPC client for one store's UCP catalog tools."""

    # Hard minimum gap between ANY two HTTP calls from one client
    # (runbook rate correction: sequential, >=1.5s, never probe ceilings).
    MIN_CALL_GAP_SECONDS = 1.5

    def __init__(self, store_url: str, profile_url=_UNSET, session=None,
                 timeout: int = 20):
        self.store_url = store_url.rstrip("/")
        if profile_url is _UNSET:
            profile_url = os.environ.get(UCP_PROFILE_ENV) or None
        self.profile_url = profile_url
        self.session = session or requests.Session()
        # Honest bot identity on EVERY request this client makes —
        # a bare requests.Session default UA on the well-known fetch
        # reintroduced the no-robots/no-identity defect class that
        # scrape_promo_codes was deleted for (red team #4, MINOR-11).
        self.session.headers.update({"User-Agent": BOT_USER_AGENT})
        self.timeout = timeout
        self._endpoint: str | None = None
        self._rpc_id = 0
        self._last_call_monotonic: float | None = None
        # Version header from the last response; a change of meaning-pin
        # is detected by version_mismatch() and must raise a
        # SCHEMA_REVERIFY notice in the caller's report (C22).
        self.api_version: str | None = None

    def _throttle(self):
        """Enforce the sequential >=1.5s inter-call gap."""
        if self._last_call_monotonic is not None:
            elapsed = time.monotonic() - self._last_call_monotonic
            if elapsed < self.MIN_CALL_GAP_SECONDS:
                time.sleep(self.MIN_CALL_GAP_SECONDS - elapsed)
        self._last_call_monotonic = time.monotonic()

    # -- endpoint resolution ------------------------------------------------

    def resolve_endpoint(self) -> str:
        """Endpoint from /.well-known/ucp, else {store}/api/ucp/mcp.

        The well-known document may advertise a different canonical host
        (wild-oak-trail -> wild-oak-trail.myshopify.com). Cached per client.
        """
        if self._endpoint:
            return self._endpoint
        well_known_url = f"{self.store_url}/.well-known/ucp"
        endpoint = None
        if not is_allowed_by_robots(well_known_url):
            logger.warning(f"robots.txt disallows {well_known_url} — using fallback endpoint")
        else:
            self._throttle()
            try:
                resp = self.session.get(well_known_url, timeout=self.timeout)
                log_request(well_known_url, status_code=resp.status_code)
                if resp.status_code == 200:
                    endpoint = _endpoint_from_well_known(resp.json())
            except (requests.RequestException, ValueError) as e:
                logger.warning(f"/.well-known/ucp unusable for {self.store_url}: {e}")
        self._endpoint = endpoint or f"{self.store_url}/api/ucp/mcp"
        return self._endpoint

    # -- transport ----------------------------------------------------------

    def _headers(self, with_agent_header: bool = False) -> dict:
        headers = {
            "Content-Type": "application/json",
            # Responses are application/json today, but the endpoint is an
            # MCP surface — advertise both accepted types (C18).
            "Accept": "application/json, text/event-stream",
            "x-shopify-ucp-mcp-api-version": UCP_API_VERSION,
        }
        if with_agent_header and self.profile_url:
            # BOTH this header AND arguments.meta are required on
            # tools/call; meta alone gets a bare 422 with no diagnostic
            # (UCP_API_RUNBOOK.md section 3, found by removing the header
            # from a working call).
            headers["UCP-Agent"] = f'profile="{self.profile_url}"'
        return headers

    def _post_rpc(self, method: str, params: dict | None,
                  with_agent_header: bool = False) -> dict:
        """POST one JSON-RPC message, applying the error taxonomy.

        Order matters: parse the body BEFORE any status-based raise. The
        profile gate answers 422 WITH a JSON-RPC diagnostic body; a
        raise_for_status-first client throws that diagnostic away (C18).
        """
        endpoint = self.resolve_endpoint()
        # Robots is checked against the host ACTUALLY called — the
        # well-known document may have moved us to another host.
        if not is_allowed_by_robots(endpoint):
            raise UcpTransportError(f"robots.txt disallows {endpoint}")
        self._rpc_id += 1
        payload = {"jsonrpc": "2.0", "id": self._rpc_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._throttle()
        try:
            resp = self.session.post(
                endpoint, json=payload,
                headers=self._headers(with_agent_header=with_agent_header),
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise UcpTransportError(f"POST {endpoint} failed: {e}") from e
        log_request(endpoint, status_code=resp.status_code)
        self.api_version = resp.headers.get("x-shopify-ucp-mcp-api-version")

        # 429/503 is a HARD STOP before anything else: honoring the store's
        # lockout means surfacing retry-after and NOT retrying — a naive
        # retry policy burned 42 refused requests on planting-tree
        # (runbook rate correction).
        if resp.status_code in (429, 503):
            raise UcpRateLimited(
                resp.status_code, retry_after=_retry_after_seconds(resp))

        try:
            body = resp.json()
        except ValueError as e:
            if resp.status_code == 422:
                # Decoder rule (UCP_API_RUNBOOK.md section 3): a bare 422
                # with NO diagnostic body usually means the UCP-Agent
                # HTTP header is missing — suspect that first.
                raise UcpTransportError(
                    f"bare HTTP 422 without diagnostic body from {endpoint} "
                    f"— suspect a missing UCP-Agent header first"
                ) from e
            raise UcpTransportError(
                f"HTTP {resp.status_code} with non-JSON body from {endpoint}"
            ) from e

        if isinstance(body, dict) and "error" in body:
            err = body["error"] or {}
            data = err.get("data") or {}
            raise UcpRpcError(
                http_status=resp.status_code,
                code=err.get("code"),
                message=err.get("message", ""),
                data_code=data.get("code"),
            )
        if resp.status_code != 200:
            raise UcpTransportError(
                f"HTTP {resp.status_code} without JSON-RPC error body"
            )
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict):
            raise UcpSchemaError("response has no result object")
        if result.get("isError"):
            raise UcpToolError(_tool_error_text(result))
        return result

    # -- tools --------------------------------------------------------------

    def tools_list(self) -> list[dict]:
        """tools/list — ungated (no envelope, no profile), plain JSON-RPC."""
        result = self._post_rpc("tools/list", None)
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise UcpSchemaError("tools/list result has no tools[] list")
        return tools

    def _call_tool(self, name: str, catalog: dict) -> dict:
        if name not in CATALOG_TOOLS:
            # Checkout/cart/order tools live on this endpoint too. This
            # client refuses them by construction, not by convention.
            raise ValueError(f"tool {name!r} is not a catalog read — refusing")
        if not self.profile_url:
            raise UcpNotActivated(
                "tools/call requires a hosted agent profile (O5, unresolved). "
                f"Set {UCP_PROFILE_ENV} to the HELIOS-hosted profile URL once "
                "it is live — never gaia's plantpricetracker.com profile: "
                "reputation and merchant blocking attach to the profile."
            )
        # Profile placement is EXACT (runbook section 3): arguments.meta,
        # not params._meta / params.meta / arguments._meta — eight wrong
        # placements all fail with "Missing profile uri".
        params = {
            "name": name,
            "arguments": {
                "meta": {"ucp-agent": {"profile": self.profile_url}},
                "catalog": catalog,
            },
        }
        return self._post_rpc("tools/call", params, with_agent_header=True)

    def lookup_catalog(self, ids: list[str], context: dict | None = None,
                       filters: dict | None = None) -> dict:
        """Batch lookup by Product or ProductVariant gid (1-10 ids).

        TRAP (C21): catalog.filters.available DEFAULTS TRUE server-side —
        an unfiltered lookup HIDES sold-out variants. A variant absent
        from the response therefore means "hidden or gone", which callers
        must classify UNRESOLVED, never drift. Once O5 unblocks, probe the
        known sold-out DELTA Max variant (gid 41674486349964) both ways
        once and pin the observed behavior here.
        """
        if not 1 <= len(ids) <= 10:
            raise ValueError(f"lookup_catalog takes 1-10 ids, got {len(ids)}")
        for gid in ids:
            if not (isinstance(gid, str) and (
                    gid.startswith("gid://shopify/Product/")
                    or gid.startswith("gid://shopify/ProductVariant/"))):
                raise ValueError(f"not a Product/ProductVariant gid: {gid!r}")
        catalog: dict = {"ids": list(ids), "context": {**DEFAULT_CONTEXT, **(context or {})}}
        if filters is not None:
            catalog["filters"] = filters
        return self._call_tool("lookup_catalog", catalog)

    def get_product(self, product_id: str, context: dict | None = None,
                    filters: dict | None = None) -> dict:
        """Single product by Product gid. NO handle lookup exists (C20)."""
        if not (isinstance(product_id, str)
                and product_id.startswith("gid://shopify/Product/")):
            raise ValueError(f"get_product takes a Product gid, got {product_id!r}")
        catalog: dict = {"id": product_id, "context": {**DEFAULT_CONTEXT, **(context or {})}}
        if filters is not None:
            catalog["filters"] = filters
        return self._call_tool("get_product", catalog)

    def search_catalog(self, query: str, context: dict | None = None,
                       filters: dict | None = None) -> dict:
        catalog: dict = {"query": query, "context": {**DEFAULT_CONTEXT, **(context or {})}}
        if filters is not None:
            catalog["filters"] = filters
        return self._call_tool("search_catalog", catalog)


def version_mismatch(client: UcpClient) -> bool:
    """True when the store answered with a different schema version pin.

    Schema meaning is pinned to 2026-04-08 (C22); a different answer means
    parsing assumptions may be stale — callers raise a SCHEMA_REVERIFY
    notice, they do not guess.
    """
    return client.api_version is not None and client.api_version != UCP_API_VERSION


# ---------------------------------------------------------------------------
# Response parsing (EXAMPLE-DERIVED — see module docstring)
# ---------------------------------------------------------------------------

def variants_from_result(result: dict) -> dict[str, dict]:
    """{variant_id: {sku, amount, currency, available, status, running_low,
    title, product_id}} from a catalog tool result.

    Money is left as (amount, currency) — conversion to cents happens at
    the comparison site via minor_units_to_cents so a single non-USD
    variant poisons only its own triple (UNRESOLVED), not the whole parse.
    checkout_url is present in real responses and deliberately not
    extracted: nothing in this codebase may follow it (section 0).
    """
    sc = result.get("structuredContent")
    if not isinstance(sc, dict) or not isinstance(sc.get("products"), list):
        raise UcpSchemaError("result.structuredContent.products[] missing")
    out: dict[str, dict] = {}
    for product in sc["products"]:
        if not isinstance(product, dict):
            raise UcpSchemaError("non-object product entry")
        pid = gid_to_id(product.get("id"))
        variants = product.get("variants")
        if not isinstance(variants, list):
            raise UcpSchemaError(f"product {pid}: variants[] missing")
        for v in variants:
            if not isinstance(v, dict):
                raise UcpSchemaError(f"product {pid}: non-object variant")
            vid = gid_to_id(v.get("id"))
            price = v.get("price")
            if not isinstance(price, dict) or "amount" not in price:
                raise UcpSchemaError(f"variant {vid}: price{{amount}} missing")
            availability = v.get("availability")
            if not isinstance(availability, dict):
                raise UcpSchemaError(f"variant {vid}: availability{{}} missing")
            # list_price (compare-at) IS returned by 4 of 6 gaia retailers
            # (runbook section 4, corrected 2026-08-13 — the original "no
            # compare-at on this path" was an FGT/planting-tree-only result
            # wrongly generalised). Extracting it restores a was-price
            # freshness comparison when UCP becomes the arbiter. None-safe:
            # stores that omit it simply cannot have was-price verified.
            list_price = v.get("list_price")
            if not isinstance(list_price, dict):
                list_price = None
            out[vid] = {
                "product_id": pid,
                "sku": v.get("sku") or None,
                "title": v.get("title", ""),
                "amount": price.get("amount"),
                "currency": price.get("currency"),
                "list_amount": list_price.get("amount") if list_price else None,
                "list_currency": list_price.get("currency") if list_price else None,
                "available": availability.get("available"),
                "status": availability.get("status"),
                "running_low": availability.get("running_low"),
            }
    return out


def _endpoint_from_well_known(doc) -> str | None:
    """Find an /api/ucp/mcp endpoint URL anywhere in the well-known doc.

    Shape is walked defensively: the document layout is example-derived,
    but the one invariant red team #3 confirmed live is that the endpoint
    string ends with /api/ucp/mcp and may sit on a different host.
    """
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            if node.startswith("https://") and node.rstrip("/").endswith("/api/ucp/mcp"):
                found.append(node.rstrip("/"))

    walk(doc)
    return found[0] if found else None


def _tool_error_text(result: dict) -> str:
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            return str(item.get("text", ""))[:500]
    return json.dumps(result)[:500]


def _retry_after_seconds(resp) -> int | None:
    """Advertised lockout length: Retry-After header, else the JSON-RPC
    message payload ("... retry after 1933 seconds", runbook-observed)."""
    header = resp.headers.get("Retry-After")
    if header and str(header).isdigit():
        return int(header)
    try:
        body = resp.json()
        message = str((body.get("error") or {}).get("message", ""))
    except (ValueError, AttributeError):
        return None
    m = re.search(r"retry after (\d+)", message, re.IGNORECASE)
    return int(m.group(1)) if m else None

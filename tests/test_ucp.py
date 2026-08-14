"""Tests for the UCP catalog client (scrapers/ucp.py) — fixtures only.

NO live UCP calls exist anywhere in this suite: tools/call is profile-gated
(C18) and O5 is open, so every HTTP interaction here is a `responses` mock.
The zero-socket test proves that mechanically.
"""

import json
import socket

import pytest
import responses

from tests.conftest import load_fixture
from scrapers import ucp
from scrapers.ucp import (
    UcpClient,
    UcpCurrencyError,
    UcpNotActivated,
    UcpRateLimited,
    UcpRpcError,
    UcpSchemaError,
    UcpToolError,
    UcpTransportError,
    dollars_to_cents,
    gid_to_id,
    minor_units_to_cents,
    product_gid,
    variant_gid,
    variants_from_result,
    version_mismatch,
)

STORE = "https://shopsolarkits.com"
ENDPOINT = f"{STORE}/api/ucp/mcp"
PROFILE = "https://example.com/helios-agent-profile.json"


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """The client's >=1.5s throttle must not slow the suite down."""
    monkeypatch.setattr("time.sleep", lambda s: None)


def _client(profile=PROFILE):
    return UcpClient(STORE, profile_url=profile)


def _add_well_known_404():
    responses.add(responses.GET, f"{STORE}/.well-known/ucp", status=404)


# --- money: minor units and cents (C19) ---


def test_minor_units_are_cents_for_usd():
    # {"amount": 600, "currency": "USD"} is $6.00 -> 600 cents
    assert minor_units_to_cents(600, "USD") == 600
    assert minor_units_to_cents(56900, "USD") == 56900


def test_minor_units_non_usd_raises_currency_error():
    with pytest.raises(UcpCurrencyError) as e:
        minor_units_to_cents(600, "CAD")
    assert e.value.currency == "CAD"
    with pytest.raises(UcpCurrencyError):
        minor_units_to_cents(600, None)


def test_minor_units_must_be_int():
    """A float or string amount is a schema surprise, never coerced."""
    with pytest.raises(UcpSchemaError):
        minor_units_to_cents(600.0, "USD")
    with pytest.raises(UcpSchemaError):
        minor_units_to_cents("600", "USD")
    with pytest.raises(UcpSchemaError):
        minor_units_to_cents(True, "USD")


def test_dollars_to_cents_is_exact():
    assert dollars_to_cents(569.0) == 56900
    # float 1470.99 * 100 = 147098.99999999997 — Decimal(str()) avoids it
    assert dollars_to_cents(1470.99) == 147099
    assert dollars_to_cents("899.00") == 89900
    assert dollars_to_cents(6) == 600


def test_dollars_to_cents_rejects_junk():
    with pytest.raises(ValueError):
        dollars_to_cents(None)
    with pytest.raises(ValueError):
        dollars_to_cents("N/A")
    with pytest.raises(ValueError):
        dollars_to_cents(1.005)  # sub-cent


def test_dollars_to_cents_rejects_non_finite():
    """Infinity round-trips through json.loads (red team #4, MINOR-14) —
    it must raise ValueError here, never OverflowError deep in a caller."""
    with pytest.raises(ValueError):
        dollars_to_cents(float("inf"))
    with pytest.raises(ValueError):
        dollars_to_cents(float("-inf"))
    with pytest.raises(ValueError):
        dollars_to_cents(float("nan"))
    assert json.loads("Infinity") == float("inf")  # the round-trip is real


# --- gid helpers ---


def test_gid_roundtrip():
    assert gid_to_id("gid://shopify/ProductVariant/41679254454412") == "41679254454412"
    assert gid_to_id("gid://shopify/Product/7296500433036") == "7296500433036"
    assert variant_gid(41679254454412) == "gid://shopify/ProductVariant/41679254454412"
    assert product_gid("7296500433036") == "gid://shopify/Product/7296500433036"


def test_gid_rejects_junk():
    with pytest.raises(UcpSchemaError):
        gid_to_id("https://example.com/41679254454412")
    with pytest.raises(UcpSchemaError):
        gid_to_id("gid://shopify/ProductVariant/not-a-number")
    with pytest.raises(UcpSchemaError):
        gid_to_id(None)


# --- O5 gate: no profile, no HTTP ---


def test_tools_call_without_profile_raises_before_any_http():
    """The profile gate means a profile-less call CANNOT succeed (C18).

    No responses are registered: if the client attempted HTTP, the test
    would fail with a connection error, not UcpNotActivated.
    """
    client = _client(profile=None)
    with pytest.raises(UcpNotActivated):
        client.lookup_catalog([variant_gid(41679254454412)])
    with pytest.raises(UcpNotActivated):
        client.get_product(product_gid(7296500433036))
    with pytest.raises(UcpNotActivated):
        client.search_catalog("river 2 pro")


def test_profile_defaults_from_env(monkeypatch):
    monkeypatch.setenv(ucp.UCP_PROFILE_ENV, "https://example.com/p.json")
    assert UcpClient(STORE).profile_url == "https://example.com/p.json"
    monkeypatch.delenv(ucp.UCP_PROFILE_ENV)
    assert UcpClient(STORE).profile_url is None


def test_non_catalog_tools_are_refused_even_with_profile():
    """Checkout/cart/order tools share the endpoint and are OFF-LIMITS
    (section 0) — refused by construction, before the O5 gate check."""
    client = _client()
    with pytest.raises(ValueError):
        client._call_tool("checkout_create", {})
    with pytest.raises(ValueError):
        client._call_tool("cart_update", {})


# --- endpoint resolution ---


@responses.activate
def test_endpoint_falls_back_when_well_known_unusable():
    _add_well_known_404()
    client = _client()
    assert client.resolve_endpoint() == ENDPOINT


@responses.activate
def test_endpoint_resolves_canonical_host_from_well_known():
    """wild-oak-trail advertises its endpoint on the .myshopify.com host."""
    fixture = load_fixture("ucp", "well_known_wild_oak_trail.json")
    responses.add(
        responses.GET, "https://www.wildoaktrail.com/.well-known/ucp",
        json=fixture, status=200,
    )
    client = UcpClient("https://www.wildoaktrail.com", profile_url=PROFILE)
    assert client.resolve_endpoint() == "https://wild-oak-trail.myshopify.com/api/ucp/mcp"


# --- tools/list ---


@responses.activate
def test_tools_list_plain_jsonrpc_and_version_capture():
    _add_well_known_404()
    responses.add(
        responses.POST, ENDPOINT,
        json=load_fixture("ucp", "tools_list.json"),
        status=200,
        headers={"x-shopify-ucp-mcp-api-version": "2026-04-08"},
    )
    client = _client(profile=None)  # tools/list is ungated
    tools = client.tools_list()

    assert len(tools) == 13
    names = {t["name"] for t in tools}
    assert {"search_catalog", "lookup_catalog", "get_product"} <= names

    # Plain JSON-RPC: no initialize, no session, no meta (C18)
    req = json.loads(responses.calls[-1].request.body)
    assert req["method"] == "tools/list"
    assert "params" not in req
    # Version + Accept headers per C18/C22
    sent = responses.calls[-1].request.headers
    assert sent["x-shopify-ucp-mcp-api-version"] == "2026-04-08"
    assert sent["Accept"] == "application/json, text/event-stream"
    # tools/list is ungated: no UCP-Agent header (profile-less client)
    assert "UCP-Agent" not in sent

    assert client.api_version == "2026-04-08"
    assert version_mismatch(client) is False


@responses.activate
def test_version_change_is_detectable():
    _add_well_known_404()
    responses.add(
        responses.POST, ENDPOINT,
        json=load_fixture("ucp", "tools_list.json"), status=200,
        headers={"x-shopify-ucp-mcp-api-version": "2027-01-01"},
    )
    client = _client(profile=None)
    client.tools_list()
    assert version_mismatch(client) is True  # caller raises SCHEMA_REVERIFY


# --- lookup_catalog happy path against real captured data ---


@responses.activate
def test_lookup_catalog_envelope_and_parse():
    _add_well_known_404()
    responses.add(
        responses.POST, ENDPOINT,
        json=load_fixture("ucp", "lookup_catalog_river.json"), status=200,
    )
    client = _client()
    result = client.lookup_catalog(
        [variant_gid(41679254454412), variant_gid(41737132769420)]
    )

    # Envelope shape (C18/C21 + runbook section 3): the profile goes in
    # params.arguments.meta["ucp-agent"].profile — no other placement works
    req = json.loads(responses.calls[-1].request.body)
    assert req["method"] == "tools/call"
    assert req["params"]["name"] == "lookup_catalog"
    args = req["params"]["arguments"]
    assert args["meta"]["ucp-agent"]["profile"] == PROFILE
    assert "_meta" not in req["params"] and "meta" not in req["params"]
    assert "_meta" not in args
    # BOTH the meta field AND the UCP-Agent HTTP header are required on
    # tools/call (meta alone = bare 422 with no diagnostic, runbook)
    sent = responses.calls[-1].request.headers
    assert sent["UCP-Agent"] == f'profile="{PROFILE}"'
    assert args["catalog"]["ids"] == [
        "gid://shopify/ProductVariant/41679254454412",
        "gid://shopify/ProductVariant/41737132769420",
    ]
    # context always pins country + currency
    assert args["catalog"]["context"]["address_country"] == "US"
    assert args["catalog"]["context"]["currency"] == "USD"

    variants = variants_from_result(result)
    unit = variants["41679254454412"]
    assert unit["sku"] == "ZMR620-B-US"
    assert minor_units_to_cents(unit["amount"], unit["currency"]) == 56900
    assert unit["available"] is True
    bundle = variants["41737132769420"]
    assert bundle["sku"] == "RIVER2PRO-160-1-US"
    assert minor_units_to_cents(bundle["amount"], bundle["currency"]) == 89900

    # checkout_url is present in responses and never followed: the only
    # HTTP performed was well-known + one POST to the endpoint.
    urls = [c.request.url for c in responses.calls]
    assert not any("/cart/" in u for u in urls)


@responses.activate
def test_lookup_catalog_validates_ids():
    client = _client()
    with pytest.raises(ValueError):
        client.lookup_catalog([])
    with pytest.raises(ValueError):
        client.lookup_catalog([variant_gid(i) for i in range(11)])
    with pytest.raises(ValueError):
        client.lookup_catalog(["41679254454412"])  # bare id, not a gid


def test_get_product_requires_product_gid():
    client = _client()
    with pytest.raises(ValueError):
        client.get_product(variant_gid(41679254454412))
    with pytest.raises(ValueError):
        client.get_product("ecoflow-river-2-pro")  # no handle lookup exists (C20)


# --- error taxonomy ---


@responses.activate
def test_422_profile_gate_parses_jsonrpc_body_before_status():
    """HTTP 422 WITH a JSON-RPC error body must surface the diagnostic,
    never a bare HTTPError (C18)."""
    _add_well_known_404()
    responses.add(
        responses.POST, ENDPOINT,
        json=load_fixture("ucp", "error_422_invalid_profile.json"), status=422,
    )
    client = _client()
    with pytest.raises(UcpRpcError) as e:
        client.lookup_catalog([variant_gid(41679254454412)])
    assert e.value.http_status == 422
    assert e.value.code == -32001
    assert e.value.data_code == "invalid_profile_url"


@responses.activate
def test_200_with_iserror_raises_tool_error():
    _add_well_known_404()
    responses.add(
        responses.POST, ENDPOINT,
        json=load_fixture("ucp", "result_iserror.json"), status=200,
    )
    with pytest.raises(UcpToolError):
        _client().lookup_catalog([variant_gid(41679254454412)])


@responses.activate
def test_non_200_without_rpc_body_is_transport_error():
    _add_well_known_404()
    responses.add(responses.POST, ENDPOINT, body="Bad Gateway", status=502)
    with pytest.raises(UcpTransportError):
        _client().lookup_catalog([variant_gid(41679254454412)])


@responses.activate
def test_429_is_hard_stop_with_retry_after_and_no_retry(no_sleep):
    """Runbook rate correction: planting-tree answers 429 / -32000
    "retry after 1933 seconds"; a naive retry burned 42 requests into the
    lockout. One POST, UcpRateLimited, retry-after surfaced, NO retry."""
    _add_well_known_404()
    responses.add(
        responses.POST, ENDPOINT, status=429,
        json={"jsonrpc": "2.0", "id": 1, "error": {
            "code": -32000,
            "message": "Too many requests, please retry after 1933 seconds"}},
    )
    with pytest.raises(UcpRateLimited) as e:
        _client().lookup_catalog([variant_gid(41679254454412)])
    assert e.value.http_status == 429
    assert e.value.retry_after == 1933
    posts = [c for c in responses.calls if c.request.method == "POST"]
    assert len(posts) == 1, "429 must never be retried"


@responses.activate
def test_503_hard_stop_reads_retry_after_header(no_sleep):
    _add_well_known_404()
    responses.add(responses.POST, ENDPOINT, status=503, body="",
                  headers={"Retry-After": "60"})
    with pytest.raises(UcpRateLimited) as e:
        _client().lookup_catalog([variant_gid(41679254454412)])
    assert e.value.retry_after == 60


def test_sequential_min_gap_between_calls(monkeypatch):
    """>=1.5s between any two HTTP calls (runbook rate correction)."""
    sleeps = []
    clock = {"now": 100.0}
    monkeypatch.setattr(ucp.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(ucp.time, "monotonic", lambda: clock["now"])
    client = _client(profile=None)
    client._throttle()
    assert sleeps == []  # first call never waits
    clock["now"] += 0.4
    client._throttle()
    assert len(sleeps) == 1 and abs(sleeps[0] - 1.1) < 1e-9
    clock["now"] += 2.0
    client._throttle()
    assert len(sleeps) == 1  # gap already satisfied


@responses.activate
def test_well_known_fetch_uses_bot_ua(no_sleep):
    """MINOR-11: no bare requests defaults on ANY request this client
    makes — the well-known fetch identifies as HeliosPriceBot too."""
    fixture = load_fixture("ucp", "well_known_wild_oak_trail.json")
    responses.add(
        responses.GET, "https://www.wildoaktrail.com/.well-known/ucp",
        json=fixture, status=200,
    )
    client = UcpClient("https://www.wildoaktrail.com", profile_url=PROFILE)
    client.resolve_endpoint()
    ua = responses.calls[0].request.headers["User-Agent"]
    assert ua.startswith("HeliosPriceBot")


@responses.activate
def test_list_price_extracted_when_present(no_sleep):
    """Runbook section 4 (corrected): list_price IS returned by 4 of 6
    retailers — extracting it restores was-price comparison in
    UCP-arbiter mode. None-safe for stores that omit it."""
    _add_well_known_404()
    responses.add(
        responses.POST, ENDPOINT,
        json=load_fixture("ucp", "lookup_catalog_river.json"), status=200,
    )
    result = _client().lookup_catalog([variant_gid(41679254454412)])
    variants = variants_from_result(result)
    unit = variants["41679254454412"]
    assert minor_units_to_cents(unit["list_amount"], unit["list_currency"]) == 74800
    bundle = variants["41737132769420"]
    assert bundle["list_amount"] is None  # omitted -> None, never a guess


@responses.activate
def test_bare_422_without_body_points_at_missing_agent_header():
    """Decoder rule from the runbook: bare 422 with NO diagnostic body ->
    suspect the missing UCP-Agent header first."""
    _add_well_known_404()
    responses.add(responses.POST, ENDPOINT, body="", status=422)
    with pytest.raises(UcpTransportError) as e:
        _client().lookup_catalog([variant_gid(41679254454412)])
    assert "UCP-Agent" in str(e.value)


def test_agent_profile_fixture_is_read_only_by_construction():
    """The O5 profile shape (runbook sections 2-3): a services block whose
    dev.ucp.shopping value is a LIST, capability values as ARRAYS of
    version objects, catalog capabilities only — nothing that transacts."""
    profile = load_fixture("ucp", "helios_agent_profile.json")["ucp"]
    assert isinstance(profile["services"]["dev.ucp.shopping"], list)
    caps = profile["capabilities"]
    assert set(caps) == {
        "dev.ucp.shopping.catalog.search",
        "dev.ucp.shopping.catalog.lookup",
    }
    for value in caps.values():
        assert isinstance(value, list) and all("version" in v for v in value)
    blob = json.dumps(profile).lower()
    for forbidden in ("cart", "checkout", "order", "payment"):
        assert forbidden not in blob, f"profile must never declare {forbidden}"


@responses.activate
def test_missing_result_is_schema_error():
    _add_well_known_404()
    responses.add(responses.POST, ENDPOINT, json={"jsonrpc": "2.0", "id": 1}, status=200)
    with pytest.raises(UcpSchemaError):
        _client().lookup_catalog([variant_gid(41679254454412)])


def test_variants_from_result_schema_violations():
    with pytest.raises(UcpSchemaError):
        variants_from_result({})  # no structuredContent
    with pytest.raises(UcpSchemaError):
        variants_from_result({"structuredContent": {"products": [
            {"id": "gid://shopify/Product/1", "variants": [
                {"id": "gid://shopify/ProductVariant/2"}  # no price
            ]}
        ]}})


# --- the suite opens zero real sockets ---


@responses.activate
def test_ucp_client_opens_zero_real_sockets(monkeypatch):
    """Mechanical proof the client under test never leaves the process.

    socket.socket is replaced with a tripwire; `responses` intercepts at
    the transport-adapter layer, so a fully exercised happy path must
    complete without ever constructing a real socket.
    """
    def _tripwire(*args, **kwargs):
        raise AssertionError("UCP test attempted to open a real socket")

    monkeypatch.setattr(socket, "socket", _tripwire)

    _add_well_known_404()
    responses.add(
        responses.POST, ENDPOINT,
        json=load_fixture("ucp", "lookup_catalog_river.json"), status=200,
    )
    client = _client()
    result = client.lookup_catalog([variant_gid(41679254454412)])
    assert "41679254454412" in variants_from_result(result)

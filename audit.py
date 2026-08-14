"""
audit.py — the end-to-end correctness loop (PLAN 4b / 4c.2).

Samples (product, retailer, variant) triples and computes TWO independent
comparisons per triple:

- RENDER hop: the rendered site HTML (located via data-* provenance
  attributes, parsed with stdlib html.parser — never prose regex) vs the
  latest JSONL row. Disagreement is the ONLY defect class: RENDER_DEFECT
  alarms, quarantines the variant, and exits 3.
- FRESHNESS hop: the latest JSONL row vs the retailer's LIVE source
  (`.json` + `.js` with a cache-buster — UCP takes over when O5 resolves).
  Disagreement is STALE: expected between scrapes (flash sales), notice +
  re-scrape recommendation, NEVER quarantines.

Non-verdicts: NO_ROW (mapped pair never scraped), NO_BASELINE (no stored
sku, drift not evaluable), UNRESOLVED (variant absent live / fetch failed /
schema surprise), NOT_AUDITED (budget exhausted). Exit codes: 0 clean,
3 any RENDER_DEFECT, 4 incomplete/unverified (an audit that could not
verify must never read as success), 1 usage/config.

Money is compared as integer cents, never float equality. Console output
is ASCII-only; all writers emit LF.

Usage:
    python -X utf8 audit.py                # sample of 10
    python -X utf8 audit.py --all
    python -X utf8 audit.py --all --data-dir X --site-dir Y \
        --report-out r.json --quarantine-out q.json
"""

import argparse
import json
import logging
import random
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import requests

import build as site_build
from scrapers.polite import (
    BOT_USER_AGENT, is_allowed_by_robots, log_request, polite_delay,
)
from scrapers.ucp import dollars_to_cents

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
DEFAULT_DATA_DIR = BASE_DIR / "data"
DEFAULT_SITE_DIR = BASE_DIR / "site"

DEFAULT_SAMPLE = 10
DEFAULT_BUDGET = 25  # live requests; exhaustion -> NOT_AUDITED + exit 4
QUARANTINE_TTL_AUDITS = 5

# Verdicts (PLAN 4c taxonomy — exact strings, consumed by CI + humans)
RENDER_DEFECT = "RENDER_DEFECT"
STALE = "STALE"
CLEAN = "CLEAN"
NO_ROW = "NO_ROW"
NO_BASELINE = "NO_BASELINE"
UNRESOLVED = "UNRESOLVED"
NOT_AUDITED = "NOT_AUDITED"

# Verdicts that mean "both hops actually ran to a conclusion".
_VERIFIED_VERDICTS = (RENDER_DEFECT, STALE, CLEAN, NO_BASELINE)

_WH_FIGURE_RE = re.compile(r"([\d][\d,]*(?:\.\d+)?)\s*(k?)wh\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Provenance parsing (stdlib html.parser; data-* attributes only)
# ---------------------------------------------------------------------------

_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
              "link", "meta", "source", "track", "wbr"}


class _ProvenanceParser(HTMLParser):
    """Collects, per data-variant-id container, its data-* attributes and
    the TEXT of every data-field element inside it.

    convert_charrefs=True (the default) means handle_data receives
    already-unescaped text — html.unescape is built into the path.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.records: dict[str, dict] = {}
        # Containers whose data-variant-id is EMPTY are kept apart: a ""
        # id is not an identity, and letting it key `records` collapses
        # every id-less variant onto one slot (red team #4, MAJOR-7).
        self.unidentified: list[dict] = []
        self._stack: list[dict] = []  # {tag, container, field}

    def handle_starttag(self, tag, attrs):
        if tag in _VOID_TAGS:
            return
        attrs = dict(attrs)
        frame = {"tag": tag, "container": None, "field": None}
        if "data-variant-id" in attrs:
            vid = attrs.get("data-variant-id") or ""
            record = {
                "tier": attrs.get("data-tier"),
                "sku": attrs.get("data-sku") or None,
                "scraped_at": attrs.get("data-scraped-at"),
                "withheld": attrs.get("data-withheld"),
                "fields": {},
            }
            if vid:
                self.records[vid] = record
            else:
                self.unidentified.append(record)
            frame["container"] = record
        elif "data-field" in attrs:
            container = self._current_container()
            if container is not None:
                name = attrs["data-field"]
                field = {"text": [], "value": attrs.get("data-value")}
                container["fields"][name] = field
                frame["field"] = field
        self._stack.append(frame)

    def handle_endtag(self, tag):
        # Pop to the nearest matching open tag; tolerate minor nesting slop
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i]["tag"] == tag:
                del self._stack[i:]
                break

    def handle_data(self, data):
        for frame in reversed(self._stack):
            if frame["field"] is not None:
                frame["field"]["text"].append(data)
                break

    def _current_container(self):
        for frame in reversed(self._stack):
            if frame["container"] is not None:
                return frame["container"]
        return None


def parse_provenance_full(html_text: str) -> tuple[dict[str, dict], list[dict]]:
    """(identified records, unidentified records) — see _ProvenanceParser."""
    parser = _ProvenanceParser()
    parser.feed(html_text)
    for record in list(parser.records.values()) + parser.unidentified:
        for field in record["fields"].values():
            field["text"] = "".join(field["text"]).strip()
    return parser.records, parser.unidentified


def parse_provenance(html_text: str) -> dict[str, dict]:
    """{variant_id: {tier, sku, scraped_at, withheld, fields{name: {text, value}}}}."""
    records, _unidentified = parse_provenance_full(html_text)
    return records


def display_price_to_cents(text: str):
    """"$1,408.12" -> 140812 cents; None when the text is not a price."""
    if not text:
        return None
    m = re.fullmatch(r"\$([\d,]+(?:\.\d{1,2})?)", text.strip())
    if not m:
        return None
    try:
        return dollars_to_cents(m.group(1).replace(",", ""))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Live source (.json + .js today; ucp.lookup_catalog when O5 resolves)
# ---------------------------------------------------------------------------

class LiveFetcher:
    """Budgeted, polite fetcher for the freshness hop."""

    def __init__(self, budget: int = DEFAULT_BUDGET):
        self.budget = budget
        self.used = 0
        self.errors: list[str] = []
        self.session = requests.Session()
        self.session.headers.update({
            # The audit identifies itself honestly — it is a verification
            # bot, not a shopper (LIVE BUDGET rule: HeliosPriceBot UA).
            "User-Agent": BOT_USER_AGENT,
            "Accept": "application/json",
        })
        self._first = True

    def _get_json(self, url: str):
        if not is_allowed_by_robots(url):
            self.errors.append(f"robots disallows {url}")
            return None
        if not self._first:
            polite_delay(3, 8)
        self._first = False
        self.used += 1
        try:
            resp = self.session.get(url, timeout=20)
            log_request(url, status_code=resp.status_code)
            if resp.status_code != 200:
                self.errors.append(f"HTTP {resp.status_code} for {url}")
                return None
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            self.errors.append(f"fetch failed for {url}: {e}")
            return None

    def fetch_pair(self, base_url: str, handle: str):
        """Live truth for one (product, retailer) pair, or None on failure.

        Cache-buster on both URLs: CDN-cached product JSON is exactly the
        stale-arbiter failure this loop exists to avoid.
        """
        if self.budget - self.used < 2:
            return "budget"
        buster = int(time.time() * 1000)
        base = base_url.rstrip("/")
        doc = self._get_json(f"{base}/products/{handle}.json?_hb={buster}")
        if not isinstance(doc, dict) or "product" not in doc:
            return None
        js = self._get_json(f"{base}/products/{handle}.js?_hb={buster}")

        product = doc["product"]
        variants: dict[str, dict] = {}
        for v in product.get("variants") or []:
            if not isinstance(v, dict) or v.get("id") is None:
                continue
            try:
                price_cents = dollars_to_cents(v.get("price"))
            except ValueError:
                price_cents = None
            compare_raw = v.get("compare_at_price")
            try:
                compare_cents = dollars_to_cents(compare_raw) if compare_raw else None
            except ValueError:
                compare_cents = None
            variants[str(v["id"])] = {
                "price_cents": price_cents,
                "compare_at_cents": compare_cents,
                "sku": (v.get("sku") or "").strip() or None,
                "title": v.get("title", ""),
            }
        availability: dict[str, bool] = {}
        if isinstance(js, dict):
            for v in js.get("variants") or []:
                if isinstance(v, dict) and v.get("id") is not None \
                        and isinstance(v.get("available"), bool):
                    availability[str(v["id"])] = v["available"]
        return {
            "variants": variants,
            "availability": availability,
            "title": product.get("title", ""),
            "body_html": product.get("body_html", "") or "",
        }


# ---------------------------------------------------------------------------
# Hop comparisons
# ---------------------------------------------------------------------------

def _expected_wh_display(variant_data: dict, product: dict):
    cls = site_build.classify_variant(
        variant_data.get("raw_variant", ""), product.get("name", ""))
    per_wh = site_build.dollars_per_wh(
        variant_data.get("price"),
        (product.get("specs") or {}).get("capacity_wh"), cls)
    return site_build.format_dollars_per_wh(per_wh) if per_wh is not None else None


def check_render(vid: str, variant_data: dict, product: dict,
                 page_prov: dict, home_prov: dict,
                 row_scraped_at: str = "") -> list[dict]:
    """Mismatches between what the site DISPLAYS and the JSONL row.

    Non-quarantined path only — quarantined variants go through
    check_quarantine_markers + the shadow recheck instead.
    """
    mismatches = []

    def bad(where, field, observed, expected):
        mismatches.append({"where": where, "field": field,
                           "observed": observed, "expected": expected})

    rec = page_prov.get(vid)
    if rec is None:
        bad("product-page", "row", "absent", "rendered variant row")
        return mismatches
    if rec.get("withheld") == "stale":
        # Withheld-by-age is a policy render, not a number to compare.
        return mismatches

    # Provenance attributes must not lie (red team #4, MINOR-10): a wrong
    # data-sku or data-scraped-at breaks every downstream trace.
    stored_sku = variant_data.get("sku") or None
    if (rec.get("sku") or None) != stored_sku:
        bad("product-page", "sku-attr", rec.get("sku"), stored_sku)
    if row_scraped_at and rec.get("scraped_at") != row_scraped_at:
        bad("product-page", "scraped-at-attr", rec.get("scraped_at"), row_scraped_at)

    row_price_cents = None
    try:
        row_price_cents = dollars_to_cents(variant_data.get("price"))
    except ValueError:
        pass
    shown = (rec["fields"].get("price") or {}).get("text", "")
    if display_price_to_cents(shown) != row_price_cents:
        bad("product-page", "price", shown or "absent",
            site_build.money(variant_data.get("price"))
            if row_price_cents is not None else "no displayable price")

    was = variant_data.get("was_price")
    shown_was = (rec["fields"].get("was-price") or {}).get("text", "")
    expected_was = site_build.money(was) if isinstance(was, (int, float)) else ""
    if (shown_was or "") != expected_was:
        bad("product-page", "was-price", shown_was or "absent", expected_was or "absent")

    avail_field = rec["fields"].get("availability") or {}
    expected_avail = site_build._avail_value(variant_data.get("available"))
    if avail_field.get("value") != expected_avail:
        bad("product-page", "availability", avail_field.get("value"), expected_avail)

    expected_wh = _expected_wh_display(variant_data, product)
    shown_wh = (rec["fields"].get("wh") or {}).get("text") or None
    if shown_wh != expected_wh:
        bad("product-page", "wh", shown_wh or "absent", expected_wh or "absent")

    # Home cell, when this variant is the one the cell renders.
    hrec = home_prov.get(vid)
    if hrec is not None and not hrec.get("withheld"):
        shown_home = (hrec["fields"].get("price") or {}).get("text", "")
        if display_price_to_cents(shown_home) != row_price_cents:
            bad("home", "price", shown_home or "absent",
                site_build.money(variant_data.get("price"))
                if row_price_cents is not None else "no displayable price")
        havail = (hrec["fields"].get("availability") or {}).get("value")
        if havail is not None and havail != expected_avail:
            bad("home", "availability", havail, expected_avail)
        # Home $/Wh: same-formatter string equality, exactly like the
        # product page — a doubled home $/Wh sailed through as CLEAN
        # (red team #4, MAJOR-8).
        shown_home_wh = (hrec["fields"].get("wh") or {}).get("text") or None
        if shown_home_wh != expected_wh:
            bad("home", "wh", shown_home_wh or "absent", expected_wh or "absent")
        if row_scraped_at and hrec.get("scraped_at") != row_scraped_at:
            bad("home", "scraped-at-attr", hrec.get("scraped_at"), row_scraped_at)
    return mismatches


def _cheapest_vid(row: dict):
    """str variant_id of the row's cheapest numerically-priced variant."""
    priced = [d for d in (row.get("variants") or {}).values()
              if isinstance(d, dict) and isinstance(d.get("price"), (int, float))]
    if not priced:
        return None
    vid = min(priced, key=lambda d: d["price"]).get("variant_id")
    return str(vid) if vid not in (None, "") else None


def check_quarantine_markers(vid: str, cheapest_vid, page_prov: dict,
                             home_prov: dict) -> tuple[str, str | None]:
    """Positive-evidence recheck of a quarantined variant's withhold.

    Returns ("ok"|"leak"|"absent", detail). BOTH surfaces are verified
    (red team #4, CRITICAL-1): the product page row AND — when this
    variant is the row's cheapest — the home cell must carry the
    quarantine marker with no price text. A price anywhere is a leak
    (RENDER_DEFECT). Anything short of the marker is ABSENCE, which is
    never a clean recheck (CRITICAL-2: assert on markers, not absence).
    """
    leaks: list[str] = []
    absences: list[str] = []

    def price_text(record):
        return ((record or {}).get("fields", {}).get("price") or {}).get("text", "")

    rec = page_prov.get(vid)
    if rec is None:
        absences.append("product-page row absent")
    elif rec.get("withheld") != "quarantine":
        if price_text(rec):
            leaks.append(f"product-page shows price {price_text(rec)!r} "
                         f"without the quarantine marker")
        else:
            absences.append(
                f"product-page marker is {rec.get('withheld') or 'missing'}, "
                f"not quarantine")
    elif price_text(rec):
        leaks.append(f"product-page carries the marker but still shows "
                     f"price {price_text(rec)!r}")

    hrec = home_prov.get(vid)
    if vid == cheapest_vid:
        # The quarantined variant is the cell's variant: the WHOLE cell
        # must be withheld (never next-cheapest substitution, PLAN 4c.3).
        if hrec is None:
            absences.append("home cell for the quarantined cheapest variant absent")
        elif hrec.get("withheld") != "quarantine":
            if price_text(hrec):
                leaks.append(f"home cell shows price {price_text(hrec)!r} "
                             f"without the quarantine marker")
            else:
                absences.append(
                    f"home marker is {hrec.get('withheld') or 'missing'}, "
                    f"not quarantine")
        elif price_text(hrec):
            leaks.append(f"home cell carries the marker but still shows "
                         f"price {price_text(hrec)!r}")
    elif hrec is not None and not hrec.get("withheld") and price_text(hrec):
        leaks.append(f"home cell renders the quarantined variant's price "
                     f"{price_text(hrec)!r}")

    if leaks:
        return "leak", "; ".join(leaks)
    if absences:
        return "absent", "; ".join(absences)
    return "ok", None


def check_freshness(variant_data: dict, live_variant: dict,
                    live_available) -> list[dict]:
    """Differences between the JSONL row and the live source (STALE, never
    a defect: prices move between scrape and audit)."""
    diffs = []

    def moved(field, stored, live):
        diffs.append({"field": field, "stored": stored, "live": live})

    try:
        stored_cents = dollars_to_cents(variant_data.get("price"))
    except ValueError:
        stored_cents = None
    if stored_cents != live_variant["price_cents"]:
        moved("price", stored_cents, live_variant["price_cents"])

    was = variant_data.get("was_price")
    try:
        stored_was = dollars_to_cents(was) if was is not None else None
    except ValueError:
        stored_was = None
    # Freshness-hop was-price compares ONLY vs .json compare_at_price —
    # UCP has no was-price field (C21), so this comparison retires when
    # the arbiter switches unless .json stays a secondary source.
    if stored_was != live_variant["compare_at_cents"]:
        moved("was_price", stored_was, live_variant["compare_at_cents"])

    stored_avail = variant_data.get("available")
    if isinstance(stored_avail, bool) and isinstance(live_available, bool) \
            and stored_avail != live_available:
        moved("availability", stored_avail, live_available)
    return diffs


def check_capacity(product: dict, live_title: str, live_body: str) -> str | None:
    """CONFIRMED / ABSENT / CONTRADICTED against Wh figures in live text.

    Closes the loop's blind spot: capacity_wh is hand-authored
    (specs.capacity_source records where from), and a wrong capacity makes
    every $/Wh confidently wrong. CONTRADICTED = alarm.
    """
    capacity = (product.get("specs") or {}).get("capacity_wh")
    if not isinstance(capacity, (int, float)) or capacity <= 0:
        return None  # nothing claimed, nothing to contradict
    text = f"{live_title} {live_body}"
    figures = []
    for raw, kilo in _WH_FIGURE_RE.findall(text):
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        figures.append(value * 1000 if kilo else value)
    if not figures:
        return "ABSENT"
    # 1% tolerance: listings round (5.12kWh == 5120Wh exactly, but
    # 1,433Wh vs 1433.6 style rounding must not read as contradiction).
    if any(abs(f - capacity) <= capacity * 0.01 for f in figures):
        return "CONFIRMED"
    return "CONTRADICTED"


# ---------------------------------------------------------------------------
# The audit run
# ---------------------------------------------------------------------------

def _load_inputs(data_dir: Path):
    products = {p["id"]: p for p in site_build.load_json(data_dir / "products.json")}
    retailers = {r["id"]: r for r in site_build.load_json(data_dir / "retailers.json")}
    handle_maps = site_build.load_json(data_dir / "handle_maps.json")
    active_ids = [pid for pid, p in products.items() if p.get("active") is True]
    latest = site_build.load_latest_prices(data_dir / "prices", active_ids)
    quarantine = site_build.load_quarantine(data_dir)
    return products, retailers, handle_maps, latest, quarantine


def run_audit(data_dir: Path = DEFAULT_DATA_DIR, site_dir: Path = DEFAULT_SITE_DIR,
              report_out: Path | None = None, quarantine_out: Path | None = None,
              sample_n: int = DEFAULT_SAMPLE, audit_all: bool = False,
              budget: int = DEFAULT_BUDGET, now=None, seed=None,
              templates_dir: Path | None = None):
    """Run the audit. Returns (report, exit_code)."""
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    report_out = report_out or (data_dir / "audit_report.json")
    quarantine_out = quarantine_out or (data_dir / "quarantine.json")
    templates_dir = templates_dir or site_build.TEMPLATES_DIR

    # Quarantine shape is validated inside _load_inputs (build.load_quarantine)
    # BEFORE any live request (MINOR-13): a malformed file must cost zero
    # budget and exit 1, not crash mid-run.
    products, retailers, handle_maps, latest, quarantine = _load_inputs(data_dir)

    # ---- triple space: latest JSONL rows intersected with handle_maps ----
    pair_results = []   # NO_ROW entries (pair-level, no live cost)
    triples = []
    for retailer_id, mapping in sorted(handle_maps.items()):
        retailer = retailers.get(retailer_id)
        if retailer is None:
            continue
        for product_id, handle in sorted(mapping.items()):
            product = products.get(product_id)
            if product is None or product.get("active") is not True:
                continue
            row = latest.get(product_id, {}).get(retailer_id)
            if not row or not row.get("variants"):
                pair_results.append({
                    "retailer_id": retailer_id, "product_id": product_id,
                    "variant_id": None, "verdict": NO_ROW,
                    "detail": "mapped pair has no scraped row - coverage gap, not mismatch",
                })
                continue
            for tier, vdata in row["variants"].items():
                if not isinstance(vdata, dict):
                    continue
                raw_vid = vdata.get("variant_id")
                # An empty/missing id is non-joinable (MAJOR-7): the
                # triple still gets audited (verdict UNRESOLVED) but it
                # can never key a quarantine entry, so its sampling key
                # is synthetic and collision-proof against real keys.
                joinable = raw_vid not in (None, "")
                triples.append({
                    "retailer_id": retailer_id, "product_id": product_id,
                    "handle": handle, "tier": tier,
                    "variant_id": str(raw_vid) if joinable else None,
                    "joinable": joinable,
                    "variant_data": vdata, "row": row,
                    "product": product, "retailer": retailer,
                })

    # ---- sampling: ALL quarantined variants first, random fill to N ----
    def _sample_key(t):
        if t["joinable"]:
            return f"{t['retailer_id']}:{t['product_id']}:{t['variant_id']}"
        return f"{t['retailer_id']}:{t['product_id']}:~noid~{t['tier']}"

    by_key = {_sample_key(t): t for t in triples}
    unobservable_qkeys = [k for k in quarantine if k not in by_key]
    sampled_keys = [k for k in quarantine if k in by_key]
    if audit_all:
        sampled_keys = list(by_key)
    else:
        rest = [k for k in by_key if k not in sampled_keys]
        rng = random.Random(seed)
        rng.shuffle(rest)
        sampled_keys += rest[:max(0, sample_n - len(sampled_keys))]
    sampled = [by_key[k] for k in sampled_keys]

    # ---- parse rendered pages once ----
    home_prov = {}
    index_path = site_dir / "index.html"
    if index_path.exists():
        home_prov = parse_provenance(index_path.read_text(encoding="utf-8"))
    page_prov_cache: dict[str, dict] = {}

    def page_prov_for(product_id: str) -> dict:
        if product_id not in page_prov_cache:
            path = site_dir / "products" / f"{product_id}.html"
            page_prov_cache[product_id] = (
                parse_provenance(path.read_text(encoding="utf-8"))
                if path.exists() else {}
            )
        return page_prov_cache[product_id]

    # ---- shadow rebuild (lazy, once): the world with NO quarantine ----
    # A quarantined entry may only clear when a rebuild WITHOUT it would
    # render correctly (red team #4, MAJOR-3): clearing on "marker present
    # + freshness clean" lets a persistent build defect oscillate
    # wrong-price/withheld forever, republishing the wrong price every
    # other cycle.
    shadow_state: dict = {}

    def shadow_prov_for(product_id: str) -> tuple[dict, dict]:
        if "dir" not in shadow_state:
            sdir = Path(tempfile.mkdtemp(prefix="helios-shadow-"))
            site_build.build_site(data_dir=data_dir, site_dir=sdir,
                                  templates_dir=templates_dir, now=now,
                                  quarantine_override={})
            shadow_state["dir"] = sdir
            idx = sdir / "index.html"
            shadow_state["home"] = (
                parse_provenance(idx.read_text(encoding="utf-8"))
                if idx.exists() else {})
            shadow_state["pages"] = {}
        pages = shadow_state["pages"]
        if product_id not in pages:
            path = shadow_state["dir"] / "products" / f"{product_id}.html"
            pages[product_id] = (
                parse_provenance(path.read_text(encoding="utf-8"))
                if path.exists() else {})
        return pages[product_id], shadow_state["home"]

    # ---- fetch live per pair, verdict per triple ----
    fetcher = LiveFetcher(budget=budget)
    live_by_pair: dict[tuple, object] = {}
    results = []
    alarms = []
    notices = []
    retailer_avail_sample: dict[str, list] = {}

    for triple in sampled:
        entry = {
            "retailer_id": triple["retailer_id"],
            "product_id": triple["product_id"],
            "variant_id": triple["variant_id"],
            "tier": triple["tier"],
            "sku_stored": triple["variant_data"].get("sku"),
            "sku_drift": False,
        }

        # Non-joinable identity (MAJOR-7): never RENDER_DEFECT, never
        # quarantined — there is no key to hang either on.
        if not triple["joinable"]:
            entry["verdict"] = UNRESOLVED
            entry["detail"] = ("variant has no id - not joinable; "
                               "excluded from quarantine by construction")
            results.append(entry)
            continue

        # Non-finite/junk stored price (MINOR-14): Infinity round-trips
        # through JSON and nothing derived from it is comparable.
        try:
            dollars_to_cents(triple["variant_data"].get("price"))
        except ValueError:
            entry["verdict"] = UNRESOLVED
            entry["detail"] = "stored price is not a finite money value"
            results.append(entry)
            continue

        pair = (triple["retailer_id"], triple["product_id"])
        if pair not in live_by_pair:
            live = fetcher.fetch_pair(triple["retailer"]["url"], triple["handle"])
            live_by_pair[pair] = live
            if isinstance(live, dict):
                cap = check_capacity(triple["product"], live["title"], live["body_html"])
                if cap == "CONTRADICTED":
                    alarms.append(
                        f"CAPACITY CONTRADICTED: {triple['product_id']} claims "
                        f"{triple['product']['specs']['capacity_wh']} Wh but live "
                        f"text at {triple['retailer_id']} shows no matching figure"
                    )
                elif cap == "ABSENT":
                    notices.append(
                        f"capacity not confirmable from live text: "
                        f"{triple['product_id']} at {triple['retailer_id']}"
                    )
        live = live_by_pair[pair]

        qkey = f"{triple['retailer_id']}:{triple['product_id']}:{triple['variant_id']}"
        quarantined = qkey in quarantine

        if live == "budget":
            entry["verdict"] = NOT_AUDITED
            entry["detail"] = "live-request budget exhausted before this pair"
            results.append(entry)
            continue

        # RENDER hop first: it is the defect class and needs no live data.
        if quarantined:
            # Recheck needs POSITIVE evidence on BOTH surfaces
            # (CRITICAL-1/2), then a shadow rebuild proving the defect is
            # actually gone (MAJOR-3).
            status, detail = check_quarantine_markers(
                triple["variant_id"], _cheapest_vid(triple["row"]),
                page_prov_for(triple["product_id"]), home_prov)
            if status == "leak":
                entry["verdict"] = RENDER_DEFECT
                entry["mismatches"] = [{
                    "where": "quarantine-recheck", "field": "leak",
                    "observed": detail, "expected": "withheld marker, no price"}]
                entry["detail"] = f"quarantine leak: {detail}"
                alarms.append(f"RENDER_DEFECT {qkey}: {entry['detail']}")
                results.append(entry)
                continue
            if status == "absent":
                entry["verdict"] = UNRESOLVED
                entry["detail"] = (f"quarantine recheck lacks positive marker "
                                   f"evidence: {detail}")
                results.append(entry)
                continue
            shadow_page, shadow_home = shadow_prov_for(triple["product_id"])
            shadow_mismatches = check_render(
                triple["variant_id"], triple["variant_data"], triple["product"],
                shadow_page, shadow_home,
                row_scraped_at=triple["row"].get("timestamp") or "")
            if shadow_mismatches:
                entry["verdict"] = RENDER_DEFECT
                entry["mismatches"] = shadow_mismatches
                entry["detail"] = (
                    "shadow recheck failed - rebuilding without the "
                    "quarantine entry would re-render wrong: "
                    + "; ".join(f"{m['where']}/{m['field']}: shown "
                                f"{m['observed']!r}, row says {m['expected']!r}"
                                for m in shadow_mismatches))
                alarms.append(f"RENDER_DEFECT {qkey}: {entry['detail']}")
                results.append(entry)
                continue
        else:
            mismatches = check_render(
                triple["variant_id"], triple["variant_data"], triple["product"],
                page_prov_for(triple["product_id"]), home_prov,
                row_scraped_at=triple["row"].get("timestamp") or "")
            if mismatches:
                entry["verdict"] = RENDER_DEFECT
                entry["mismatches"] = mismatches
                entry["detail"] = (
                    "site disagrees with its own price store: "
                    + "; ".join(f"{m['where']}/{m['field']}: shown "
                                f"{m['observed']!r}, row says {m['expected']!r}"
                                for m in mismatches)
                )
                alarms.append(f"RENDER_DEFECT {qkey}: {entry['detail']}")
                results.append(entry)
                continue

        if live is None:
            entry["verdict"] = UNRESOLVED
            entry["detail"] = "live source unavailable (see errors)"
            results.append(entry)
            continue

        live_variant = live["variants"].get(triple["variant_id"])
        if live_variant is None:
            # Absence can mean gone OR hidden (UCP filters.available
            # defaults true, C21; a .json variant can vanish on retheme).
            entry["verdict"] = UNRESOLVED
            entry["detail"] = "variant absent from live source - not evaluable"
            results.append(entry)
            continue

        # sku drift: both sides non-null only (C-A2 tripwire)
        live_sku = live_variant.get("sku")
        stored_sku = triple["variant_data"].get("sku")
        entry["sku_live"] = live_sku
        if stored_sku and live_sku and stored_sku != live_sku:
            entry["sku_drift"] = True
            alarms.append(
                f"SKU DRIFT {qkey}: stored {stored_sku!r} vs live {live_sku!r} "
                f"- the retailer may have swapped the product under this handle"
            )

        # Availability truth comes from .js ONLY; no answer for a compared
        # variant means the triple is NOT verified (red team #4, MAJOR-6).
        live_avail = live["availability"].get(triple["variant_id"])
        if not isinstance(live_avail, bool):
            entry["verdict"] = UNRESOLVED
            entry["detail"] = ("live availability unavailable - .js gave no "
                               "answer for this variant")
            fetcher.errors.append(f"no .js availability for {qkey}")
            results.append(entry)
            continue

        diffs = check_freshness(
            triple["variant_data"], live_variant, live_avail,
        )
        if diffs:
            entry["verdict"] = STALE
            entry["freshness_diffs"] = diffs
            entry["detail"] = ("row differs from live source - re-scrape "
                               "recommended (not a defect)")
        elif stored_sku is None:
            entry["verdict"] = NO_BASELINE
            entry["detail"] = "hops agree but stored row has no sku - drift not evaluable"
        else:
            entry["verdict"] = CLEAN
        results.append(entry)

        retailer_avail_sample.setdefault(triple["retailer_id"], []).append(
            (triple["product_id"], live_avail))

    # ---- all-available smell (>=8 variants across >=3 products) ----
    for retailer_id, seen in retailer_avail_sample.items():
        product_count = len({pid for pid, _ in seen})
        if len(seen) >= 8 and product_count >= 3 and all(a for _, a in seen):
            notices.append(
                f"all-available smell at {retailer_id}: {len(seen)} sampled "
                f"variants across {product_count} products all in stock - "
                f"verify availability parsing"
            )

    # ---- quarantine lifecycle ----
    quarantine = dict(quarantine)
    verdict_by_key = {f"{e['retailer_id']}:{e['product_id']}:{e['variant_id']}": e
                      for e in results if e.get("variant_id")}
    for qkey, entry in verdict_by_key.items():
        v = entry["verdict"]
        existing = quarantine.get(qkey)
        if v == RENDER_DEFECT:
            first_mm = (entry.get("mismatches") or [{}])[0]
            if existing is not None:
                # Never delete+recreate (MAJOR-3): the entry's history —
                # first_seen and the failure count — IS the oscillation
                # evidence.
                existing["tier_last_seen"] = by_key[qkey]["tier"]
                existing["observed"] = first_mm.get("observed")
                existing["expected"] = first_mm.get("expected")
                existing["last_seen"] = now_iso
                existing["consecutive_failures"] = (
                    existing.get("consecutive_failures", 0) + 1)
                existing["unobserved_audits"] = 0
            else:
                triple = by_key[qkey]
                quarantine[qkey] = {
                    "sku": triple["variant_data"].get("sku"),
                    "tier_last_seen": triple["tier"],
                    "reason": "render_defect",
                    "observed": first_mm.get("observed"),
                    "expected": first_mm.get("expected"),
                    "first_seen": now_iso,
                    "last_seen": now_iso,
                    "consecutive_failures": 1,
                    "unobserved_audits": 0,
                }
        elif existing is not None:
            if v in (CLEAN, NO_BASELINE):
                del quarantine[qkey]
                notices.append(f"quarantine cleared (recheck clean): {qkey}")
            else:
                # Any recheck that is not CLEAN increments the TTL
                # counter (red team #4, CRITICAL-2): UNRESOLVED and
                # NOT_AUDITED are unobservable; STALE could not be
                # verified clean either. A perpetually unverifiable
                # entry expires with a logged reason instead of
                # squatting forever.
                existing["unobserved_audits"] = existing.get("unobserved_audits", 0) + 1
                existing["last_seen"] = now_iso
                if existing["unobserved_audits"] >= QUARANTINE_TTL_AUDITS:
                    del quarantine[qkey]
                    notices.append(
                        f"quarantine TTL-expired after {QUARANTINE_TTL_AUDITS} "
                        f"not-clean rechecks: {qkey}")
    for qkey in unobservable_qkeys:
        entry = quarantine.get(qkey)
        if entry is None:
            continue
        rid, pid, _vid = qkey.split(":", 2)
        product = products.get(pid)
        mapped = pid in (handle_maps.get(rid) or {})
        if product is None or product.get("active") is not True or not mapped:
            del quarantine[qkey]
            notices.append(f"quarantine removed (product inactive/unmapped): {qkey}")
            continue
        entry["unobserved_audits"] = entry.get("unobserved_audits", 0) + 1
        if entry["unobserved_audits"] >= QUARANTINE_TTL_AUDITS:
            del quarantine[qkey]
            notices.append(
                f"quarantine TTL-expired after {QUARANTINE_TTL_AUDITS} "
                f"unobservable audits: {qkey}")

    # ---- report + exit code ----
    all_results = results + pair_results
    attempted = len(results)
    verified = sum(1 for e in results if e["verdict"] in _VERIFIED_VERDICTS)
    verdict_counts: dict[str, int] = {}
    for e in all_results:
        verdict_counts[e["verdict"]] = verdict_counts.get(e["verdict"], 0) + 1

    report = {
        "timestamp": now_iso,
        "verified": verified,
        "attempted": attempted,
        "verdict_counts": verdict_counts,
        "results": all_results,
        "alarms": alarms,
        "notices": notices,
        "errors": fetcher.errors,
        "live_requests_used": fetcher.used,
        "budget": budget,
        "params": {"all": audit_all, "sample_n": sample_n,
                   "data_dir": str(data_dir), "site_dir": str(site_dir)},
    }

    site_build.validate_quarantine(quarantine)  # never write an invalid map
    with open(report_out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    with open(quarantine_out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(quarantine, f, indent=2, ensure_ascii=False)

    if "dir" in shadow_state:
        shutil.rmtree(shadow_state["dir"], ignore_errors=True)

    # Console summary (ASCII only) leads with verified/attempted.
    if attempted == 0:
        # Zero attempted must never read as success (MAJOR-4): empty
        # handle_maps, an all-inactive catalog or all-NO_ROW pairs is an
        # audit that verified NOTHING.
        print("AUDIT: verified 0 / attempted 0 - nothing audited")
    else:
        print(f"AUDIT: verified {verified} / attempted {attempted}")
    for verdict in (RENDER_DEFECT, STALE, CLEAN, NO_BASELINE, UNRESOLVED,
                    NOT_AUDITED, NO_ROW):
        if verdict_counts.get(verdict):
            print(f"  {verdict}: {verdict_counts[verdict]}")
    for alarm in alarms:
        print(f"ALARM: {alarm}")
    for notice in notices:
        print(f"notice: {notice}")
    for error in fetcher.errors:
        print(f"error: {error}")
    print(f"live requests: {fetcher.used}/{budget}")
    print(f"report: {report_out}")

    if any(e["verdict"] == RENDER_DEFECT for e in results):
        exit_code = 3
    elif attempted == 0 or fetcher.errors or attempted > verified:
        # Incomplete/unverified must never read as success (PLAN 4c).
        exit_code = 4
    else:
        exit_code = 0
    print(f"exit: {exit_code}")
    return report, exit_code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Helios end-to-end audit")
    parser.add_argument("--all", action="store_true",
                        help="audit every triple instead of a sample")
    parser.add_argument("-n", "--sample", type=int, default=DEFAULT_SAMPLE)
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--site-dir", type=Path, default=DEFAULT_SITE_DIR)
    parser.add_argument("--report-out", type=Path, default=None)
    parser.add_argument("--quarantine-out", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        _report, exit_code = run_audit(
            data_dir=args.data_dir, site_dir=args.site_dir,
            report_out=args.report_out, quarantine_out=args.quarantine_out,
            sample_n=args.sample, audit_all=args.all,
            budget=args.budget, seed=args.seed,
        )
    except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError) as e:
        # ValueError covers a malformed quarantine map (MINOR-13), which
        # is validated before any live request is spent.
        logger.error(f"config/data problem: {e!r}")
        return 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

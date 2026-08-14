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
import html
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
        # EVERY identified record in document order, duplicates included.
        # `records` keeps only the last occurrence per id, which is right
        # for product/home pages and wrong for guides (see
        # parse_provenance_list).
        self.all_records: list[dict] = []
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
                record["_vid"] = vid
                self.records[vid] = record
                self.all_records.append(record)
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


def _finalize(parser: _ProvenanceParser) -> None:
    for record in parser.all_records + parser.unidentified:
        for field in record["fields"].values():
            if isinstance(field["text"], list):
                field["text"] = "".join(field["text"]).strip()


def parse_provenance_full(html_text: str) -> tuple[dict[str, dict], list[dict]]:
    """(identified records, unidentified records) — see _ProvenanceParser."""
    parser = _ProvenanceParser()
    parser.feed(html_text)
    _finalize(parser)
    return parser.records, parser.unidentified


def parse_provenance_list(html_text: str) -> list[dict]:
    """EVERY identified record on a page, duplicates included.

    parse_provenance() keys by variant_id, so a variant rendered more than
    once keeps only its LAST occurrence. That is correct for product and
    home pages, where a variant appears once, and WRONG for guides, where
    one variant legitimately appears up to three times on a page: the
    headline span, its row in the product's own table, and its row in a
    spreads table.
    """
    parser = _ProvenanceParser()
    parser.feed(html_text)
    _finalize(parser)
    return parser.all_records


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
    if rec.get("withheld") == "price_unreadable":
        # The page says the stored price is not a usable number. That is a
        # legitimate withhold, but only if it is TRUE — otherwise the
        # marker becomes a way to hide a real price behind a fake excuse.
        if site_build._usable_number(variant_data.get("price")):
            bad("product-page", "withheld", "price_unreadable",
                "a displayable price")
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

    # A stored price that is not finite and positive has no displayable
    # form, so the page correctly shows nothing; treating it as "expected
    # cents" would make that correct withhold read as a defect.
    row_price_cents = None
    if site_build._usable_number(variant_data.get("price")):
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


_MERGED_ATTRS = ("tier", "sku", "scraped_at", "withheld")


def merge_guide_records(vid: str, records: list[dict], page: str) -> dict:
    """Fold a variant's several appearances on ONE guide page into one.

    A guide renders the same variant up to three times — the headline
    span, its row in the product's table, and its row in a spreads table
    — and the tables carry different columns: the spreads table has no
    rating column at all, by design.

    Keying by variant_id alone made the LAST occurrence win, so the
    ratingless spreads row overwrote the rated one and the audit read the
    rating as "absent". On the clean tree that produced four RENDER_DEFECTs
    and quarantined two of the four ranked power stations — the withhold
    mechanism firing on correct pages, which is worse than not checking at
    all. My original docstring reasoned about duplication ACROSS guides and
    never considered duplication WITHIN one.

    Fold rule: a field present anywhere on the page counts as present, so
    an absent rating in a context that has no rating column is not a
    defect. Where two appearances disagree about the SAME field or
    attribute, that is an internal contradiction on one page and IS
    reported — stricter than either occurrence alone.
    """
    merged = {"fields": {}, "guide": page, "internal_conflicts": []}
    for attr in _MERGED_ATTRS:
        merged[attr] = None
    for record in records:
        for attr in _MERGED_ATTRS:
            value = record.get(attr)
            if value is None:
                continue
            if merged[attr] is None:
                merged[attr] = value
            elif merged[attr] != value:
                merged["internal_conflicts"].append(
                    f"{attr}: {merged[attr]!r} vs {value!r}")
        for name, field in record["fields"].items():
            if name not in merged["fields"]:
                merged["fields"][name] = field
            elif merged["fields"][name].get("text") != field.get("text"):
                merged["internal_conflicts"].append(
                    f"{name}: {merged['fields'][name].get('text')!r} vs "
                    f"{field.get('text')!r}")
    merged["_vid"] = vid
    return merged


def parse_guide_provenance(site_dir: Path) -> dict[str, dict]:
    """{variant_id: merged record} across every rendered guide page.

    The guides were an UNAUDITED surface: the render hop opened
    index.html and products/*.html only, so a wrong figure on a ranked
    buying guide — the pages most likely to be acted on — could not
    produce a RENDER_DEFECT. Guides share their freshness with the rows
    behind them, so verifying them costs ZERO extra live requests.

    Within a page, a variant's appearances are merged (see
    merge_guide_records). Across pages they are not: every product belongs
    to exactly one guide category today, so a variant on two guides means
    the scoping rules overlapped and the first record wins with the
    duplicate reported rather than silently dropped.
    """
    merged: dict[str, dict] = {}
    guides_dir = site_dir / "guides"
    if not guides_dir.is_dir():
        return merged
    for path in sorted(guides_dir.glob("*.html")):
        by_vid: dict[str, list[dict]] = {}
        for record in parse_provenance_list(path.read_text(encoding="utf-8")):
            by_vid.setdefault(record["_vid"], []).append(record)
        for vid, records in by_vid.items():
            folded = merge_guide_records(vid, records, path.name)
            if vid in merged:
                merged[vid].setdefault("duplicate_in", []).append(path.name)
                continue
            merged[vid] = folded
    return merged


def check_guide_render(vid: str, variant_data: dict, product: dict,
                       guide_prov: dict, row_scraped_at: str = "") -> list[dict]:
    """Mismatches between a guide's displayed numbers and the JSONL row.

    Same comparisons check_render makes on the product page, against the
    same store: price, the rated figure, availability, and the provenance
    attributes themselves. A variant absent from every guide is NOT a
    defect — most variants legitimately never appear on one (wrong
    category, or the product is unranked and its table omitted).
    """
    mismatches = []
    rec = guide_prov.get(vid)
    if rec is None:
        return mismatches

    def bad(field, observed, expected):
        mismatches.append({"where": f"guide:{rec.get('guide', '?')}",
                           "field": field, "observed": observed,
                           "expected": expected})

    if rec.get("duplicate_in"):
        bad("duplicate", ",".join(rec["duplicate_in"]), "one guide per variant")
    for conflict in rec.get("internal_conflicts") or []:
        bad("internal-conflict", conflict, "one value per variant per page")

    if rec.get("withheld") == "price_unreadable":
        if site_build._usable_number(variant_data.get("price")):
            bad("withheld", "price_unreadable", "a displayable price")
        return mismatches
    if rec.get("withheld"):
        # Withheld by age or quarantine is a policy render, not a number.
        return mismatches

    stored_sku = variant_data.get("sku") or None
    if (rec.get("sku") or None) != stored_sku:
        bad("sku-attr", rec.get("sku"), stored_sku)
    if row_scraped_at and rec.get("scraped_at") != row_scraped_at:
        bad("scraped-at-attr", rec.get("scraped_at"), row_scraped_at)

    expected_price = site_build.price_display(variant_data.get("price"))
    shown = (rec["fields"].get("price") or {}).get("text", "")
    if (shown or "") != expected_price:
        bad("price", shown or "absent", expected_price or "no displayable price")

    expected_avail = site_build._avail_value(variant_data.get("available"))
    avail_field = rec["fields"].get("availability") or {}
    if avail_field.get("value") != expected_avail:
        bad("availability", avail_field.get("value"), expected_avail)

    # The rated figure, under the data-field name THIS product's guide
    # uses: "wh" for $/Wh (the same name the product page uses, so it is
    # the identical comparison) and "watt" for $/W. Which one applies is
    # decided by the product's guide, not by which specs happen to be
    # non-null: a power station has an output_w but is ranked on $/Wh,
    # and expecting a $/W from it would manufacture a mismatch per row.
    spec = site_build.guide_for_product(product)
    if spec is None:
        return mismatches
    metric = site_build._METRICS[spec["metric"]]
    expected_rating = site_build.expected_rating_display(
        variant_data, product, spec["metric"])
    shown_rating = (rec["fields"].get(metric["field"]) or {}).get("text") or None
    if shown_rating != expected_rating:
        bad(metric["field"], shown_rating or "absent", expected_rating or "absent")

    # The other metric's field must not appear at all — a $/W printed on a
    # $/Wh guide would be an unrated number nobody checks.
    for other_key, other in site_build._METRICS.items():
        if other_key == spec["metric"]:
            continue
        stray = (rec["fields"].get(other["field"]) or {}).get("text")
        if stray:
            bad(other["field"], stray, "absent on a "
                f"{metric['label']} guide")
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
    #
    # The live side must be normalized the SAME WAY the scraper normalizes
    # the stored side, or the two are not comparable. shopify.py drops a
    # compare_at that is not actually a discount (`if was_price <= price:
    # was_price = None`); comparing that stored None against the RAW live
    # compare_at reported a move that never happened, and no re-scrape
    # could ever clear it because the stored row was already correct.
    # Four triples across three retailers hit this in one expansion
    # (DELTA Pro 3 @ shop-solar-kits: price $2,799.00 / compare_at
    # $2,644.09; MEGA 410 @ rich-solar: compare_at exactly == price).
    # This is a hop-implementation bug, not a taxonomy change: STALE still
    # means "stored disagrees with live" — it just has to be true.
    live_was = live_variant["compare_at_cents"]
    live_price = live_variant["price_cents"]
    if (live_was is not None and live_price is not None
            and live_was <= live_price):
        live_was = None
    if stored_was != live_was:
        moved("was_price", stored_was, live_was)

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


def listing_plain_text(title: str, body_html: str) -> str:
    """Listing text as a human reads it: tags stripped, entities decoded,
    whitespace collapsed. The one normalizer used on BOTH sides of the
    capacity-quote check, so a quote can be compared byte-for-byte."""
    text = re.sub(r"<[^>]+>", " ", body_html or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", f"{title or ''} {text}").strip()


def check_capacity_quote(product: dict, retailer_id: str,
                         live_title: str, live_body: str) -> str | None:
    """Is the recorded capacity quote really IN this retailer's listing?

    QUOTE_NOT_FOUND / FOUND / None (nothing claimed for this retailer).

    check_capacity above only asks whether SOME Wh figure near the claimed
    value exists. It cannot catch a `capacity_source` whose quotation marks
    contain a string the merchant never wrote — and one did: an
    `enphase-iq-battery-5p` source claimed listing-body '5000Wh' when both
    listings say "total usable energy capacity of 5.0 kWh". The figure was
    right and the quote was fabricated, so every numeric check passed.
    Provenance is this project's premise, so the quote itself is now
    evidence that gets verified.

    A notice, not an alarm: a merchant rewording their copy is normal and
    must not fail a run. It does mean the quote needs re-transcribing.
    """
    quotes = (product.get("specs") or {}).get("capacity_quotes") or {}
    quote = quotes.get(retailer_id)
    if not quote:
        return None
    return "FOUND" if quote in listing_plain_text(live_title, live_body) \
        else "QUOTE_NOT_FOUND"


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
    guide_prov = parse_guide_provenance(site_dir)
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
            # The shadow build's guides too: an entry must not clear while
            # a rebuild without it would render the guide wrong.
            shadow_state["guides"] = parse_guide_provenance(sdir)
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
                # Separately: is the recorded quote actually in the listing?
                # A right number with an invented quotation is still a
                # provenance failure (red team #5).
                if check_capacity_quote(
                    triple["product"], triple["retailer_id"],
                    live["title"], live["body_html"],
                ) == "QUOTE_NOT_FOUND":
                    notices.append(
                        f"QUOTE_NOT_FOUND: {triple['product_id']} at "
                        f"{triple['retailer_id']} - specs.capacity_quotes text "
                        f"is not present in the live listing; re-transcribe it"
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
            shadow_mismatches += check_guide_render(
                triple["variant_id"], triple["variant_data"], triple["product"],
                shadow_state.get("guides") or {},
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
            # Guides are a third render surface over the same store. They
            # share their freshness with the row behind them, so checking
            # them costs no live requests (LOW-9 / weakness-1).
            mismatches += check_guide_render(
                triple["variant_id"], triple["variant_data"], triple["product"],
                guide_prov, row_scraped_at=triple["row"].get("timestamp") or "")
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

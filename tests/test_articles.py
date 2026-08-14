"""Tests for the editorial layer: article pages, author identity, honesty rails.

An article is evergreen prose wrapped around live data blocks. The prose is
authored once; every number inside it is re-rendered from the price store on
each build through the SAME guide_entry() machinery the guides use. These
tests police the join: that the live blocks really are live and carry
provenance, that the withhold rules survive inside an article, and that the
prose never makes a claim the project cannot support.

Fixture timestamps are NOW-RELATIVE against a pinned clock, matching
test_build.py and test_guides.py.
"""

import json
import re
from datetime import timedelta
from html import unescape
from pathlib import Path

import pytest

from build import (
    ARTICLES,
    AUTHOR,
    STALE_MAX_HOURS,
    article_by_slug,
    build_site,
)
from tests.test_guides import (  # reuse the guide fixture catalog wholesale
    NOW,
    _write_json,
    _write_jsonl,
    parse_rows,
    rating_of,
    seed_data,
)

REPO_ROOT = Path(__file__).parent.parent
TEMPLATES_DIR = REPO_ROOT / "templates"

# Phrases that assert first-hand experience Helios does not have. "hands-on"
# is banned OUTRIGHT rather than only in affirmative form: a rule with
# exceptions rots, so our own disclaimers say "physically tested" instead.
HANDS_ON_PHRASES = [
    "we tested", "hands-on", "hands on", "in our testing", "our testing",
    "we tried", "we reviewed", "after testing", "we measured the",
    "our review unit", "we unboxed", "on our test bench",
]
POPULARITY_PHRASES = [
    "trusted by", "thousands of", "millions of", "award-winning",
    "industry-leading", "years of experience", "best in class", "#1 ",
]


def _ts(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


@pytest.fixture
def built(tmp_path):
    """The fixture catalog from test_guides, built with articles."""
    data_dir = seed_data(tmp_path)
    site_dir = tmp_path / "site"
    summary = build_site(data_dir=data_dir, site_dir=site_dir,
                         templates_dir=TEMPLATES_DIR, now=NOW)
    return data_dir, site_dir, summary


def article_paths(site_dir: Path):
    return [p for p in sorted((site_dir / "articles").glob("*.html"))
            if p.name != "index.html"]


def article_html(site_dir: Path, slug: str) -> str:
    return (site_dir / "articles" / f"{slug}.html").read_text(encoding="utf-8")


def flat(text: str) -> str:
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# The system renders
# ---------------------------------------------------------------------------

def test_all_eight_articles_render(built):
    _, site_dir, summary = built
    assert len(ARTICLES) == 8
    assert summary["article_pages"] == 8
    for spec in ARTICLES:
        path = site_dir / "articles" / f"{spec['slug']}.html"
        assert path.exists(), spec["slug"]
        assert spec["h1"] in path.read_text(encoding="utf-8")
    assert (site_dir / "articles" / "index.html").exists()


def test_article_slugs_are_unique_and_url_safe():
    slugs = [a["slug"] for a in ARTICLES]
    assert len(slugs) == len(set(slugs))
    for slug in slugs:
        assert re.fullmatch(r"[a-z0-9-]+", slug), slug
        assert article_by_slug(slug)["slug"] == slug
    assert article_by_slug("does-not-exist") is None


def test_every_article_leads_with_a_direct_answer(built):
    """The AI-retrieval pattern: answer the title question in the first
    block, not after six paragraphs of preamble."""
    _, site_dir, _ = built
    for path in article_paths(site_dir):
        html = path.read_text(encoding="utf-8")
        match = re.search(
            r'<p class="answer"><strong>Direct answer:</strong>(.*?)</p>',
            html, re.S)
        assert match, f"{path.name} has no Direct answer"
        answer = flat(re.sub(r"<[^>]+>", "", match.group(1)))
        assert len(answer.split()) >= 25, f"{path.name}: answer too thin"
        # the answer must precede the body. Match the DIV, not the string:
        # base.html's inline CSS declares .authorbox far earlier.
        assert html.index('<p class="answer">') < html.index(
            '<div class="authorbox">')


def test_articles_index_lists_every_article(built):
    _, site_dir, _ = built
    html = (site_dir / "articles" / "index.html").read_text(encoding="utf-8")
    for spec in ARTICLES:
        assert f'articles/{spec["slug"]}.html' in html
        assert spec["h1"] in html


def test_articles_are_reachable_from_the_nav_on_every_page(built):
    _, site_dir, _ = built
    for page in site_dir.rglob("*.html"):
        depth = len(page.relative_to(site_dir).parts) - 1
        prefix = "../" * depth
        assert f'href="{prefix}articles/index.html"' in \
            page.read_text(encoding="utf-8"), page.name


# ---------------------------------------------------------------------------
# Author identity
# ---------------------------------------------------------------------------

def test_every_article_carries_the_byline_and_author_block(built):
    _, site_dir, _ = built
    for path in article_paths(site_dir):
        html = path.read_text(encoding="utf-8")
        assert 'class="authorbox"' in html, path.name
        assert AUTHOR["name"] in html, path.name
        assert AUTHOR["bio"][:40] in flat(html), path.name
        assert "about.html" in html and "disclosure.html" in html, path.name


def test_author_bio_claims_nothing_but_building_this_tracker():
    """STRICT: no credentials, no experience claims, no test history. The
    bio may only assert what this repository itself demonstrates."""
    bio = (AUTHOR["bio"] + " " + AUTHOR["disclaimer"]).lower()
    for phrase in HANDS_ON_PHRASES + POPULARITY_PHRASES:
        assert phrase not in bio, phrase
    for phrase in ("engineer", "certified", "expert", "veteran", "decade",
                   "phd", "degree", "installer", "electrician", "consultant"):
        assert phrase not in bio, f"unverifiable credential claim: {phrase}"
    # ...and it must actually say the one true thing
    assert "builds and runs helios" in bio


def test_about_page_names_the_operator(built):
    _, site_dir, _ = built
    text = flat((site_dir / "about.html").read_text(encoding="utf-8"))
    assert "Who runs this" in text
    assert "Brandon Hall" in text
    assert "built and operated by" in text


# ---------------------------------------------------------------------------
# The honesty rails
# ---------------------------------------------------------------------------

def test_no_page_claims_hands_on_experience(built):
    """The differentiator, enforced. Helios has never physically tested a
    unit, so no page may imply otherwise — including our own disclaimers,
    which say "physically tested" rather than using the banned phrase."""
    _, site_dir, _ = built
    for page in site_dir.rglob("*.html"):
        low = page.read_text(encoding="utf-8").lower()
        for phrase in HANDS_ON_PHRASES:
            assert phrase not in low, f"{page.name}: {phrase!r}"


def test_no_page_makes_unverifiable_popularity_claims(built):
    _, site_dir, _ = built
    for page in site_dir.rglob("*.html"):
        low = page.read_text(encoding="utf-8").lower()
        for phrase in POPULARITY_PHRASES:
            assert phrase not in low, f"{page.name}: {phrase!r}"


def test_every_article_states_the_no_testing_position(built):
    """Not just the absence of a false claim — the presence of a true one."""
    _, site_dir, _ = built
    for path in article_paths(site_dir):
        text = flat(path.read_text(encoding="utf-8")).lower()
        assert ("not physically tested" in text
                or "never physically tested" in text
                or "does not physically test" in text
                or "have not run either" in text), path.name


def test_prose_never_hardcodes_a_dollar_figure():
    """Prose ages; data blocks refresh. A price typed into a sentence is a
    number nothing recomputes — the exact failure this system exists to
    prevent. Currency amounts in prose are therefore banned outright.

    Illustrative arithmetic (kWh/year, Wh/day) is fine and is not currency.
    """
    money_in_prose = re.compile(r"\$[\d,]+(?:\.\d\d)?")
    for spec in ARTICLES:
        for block in spec["blocks"]:
            if block["kind"] not in ("prose", "callout"):
                continue
            hits = money_in_prose.findall(block["html"])
            # "$2,000" in the under-$2000 article is a scope threshold that
            # also appears as a machine-checked max_price on the block.
            allowed = {"$2,000"}
            assert not (set(hits) - allowed), (spec["slug"], hits)


def test_under_2000_threshold_in_prose_matches_the_enforced_filter():
    """The one currency figure allowed in prose must be the same number the
    ranking block actually filters on, or the page promises a scope it does
    not apply."""
    spec = article_by_slug("best-home-backup-battery-under-2000")
    ranking = next(b for b in spec["blocks"] if b["kind"] == "ranking")
    assert ranking["max_price"] == 2000
    assert "$2,000" in spec["h1"] or "2,000" in spec["h1"]


# ---------------------------------------------------------------------------
# Live data blocks
# ---------------------------------------------------------------------------

def test_article_ratings_equal_the_product_page_for_the_same_variant(built):
    """An article may not disagree with a product page about a number."""
    _, site_dir, _ = built
    compared = 0
    for path in article_paths(site_dir):
        for arow in parse_rows(path.read_text(encoding="utf-8")):
            if rating_of(arow) is None:
                continue
            product_page = (site_dir / "products"
                            / f"{arow['attrs']['data-product-id']}.html")
            if not product_page.exists():
                continue
            for prow in parse_rows(product_page.read_text(encoding="utf-8")):
                if (prow["attrs"].get("data-tier") == arow["attrs"]["data-tier"]
                        and prow["attrs"].get("data-variant-id")
                        == arow["attrs"].get("data-variant-id")):
                    assert prow["fields"].get("wh") == rating_of(arow)
                    compared += 1
    assert compared >= 3, "cross-surface check proved nothing"


def test_bundle_never_rated_inside_an_article(built):
    """PLAN 2b holds inside article data blocks too."""
    _, site_dir, _ = built
    bundle_skus = {"RACK-2", "SKU-BK", "PAL-8", "PAL-12"}
    rated = 0
    for path in article_paths(site_dir):
        for row in parse_rows(path.read_text(encoding="utf-8")):
            has = rating_of(row) is not None
            rated += int(has)
            if row["attrs"].get("data-sku") in bundle_skus:
                assert not has, f"{path.name}: bundle rated {rating_of(row)}"
    assert rated > 0, "no rated rows in any article — assertion is vacuous"


def test_sold_out_offer_never_sets_an_article_headline(built):
    """HIGH-1's rule, inside articles."""
    _, site_dir, _ = built
    for path in article_paths(site_dir):
        html = path.read_text(encoding="utf-8")
        for block in re.findall(r'<p class="muted" [^>]*>.*?class="headline".*?</p>', html, re.S):
            assert 'data-value="false"' not in block, path.name


def test_stale_rows_are_withheld_inside_articles(tmp_path):
    data_dir = seed_data(tmp_path)
    path = data_dir / "prices" / "station-a.jsonl"
    rows = [json.loads(x) for x in
            path.read_text(encoding="utf-8").splitlines() if x.strip()]
    for row in rows:
        row["timestamp"] = _ts(STALE_MAX_HOURS + 1)
    _write_jsonl(path, rows)
    site_dir = tmp_path / "site"
    build_site(data_dir=data_dir, site_dir=site_dir,
               templates_dir=TEMPLATES_DIR, now=NOW)
    for page in article_paths(site_dir):
        html = page.read_text(encoding="utf-8")
        assert "$500.00" not in html, page.name
        assert "$600.00" not in html, page.name


def test_quarantined_variant_is_withheld_inside_articles(tmp_path):
    data_dir = seed_data(tmp_path)
    _write_json(data_dir / "quarantine.json", {
        "r1:station-a:41": {
            "sku": "SKU-A", "tier_last_seen": "main", "reason": "render_defect",
            "observed": "$1.00", "expected": "$500.00",
            "first_seen": _ts(24), "last_seen": _ts(1),
            "consecutive_failures": 1, "unobserved_audits": 0,
        }
    })
    site_dir = tmp_path / "site"
    build_site(data_dir=data_dir, site_dir=site_dir,
               templates_dir=TEMPLATES_DIR, now=NOW)
    found = False
    for page in article_paths(site_dir):
        html = page.read_text(encoding="utf-8")
        assert "$500.00" not in html, page.name
        if 'data-withheld="quarantine"' in html:
            found = True
    assert found, "quarantine marker never rendered in any article"


def test_specs_table_says_not_published_rather_than_guessing(built):
    """The fixture's panel-no-output has no output_w. A spec table must
    show the gap, not omit the row and imply we checked."""
    _, site_dir, _ = built
    html = article_html(site_dir, "best-power-station-for-camping")
    assert "not published" in html


def test_answer_degrades_when_the_data_is_missing(tmp_path):
    """Withhold-in-sentence-form: with no price rows at all, the ledes must
    still be honest sentences rather than blanks or stale numbers."""
    data_dir = seed_data(tmp_path)
    for jsonl in (data_dir / "prices").glob("*.jsonl"):
        jsonl.unlink()
    site_dir = tmp_path / "site"
    build_site(data_dir=data_dir, site_dir=site_dir,
               templates_dir=TEMPLATES_DIR, now=NOW)
    for path in article_paths(site_dir):
        html = path.read_text(encoding="utf-8")
        match = re.search(
            r'<p class="answer"><strong>Direct answer:</strong>(.*?)</p>',
            html, re.S)
        answer = flat(re.sub(r"<[^>]+>", "", match.group(1)))
        assert len(answer.split()) >= 20, path.name
        assert "None" not in answer, f"{path.name}: leaked a None"
        assert "$0.00" not in answer, path.name


# ---------------------------------------------------------------------------
# Citations (article 7)
# ---------------------------------------------------------------------------

def test_sale_article_cites_dated_external_sources(built):
    _, site_dir, _ = built
    spec = article_by_slug("when-do-power-stations-go-on-sale")
    citations = next(b for b in spec["blocks"] if b["kind"] == "citations")
    assert len(citations["sources"]) >= 3
    for item in citations["sources"]:
        assert item["url"].startswith("https://")
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", item["date"]), item
        assert item["publisher"] and item["title"]
    html = article_html(site_dir, "when-do-power-stations-go-on-sale")
    for item in citations["sources"]:
        assert item["url"] in html


def test_sale_article_admits_its_history_is_thin(built):
    _, site_dir, _ = built
    text = flat(article_html(site_dir, "when-do-power-stations-go-on-sale"))
    assert "Our price history is very short" in text
    assert "2026-08-13" in text
    # alerts are planned, and the page must say so unambiguously
    assert "planned and does not exist" in text


def test_history_block_counts_come_from_the_store(built):
    _, site_dir, _ = built
    html = article_html(site_dir, "when-do-power-stations-go-on-sale")
    days = re.search(r'data-field="history-days">(\d+)<', html)
    rows = re.search(r'data-field="history-rows">(\d+)<', html)
    assert days and rows
    assert int(rows.group(1)) > 0
    assert int(days.group(1)) >= 1


# ---------------------------------------------------------------------------
# Retailer article (BLOCKER-2's lesson, in prose)
# ---------------------------------------------------------------------------

def test_retailer_article_makes_only_observed_claims(built):
    _, site_dir, _ = built
    text = flat(article_html(site_dir, "is-shop-solar-kits-legit"))
    # it must disclaim the things it cannot know
    assert "never bought" in text.lower()
    assert "We hold no data" in text
    for phrase in ("fast shipping", "great support", "excellent service",
                   "highly recommend", "would recommend", "trustworthy seller"):
        assert phrase not in text.lower(), phrase


def test_retailer_report_numbers_are_computed(built):
    _, site_dir, _ = built
    html = article_html(site_dir, "is-shop-solar-kits-legit")
    mapped = re.search(r'data-field="mapped-products">(\d+)<', html)
    assert mapped, "retailer report did not render"
    # the fixture maps no products through handle_maps, so the honest
    # answer is zero rather than a borrowed figure
    assert mapped.group(1).isdigit()


def test_no_shipping_claim_is_invented(built):
    """retailers.json holds no shipping data. The page must say so instead
    of sourcing a figure from anywhere else."""
    from build import load_json
    retailers = load_json(REPO_ROOT / "data" / "retailers.json")
    assert not any("shipping" in r for r in retailers), \
        "shipping data now exists — the article should be updated to use it"
    _, site_dir, _ = built
    text = flat(article_html(site_dir, "is-shop-solar-kits-legit"))
    assert "no shipping data at all" in text


# ---------------------------------------------------------------------------
# Sitemap / SEO
# ---------------------------------------------------------------------------

def test_sitemap_lists_every_article_exactly_once(tmp_path):
    data_dir = seed_data(tmp_path)
    _write_json(data_dir / "site_config.json",
                {"site_base_url": "https://helios.test"})
    site_dir = tmp_path / "site"
    build_site(data_dir=data_dir, site_dir=site_dir,
               templates_dir=TEMPLATES_DIR, now=NOW)
    sitemap = (site_dir / "sitemap.xml").read_text(encoding="utf-8")
    locs = re.findall(r"<loc>([^<]+)</loc>", sitemap)
    assert len(locs) == len(set(locs))
    for spec in ARTICLES:
        want = f"https://helios.test/articles/{spec['slug']}.html"
        assert locs.count(want) == 1, spec["slug"]
    assert locs.count("https://helios.test/articles/index.html") == 1


def test_article_meta_descriptions_are_data_derived(built):
    _, site_dir, _ = built
    for path in article_paths(site_dir):
        html = path.read_text(encoding="utf-8")
        desc = re.search(r'<meta name="description" content="([^"]*)"', html)
        assert desc, path.name
        assert 0 < len(desc.group(1)) <= 160, path.name
    # the head-to-head description should carry a live rating
    head = article_html(site_dir, "ecoflow-delta-pro-3-vs-bluetti-ac200l")
    desc = re.search(r'<meta name="description" content="([^"]*)"',
                     head).group(1)
    assert "/Wh" in desc or "rateable" in desc


def test_article_titles_are_unique_across_the_site(built):
    _, site_dir, _ = built
    titles = [re.search(r"<title>(.*?)</title>", p.read_text(encoding="utf-8"),
                        re.S).group(1)
              for p in site_dir.rglob("*.html")]
    assert len(titles) == len(set(titles))


def test_articles_have_no_external_assets(built):
    _, site_dir, _ = built
    for path in article_paths(site_dir):
        html = path.read_text(encoding="utf-8")
        assert "<script" not in html
        assert not re.search(r'<link[^>]+rel="stylesheet"', html)
        assert not re.search(r'<img[^>]+src="https?:', html)


def test_outbound_retailer_links_are_nofollow_sponsored(built):
    _, site_dir, _ = built
    checked = 0
    for path in article_paths(site_dir):
        for row in parse_rows(path.read_text(encoding="utf-8")):
            for link in row["links"]:
                if link.get("href", "").startswith("http"):
                    assert link.get("rel") == "nofollow sponsored"
                    checked += 1
    assert checked > 0


def test_citation_links_are_nofollow_but_not_sponsored(built):
    """A cited news article is not a commercial link and must not be
    labelled as one — mislabelling in either direction is a lie."""
    _, site_dir, _ = built
    html = article_html(site_dir, "when-do-power-stations-go-on-sale")
    block = html[html.find('<ul class="citations">'):]
    block = block[:block.find("</ul>")]
    links = re.findall(r'<a href="([^"]+)" rel="([^"]*)"', block)
    assert links
    for href, rel in links:
        assert href.startswith("https://")
        assert rel == "nofollow", (href, rel)


# ---------------------------------------------------------------------------
# The macros are shared, not copied
# ---------------------------------------------------------------------------

def test_guide_and_article_share_one_table_implementation():
    """A second copy of the row markup is how an article ends up publishing
    a number a guide withholds."""
    macros = (TEMPLATES_DIR / "_macros.html").read_text(encoding="utf-8")
    for name in ("annotations", "availability", "rowattrs", "offer_table",
                 "headline"):
        assert f"macro {name}(" in macros
    for template in ("guide.html", "article.html"):
        text = (TEMPLATES_DIR / template).read_text(encoding="utf-8")
        assert 'from "_macros.html" import' in text, template
        assert "{%- macro " not in text and "{% macro " not in text, template


def test_article_rows_are_visible_to_the_audit(built):
    """Articles are a render surface; audit.py must be able to join them."""
    from audit import parse_content_provenance
    _, site_dir, _ = built
    prov = parse_content_provenance(site_dir)
    assert prov, "audit sees nothing on the content pages"
    article_vids = set()
    for path in article_paths(site_dir):
        for row in parse_rows(path.read_text(encoding="utf-8")):
            vid = row["attrs"].get("data-variant-id")
            if vid:
                article_vids.add(vid)
    assert article_vids
    assert article_vids <= set(prov), "audit misses article variants"
    for vid in article_vids:
        assert not prov[vid]["internal_conflicts"], (vid, prov[vid])


# ---------------------------------------------------------------------------
# An article-shaped fixture
# ---------------------------------------------------------------------------
# The guide fixture is a synthetic catalog; the articles reference REAL
# product ids. Seeding those ids here keeps the article tests self-contained
# while still exercising the ids the shipped articles actually name — so a
# renamed or dropped product fails a test instead of silently emptying a
# section of the site.

ARTICLE_RETAILERS = [
    {"id": "shop-solar-kits", "name": "Shop Solar Kits",
     "url": "https://shopsolarkits.example", "scraper_type": "shopify",
     "active": True, "priority": 1,
     "affiliate": {"network": "unknown", "commission": "unknown",
                   "cookie_days": None, "link_template": "",
                   "notes": "Program terms unverified."}},
    {"id": "wild-oak-trail", "name": "Wild Oak Trail",
     "url": "https://wildoaktrail.example", "scraper_type": "shopify",
     "active": True, "priority": 2, "affiliate": None},
]


def _aproduct(pid, name, category, cap=None, out=None, chem=None, lb=None):
    return {"id": pid, "name": name, "brand": "TestBrand",
            "category": category,
            "specs": {"capacity_wh": cap, "output_w": out, "chemistry": chem,
                      "weight_lb": lb, "capacity_source": "test" if cap else None},
            "active": True, "notes": None}


def seed_article_data(tmp_path: Path) -> Path:
    """A catalog carrying every product id the shipped articles reference."""
    data_dir = tmp_path / "data"
    prices = data_dir / "prices"
    prices.mkdir(parents=True)

    products = [
        _aproduct("ecoflow-delta-pro-3", "EcoFlow DELTA Pro 3",
                  "portable-power-station", cap=4096, out=4000, chem="LiFePO4"),
        _aproduct("bluetti-ac200l", "Bluetti AC200L",
                  "portable-power-station", cap=2048, out=2400, chem="LiFePO4",
                  lb=62.4),
        _aproduct("ecoflow-river-2-pro", "EcoFlow RIVER 2 Pro",
                  "portable-power-station", cap=768, out=800, chem="LiFePO4",
                  lb=17.2),
        _aproduct("ecoflow-river-3", "EcoFlow RIVER 3",
                  "portable-power-station", cap=245, out=300, chem="LiFePO4"),
        # NCM, and sold out at both retailers -> exercises the unranked path
        _aproduct("ecoflow-delta-max", "EcoFlow DELTA Max",
                  "portable-power-station", cap=2016, out=2400, chem="NCM",
                  lb=48.0),
        _aproduct("bluetti-ac180", "Bluetti AC180", "portable-power-station",
                  cap=None, out=1800, chem="LiFePO4", lb=35.3),
        _aproduct("anker-solix-f2600", "Anker SOLIX F2600",
                  "portable-power-station", cap=2560, out=2400, chem="LiFePO4",
                  lb=67.2),
        _aproduct("eg4-ll-s-48v-100ah", "EG4 LL-S 48V 100Ah",
                  "server-rack-battery", cap=5120, chem="LiFePO4"),
        _aproduct("eg4-lifepower4", "EG4 LifePower4 V2",
                  "server-rack-battery", cap=5120, chem="LiFePO4"),
        _aproduct("expensive-wall", "Expensive Wall Battery", "home-battery",
                  cap=16000, chem="LiFePO4"),
    ]
    _write_json(data_dir / "products.json", products)
    _write_json(data_dir / "retailers.json", ARTICLE_RETAILERS)
    _write_json(data_dir / "handle_maps.json", {
        "shop-solar-kits": {p["id"]: f"h-{p['id']}" for p in products},
        "wild-oak-trail": {p["id"]: f"h-{p['id']}" for p in products
                           if p["id"] != "eg4-ll-s-48v-100ah"},
    })

    def row(rid, pid, variants, ts_hours=3):
        return {"retailer_id": rid,
                "retailer_name": dict((r["id"], r["name"])
                                      for r in ARTICLE_RETAILERS)[rid],
                "timestamp": _ts(ts_hours),
                "url": f"https://{rid}.example/products/{pid}",
                "variants": variants, "in_stock": True}

    def v(price, raw, vid, sku=None, available=True):
        return {"price": price, "was_price": None, "available": available,
                "raw_variant": raw, "variant_id": vid, "sku": sku}

    _write_jsonl(prices / "ecoflow-delta-pro-3.jsonl", [
        row("shop-solar-kits", "ecoflow-delta-pro-3",
            {"main": v(2799.00, "DELTA PRO 3 [Main Unit Only]", 3001,
                       "EFDELTAPRO3-US"),
             "kit": v(3999.00, "DELTA PRO 3 + 400W Panel", 3002, "EFDP3-KIT")}),
        row("wild-oak-trail", "ecoflow-delta-pro-3",
            {"main": v(2799.00, "EcoFlow DELTA Pro 3 (Main Unit Only)", 3003,
                       "EFDELTAPRO3-US")}),
    ])
    _write_jsonl(prices / "bluetti-ac200l.jsonl", [
        row("wild-oak-trail", "bluetti-ac200l",
            {"main": v(1599.00, "AC200L Only", 3010, "AC200L-US")}),
    ])
    _write_jsonl(prices / "ecoflow-river-2-pro.jsonl", [
        row("shop-solar-kits", "ecoflow-river-2-pro",
            {"main": v(569.00, "River 2 Pro [Main Unit Only]", 3020,
                       "ZMR620-B-US")}),
    ])
    _write_jsonl(prices / "ecoflow-river-3.jsonl", [
        row("shop-solar-kits", "ecoflow-river-3",
            {"main": v(199.00, "River 3 [Main Unit Only]", 3030,
                       "EFRIVER3-US")}),
        row("wild-oak-trail", "ecoflow-river-3",
            {"main": v(219.00, "Default Title", 3031, "EFRIVER3-US")}),
    ])
    # sold out at every retailer -> must never take a rank or a headline
    _write_jsonl(prices / "ecoflow-delta-max.jsonl", [
        row("shop-solar-kits", "ecoflow-delta-max",
            {"main": v(1049.00, "DELTA MAX [Unit Only]", 3040, "DELTA2000-US",
                       available=False)}),
    ])
    _write_jsonl(prices / "bluetti-ac180.jsonl", [
        row("wild-oak-trail", "bluetti-ac180",
            {"main": v(699.00, "AC180 Only", 3050, "AC180-US")}),
    ])
    _write_jsonl(prices / "anker-solix-f2600.jsonl", [
        row("shop-solar-kits", "anker-solix-f2600",
            {"main": v(1254.00, "Anker Solix F2600 [Main Unit Only]", 3060,
                       "A1781111")}),
    ])
    # unit + multi-pack bundle on ONE product: the $/Wh discipline, visible
    _write_jsonl(prices / "eg4-ll-s-48v-100ah.jsonl", [
        row("shop-solar-kits", "eg4-ll-s-48v-100ah",
            {"one": v(1536.99, "1 Battery Only", 3070, "LLS-1"),
             "two": v(3072.00, "2 Batteries Only", 3071, "LLS-2")}),
    ])
    _write_jsonl(prices / "eg4-lifepower4.jsonl", [
        row("shop-solar-kits", "eg4-lifepower4",
            {"main": v(1470.99, "Default Title", 3080, "LP4-1")}),
    ])
    # over the $2,000 ceiling -> must be excluded from that article's scope
    _write_jsonl(prices / "expensive-wall.jsonl", [
        row("shop-solar-kits", "expensive-wall",
            {"main": v(3399.99, "Default Title", 3090, "WALL-1")}),
    ])
    return data_dir


@pytest.fixture
def built_articles(tmp_path):
    data_dir = seed_article_data(tmp_path)
    site_dir = tmp_path / "site"
    summary = build_site(data_dir=data_dir, site_dir=site_dir,
                         templates_dir=TEMPLATES_DIR, now=NOW)
    return data_dir, site_dir, summary


def test_every_product_an_article_names_exists_in_the_real_catalog():
    """A renamed or dropped product must fail a test, not silently empty a
    section of a shipped article."""
    from build import load_json
    catalog = {p["id"] for p in
               load_json(REPO_ROOT / "data" / "products.json")}
    for spec in ARTICLES:
        for block in spec["blocks"]:
            for pid in block.get("ids") or []:
                assert pid in catalog, f"{spec['slug']} references {pid}"


def test_articles_render_live_prices_with_provenance_on_real_ids(built_articles):
    """The provenance check that matters, against a catalog carrying the
    ids the shipped articles actually name."""
    _, site_dir, _ = built_articles
    for path in article_paths(site_dir):
        html = path.read_text(encoding="utf-8")
        assert ('data-field="price"' in html
                or 'data-field="best-price"' in html), path.name
        for row in parse_rows(html):
            assert row["attrs"].get("data-scraped-at"), path.name
            assert row["attrs"].get("data-retailer-id"), path.name
            assert "data-tier" in row["attrs"], path.name
            assert "data-sku" in row["attrs"], path.name


def test_head_to_head_shows_both_sides_live(built_articles):
    _, site_dir, _ = built_articles
    html = article_html(site_dir, "ecoflow-delta-pro-3-vs-bluetti-ac200l")
    assert "$2,799.00" in html and "$1,599.00" in html
    assert "$0.68/Wh" in html          # 2799 / 4096
    assert "$0.78/Wh" in html          # 1599 / 2048
    # the kit variant is a bundle: price shown, no rating
    assert "$3,999.00" in html
    assert "$0.98/Wh" not in html      # 3999 / 4096 -- never derived


def test_price_ceiling_excludes_dearer_products(built_articles):
    _, site_dir, _ = built_articles
    html = article_html(site_dir, "best-home-backup-battery-under-2000")
    assert "$1,470.99" in html and "$1,536.99" in html
    assert "$3,399.99" not in html, "a product over the ceiling was listed"
    assert "Expensive Wall Battery" not in html


def test_multipack_never_rated_in_the_methodology_article(built_articles):
    """The article that explains the rule must demonstrate it."""
    _, site_dir, _ = built_articles
    html = article_html(site_dir, "what-dollars-per-wh-tells-you")
    assert "$1,536.99" in html
    assert "$0.30/Wh" in html          # the single battery
    assert "$3,072.00" in html         # the pack price is still shown
    assert "$0.60/Wh" not in html      # 3072 / 5120 -- the wrong number


def test_sold_out_product_is_not_the_camping_headline(built_articles):
    _, site_dir, _ = built_articles
    html = article_html(site_dir, "best-power-station-for-camping")
    assert "EcoFlow DELTA Max" in html
    for block in re.findall(r'<p class="muted" [^>]*>.*?class="headline".*?</p>', html, re.S):
        assert "DELTA Max" not in block


# ---------------------------------------------------------------------------
# The prose must follow the numbers (red team #5: HIGH-1..HIGH-4)
# ---------------------------------------------------------------------------
# Four defects of one shape: a comparative or a verdict typed into a
# sentence, sitting next to computed figures that could contradict it. The
# tests below re-derive the claim from the data independently and, where the
# claim has a direction, flip the data and require the prose to flip.

H2H = "ecoflow-delta-pro-3-vs-bluetti-ac200l"
SSK = "is-shop-solar-kits-legit"


def answer_of(site_dir: Path, slug: str) -> str:
    """The Direct-answer lede as flat text."""
    html = article_html(site_dir, slug)
    match = re.search(
        r'<p class="answer"><strong>Direct answer:</strong>(.*?)</p>',
        html, re.S)
    assert match, slug
    return flat(re.sub(r"<[^>]+>", "", match.group(1)))


def _build(data_dir: Path, site_dir: Path):
    build_site(data_dir=data_dir, site_dir=site_dir,
               templates_dir=TEMPLATES_DIR, now=NOW)
    return site_dir


def _reprice(data_dir: Path, product_id: str, price: float) -> None:
    path = data_dir / "prices" / f"{product_id}.jsonl"
    rows = [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for row in rows:
        for variant in row["variants"].values():
            variant["price"] = price
    _write_jsonl(path, rows)


def test_head_to_head_lede_direction_follows_the_numbers(tmp_path):
    """HIGH-4. The lede used to hardcode 'currently costs less per
    watt-hour' and 'the DELTA Pro 3 is the cheaper energy'. Repricing the
    AC200L then made it assert the opposite of the two rates it quoted in
    the same sentence."""
    data_dir = seed_article_data(tmp_path)

    # Baseline: DP3 $2,799/4,096Wh = $0.68; AC200L $1,599/2,048Wh = $0.78.
    base = answer_of(_build(data_dir, tmp_path / "site-base"), H2H)
    assert "costs less per watt-hour" in base
    assert "the DELTA Pro 3 is the cheaper energy" in base
    assert "The AC200L is the smaller cheque" in base

    # Flip the ENERGY direction: AC200L at $999 = $0.49/Wh, under the DP3.
    _reprice(data_dir, "bluetti-ac200l", 999.00)
    flipped = answer_of(_build(data_dir, tmp_path / "site-cheap"), H2H)
    assert "$0.49/Wh" in flipped and "$0.68/Wh" in flipped
    assert "costs more per watt-hour" in flipped
    assert "the AC200L is the cheaper energy" in flipped
    assert "the DELTA Pro 3 is the cheaper energy" not in flipped
    assert "costs less per watt-hour" not in flipped

    # Flip the CHEQUE direction: AC200L at $3,999, dearer than the DP3.
    _reprice(data_dir, "bluetti-ac200l", 3999.00)
    dear = answer_of(_build(data_dir, tmp_path / "site-dear"), H2H)
    assert "The DELTA Pro 3 is the smaller cheque" in dear
    assert "The AC200L is the smaller cheque" not in dear
    assert "the DELTA Pro 3 is the cheaper energy" in dear


def test_head_to_head_lede_is_level_when_the_rates_are_level(tmp_path):
    """No direction in the data, no direction in the prose."""
    data_dir = seed_article_data(tmp_path)
    # 4,096 Wh and 2,048 Wh at the same $/Wh: $1,399.50 = half of $2,799.
    _reprice(data_dir, "bluetti-ac200l", 1399.50)
    answer = answer_of(_build(data_dir, tmp_path / "site"), H2H)
    assert "matches it on cost per watt-hour" in answer
    assert "neither is the cheaper energy today" in answer
    assert "cheaper energy: " not in answer


# --- the retailer buckets ---------------------------------------------------

BUCKET_RETAILER = {
    "id": "alte-store", "name": "AltE Store",
    "url": "https://altestore.example", "scraper_type": "shopify",
    "active": True, "priority": 3, "affiliate": None,
}

# product id -> {retailer_id: price}. Crafted so every bucket is occupied,
# including the two the old code got wrong: a tie at the top (which it
# reported as "most expensive") and a strictly mid-pack product (which it
# counted nowhere at all).
BUCKET_CASES = {
    "bucket-strict-cheap": {"shop-solar-kits": 100.0, "wild-oak-trail": 120.0,
                            "alte-store": 130.0},
    "bucket-strict-dear": {"shop-solar-kits": 130.0, "wild-oak-trail": 100.0,
                           "alte-store": 120.0},
    "bucket-tied-low": {"shop-solar-kits": 100.0, "wild-oak-trail": 100.0,
                        "alte-store": 120.0},
    "bucket-tied-top": {"shop-solar-kits": 120.0, "wild-oak-trail": 120.0,
                        "alte-store": 100.0},
    "bucket-mid": {"shop-solar-kits": 110.0, "wild-oak-trail": 100.0,
                   "alte-store": 120.0},
    # only one retailer -> not comparable, must not be counted anywhere
    "bucket-alone": {"shop-solar-kits": 100.0},
}


def seed_bucket_data(tmp_path: Path) -> Path:
    """The article catalog plus a third retailer and one product per bucket."""
    data_dir = seed_article_data(tmp_path)
    retailers = json.loads(
        (data_dir / "retailers.json").read_text(encoding="utf-8"))
    retailers.append(BUCKET_RETAILER)
    _write_json(data_dir / "retailers.json", retailers)
    names = {r["id"]: r["name"] for r in retailers}

    products = json.loads(
        (data_dir / "products.json").read_text(encoding="utf-8"))
    handle_maps = json.loads(
        (data_dir / "handle_maps.json").read_text(encoding="utf-8"))
    for index, (pid, prices) in enumerate(BUCKET_CASES.items()):
        products.append(_aproduct(pid, f"Bucket Case {index}",
                                  "portable-power-station", cap=1000,
                                  chem="LiFePO4"))
        rows = []
        for rid, price in prices.items():
            handle_maps.setdefault(rid, {})[pid] = f"h-{pid}"
            rows.append({
                "retailer_id": rid, "retailer_name": names[rid],
                "timestamp": _ts(3),
                "url": f"https://{rid}.example/products/{pid}",
                "in_stock": True,
                "variants": {"main": {
                    "price": price, "was_price": None, "available": True,
                    "raw_variant": "Default Title",
                    "variant_id": 9000 + index * 10 + len(rows),
                    "sku": f"{pid}-SKU"}},
            })
        _write_jsonl(data_dir / "prices" / f"{pid}.jsonl", rows)
    _write_json(data_dir / "products.json", products)
    _write_json(data_dir / "handle_maps.json", handle_maps)
    return data_dir


def independent_positions(data_dir: Path, retailer_id: str) -> dict:
    """Recompute the cross-retailer position tally straight off the store.

    Deliberately NOT a call into build: it re-reads data/prices/*.jsonl and
    applies the definitions in words, so a bug shared with _retailer_report
    cannot hide behind agreement with itself.

    It models only the withholding the fixtures exercise — sold out, and a
    price that is not a usable number. It does NOT model quarantine or
    staleness, so a fixture that adds either will legitimately diverge from
    the rendered tally; check the buckets still SUM in that case rather than
    expecting equality.
    """
    counts = {"strictly_cheapest": 0, "tied_low": 0, "mid_pack": 0,
              "tied_top": 0, "strictly_dearest": 0, "same_everywhere": 0}
    compared = 0
    for path in sorted((data_dir / "prices").glob("*.jsonl")):
        by_retailer: dict[str, float] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            for variant in row["variants"].values():
                if variant.get("available") is False:
                    continue
                price = variant.get("price")
                if not isinstance(price, (int, float)) or price <= 0:
                    continue
                current = by_retailer.get(row["retailer_id"])
                if current is None or price < current:
                    by_retailer[row["retailer_id"]] = price
        if len(by_retailer) < 2 or retailer_id not in by_retailer:
            continue
        compared += 1
        mine = by_retailer[retailer_id]
        others = [p for rid, p in by_retailer.items() if rid != retailer_id]
        if all(p == mine for p in others):
            counts["same_everywhere"] += 1
        elif all(p >= mine for p in others):
            counts["strictly_cheapest" if all(p > mine for p in others)
                   else "tied_low"] += 1
        elif all(p <= mine for p in others):
            counts["strictly_dearest" if all(p < mine for p in others)
                   else "tied_top"] += 1
        else:
            counts["mid_pack"] += 1
    return {"compared": compared, **counts}


def rendered_positions(site_dir: Path) -> dict:
    html = article_html(site_dir, SSK)
    found = dict(re.findall(
        r'data-field="position-([a-z-]+)">(\d+)<', html))
    compared = re.search(r'data-field="compared">(\d+)<', html)
    return {"compared": int(compared.group(1)) if compared else 0,
            **{key.replace("-", "_"): int(value)
               for key, value in found.items()}}


def test_retailer_buckets_sum_to_the_comparable_product_count(tmp_path):
    """HIGH-3. '17 products... cheapest 6, most expensive 4, tied 4' = 14:
    the classification had no else branch, so mid-pack products fell into no
    bucket and the sentence did not add up to its own denominator."""
    data_dir = seed_bucket_data(tmp_path)
    site_dir = _build(data_dir, tmp_path / "site")
    expected = independent_positions(data_dir, "shop-solar-kits")
    rendered = rendered_positions(site_dir)

    buckets = {key: value for key, value in rendered.items()
               if key != "compared"}
    assert len(buckets) == 6, buckets
    assert sum(buckets.values()) == rendered["compared"]
    assert rendered == expected

    # ...and the count itself is the comparable set, not the mapped set.
    handle_maps = json.loads(
        (data_dir / "handle_maps.json").read_text(encoding="utf-8"))
    assert rendered["compared"] < len(handle_maps["shop-solar-kits"])
    assert rendered["compared"] == expected["compared"] > 0


def test_a_tie_at_the_top_is_never_called_most_expensive(tmp_path):
    """HIGH-2. `mine == low == high` was the only tie the old code knew, so
    sharing the TOP price with another retailer was reported as being the
    dearest of them."""
    data_dir = seed_bucket_data(tmp_path)
    site_dir = _build(data_dir, tmp_path / "site")
    rendered = rendered_positions(site_dir)
    # one crafted tie at the top, one crafted strictly-dearest product
    assert rendered["tied_top"] == 1
    assert rendered["strictly_dearest"] == 1
    assert rendered["mid_pack"] == 1
    assert rendered["tied_low"] == 1

    answer = answer_of(site_dir, SSK)
    assert "tied for the highest price on 1" in answer
    assert "strictly most expensive on 1" in answer
    assert "mid-pack on 1" in answer
    # the sentence's own numbers add up to the denominator it states
    stated = re.search(r"On the (\d+) products where we can compare", answer)
    assert stated
    listed = [int(n) for n in re.findall(r" on (\d+)[,.]", answer)]
    assert listed and sum(listed) == int(stated.group(1))
    assert int(stated.group(1)) == rendered["compared"]


def test_empty_buckets_are_omitted_from_the_lede_but_shown_in_the_panel(tmp_path):
    """A zero is a fact in the panel and noise in a sentence — but the
    sentence must still add up without it."""
    data_dir = seed_article_data(tmp_path)  # no 3-retailer products
    site_dir = _build(data_dir, tmp_path / "site")
    rendered = rendered_positions(site_dir)
    assert rendered["tied_top"] == 0
    answer = answer_of(site_dir, SSK)
    assert "tied for the highest price on 0" not in answer
    assert sum(int(n) for n in re.findall(r" on (\d+)[,.]", answer)) == \
        rendered["compared"]


# --- the audit sentence -----------------------------------------------------

def _audit_report(verdicts: list[str], retailer_id="shop-solar-kits") -> dict:
    return {
        "timestamp": "2026-08-14T06:00:00+00:00",
        "results": [{"retailer_id": retailer_id, "product_id": f"p{i}",
                     "variant_id": str(i), "verdict": verdict}
                    for i, verdict in enumerate(verdicts)],
    }


def test_ssk_lede_never_claims_an_audit_result_the_panel_does_not_show(tmp_path):
    """HIGH-1. The lede asserted that the retailer's published prices 'match
    what our audit re-reads from its own product endpoints' while the panel
    below it showed NOT_AUDITED for every check — the audit samples a
    rotation, so most builds carry no verdict for any one retailer."""
    data_dir = seed_article_data(tmp_path)
    _write_json(data_dir / "audit_report.json",
                _audit_report(["NOT_AUDITED"] * 12))
    site_dir = _build(data_dir, tmp_path / "site")
    html = article_html(site_dir, SSK)
    answer = answer_of(site_dir, SSK)

    assert "NOT_AUDITED: 12" in flat(html)
    assert "rotating sample" in answer
    assert "no verdict for this retailer" in answer
    for claim in ("published price agreeing", "prices match",
                  "match what our audit"):
        assert claim not in answer, claim


def test_ssk_lede_reports_clean_verdicts_when_they_exist(tmp_path):
    data_dir = seed_article_data(tmp_path)
    _write_json(data_dir / "audit_report.json",
                _audit_report(["CLEAN"] * 3 + ["NOT_AUDITED"] * 9))
    site_dir = _build(data_dir, tmp_path / "site")
    answer = answer_of(site_dir, SSK)
    assert "re-read 3 of its tracked listings" in answer
    assert "found the published price agreeing" in answer
    assert "2026-08-14" in answer
    assert "rotating sample" not in answer


def test_ssk_lede_leads_with_the_defect_when_the_audit_found_one(tmp_path):
    """A favourable claim is conditioned on the data; so is an unfavourable
    one. A RENDER_DEFECT outranks any number of CLEAN reads."""
    data_dir = seed_article_data(tmp_path)
    _write_json(data_dir / "audit_report.json",
                _audit_report(["CLEAN"] * 5 + ["RENDER_DEFECT"]))
    site_dir = _build(data_dir, tmp_path / "site")
    answer = answer_of(site_dir, SSK)
    assert "1 disagreement" in answer
    assert "verify clean" in answer
    assert "found the published price agreeing" not in answer


def test_build_and_audit_agree_on_which_verdicts_are_evidence():
    """build.py cannot import audit.py (audit imports build), so the verdict
    vocabulary is duplicated. If audit gains a verdict class and this copy
    does not, the article silently starts counting it as no-evidence — or,
    worse, as a pass."""
    import audit

    from build import EVIDENCE_VERDICTS
    assert set(EVIDENCE_VERDICTS) == set(audit._VERIFIED_VERDICTS)


def test_verdicts_for_other_retailers_do_not_count_as_this_one(tmp_path):
    data_dir = seed_article_data(tmp_path)
    _write_json(data_dir / "audit_report.json",
                _audit_report(["CLEAN"] * 4, retailer_id="wild-oak-trail"))
    site_dir = _build(data_dir, tmp_path / "site")
    answer = answer_of(site_dir, SSK)
    assert "no verdict for this retailer" in answer
    assert "re-read 4" not in answer


# --- the sale-cadence claim -------------------------------------------------

def test_sale_article_states_no_cadence_its_citations_do_not_support(built):
    """MEDIUM-8. 'a major promotional window roughly every six to eight
    weeks' was contradicted by the article's own four dated citations, whose
    gaps run from two to six weeks."""
    _, site_dir, _ = built
    text = flat(article_html(site_dir, "when-do-power-stations-go-on-sale"))
    for phrase in ("every six to eight weeks", "every six weeks",
                   "every eight weeks"):
        assert phrase not in text, phrase

    spec = article_by_slug("when-do-power-stations-go-on-sale")
    citations = next(b for b in spec["blocks"] if b["kind"] == "citations")
    from datetime import date
    dates = sorted(date.fromisoformat(s["date"]) for s in citations["sources"])
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    # the rendered spacing line is measured, not asserted
    assert (f"gaps between consecutive sources run from {min(gaps)} to "
            f"{max(gaps)} days") in text
    assert f"spanning {dates[0]} to {dates[-1]}" in text


def test_chemistry_article_does_not_claim_ncm_dominates_evs(built):
    """HIGH-5. LFP passed the nickel chemistries globally in 2025."""
    _, site_dir, _ = built
    html = article_html(site_dir, "lifepo4-vs-ncm-plain-english")
    text = flat(html)
    assert "still dominates electric vehicles" not in text
    assert "more than half of all EV batteries deployed worldwide in 2025" \
        in text
    # cited, and cited as a citation rather than as a commercial link
    assert ('<a href="https://www.iea.org/reports/global-ev-outlook-2026/'
            'electric-vehicle-batteries" rel="nofollow">') in html
    assert "sponsored" not in html[html.find("iea.org") - 200:
                                   html.find("iea.org") + 200]
    # MEDIUM-6: NCM cycle ratings are published in the thousands, not the
    # "several hundred" the article used to claim
    assert "800 to 2,000 range" in text
    assert "NCM packs for several hundred" not in text


def test_fridge_arithmetic_divides_by_the_efficiency(built):
    """LOW-9. Conversion losses divide; the article multiplied by 1.2."""
    _, site_dir, _ = built
    html = article_html(site_dir,
                        "how-many-watt-hours-to-run-a-refrigerator")
    text = flat(re.sub(r"<[^>]+>", "", unescape(html)))
    assert "2,000 ÷ 0.8 = 2,500 Wh" in text
    assert "not 2,000 × 1.2" in text
    assert "2,400 Wh" not in text


def test_head_to_head_does_not_infer_surge_from_continuous_output(built):
    """LOW-11. Starting surge is not in the catalog, so the page may not
    tell you which unit will start a load."""
    _, site_dir, _ = built
    text = flat(article_html(site_dir, H2H))
    assert "cannot start at all" not in text
    assert "starting surge is a separate rating" in text.lower()
    assert "our catalog does not store it" in text

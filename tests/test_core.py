from __future__ import annotations

import csv
from pathlib import Path

from nsmpa.classify import classify_publication
from nsmpa.config import Settings
from nsmpa.crawl import crawl_publication
from nsmpa.db import Database
from nsmpa.discovery import score_candidate
from nsmpa.extract import analyze_page
from nsmpa.ingest import import_ipeds
from nsmpa.models import FetchResult, SearchResult
from nsmpa.research import create_research_run, import_entities_csv, similarity_score, sync_student_entities
from nsmpa.search import SearchBroker, SearchProvider
from nsmpa.support_orgs import seed_support_orgs
from nsmpa.utils import host_is_public, normalize_url


def make_db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.sqlite3")


def seed_pub(db: Database) -> int:
    db.execute(
        "INSERT INTO institutions(unitid,name,city,state,website,control,level,included) VALUES(?,?,?,?,?,?,?,1)",
        ("1", "Example University", "Example", "CO", "https://example.edu", 1, 1),
    )
    db.execute(
        "INSERT INTO publications(unitid,name,homepage_url,domain,confidence) VALUES(?,?,?,?,?)",
        ("1", "The Example", "https://news.example.edu", "news.example.edu", 1.0),
    )
    db.execute("INSERT INTO crawl_runs(id,config_json) VALUES('run1','{}')")
    db.conn.commit()
    return int(db.execute("SELECT id FROM publications").fetchone()["id"])


def insert_page(db: Database, pub_id: int, status: str = "fetched", depth: int = 1, policy_score: float = 10.0, n: int = 1):
    for i in range(n):
        db.execute(
            """
            INSERT INTO pages(publication_id,run_id,requested_url,depth,status,http_status,text_length,
              noindex,nofollow,policy_score,evidence_tags_json,headers_json)
            VALUES(?,?,?,?,?,200,100,0,0,?,'[]','{}')
            """,
            (pub_id, "run1", f"https://news.example.edu/page-{i}-{depth}-{status}", depth, status, policy_score),
        )
    db.conn.commit()


def insert_evidence(db: Database, pub_id: int, tag: str, excerpt: str):
    db.execute(
        """
        INSERT INTO evidence(publication_id,run_id,tag,excerpt,source_url,evidence_type,confidence,excerpt_sha256)
        VALUES(?, 'run1', ?, ?, 'https://news.example.edu/policy', 'written_policy', .9, ?)
        """,
        (pub_id, tag, excerpt, str(abs(hash(excerpt)))),
    )
    db.conn.commit()


def test_html_noindex_and_policy_extraction():
    html = b"""
    <html><head><title>Content Removal Policy</title>
    <meta name="robots" content="noindex, follow">
    <link rel="canonical" href="https://paper.edu/policy"></head>
    <body><p>Requests to de-index an article may be considered on a case-by-case basis
    when charges were dismissed or circumstances have changed.</p>
    <a href="/about">About</a></body></html>
    """
    a = analyze_page("https://paper.edu/removal-policy", html, "text/html", {})
    assert a.noindex is True
    assert "deindex" in a.evidence_tags
    assert "case_by_case" in a.evidence_tags
    assert "dismissed_charges" in a.evidence_tags
    assert a.policy_score >= 20
    assert a.canonical_url == "https://paper.edu/policy"


def test_x_robots_noindex():
    html = b"<html><body>ordinary article</body></html>"
    a = analyze_page("https://paper.edu/a", html, "text/html", {"x-robots-tag": "googlebot: noindex"})
    assert a.noindex is True


def test_candidate_scoring():
    result = SearchResult(
        url="https://daily.example.edu/",
        title="The Daily Example | Independent Student Newspaper",
        snippet="Student news at Example University",
        rank=1,
        provider="test",
    )
    score, reasons = score_candidate("Example University", "https://www.example.edu", result)
    assert score >= 0.6
    assert "student_news_terms" in reasons


def test_ipeds_import_and_filters(tmp_path: Path):
    p = tmp_path / "HD2024.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["UNITID", "INSTNM", "CITY", "STABBR", "WEBADDR", "CONTROL", "LEVEL"])
        w.writeheader()
        w.writerow({"UNITID":"10","INSTNM":"Public U","CITY":"X","STABBR":"CO","WEBADDR":"public.edu","CONTROL":"1","LEVEL":"1"})
        w.writerow({"UNITID":"11","INSTNM":"Two Year","CITY":"X","STABBR":"CO","WEBADDR":"two.edu","CONTROL":"1","LEVEL":"2"})
        w.writerow({"UNITID":"12","INSTNM":"For Profit","CITY":"X","STABBR":"CO","WEBADDR":"fp.edu","CONTROL":"3","LEVEL":"1"})
    settings = Settings(database_path=tmp_path/"db.sqlite3", output_dir=tmp_path/"out", snapshot_dir=tmp_path/"snap")
    db = make_db(tmp_path)
    stats = import_ipeds(db, settings, p, 2024)
    assert stats == {"rows": 3, "included": 1}
    assert db.execute("SELECT included FROM institutions WHERE unitid='10'").fetchone()["included"] == 1
    assert db.execute("SELECT included FROM institutions WHERE unitid='11'").fetchone()["included"] == 0
    assert db.execute("SELECT website FROM institutions WHERE unitid='10'").fetchone()["website"] == "https://public.edu"
    db.close()


def test_classify_explicit_relief(tmp_path: Path):
    db = make_db(tmp_path)
    pub = seed_pub(db)
    insert_page(db, pub, depth=0, n=1)
    insert_page(db, pub, depth=1, n=14)
    insert_evidence(db, pub, "deindex", "Editors may de-index an article on a case-by-case basis when appropriate.")
    result = classify_publication(db, pub, "run1")
    assert result["primary_class"] == "A_EXPLICIT_RELIEF"
    db.close()


def test_classify_strict_archive(tmp_path: Path):
    db = make_db(tmp_path)
    pub = seed_pub(db)
    insert_page(db, pub, depth=0, n=1)
    insert_page(db, pub, depth=1, n=14)
    insert_evidence(db, pub, "archive_integrity", "We do not remove or unpublish accurate articles because the archive is a historical record.")
    result = classify_publication(db, pub, "run1")
    assert result["primary_class"] == "E_STRICT_ARCHIVE"
    db.close()


def test_no_policy_requires_coverage(tmp_path: Path):
    db = make_db(tmp_path)
    pub = seed_pub(db)
    insert_page(db, pub, depth=0, policy_score=0, n=1)
    insert_page(db, pub, depth=1, policy_score=0, n=14)
    result = classify_publication(db, pub, "run1")
    assert result["primary_class"] == "F_NO_PUBLIC_POLICY_FOUND"
    assert result["inspection_complete"] is True
    db.close()


def test_incomplete_is_undetermined(tmp_path: Path):
    db = make_db(tmp_path)
    pub = seed_pub(db)
    insert_page(db, pub, depth=0, policy_score=0, n=1)
    result = classify_publication(db, pub, "run1")
    assert result["primary_class"] == "U_UNDETERMINED"
    db.close()


def test_url_security_helpers():
    assert host_is_public("127.0.0.1") is False
    assert host_is_public("10.1.2.3") is False
    assert host_is_public("169.254.1.1") is False
    assert host_is_public("localhost") is False
    assert host_is_public("example.com") is True
    assert normalize_url("HTTPS://Example.COM/a/?utm_source=x&b=2#frag") == "https://example.com/a?b=2"



class FakeFetcher:
    async def fetch(self, url: str) -> FetchResult:
        if url == "https://news.example.edu/":
            body = b'<html><head><title>The Example</title></head><body><a href="/editorial-policy">Policy</a><a href="/story">Story</a></body></html>'
            return FetchResult(url, url, 200, {"content-type":"text/html"}, body, 1, "text/html")
        if url == "https://news.example.edu/editorial-policy":
            body = b'<html><head><title>Editorial Policy</title></head><body>Requests to de-index an article may be considered on a case-by-case basis when charges were dismissed.</body></html>'
            return FetchResult(url, url, 200, {"content-type":"text/html"}, body, 1, "text/html")
        if url == "https://news.example.edu/story":
            body = b'<html><head><meta name="robots" content="noindex"><title>Old Story</title></head><body>Article text</body></html>'
            return FetchResult(url, url, 200, {"content-type":"text/html"}, body, 1, "text/html")
        return FetchResult(url, url, 404, {"content-type":"text/html"}, b"", 1, "text/html")


async def test_crawl_persists_policy_and_noindex(tmp_path: Path, monkeypatch):
    import nsmpa.crawl as crawl_mod

    async def no_sitemaps(fetcher, homepage, settings):
        return []

    monkeypatch.setattr(crawl_mod, "_sitemap_candidates", no_sitemaps)
    db = make_db(tmp_path)
    pub_id = seed_pub(db)
    settings = Settings(
        database_path=tmp_path/"test.sqlite3",
        output_dir=tmp_path/"out",
        snapshot_dir=tmp_path/"snap",
        crawl_max_pages_per_publication=30,
    )
    pub = db.execute("SELECT * FROM publications WHERE id=?", (pub_id,)).fetchone()
    stats = await crawl_publication(db, settings, FakeFetcher(), pub, "run1")
    assert stats["fetched"] >= 3
    assert db.execute("SELECT COUNT(*) n FROM evidence WHERE tag='deindex'").fetchone()["n"] >= 1
    assert db.execute("SELECT COUNT(*) n FROM pages WHERE noindex=1").fetchone()["n"] == 1
    db.close()


def test_candidate_scoring_rejects_institutional_pr_pages():
    pressroom = SearchResult(
        url="https://www.una.edu/pressroom",
        title="Campus News",
        snippet="University of North Alabama news and stories",
        rank=0,
        provider="institution_site",
    )
    score, reasons = score_candidate("University of North Alabama", "https://www.una.edu", pressroom)
    assert score < 0.15
    assert "institutional_pr_penalty" in reasons

    newsroom = SearchResult(
        url="https://erau.edu/about/news-and-stories/news",
        title="Newsroom",
        snippet="Embry-Riddle Aeronautical University news and stories",
        rank=0,
        provider="institution_site",
    )
    score, reasons = score_candidate("Embry-Riddle Aeronautical University-Prescott", "https://erau.edu", newsroom)
    assert score < 0.15
    assert "institutional_pr_penalty" in reasons


def test_candidate_scoring_accepts_student_media_hub():
    result = SearchResult(
        url="https://www.uaa.alaska.edu/students/leadership/media.cshtml",
        title="Student Media",
        snippet="Student-media page discovered on University of Alaska Anchorage site",
        rank=0,
        provider="institution_sitemap",
    )
    score, reasons = score_candidate("University of Alaska Anchorage", "https://www.uaa.alaska.edu", result)
    assert score >= 0.60
    assert "explicit_student_journalism_signal" in reasons


def test_candidate_scoring_accepts_independent_student_paper():
    result = SearchResult(
        url="https://www.thenorthernlight.org/",
        title="The Northern Light",
        snippet="External link from student media page at https://www.uaa.alaska.edu/students/leadership/media.cshtml",
        rank=0,
        provider="institution_student_media_external",
    )
    score, reasons = score_candidate("University of Alaska Anchorage", "https://www.uaa.alaska.edu", result)
    assert score >= 0.50
    assert "explicit_student_journalism_signal" in reasons



class FakeSearchProvider(SearchProvider):
    name = "fake"
    def __init__(self):
        self.calls = 0
    async def search(self, query: str, count: int = 8):
        self.calls += 1
        return [SearchResult(url="https://paper.example/policy", title="Unpublishing policy", snippet="May de-index when charges were dismissed", rank=1, provider=self.name, query=query)]


async def test_search_broker_caches_and_ledgers(tmp_path: Path):
    db = make_db(tmp_path)
    settings = Settings(database_path=tmp_path/"test.sqlite3", output_dir=tmp_path/"out", snapshot_dir=tmp_path/"snap", research_snapshot_dir=tmp_path/"rsnap")
    rid = create_research_run(db, settings, "test", "research1")
    provider = FakeSearchProvider()
    broker = SearchBroker(db, settings, rid, provider)
    r1, _q1, cached1 = await broker.search('site:paper.example unpublish', purpose="policy")
    r2, _q2, cached2 = await broker.search('site:paper.example unpublish', purpose="policy")
    assert len(r1) == 1 and len(r2) == 1
    assert cached1 is False and cached2 is True
    assert provider.calls == 1
    assert db.execute("SELECT COUNT(*) n FROM search_queries").fetchone()["n"] == 2
    assert db.execute("SELECT COUNT(*) n FROM search_results").fetchone()["n"] == 2
    db.close()


def test_seed_support_orgs_is_idempotent(tmp_path: Path):
    db = make_db(tmp_path)
    first = seed_support_orgs(db)
    second = seed_support_orgs(db)
    n = db.execute("SELECT COUNT(*) n FROM research_entities WHERE cohort='support_org'").fetchone()["n"]
    assert first["inserted"] > 20
    assert second["updated"] == first["total_seed"]
    assert n == first["total_seed"]
    db.close()


def test_import_generic_professional_newsrooms(tmp_path: Path):
    p = tmp_path / "newsrooms.csv"
    p.write_text("name,url,state\nExample News,https://examplenews.org,CO\n", encoding="utf-8")
    db = make_db(tmp_path)
    stats = import_entities_csv(db, p, "professional_newsroom", "test_directory")
    assert stats["inserted"] == 1
    row = db.execute("SELECT * FROM research_entities WHERE cohort='professional_newsroom'").fetchone()
    assert row["domain"] == "examplenews.org"
    db.close()


def test_sync_student_entities(tmp_path: Path):
    db = make_db(tmp_path)
    seed_pub(db)
    stats = sync_student_entities(db)
    assert stats["synced"] == 1
    row = db.execute("SELECT * FROM research_entities WHERE cohort='student_media'").fetchone()
    assert row["parent_name"] == "Example University"
    db.close()


def test_case_similarity_ranks_close_fact_pattern(tmp_path: Path):
    settings = Settings(database_path=tmp_path/"db.sqlite3", output_dir=tmp_path/"out", snapshot_dir=tmp_path/"snap", research_snapshot_dir=tmp_path/"rsnap")
    close = similarity_score(settings, "The student asked Google to de-index the old arrest story after charges were dismissed and the conviction was vacated. Editors added an update.", "student_media")
    generic = similarity_score(settings, "We maintain a permanent archive of published stories.", "professional_newsroom")
    assert close > 50
    assert close > generic


async def test_sitemap_candidates_follow_index_and_skip_failures(tmp_path: Path):
    import httpx
    from conftest import make_settings, public_resolver

    from nsmpa.crawl import _sitemap_candidates
    from nsmpa.fetch import HardenedFetcher
    index = (b'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
             b'<sitemap><loc>https://news.example.edu/posts.xml</loc></sitemap>'
             b'<sitemap><loc>https://news.example.edu/broken.xml</loc></sitemap></sitemapindex>')
    posts = (b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
             b'<url><loc>https://news.example.edu/editorial-policy</loc></url></urlset>')

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        if req.url.path == "/sitemap.xml":
            return httpx.Response(200, headers={"content-type": "application/xml"}, content=index)
        if req.url.path == "/posts.xml":
            return httpx.Response(200, headers={"content-type": "application/xml"}, content=posts)
        if req.url.path == "/broken.xml":
            raise httpx.ConnectError("boom", request=req)
        return httpx.Response(404)
    s = make_settings(tmp_path, max_retries=0)
    f = HardenedFetcher(s, transport=httpx.MockTransport(handler), resolver=public_resolver)
    try:
        urls = await _sitemap_candidates(f, "https://news.example.edu/", s)
    finally:
        await f.close()
    assert "https://news.example.edu/editorial-policy" in urls     # nested sitemap followed; broken one skipped

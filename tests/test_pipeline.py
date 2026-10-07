"""End-to-end tests against a deterministic fake web and fake search provider (no network, no credits)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from conftest import make_settings, public_resolver
from nsmpa.db import Database
from nsmpa.discovery import discover_all, promote_candidates, clean_publication_name
from nsmpa.export import export_research
from nsmpa.fetch import HardenedFetcher
from nsmpa.models import SearchResult
from nsmpa.report import build_report
from nsmpa.research import import_entities_csv, merge_duplicate_entities, research_all, start_research
from nsmpa.review import open_items, record_decision
from nsmpa.runs import StopController, item_counts
from nsmpa.search import SearchAuthError, SearchBroker, SearchProvider, parse_serper_response
from nsmpa.validate import cohort_metrics

PAGE = "<html><head><title>{title}</title></head><body><nav>Home | Subscribe | Please do not remove this page</nav><main>{body}</main><footer>All rights reserved.</footer></body></html>"

WEB: dict[str, tuple[int, str]] = {
    # --- Strict-archive newsroom
    "https://strict.example/": (200, PAGE.format(title="Strict Times", body="<p>Local news.</p><a href='/ethics-policy/'>Ethics policy</a><a href='/about/'>About</a>")),
    "https://strict.example/ethics-policy/": (200, PAGE.format(title="Ethics Policy | Strict Times", body=(
        "<p>We do not remove stories from our archive. Our archive is the historical record of the community. "
        "If charges are dismissed, we will update the story to reflect the outcome of the case.</p>"))),
    "https://strict.example/about/": (200, PAGE.format(title="About", body="<p>The Strict Times has served the valley since 1901 and covers local government.</p>")),
    # --- Changed-circumstances newsroom (+ third-party practice report)
    "https://relief.example/": (200, PAGE.format(title="Relief Daily", body="<a href='/unpublishing-policy'>Unpublishing policy</a>")),
    "https://relief.example/unpublishing-policy": (200, PAGE.format(title="Unpublishing Policy", body=(
        "<p>We generally do not unpublish articles. However, in rare cases editors may remove a name from an article "
        "about an arrest when the charges were later dismissed or the record was expunged, because search engines keep "
        "old stories prominent for years.</p>"))),
    "https://thirdparty.example/story": (200, PAGE.format(title="Media column", body=(
        "<p>The Relief Daily removed the name of a former student from a 2015 arrest story after the charges were dismissed. "
        "Another paper in Ohio refused to change anything.</p>"))),
    # --- Blocked newsroom (never a negative finding)
    "https://blocked.example/": (403, "Forbidden"),
    "https://blocked.example/policy": (403, "Forbidden"),
    # --- Empty-but-accessible newsroom
    "https://quiet.example/": (200, PAGE.format(title="Quiet Gazette", body="<p>Sports and weather.</p><a href='/about'>About us</a>")),
    "https://quiet.example/about": (200, PAGE.format(title="About us", body="<p>The Quiet Gazette is a weekly newspaper owned by local residents since 1950.</p>")),
    # --- Student discovery
    "https://www.state.edu/": (200, PAGE.format(title="State University", body=(
        "<a href='/news/'>University News</a><a href='/student-life/media'>Student media</a>"))),
    "https://www.state.edu/student-life/media": (200, PAGE.format(title="Student Media | State University", body=(
        "<p>Student media at State University includes the independent student newspaper.</p>"
        "<a href='https://www.statelantern.com/'>The State Lantern</a>"))),
    "https://www.state.edu/news/": (200, PAGE.format(title="Newsroom | University Communications", body="<p>Press releases from the Office of University Communications. Media relations contacts.</p>")),
    "https://www.statelantern.com/": (200, PAGE.format(title="The State Lantern | Independent student newspaper of State University", body=(
        "<p>The State Lantern is the student-run newspaper of State University. Editor-in-chief: A. Writer.</p>"
        + "".join(f"<a href='/2026/0{i}/story-{i}'>Story {i}</a>" for i in range(1, 7))))),
}


def handler(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/robots.txt":
        return httpx.Response(404)
    key = str(req.url)
    if key in WEB:
        status, body = WEB[key]
        return httpx.Response(status, headers={"content-type": "text/html; charset=utf-8"}, content=body.encode())
    return httpx.Response(404, headers={"content-type": "text/html"}, content=b"not found")


class FakeSearch(SearchProvider):
    name = "fake"

    def __init__(self, routes: dict[str, list[tuple[str, str, str]]] | None = None, fail_auth: bool = False):
        self.routes = routes or {}
        self.calls: list[str] = []
        self.fail_auth = fail_auth

    async def search(self, query: str, count: int = 8):
        self.calls.append(query)
        if self.fail_auth:
            raise SearchAuthError("fake HTTP 401: invalid key")
        for needle, items in self.routes.items():
            if needle in query:
                return [SearchResult(url=u, title=t, snippet=s, rank=i + 1, provider=self.name, query=query)
                        for i, (u, t, s) in enumerate(items)]
        return []


SEARCH_ROUTES = {
    "site:strict.example (unpublish": [("https://strict.example/ethics-policy/", "Ethics Policy", "We do not remove stories from our archive.")],
    "site:relief.example (unpublish": [("https://relief.example/unpublishing-policy", "Unpublishing Policy", "editors may remove a name")],
    '"Relief Daily" (unpublished': [("https://thirdparty.example/story", "Relief Daily removed name", "Relief Daily removed the name after charges were dismissed")],
    '"State University" student newspaper': [
        ("https://www.state.edu/news/", "Newsroom | State University", "University news and stories"),
        ("https://www.statelantern.com/", "The State Lantern | Independent student newspaper", "Student-run newspaper of State University"),
    ],
}


def seed_newsrooms(tmp_path: Path, db: Database) -> None:
    p = tmp_path / "newsrooms.csv"
    p.write_text("name,url,state\nStrict Times,https://strict.example/,WA\nRelief Daily,https://relief.example/,WA\n"
                 "Blocked Post,https://blocked.example/,OR\nQuiet Gazette,https://quiet.example/,ID\n", encoding="utf-8")
    import_entities_csv(db, p, "professional_newsroom", "test_directory")


def fetcher_for(settings) -> HardenedFetcher:
    return HardenedFetcher(settings, transport=httpx.MockTransport(handler), resolver=public_resolver)


async def run_research(db, settings, provider, **kw):
    rid, _ = start_research(db, settings, cohort="professional_newsroom", limit=None, run_id=kw.pop("run_id", None),
                            max_searches=kw.get("max_searches"), command="test")
    stats = await research_all(db, settings, rid, cohort="professional_newsroom", quiet=True, provider=provider,
                               fetcher=fetcher_for(settings), **kw)
    return rid, stats


def stances(db, rid) -> dict[str, str]:
    return {r["name"]: r["stance"] for r in db.execute(
        "SELECT re.name, s.stance FROM entity_stances s JOIN research_entities re ON re.id=s.entity_id WHERE s.run_id=?", (rid,))}


# ============================================================================ research

async def test_research_end_to_end_classifies_without_false_negatives(tmp_path, db):
    settings = make_settings(tmp_path, research_concurrency=2)
    seed_newsrooms(tmp_path, db)
    provider = FakeSearch(SEARCH_ROUTES)
    rid, stats = await run_research(db, settings, provider)
    assert stats["status"] == "completed" and stats["completed"] == 4
    s = stances(db, rid)
    assert s["Strict Times"] == "UPDATE_ONLY"  # rejects removal, offers update on dismissal
    assert s["Relief Daily"] == "SUPPORTS_CHANGED_CIRCUMSTANCES"
    assert s["Blocked Post"] == "UNDETERMINED"  # 403 everywhere is never "no policy"
    assert s["Quiet Gazette"] == "NO_RELEVANT_GUIDANCE"
    # Navigation chrome ("Please do not remove this page") never became evidence.
    assert db.scalar("SELECT COUNT(*) FROM evidence_items WHERE excerpt LIKE '%do not remove this page%'") == 0
    # Third-party practice was captured as documented practice about the entity, not as its written policy.
    practice = db.execute("SELECT * FROM evidence_items WHERE evidence_class='documented_practice'").fetchall()
    assert any("Relief Daily removed the name" in p["excerpt"] for p in practice)
    assert not any("Ohio" in p["excerpt"] for p in practice)
    # Strongest supportive/adverse excerpts are linked and traceable to URL + hash.
    row = db.execute("SELECT s.* FROM entity_stances s JOIN research_entities re ON re.id=s.entity_id "
                     "WHERE re.name='Strict Times' AND s.run_id=?", (rid,)).fetchone()
    adv = db.execute("SELECT * FROM evidence_items WHERE id=?", (row["strongest_adverse_id"],)).fetchone()
    assert adv["source_url"].startswith("https://strict.example/") and adv["page_sha256"]
    assert Path(db.scalar("SELECT path FROM snapshots WHERE sha256=?", (adv["page_sha256"],))).exists()
    # Review queue has the determinate findings.
    assert len(open_items(db, rid)) >= 2
    # Queries that led to evidence are flagged as useful in the ledger.
    assert db.scalar("SELECT COUNT(*) FROM search_queries WHERE run_id=? AND produced_evidence=1", (rid,)) >= 1


async def test_evidence_is_unique_per_sentence_not_per_tag(tmp_path, db):
    settings = make_settings(tmp_path)
    seed_newsrooms(tmp_path, db)
    rid, _ = await run_research(db, settings, FakeSearch(SEARCH_ROUTES))
    rows = db.execute("SELECT entity_id, excerpt_sha256, COUNT(*) n FROM evidence_items WHERE run_id=? GROUP BY 1,2 HAVING n>1", (rid,)).fetchall()
    assert rows == []
    multi = db.scalar("SELECT MAX(c) FROM (SELECT COUNT(*) c FROM evidence_item_tags GROUP BY evidence_id)")
    assert multi >= 3  # one sentence, several concept tags, still one evidence row


async def test_rerun_uses_cache_and_spends_no_credits(tmp_path, db):
    settings = make_settings(tmp_path)
    seed_newsrooms(tmp_path, db)
    p1 = FakeSearch(SEARCH_ROUTES)
    _, s1 = await run_research(db, settings, p1)
    p2 = FakeSearch(SEARCH_ROUTES)
    _, s2 = await run_research(db, settings, p2)  # new run id, same queries
    assert s1["credits_estimated"] > 0
    assert p2.calls == [] and s2["credits_estimated"] == 0 and s2["searches_cached"] == s1["searches_live"]


async def test_refresh_search_bypasses_cache(tmp_path, db):
    settings = make_settings(tmp_path)
    seed_newsrooms(tmp_path, db)
    await run_research(db, settings, FakeSearch(SEARCH_ROUTES))
    p = FakeSearch(SEARCH_ROUTES)
    _, s = await run_research(db, settings, p, refresh_search=True)
    assert len(p.calls) == s["searches_live"] > 0


async def test_budget_exhaustion_checkpoints_and_resume_completes(tmp_path, db):
    settings = make_settings(tmp_path, research_concurrency=1)
    seed_newsrooms(tmp_path, db)
    p1 = FakeSearch(SEARCH_ROUTES)
    rid, s1 = await run_research(db, settings, p1, max_searches=8)
    assert s1["status"] == "budget_exhausted"
    assert s1["credits_estimated"] <= 8 and len(p1.calls) <= 8
    assert "max-searches" in (db.scalar("SELECT status_reason FROM research_runs WHERE id=?", (rid,)) or "")
    done_before = item_counts(db, rid).get("done", 0)
    assert done_before >= 1
    # Same run id, fresh budget: completed entities are skipped, cached queries cost nothing.
    p2 = FakeSearch(SEARCH_ROUTES)
    rid2, s2 = await run_research(db, settings, p2, run_id=rid, max_searches=100)
    assert rid2 == rid and s2["status"] == "completed"
    assert s2["already_done"] == done_before
    assert item_counts(db, rid).get("done") == 4
    assert not set(p1.calls) & set(p2.calls)  # nothing paid for twice


async def test_ctrl_c_stops_cleanly_and_is_resumable(tmp_path, db):
    settings = make_settings(tmp_path, research_concurrency=1)
    seed_newsrooms(tmp_path, db)
    stop = StopController()

    class StopAfterFirst(FakeSearch):
        async def search(self, query, count=8):
            if not stop.stop_requested:
                stop.request_stop()  # simulates one SIGINT arriving mid-entity
            return await super().search(query, count)

    rid, s = await run_research(db, settings, StopAfterFirst(SEARCH_ROUTES), stop=stop)
    assert s["status"] == "interrupted" and s["remaining"] == 3
    counts = item_counts(db, rid)
    assert counts.get("done") == 1 and counts.get("running", 0) == 0  # in-flight entity finished, nothing half-open
    assert db.scalar("SELECT status FROM research_runs WHERE id=?", (rid,)) == "interrupted"
    _, s2 = await run_research(db, settings, FakeSearch(SEARCH_ROUTES), run_id=rid)
    assert s2["status"] == "completed" and item_counts(db, rid).get("done") == 4


async def test_second_ctrl_c_force_cancels_but_keeps_completed_work(tmp_path, db):
    settings = make_settings(tmp_path, research_concurrency=1)
    seed_newsrooms(tmp_path, db)
    stop = StopController()

    class DoubleInterrupt(FakeSearch):
        async def search(self, query, count=8):
            if len(self.calls) == 1:
                stop.request_stop()
                stop.request_stop()  # second Ctrl+C -> cancel in-flight work
            return await super().search(query, count)

    rid, s = await run_research(db, settings, DoubleInterrupt(SEARCH_ROUTES), stop=stop)
    assert s["status"] == "interrupted"
    assert item_counts(db, rid).get("running", 0) == 0  # cancelled entity returned to pending


async def test_search_auth_error_stops_run_without_losing_work(tmp_path, db):
    settings = make_settings(tmp_path)
    seed_newsrooms(tmp_path, db)
    rid, s = await run_research(db, settings, FakeSearch(fail_auth=True))
    assert s["status"] == "failed" and "auth" in s["stop_reason"]
    assert db.scalar("SELECT COUNT(*) FROM search_queries WHERE run_id=? AND status='failed'", (rid,)) >= 1


async def test_broken_site_never_terminates_run(tmp_path, db, monkeypatch):
    settings = make_settings(tmp_path)
    seed_newsrooms(tmp_path, db)
    import nsmpa.research as research_mod
    original = research_mod.EntityResearcher.research

    async def boom(self, entity, budget):
        if entity["name"] == "Strict Times":
            raise RuntimeError("parser exploded")
        return await original(self, entity, budget)

    monkeypatch.setattr(research_mod.EntityResearcher, "research", boom)
    rid, s = await run_research(db, settings, FakeSearch(SEARCH_ROUTES))
    assert s["completed"] == 3 and s["failed"] == 1
    assert db.scalar("SELECT COUNT(*) FROM errors WHERE research_run_id=?", (rid,)) == 1


async def test_exports_and_report_gating(tmp_path, db):
    settings = make_settings(tmp_path)
    seed_newsrooms(tmp_path, db)
    rid, _ = await run_research(db, settings, FakeSearch(SEARCH_ROUTES))
    counts = export_research(db, rid, settings.output_dir)
    out = settings.output_dir / f"research_{rid}"
    for name in ("evidence.csv", "evidence.jsonl", "entity_stances.csv", "search_queries.csv", "run_manifest.json", "summary.md"):
        assert (out / name).exists(), name
    assert counts["evidence"] > 0
    first = json.loads((out / "evidence.jsonl").read_text().splitlines()[0])
    assert first["source_url"] and first["page_sha256"] and "classifier_cues" in first
    m = cohort_metrics(db, settings, "professional_newsroom")
    assert not m.valid_for_percentages  # 4 entities, no human review -> gates fail
    rep = build_report(db, settings, settings.output_dir)
    md = Path(rep["out_dir"], "report.md").read_text()
    assert "PRELIMINARY" in md and "withheld (gates failing)" in md
    assert rep["cohorts"]["professional_newsroom"] == "preliminary"


async def test_review_decision_propagates(tmp_path, db):
    settings = make_settings(tmp_path)
    seed_newsrooms(tmp_path, db)
    rid, _ = await run_research(db, settings, FakeSearch(SEARCH_ROUTES))
    item = open_items(db, rid, limit=1)[0]
    record_decision(db, item["id"], "accept", reviewer="tester", note="checked live page")
    st = db.execute("SELECT * FROM entity_stances WHERE id=?", (item["item_id"],)).fetchone()
    assert st["review_status"] == "accepted"
    ev_id = st["strongest_supportive_id"] or st["strongest_adverse_id"]
    assert db.scalar("SELECT verification_status FROM evidence_items WHERE id=?", (ev_id,)) == "verified"


# ============================================================================ discovery

def seed_institution(db: Database) -> None:
    db.execute("INSERT INTO institutions(unitid,name,city,state,website,control,level,included,raw_json) VALUES(?,?,?,?,?,?,?,1,?)",
               ("900001", "State University", "Capital", "WA", "https://www.state.edu/", 1, 1, json.dumps({"C21BASIC": "16"})))
    db.conn.commit()


async def test_discovery_prefers_independent_paper_over_pr_newsroom(tmp_path, db):
    settings = make_settings(tmp_path)
    seed_institution(db)
    provider = FakeSearch(SEARCH_ROUTES)
    stats = await discover_all(db, settings, quiet=True, provider=provider, fetcher=fetcher_for(settings))
    assert stats["status"] == "completed"
    assert len(provider.calls) == 1  # early stop after the first ladder query
    pr = db.execute("SELECT * FROM publication_candidates WHERE url LIKE '%state.edu/news%'").fetchone()
    assert pr is None or (pr["verified_score"] or pr["score"]) < settings.publication_confidence_threshold
    res = promote_candidates(db, settings.publication_confidence_threshold)
    assert res["promoted"] == 1
    pub = db.execute("SELECT * FROM publications").fetchone()
    assert pub["domain"] == "statelantern.com"
    assert pub["relationship"] == "independent_domain"
    assert pub["name"] == "The State Lantern"


async def test_discovery_without_search_uses_institution_site(tmp_path, db):
    settings = make_settings(tmp_path)
    seed_institution(db)
    from nsmpa.search import NullSearchProvider
    await discover_all(db, settings, quiet=True, provider=NullSearchProvider(), fetcher=fetcher_for(settings))
    promote_candidates(db, settings.publication_confidence_threshold)
    pub = db.execute("SELECT * FROM publications WHERE is_primary=1").fetchone()
    assert pub is not None and pub["domain"] == "statelantern.com"


async def test_discovery_resume_skips_done(tmp_path, db):
    settings = make_settings(tmp_path)
    seed_institution(db)
    s1 = await discover_all(db, settings, quiet=True, provider=FakeSearch(SEARCH_ROUTES), fetcher=fetcher_for(settings))
    p = FakeSearch(SEARCH_ROUTES)
    s2 = await discover_all(db, settings, run_id=s1["run_id"], quiet=True, provider=p, fetcher=fetcher_for(settings))
    assert s2["processed_this_invocation"] == 0 and p.calls == []


def test_manual_publication_never_overwritten(tmp_path, db):
    seed_institution(db)
    db.execute("INSERT INTO publications(unitid,name,homepage_url,domain,confidence,verification_status) "
               "VALUES('900001','Hand Verified','https://paper.example/','paper.example',1.0,'manual')")
    db.execute("INSERT INTO publication_candidates(unitid,url,domain,title,source,score,verified_score) "
               "VALUES('900001','https://other.example/','other.example','Other','x',0.95,0.95)")
    db.conn.commit()
    res = promote_candidates(db, 0.6)
    assert res["skipped_human_verified"] == 1
    assert db.scalar("SELECT COUNT(*) FROM publications") == 1


def test_ambiguous_candidates_flagged(tmp_path, db):
    seed_institution(db)
    for url, dom in (("https://paper-a.example/", "paper-a.example"), ("https://paper-b.example/", "paper-b.example")):
        db.execute("INSERT INTO publication_candidates(unitid,url,domain,title,source,score,verified_score) VALUES(?,?,?,?,?,?,?)",
                   ("900001", url, dom, "The Daily | Student newspaper", "serper", 0.8, 0.8))
    db.conn.commit()
    res = promote_candidates(db, 0.6, margin=0.08)
    assert res["ambiguous"] == 1
    assert db.scalar("SELECT COUNT(*) FROM review_queue WHERE item_type='publication'") == 1


def test_clean_publication_name():
    assert clean_publication_name("The Daily Barometer | Oregon State's student newspaper", "https://x", "Oregon State") == "The Daily Barometer"
    assert clean_publication_name("Home - The Spokesman", "https://x", "U") == "The Spokesman"
    assert clean_publication_name("Student Media", "https://media.u.edu/", "U").startswith("U student publication")


# ============================================================================ universe, search parsing, migrations

def test_import_merges_duplicate_domains_keeping_provenance(tmp_path, db):
    a = tmp_path / "a.csv"
    a.write_text("name,url\nExample News,https://examplenews.org\n", encoding="utf-8")
    b = tmp_path / "b.csv"
    b.write_text("name,url\nExample News Inc.,https://www.examplenews.org/\n", encoding="utf-8")
    import_entities_csv(db, a, "professional_newsroom", "inn_2026", "INN member")
    r = import_entities_csv(db, b, "professional_newsroom", "lion_2026", "LION member")
    assert r["merged_as_additional_source"] == 1
    assert db.scalar("SELECT COUNT(*) FROM research_entities WHERE cohort='professional_newsroom'") == 1
    assert {x[0] for x in db.execute("SELECT source FROM entity_sources")} == {"inn_2026", "lion_2026"}


def test_merge_duplicate_entities(db):
    for key, name in (("k1", "A"), ("k2", "A dup")):
        db.execute("INSERT INTO research_entities(cohort,source_key,name,homepage_url,domain) VALUES('professional_newsroom',?,?,?,?)",
                   (key, name, "https://a.example/", "a.example"))
    db.conn.commit()
    assert merge_duplicate_entities(db)["merged"] == 1
    assert db.scalar("SELECT COUNT(*) FROM research_entities WHERE active=1") == 1


def test_parse_serper_response_is_defensive():
    data = {"organic": [{"link": "https://a.example/", "title": "A", "snippet": "s", "position": 1},
                        {"title": "no link"}, "garbage", {"link": "", "title": "empty"},
                        {"link": "https://b.example/x", "title": None}], "credits": 1}
    res = parse_serper_response(data, "q")
    assert [r.url for r in res] == ["https://a.example/", "https://b.example/x"]
    assert parse_serper_response(None, "q") == [] and parse_serper_response({"organic": "x"}, "q") == []


async def test_serper_provider_with_mock_transport_never_leaks_key(monkeypatch):
    from nsmpa.search import SerperSearchProvider, SearchRetryable
    secret = "sk-test-SECRET-123456"
    monkeypatch.setenv("SERPER_API_KEY", secret)

    def h(req: httpx.Request):
        assert req.headers["X-API-KEY"] == secret
        body = json.loads(req.content)
        if body["q"] == "fail":
            return httpx.Response(503, json={"message": f"upstream down {secret}"})
        return httpx.Response(200, json={"organic": [{"link": "https://a.example/", "title": "A"}], "credits": 1})

    prov = SerperSearchProvider(secret, "UA", transport=httpx.MockTransport(h))
    assert secret not in repr(prov)
    res = await prov.search("ok", 8)
    assert res[0].url == "https://a.example/" and prov.last_credits == 1
    with pytest.raises(SearchRetryable) as ei:
        await prov.search("fail", 8)
    assert secret not in str(ei.value)
    await prov.aclose()


async def test_broker_retries_retryable_errors_and_ledgers_failures(tmp_path, db):
    from nsmpa.runs import create_or_resume_run
    from nsmpa.search import SearchRetryable
    settings = make_settings(tmp_path, search_max_retries=2, search_retry_backoff_seconds=0)

    class Flaky(SearchProvider):
        name = "flaky"
        n = 0
        async def search(self, q, count=8):
            Flaky.n += 1
            if Flaky.n < 3:
                raise SearchRetryable("503")
            return [SearchResult(url="https://a.example/", title="A", rank=1, provider="flaky", query=q)]

    rid, _ = create_or_resume_run(db, settings, "full_research")
    b = SearchBroker(db, settings, rid, Flaky())
    res, qid, cached = await b.search("x", purpose="t")
    assert len(res) == 1 and Flaky.n == 3 and b.credits_used == 1
    row = db.execute("SELECT * FROM search_queries WHERE id=?", (qid,)).fetchone()
    assert row["status"] == "completed" and row["latency_ms"] is not None


async def test_concurrent_identical_queries_spend_once(tmp_path, db):
    import asyncio
    from nsmpa.runs import create_or_resume_run
    settings = make_settings(tmp_path)

    class Slow(SearchProvider):
        name = "slow"
        calls = 0
        async def search(self, q, count=8):
            Slow.calls += 1
            await asyncio.sleep(0.05)
            return [SearchResult(url="https://a.example/", title="A", rank=1, provider="slow", query=q)]

    rid, _ = create_or_resume_run(db, settings, "full_research")
    b = SearchBroker(db, settings, rid, Slow())
    out = await asyncio.gather(*(b.search("same query", purpose="t") for _ in range(4)))
    assert Slow.calls == 1 and b.credits_used == 1
    assert sum(1 for _, _, cached in out if cached) == 3


def test_migration_from_v2_preserves_data_and_backs_up(tmp_path):
    from nsmpa.migrations import BASELINE_V2
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(BASELINE_V2)
    conn.execute("INSERT INTO meta(key,value) VALUES('schema_version','2')")
    conn.execute("INSERT INTO institutions(unitid,name,included) VALUES('1','Legacy U',1)")
    conn.execute("INSERT INTO research_entities(cohort,source_key,name,homepage_url,domain,source) "
                 "VALUES('support_org','k','SPJ','https://www.spj.org/','spj.org','builtin_seed')")
    conn.execute("INSERT INTO research_runs(id,config_json) VALUES('old','{}')")
    conn.commit()
    conn.close()
    db = Database(path)
    try:
        assert db.schema_version() == 3
        assert db.last_backup is not None and db.last_backup.exists()
        assert db.scalar("SELECT name FROM institutions WHERE unitid='1'") == "Legacy U"
        assert db.scalar("SELECT engine_version FROM research_runs WHERE id='old'") == "0.2"
        assert db.scalar("SELECT COUNT(*) FROM entity_sources") == 1
        assert db.pending_migrations() == []
    finally:
        db.close()
    # Re-opening is a no-op (idempotent, no second backup).
    db2 = Database(path)
    assert db2.last_backup is None
    db2.close()


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    import nsmpa.migrations as mig
    path = tmp_path / "x.sqlite3"
    Database(path, migrate=False).close()

    def broken(conn):
        conn.execute("CREATE TABLE half_done(x)")
        raise RuntimeError("boom")

    monkeypatch.setattr(mig, "MIGRATIONS", [mig.MIGRATIONS[0], (3, "broken", broken)])
    import nsmpa.db as dbmod
    monkeypatch.setattr(dbmod, "MIGRATIONS", mig.MIGRATIONS)
    with pytest.raises(RuntimeError):
        Database(path)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='half_done'").fetchone()[0] == 0
    assert 3 not in {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    conn.close()


async def test_no_search_provider_never_yields_no_guidance(tmp_path, db):
    """Regression (found in live smoke test): null-provider 'searches' must not count as inspection."""
    from nsmpa.search import NullSearchProvider
    settings = make_settings(tmp_path)
    seed_newsrooms(tmp_path, db)
    rid, s = await run_research(db, settings, NullSearchProvider())
    st = stances(db, rid)
    assert st["Quiet Gazette"] == "UNDETERMINED"
    assert db.scalar("SELECT COUNT(*) FROM search_queries WHERE run_id=?", (rid,)) == 0
    assert s["searches_live"] == 0
    # Determinate first-party policy found via homepage links is still classified.
    assert st["Strict Times"] in {"UPDATE_ONLY", "STRICT_ARCHIVE"}

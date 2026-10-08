"""Regression tests for the problems found by the first live pilot (2026-10-08)."""
from __future__ import annotations

import httpx
from conftest import make_settings, public_resolver
from test_pipeline import PAGE, FakeSearch

from nsmpa.evidence import classify_statement
from nsmpa.fetch import HardenedFetcher
from nsmpa.research import import_entities_csv, page_kind, research_all, score_target, start_research

TRAVEL = PAGE.format(title="The Easterner Online", body=(
    "<p>Seattle travel and tourism guide. What to wear to underground parties in Munich. Reasons Hotel Andra stands out. "
    "How to work and travel in the USA. Seattle vs San Francisco for starting a business.</p>"
    "<a href='/privacy-policy/'>Privacy policy</a><a href='/editorial-policy/'>Editorial policy</a>"))
PRIVACY = PAGE.format(title="Privacy Policy", body=(
    "<p>If we discover that a child under 18 has provided us with Personal Information, we will delete such information "
    "from our servers immediately.</p>"))
REAL = PAGE.format(title="The Easterner | Eastern Washington University's student newspaper", body=(
    "<p>The Easterner is the student-run newspaper of Eastern Washington University. Editor-in-chief: A. Writer.</p>"
    "<a href='/news/'>News</a><a href='/sports/'>Sports</a><a href='/opinion/'>Opinion</a>"
    "<a href='/editorial-policy/'>Editorial policy</a>"))
REAL_POLICY = PAGE.format(title="Editorial Policy | The Easterner", body=(
    "<p>We do not remove stories from our archive. In rare cases, editors may remove a name from an article when charges "
    "were later dismissed.</p>"))

WEB = {
    "https://easterneronline.com/": TRAVEL,
    "https://easterneronline.com/privacy-policy/": PRIVACY,
    "https://easterneronline.com/editorial-policy/": PAGE.format(title="Editorial policy", body="<p>Corrections are made promptly.</p>"),
    "https://theeasterner.example/": REAL,
    "https://theeasterner.example/editorial-policy/": REAL_POLICY,
}


def handler(req):
    if req.url.path == "/robots.txt":
        return httpx.Response(404)
    if req.url.host == "deadpaper.example":
        raise httpx.ConnectError("SSL: SSLV3_ALERT_HANDSHAKE_FAILURE", request=req)
    if req.url.host == "blocked.example":
        return httpx.Response(403)
    body = WEB.get(str(req.url))
    return httpx.Response(200 if body else 404, headers={"content-type": "text/html"}, content=(body or "nf").encode())


def student_entity(db, tmp_path, url):
    p = tmp_path / "s.csv"
    p.write_text(f"name,url,parent_name\nThe Easterner Online,{url},Eastern Washington University\n", encoding="utf-8")
    import_entities_csv(db, p, "student_media", "test")


async def run(db, s, routes):
    rid, _ = start_research(db, s, cohort="student_media", limit=None, run_id=None, max_searches=None, command="t")
    f = HardenedFetcher(s, transport=httpx.MockTransport(handler), resolver=public_resolver)
    provider = FakeSearch(routes)
    stats = await research_all(db, s, rid, cohort="student_media", quiet=True, provider=provider, fetcher=f)
    return rid, stats, provider


async def test_repurposed_domain_spends_nothing_and_attributes_nothing(tmp_path, db):
    s = make_settings(tmp_path, research_recover_stale_sites=False)
    student_entity(db, tmp_path, "https://easterneronline.com/")
    rid, stats, provider = await run(db, s, {})
    st = db.execute("SELECT * FROM entity_stances WHERE run_id=?", (rid,)).fetchone()
    assert st["stance"] == "UNDETERMINED" and "no longer appears to be this student publication" in st["rationale"]
    assert provider.calls == [] and stats["credits_estimated"] == 0          # no credits spent on a dead identity
    assert db.scalar("SELECT COUNT(*) FROM evidence_items WHERE run_id=? AND about_entity=1", (rid,)) == 0


async def test_recovery_finds_current_site_and_researches_it(tmp_path, db):
    s = make_settings(tmp_path)
    student_entity(db, tmp_path, "https://easterneronline.com/")
    routes = {'"Eastern Washington University" student newspaper': [
        ("https://easterneronline.com/", "The Easterner Online", "travel guide"),
        ("https://theeasterner.example/", "The Easterner | Eastern Washington University's student newspaper",
         "Independent student newspaper of Eastern Washington University")]}
    rid, _stats, _provider = await run(db, s, routes)
    ent = db.execute("SELECT * FROM research_entities WHERE name='The Easterner Online'").fetchone()
    assert ent["homepage_url"] == "https://theeasterner.example/"
    st = db.execute("SELECT * FROM entity_stances WHERE run_id=?", (rid,)).fetchone()
    assert st["stance"] == "SUPPORTS_CHANGED_CIRCUMSTANCES"
    assert not db.execute("SELECT 1 FROM evidence_items WHERE about_entity=1 AND source_url LIKE '%easterneronline%'").fetchone()


async def test_dead_homepage_spends_no_search_credits(tmp_path, db):
    s = make_settings(tmp_path, research_recover_stale_sites=False, max_retries=0)
    student_entity(db, tmp_path, "https://deadpaper.example/")
    rid, _stats, provider = await run(db, s, {})
    assert provider.calls == []
    assert "homepage could not be fetched" in db.scalar("SELECT rationale FROM entity_stances WHERE run_id=?", (rid,))


async def test_blocked_site_is_one_finding_not_dozens_of_errors(tmp_path, db):
    s = make_settings(tmp_path, research_recover_stale_sites=False, max_retries=0, blocked_fallback=False)
    student_entity(db, tmp_path, "https://blocked.example/")
    rid, stats, provider = await run(db, s, {})
    assert provider.calls == [] and stats["failed"] == 0
    assert "blocks automated access" in db.scalar("SELECT rationale FROM entity_stances WHERE run_id=?", (rid,))


def test_privacy_language_and_pages_are_not_policy():
    st = classify_statement("If we discover that a child under 18 has provided us with Personal Information, we will delete "
                            "such information from our servers immediately.")
    assert st.statement_type == "mention"
    assert page_kind("https://x.example/privacy-policy/", "Privacy Policy", False, False) == "legal_boilerplate"
    assert page_kind("https://x.example/terms-of-use", "Terms", False, False) == "legal_boilerplate"
    assert page_kind("https://x.example/editorial-policy/", "Editorial policy", False, False) == "policy"


def test_news_stories_about_others_are_not_policy_targets():
    from nsmpa.research import TIER1
    ent = {"homepage_url": "https://apnews.com/", "domain": "apnews.com", "name": "Associated Press", "cohort": "professional_newsroom"}
    story, _ = score_target(ent, "https://apnews.com/article/2024/05/supreme-court-ethics-code-thomas",
                            "Supreme Court ethics code debate", "Justices and ethics", 1, TIER1[1])
    policy, _ = score_target(ent, "https://apnews.com/about/news-values-and-principles", "AP News Values and Principles",
                             "Our standards: we do not remove stories", 2, TIER1[1])
    plain, _ = score_target(ent, "https://apnews.com/hub/sports", "Sports", "Scores", 1, TIER1[0])
    assert story < 0.28 <= policy and plain < 0.28

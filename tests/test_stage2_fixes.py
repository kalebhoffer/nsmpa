"""Regression cases from Stage 2 (professional newsrooms), batch 1 (Oct 2026)."""
from __future__ import annotations

import pytest

from nsmpa.evidence import classify_statement
from nsmpa.research import attribute_statement, is_news_story, speaks_for_newsroom

PAPER = {"name": "Island Review", "homepage_url": "https://www.bainbridgereview.com/", "domain": "bainbridgereview.com",
         "cohort": "professional_newsroom"}
BANGOR = {"name": "Bangor Daily News", "homepage_url": "https://www.bangordailynews.com/", "domain": "bangordailynews.com",
          "cohort": "professional_newsroom"}


def test_byline_is_not_the_newsroom_speaking():
    assert not speaks_for_newsroom(PAPER, "Spartan boys take down Curtis in water polo. By Luke Caputo Bainbridge Island Review")
    assert speaks_for_newsroom(BANGOR, "The Bangor Daily News reviews requests to remove old crime stories from Google.")


def test_sports_story_on_own_site_is_not_policy():
    about, cls = attribute_statement(
        PAPER, first=True, kind="other", url="https://www.bainbridgereview.com/2026/09/24/spartan-boys-take-down-curtis/",
        published=None, author=None, statement_type="case_by_case",
        excerpt="Spartan boys take down Curtis in water polo. By Luke Caputo Bainbridge Island Review", context="")
    assert about is False and cls == "secondary_report"


@pytest.mark.parametrize("url,expected", [
    ("https://www.chronline.com/stories/interior-department-renaming-sites,299513", True),
    ("https://www.djc.com/news/co/cis.html?id=12076662", True),
    ("https://chicago.suntimes.com/right-to-be-forgotten-policy", False),
    ("https://www.wcbu.org/public-safety-reporting-policy", False),
])
def test_article_urls_are_news_stories(url, expected):
    assert is_news_story("other", url, None, None) is expected


@pytest.mark.parametrize("sentence", [
    "While we endeavor to help writers clearly convey their intended message, we will delete content that is rude or known to "
    "be inaccurate.",
    "It is unlikely that documents of this kind would require extensive redactions like more sensitive government information.",
])
def test_letters_and_documents_are_not_relief(sentence):
    assert classify_statement(sentence).direction == "neutral"


def test_routine_correction_notes_do_not_make_update_only(tmp_path, db):
    from conftest import make_settings

    from nsmpa.research import import_entities_csv
    from nsmpa.runs import create_or_resume_run
    from nsmpa.stance import classify_entity
    s = make_settings(tmp_path)
    p = tmp_path / "e.csv"
    p.write_text("name,url\nExample Herald,https://herald.example/\n")
    import_entities_csv(db, p, "professional_newsroom", "t")
    ent = db.execute("SELECT * FROM research_entities WHERE name='Example Herald'").fetchone()
    create_or_resume_run(db, s, "full_research", "run-u")
    db.execute("INSERT INTO evidence_items(run_id,entity_id,cohort,source_url,excerpt,excerpt_sha256,near_dup_key,first_party,"
               "about_entity,evidence_class,statement_type,direction,extraction_confidence) VALUES "
               "('run-u',?,?,?,?,?,?,1,1,'editorial_statement','update_remedy','neutral',0.6)",
               (ent["id"], "professional_newsroom", "https://herald.example/2024/01/story/",
                "Editor's note: This story has been updated with additional information.", "h1", "n1"))
    assert classify_entity(db, s, "run-u", ent).stance != "UPDATE_ONLY"
    db.execute("INSERT INTO evidence_items(run_id,entity_id,cohort,source_url,excerpt,excerpt_sha256,near_dup_key,first_party,"
               "about_entity,evidence_class,statement_type,direction,extraction_confidence) VALUES "
               "('run-u',?,?,?,?,?,?,1,1,'written_policy','update_remedy','neutral',0.6)",
               (ent["id"], "professional_newsroom", "https://herald.example/corrections-policy/",
                "When charges are dismissed we update the story; we add an editor's note rather than removing it.", "h2", "n2"))
    assert classify_entity(db, s, "run-u", ent).stance == "UPDATE_ONLY"


def test_removing_a_published_letter_under_legal_threat_is_still_relief_language():
    st = classify_statement("When the Daily Illini was faced with the threat of a lawsuit, the staff had to decide whether to "
                            "comply with a demand to remove an old letter to the editor condemning gay marriage.")
    assert "comment_moderation" not in st.cues


async def test_circuit_breaker_reopens_after_cooldown(tmp_path):
    import httpx
    from conftest import make_settings, public_resolver

    from nsmpa.fetch import HardenedFetcher
    calls = {"n": 0}

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] <= 3 else httpx.Response(200, headers={"content-type": "text/html"},
                                                                          content=b"<html><body>ok</body></html>")
    s = make_settings(tmp_path, host_failure_threshold=3, host_circuit_cooldown_seconds=0.2, max_retries=0)
    f = HardenedFetcher(s, transport=httpx.MockTransport(handler), resolver=public_resolver)
    try:
        for _ in range(3):
            await f.fetch_safe("https://flaky.example/a")
        assert (await f.fetch_safe("https://flaky.example/b")).access_class == "host_unavailable"   # circuit open
        import asyncio
        await asyncio.sleep(0.25)
        assert (await f.fetch_safe("https://flaky.example/c")).access_class == "ok"                 # half-open retry succeeds
    finally:
        await f.close()


@pytest.mark.parametrize("excerpt,title,expected", [
    ("So, in our community it is considered ethical to control and hide information.", "Letters, March 4", False),
    ("We also feel strongly about continuing to produce a printed paper, a printed historical record.", "The Graphic 1877-2024", False),
    ("We do not remove stories from our archive, even when asked.", "Some headline", True),
    ("In the interest of minimizing harm to those involved, we have removed the document containing that information.", "", True),
    ("Our archive is part of the community's history.", "From the editor: why we keep our archive", True),
])
def test_first_person_on_story_pages(excerpt, title, expected):
    assert speaks_for_newsroom(PAPER, excerpt, title) is expected


@pytest.mark.parametrize("sentence", [
    "Remove the reporter, remove the story. Studies show communities without local papers see higher government costs.",
    "Journalists will not suppress essential information or falsify any document.",
])
def test_person_imperatives_and_reporting_ethics_are_not_relief(sentence):
    assert classify_statement(sentence).direction == "neutral"


@pytest.mark.parametrize("url,title,expected", [
    ("https://www.nbcchicago.com/news/local/nbc-chicago-news-standards-and-publishing-principles/3069505/", "", False),
    ("https://www.newschannel5.com/news/newschannel-5-initiative-removes-old-articles-to-offer-you-a-fresh-start", "", False),
    ("https://www.chronline.com/stories/interior-department-renaming-sites,299513", "Interior Department renaming sites", True),
    ("https://www.adn.com/politics/2026/06/22/rare-legislative-subpoena-issued-on-removal-of-dan-j-sullivan-from-ballot/", "", True),
    ("https://www.bainbridgereview.com/2011/03/03/citys-code-of-ethics-isnt-all-that-ethical-letters-march-4/", "", True),
])
def test_policy_named_pages_are_not_news_stories(url, title, expected):
    assert is_news_story("other", url, None, None, title) is expected


def test_combined_names_and_verb_forms_speak_for_newsroom():
    nc5 = {"name": "NewsChannel 5 Nashville / WTVF", "homepage_url": "https://www.newschannel5.com/", "domain": "newschannel5.com",
           "cohort": "broadcast_newsroom"}
    assert speaks_for_newsroom(nc5, "NewsChannel 5 Nashville has offered a Fresh Start program for old crime stories.")
    assert speaks_for_newsroom(PAPER, "If the subject provides proof charges were dropped, we will consider removing the story.")


def test_precedent_sources_are_not_discounted_as_news_stories():
    seed = {"name": "The Boston Globe", "homepage_url": "https://www.bostonglobe.com/2021/01/22/metro/fresh-start/",
            "domain": "bostonglobe.com", "cohort": "precedent_case"}
    about, _ = attribute_statement(seed, first=True, kind="article", url=seed["homepage_url"], published="2021-01-22",
                                   author="Staff", statement_type="case_by_case", excerpt="Editors will review requests.",
                                   context="")
    assert about is True


def test_blocked_site_is_never_no_guidance_even_after_reclassify(tmp_path, db, monkeypatch):
    from conftest import make_settings

    import nsmpa.stance as stance_mod
    from nsmpa.research import import_entities_csv
    from nsmpa.runs import create_or_resume_run
    from nsmpa.stance import classify_entity
    full = {"first_party_pages_attempted": 5, "first_party_pages_ok": 5, "first_party_inaccessible": 0,
            "first_party_policy_pages_ok": 2, "homepage_ok": True, "pages_ok_total": 5, "queries": 3, "queries_failed": 0,
            "tier1_queries": 3, "tier1_completed": 3}
    monkeypatch.setattr(stance_mod, "coverage_for", lambda db, run_id, entity_id: dict(full))   # coverage alone would allow it
    s = make_settings(tmp_path)
    p = tmp_path / "e.csv"
    p.write_text("name,url\nBlocked Times,https://blocked.example/\n")
    import_entities_csv(db, p, "professional_newsroom", "t")
    ent = db.execute("SELECT * FROM research_entities WHERE name='Blocked Times'").fetchone()
    create_or_resume_run(db, s, "full_research", "run-b")
    db.execute("INSERT INTO research_pages(run_id,entity_id,requested_url,final_url,status,access_class,page_kind,first_party) "
               "VALUES('run-b',?,?,?,'failed','blocked','homepage',1)", (ent["id"], "https://blocked.example/", "https://blocked.example/"))
    res = classify_entity(db, s, "run-b", ent)
    assert res.stance == "UNDETERMINED" and res.coverage["live_site"] == "blocks_automated_access"


def test_directory_site_is_not_a_newspapers_homepage():
    from nsmpa.identity import check_identity
    ent = {"name": "Bryant Daily", "cohort": "professional_newsroom", "homepage_url": "https://www.thepaperboy.com/",
           "parent_name": None}
    assert check_identity(ent, {}, "ok", None).status == "mismatch"

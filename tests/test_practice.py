"""Deep practice digging, per-action positions, voices and case-match (no network, no credits)."""
from __future__ import annotations

import json

import httpx

from conftest import make_settings, public_resolver
from nsmpa.config import MyCase
from nsmpa.evidence import action_positions, extract_voices
from nsmpa.fetch import HardenedFetcher
from nsmpa.practice import choose_samples, evenly
from nsmpa.research import import_entities_csv, research_all, start_research
from nsmpa.similarity import score_case_match
from test_pipeline import PAGE, FakeSearch


def article(title: str, body: str, noindex: bool = False) -> str:
    meta = '<meta name="robots" content="noindex, follow">' if noindex else ""
    return (f"<html><head><title>{title}</title>{meta}<meta name='author' content='Staff'></head>"
            f"<body><nav>Home</nav><main>{body}</main></body></html>")


SITEMAP = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{}</urlset>"""


def site(domain: str, *, sitewide_noindex: bool) -> dict[str, tuple[int, str, str]]:
    base = f"https://{domain}"
    urls = [f"{base}/2014/03/student-arrested-for-assault-near-campus/",
            f"{base}/2016/09/police-charged-two-in-dorm-theft/",
            f"{base}/2019/04/homecoming-parade-draws-record-crowd/",
            f"{base}/2020/10/library-renovation-finished-this-fall/",
            f"{base}/2022/02/basketball-team-wins-conference-title/"]
    pages = {
        f"{base}/": (200, "text/html", PAGE.format(title="Campus Daily", body="<p>Student news.</p><a href='/about/'>About</a>")),
        f"{base}/about/": (200, "text/html", PAGE.format(title="About", body="<p>The Campus Daily is the student newspaper of Example University, founded in 1920.</p>")),
        f"{base}/sitemap.xml": (200, "application/xml", SITEMAP.format("".join(f"<url><loc>{u}</loc></url>" for u in urls))),
        urls[0]: (200, "text/html", article("Student arrested for assault near campus", (
            "<p>Editor's note: The charges against the student were dismissed in 2015. This story has been updated to remove "
            "the student's name.</p><p>Police arrested a student on Tuesday.</p>"), noindex=True)),
        urls[1]: (200, "text/html", article("Police charge two in dorm theft", "<p>Two students were charged with theft.</p>",
                                             noindex=sitewide_noindex)),
    }
    for u in urls[2:]:
        pages[u] = (200, "text/html", article("Ordinary story", "<p>Ordinary campus coverage with no crime.</p>", noindex=sitewide_noindex))
    return pages


WEB = {**site("daily.example", sitewide_noindex=False), **site("allnoindex.example", sitewide_noindex=True)}


def handler(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/robots.txt":
        return httpx.Response(404)
    hit = WEB.get(str(req.url))
    if not hit:
        return httpx.Response(404, headers={"content-type": "text/html"}, content=b"nf")
    status, ctype, body = hit
    return httpx.Response(status, headers={"content-type": ctype}, content=body.encode())


async def run(tmp_path, db, **settings_kw):
    p = tmp_path / "e.csv"
    p.write_text("name,url\nCampus Daily,https://daily.example/\nAll Noindex Times,https://allnoindex.example/\n", encoding="utf-8")
    import_entities_csv(db, p, "student_media", "test")
    settings = make_settings(tmp_path, research_crime_article_sample=5, research_baseline_article_sample=3, **settings_kw)
    rid, _ = start_research(db, settings, cohort="student_media", limit=None, run_id=None, max_searches=None, command="t")
    fetcher = HardenedFetcher(settings, transport=httpx.MockTransport(handler), resolver=public_resolver)
    stats = await research_all(db, settings, rid, cohort="student_media", quiet=True, provider=FakeSearch({}), fetcher=fetcher)
    return rid, stats


def stance_row(db, rid, name):
    return db.execute("SELECT s.* FROM entity_stances s JOIN research_entities re ON re.id=s.entity_id "
                      "WHERE s.run_id=? AND re.name=?", (rid, name)).fetchone()


async def test_archive_digging_finds_practice_without_written_policy(tmp_path, db):
    rid, stats = await run(tmp_path, db)
    assert stats["status"] == "completed"
    row = stance_row(db, rid, "Campus Daily")
    assert row["relief_mode"] == "DEINDEX_OR_ANONYMIZE_PRESERVING_ARCHIVE"
    actions = json.loads(row["action_positions_json"])
    assert actions["deindex"]["technical"] == "noindex_observed"
    assert actions["anonymize"]["practice"] == "granted"
    assert "relief_practiced_without_written_policy" in row["review_reasons_json"]
    tech = db.execute("SELECT * FROM evidence_items WHERE statement_type='technical_noindex'").fetchall()
    assert len(tech) == 1 and "3 of 3 sampled comparison articles" in tech[0]["excerpt"]
    note = db.execute("SELECT * FROM evidence_items WHERE excerpt LIKE '%updated to remove%'").fetchone()
    assert note["evidence_class"] == "documented_practice" and note["direction"] == "supportive"
    # The crime narrative itself ("Police arrested a student") is not stored as evidence.
    assert db.scalar("SELECT COUNT(*) FROM evidence_items WHERE excerpt LIKE 'Police arrested a student%'") == 0


async def test_sitewide_noindex_is_not_targeted_deindexing(tmp_path, db):
    rid, _ = await run(tmp_path, db)
    row = stance_row(db, rid, "All Noindex Times")
    assert json.loads(row["action_positions_json"])["deindex"]["technical"] == "none"
    assert db.scalar("SELECT COUNT(*) FROM evidence_items e JOIN research_entities re ON re.id=e.entity_id "
                     "WHERE re.name='All Noindex Times' AND e.statement_type='technical_sitewide_noindex'") >= 1


async def test_practice_dig_never(tmp_path, db):
    rid, _ = await run(tmp_path, db, research_practice_dig="never")
    assert db.scalar("SELECT COUNT(*) FROM research_pages WHERE run_id=? AND page_kind='crime_article'", (rid,)) == 0


def test_choose_samples_is_oldest_to_newest_and_excludes_listings():
    urls = [f"https://x.example/{y}/01/student-arrested-{y}/" for y in range(2005, 2025)] + \
           ["https://x.example/tag/police/", "https://x.example/2010/05/campus-garden-opens-for-spring/"]
    crime, base = choose_samples(urls, 4, 1)
    assert crime[0].startswith("https://x.example/2005/") and crime[-1].startswith("https://x.example/2024/")
    assert "https://x.example/tag/police/" not in crime and base == ["https://x.example/2010/05/campus-garden-opens-for-spring/"]
    assert evenly(list("abcdef"), 3) == ["a", "c", "f"]


def test_action_positions_split_deindex_from_unpublish():
    p = action_positions("We do not unpublish stories, but we may remove a name from search results when charges are dismissed.")
    assert p == {"unpublish": "rejected", "deindex": "permitted"}
    assert action_positions("We update stories rather than unpublish them.") == {"update": "permitted", "unpublish": "rejected"}
    assert action_positions("Editors may anonymize a story about a minor.") == {"anonymize": "permitted"}


def test_voice_extraction_keeps_both_sides():
    text = ('"We will remove names from old minor-crime stories because search engines make them follow people forever," '
            'said Chris Quinn, editor of cleveland.com. Jane Roe, a media ethicist, said: "Newsrooms should never unpublish '
            'accurate stories; the archive is the historical record."')
    voices = {v.person_name: v for v in extract_voices(text)}
    assert voices["Chris Quinn"].statement.direction == "supportive"
    assert voices["Jane Roe"].statement.direction == "adverse"
    assert voices["Chris Quinn"].role.startswith("editor of cleveland")


def test_case_match_uses_only_my_facts():
    from nsmpa.config import CaseProfile
    mine = MyCase(student_or_university_setting=True, criminal_allegation_or_arrest=True, charges_dismissed_or_dropped=True,
                  substantial_time_passed=False, private_individual_no_public_role=False, search_engine_prominence=False,
                  reputational_consequences=False, relief_sought=["deindex"])
    text = "The student's arrest story was de-indexed after the charges were dismissed."
    full = score_case_match(CaseProfile(), mine, text)
    assert full.score == 100.0 or full.score > 80
    partial = score_case_match(CaseProfile(), mine, "The arrest story was updated.")
    assert partial.score < full.score

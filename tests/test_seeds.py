from __future__ import annotations

import httpx

from conftest import make_settings, public_resolver
from nsmpa.fetch import HardenedFetcher
from nsmpa.seeds import run_seeds, set_precedent_status
from test_pipeline import PAGE, FakeSearch

WEB = {
    "https://www.spj.org/ethicscode.asp": PAGE.format(title="SPJ Code of Ethics", body=(
        "<p>Minimize Harm. Consider the long-term implications of the extended reach and permanence of publication. "
        "Provide updated and more complete information as appropriate.</p>")),
    "https://news.example/globe-fresh-start": PAGE.format(title="The Boston Globe launches Fresh Start", body=(
        "<p>The Boston Globe will consider requests to anonymize or update old stories about minor crimes under its Fresh Start initiative. "
        "“We do not want a single mistake to define someone forever, so we may remove names from stories that no longer serve the public,” "
        "said Brian McGrory, editor of the Globe.</p>")),
    "https://news.example/sullivan-column": PAGE.format(title="Columnist on unpublishing", body=(
        "<p>Margaret Sullivan, a media columnist, said: “Newsrooms should be very reluctant to unpublish accurate stories, "
        "because the archive is part of the historical record.”</p>")),
}


def handler(req):
    if req.url.path == "/robots.txt":
        return httpx.Response(404)
    body = WEB.get(str(req.url))
    return httpx.Response(200 if body else 404, headers={"content-type": "text/html"}, content=(body or "nf").encode())


ROUTES = {
    '"Boston Globe" "Fresh Start"': [("https://news.example/globe-fresh-start", "The Boston Globe launches Fresh Start", "Boston Globe")],
    '"Margaret Sullivan"': [("https://news.example/sullivan-column", "Margaret Sullivan on unpublishing", "Margaret Sullivan")],
    '"Brian McGrory"': [("https://news.example/globe-fresh-start", "Brian McGrory on Fresh Start", "Brian McGrory")],
}


def fetcher(settings):
    return HardenedFetcher(settings, transport=httpx.MockTransport(handler), resolver=public_resolver)


async def test_precedents_find_sources_but_never_self_verify(tmp_path, db):
    s = make_settings(tmp_path)
    res = await run_seeds(db, s, "precedents", quiet=True, provider=FakeSearch(ROUTES), fetcher=fetcher(s))
    assert res["status"] == "completed"
    st = {r["seed_key"]: r["status"] for r in db.execute("SELECT * FROM precedent_seeds")}
    assert st["spj_code_minimize_harm"] == "sources_found"
    assert st["boston_globe_fresh_start"] == "sources_found"
    assert st["bangor_daily_news_policy"] == "not_found"
    assert "human_verified" not in st.values()
    set_precedent_status(db, "boston_globe_fresh_start", "human_verified", "read live page")
    res2 = await run_seeds(db, s, "precedents", quiet=True, provider=FakeSearch(ROUTES), fetcher=fetcher(s))
    assert db.scalar("SELECT status FROM precedent_seeds WHERE seed_key='boston_globe_fresh_start'") == "human_verified"
    assert res2["searches_live"] == 0  # all cached


async def test_expert_voices_capture_both_directions(tmp_path, db):
    s = make_settings(tmp_path)
    await run_seeds(db, s, "experts", quiet=True, provider=FakeSearch(ROUTES), fetcher=fetcher(s))
    rows = {r["person_name"]: r for r in db.execute("SELECT * FROM voices")}
    assert rows["Brian McGrory"]["direction"] == "supportive" and rows["Brian McGrory"]["expert_id"]
    assert rows["Margaret Sullivan"]["direction"] == "adverse" and rows["Margaret Sullivan"]["expert_id"]


async def test_seed_cohorts_excluded_from_default_research_and_denominators(tmp_path, db):
    from nsmpa.research import select_entities
    from nsmpa.validate import all_cohorts
    s = make_settings(tmp_path)
    await run_seeds(db, s, "experts", quiet=True, provider=FakeSearch(ROUTES), fetcher=fetcher(s))
    assert not [r for r in select_entities(db, None, None) if r["cohort"] in {"expert", "precedent_case"}]
    assert "expert" not in all_cohorts(db)

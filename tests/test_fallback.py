"""Blocked-site fallback (archive, snippets, AI search with confirmation, other sources) and researcher captures."""
from __future__ import annotations

import json
import threading
from urllib.parse import unquote

import httpx
import pytest
from conftest import make_settings, public_resolver
from test_pipeline import PAGE, FakeSearch

from nsmpa.capture import CaptureError, add_capture, capture_queue
from nsmpa.fetch import HardenedFetcher
from nsmpa.research import import_entities_csv, research_all, start_research

HOME = PAGE.format(title="The Daily | Student newspaper of the University of Example", body=(
    "<p>The Daily is the independent student newspaper of the University of Example. Editor-in-chief: A. Writer.</p>"
    "<a href='https://www.dailyex.com/page/policies'>Policies</a><a href='https://www.dailyex.com/news/'>News</a>"))
POLICY = PAGE.format(title="Policies | The Daily", body=(
    "<p>Online archives are a part of the institutional memory of the newspaper and a historical record of our community. "
    "As such, we will not remove nor attempt to hide from commercial search engines any material in our online archives. "
    "In certain instances, we may choose to update a story if it is found to be wildly inaccurate.</p>"))
PARKED = PAGE.format(title="dailyex.com", body="<p>This domain is for sale. Buy this domain today from HugeDomains.</p>")

REAL_QUOTE = ("As such, we will not remove nor attempt to hide from commercial search engines any material in our online "
              "archives.")
FAKE_QUOTE = "The Daily will unpublish any article about a dismissed charge upon request within thirty days of receiving it."


def make_handler(home_html=HOME):
    def handler(req: httpx.Request):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        host = req.url.host
        if host in {"www.dailyex.com", "dailyex.com"}:
            return httpx.Response(403, headers={"server": "awselb/2.0"})
        if host == "web.archive.org":
            path = unquote(str(req.url))
            if "/cdx/" in path:
                if "matchType=domain" in path:
                    return httpx.Response(200, json=[["original", "timestamp"],
                                                     ["https://www.dailyex.com/page/policies", "20261001000000"]])
                return httpx.Response(200, json=[["timestamp", "original"], ["20261006000000", "x"]])
            if "/web/" in path and "id_/" in path:
                target = path.split("id_/", 1)[1]
                body = home_html if target.rstrip("/").endswith("dailyex.com") else POLICY if "policies" in target else None
                if body:
                    return httpx.Response(200, headers={"content-type": "text/html"}, content=body.encode())
            return httpx.Response(404)
        return httpx.Response(404)
    return handler


ROUTES = {"site:dailyex.com (unpublish": [
    ("https://www.dailyex.com/page/policies", "Policies | The Daily",
     "Oct 1, 2026 — we will not remove nor attempt to hide from commercial search engines any material in our online archives"),
    ("https://www.dailyex.com/2019/requests", "Requests | The Daily",
     "Mar 3, 2019 — Editors may remove a name from an article when charges are dismissed and the person asks us to.")]}


def entity(db, tmp_path):
    p = tmp_path / "e.csv"
    p.write_text("name,url,parent_name\nThe Daily,https://www.dailyex.com/,University of Example\n", encoding="utf-8")
    import_entities_csv(db, p, "student_media", "test")
    return int(db.scalar("SELECT id FROM research_entities WHERE name='The Daily'"))


@pytest.fixture
def fake_ai(monkeypatch):
    calls = []

    def fake_call_json(client, settings, user_text, system, schema, *, grounded=False):
        calls.append(grounded)
        return ({"policy_urls": ["https://www.dailyex.com/page/policies"],
                 "quotes": [{"quote": REAL_QUOTE, "url": "https://www.dailyex.com/search/"},
                            {"quote": FAKE_QUOTE, "url": "https://www.dailyex.com/made-up"}],
                 "summary": "Will not remove archived material."},
                {"model": "fake", "input_tokens": 1, "output_tokens": 1, "stop_reason": "STOP",
                 "sources": [{"title": "dailyex.com", "uri": "https://vertexaisearch.example/redirect"}], "search_queries": ["x"]})
    import nsmpa.ai_review as ar
    monkeypatch.setattr(ar, "make_client", lambda settings: object())
    monkeypatch.setattr(ar, "call_json", fake_call_json)
    return calls


async def run(db, s, routes, handler):
    rid, _ = start_research(db, s, cohort="student_media", limit=None, run_id=None, max_searches=None, command="t")
    f = HardenedFetcher(s, transport=httpx.MockTransport(handler), resolver=public_resolver)
    provider = FakeSearch(routes)
    stats = await research_all(db, s, rid, cohort="student_media", quiet=True, provider=provider, fetcher=f)
    return rid, stats, provider


async def test_blocked_site_is_researched_from_archive_with_provenance(tmp_path, db, fake_ai):
    s = make_settings(tmp_path, research_recover_stale_sites=False, max_retries=0)
    eid = entity(db, tmp_path)
    rid, stats, _provider = await run(db, s, ROUTES, make_handler())
    assert stats["failed"] == 0
    st = db.execute("SELECT * FROM entity_stances WHERE run_id=? AND entity_id=?", (rid, eid)).fetchone()
    assert st["stance"] in {"STRICT_ARCHIVE", "UPDATE_ONLY"}, st["rationale"]
    cov = json.loads(st["coverage_json"])
    assert cov["live_site"] == "blocks_automated_access" and cov["fallback"]["archived_pages"] >= 2
    assert "site_blocks_robots_evidence_from_archive_or_leads" in json.loads(st["review_reasons_json"])
    # The deciding excerpt came from the archive, cites the original URL, and links the archived copy.
    adv = db.execute("SELECT * FROM evidence_items WHERE id=?", (st["strongest_adverse_id"],)).fetchone()
    assert adv["acquisition"] == "archive" and adv["source_url"] == "https://www.dailyex.com/page/policies"
    assert adv["archive_url"].startswith("https://web.archive.org/web/") and adv["archive_ts"]
    # Snippets are stored, labelled, and are never the deciding evidence.
    snips = db.execute("SELECT * FROM evidence_items WHERE run_id=? AND acquisition='snippet'", (rid,)).fetchall()
    assert snips and all(r["id"] not in (st["strongest_adverse_id"], st["strongest_supportive_id"]) for r in snips)
    assert any(r["direction"] == "supportive" for r in snips) and st["supportive_count"] == 0   # a lead, not a finding


async def test_ai_quotes_are_leads_until_found_word_for_word(tmp_path, db, fake_ai):
    s = make_settings(tmp_path, research_recover_stale_sites=False, max_retries=0)
    eid = entity(db, tmp_path)
    _rid, _, provider = await run(db, s, ROUTES, make_handler())
    assert fake_ai == [True]                                    # one grounded (web-search) call
    leads = {r["quote"]: r for r in db.execute("SELECT * FROM ai_leads WHERE entity_id=?", (eid,))}
    assert leads[REAL_QUOTE]["status"] == "confirmed" and leads[REAL_QUOTE]["confirmed_via"] == "archive"
    assert leads[REAL_QUOTE]["confirmed_url"] == "https://www.dailyex.com/page/policies"   # not the AI's wrong URL
    assert leads[FAKE_QUOTE]["status"] == "unconfirmed"
    # The invented quote never becomes evidence.
    assert not db.execute("SELECT 1 FROM evidence_items WHERE excerpt LIKE '%thirty days%'").fetchone()
    # An exact-phrase search was spent trying to locate it, within the cap.
    assert sum(1 for q in provider.calls if q.startswith('"')) <= s.blocked_fallback_phrase_searches


async def test_silence_from_a_blocked_site_is_never_no_policy(tmp_path, db, monkeypatch):
    s = make_settings(tmp_path, research_recover_stale_sites=False, max_retries=0, blocked_fallback_ai=False)
    eid = entity(db, tmp_path)
    home_only = PAGE.format(title="The Daily | Student newspaper of the University of Example",
                            body="<p>The Daily is the independent student newspaper of the University of Example.</p>")

    def handler(req):
        if req.url.host == "web.archive.org" and "matchType=domain" in unquote(str(req.url)):
            return httpx.Response(200, json=[["original", "timestamp"]])
        return make_handler(home_only)(req)
    rid, _, _ = await run(db, s, {}, handler)
    st = db.execute("SELECT * FROM entity_stances WHERE run_id=? AND entity_id=?", (rid, eid)).fetchone()
    assert st["stance"] == "UNDETERMINED"


async def test_parked_archived_homepage_stops_research(tmp_path, db, fake_ai):
    s = make_settings(tmp_path, research_recover_stale_sites=False, max_retries=0)
    eid = entity(db, tmp_path)
    rid, _, provider = await run(db, s, ROUTES, make_handler(PARKED))
    st = db.execute("SELECT * FROM entity_stances WHERE run_id=? AND entity_id=?", (rid, eid)).fetchone()
    assert st["stance"] == "UNDETERMINED" and "archived homepage" in st["rationale"]
    assert provider.calls == [] and fake_ai == []


async def test_capture_adds_labelled_evidence_confirms_leads_and_updates_stance(tmp_path, db, fake_ai):
    s = make_settings(tmp_path, research_recover_stale_sites=False, max_retries=0, blocked_fallback_phrase_searches=0)
    eid = entity(db, tmp_path)
    base = make_handler(PAGE.format(title="The Daily | Student newspaper of the University of Example",
                                    body="<p>The Daily is the independent student newspaper of the University of Example.</p>"))

    def homepage_only(req):  # the archive has no copy of the policy page: only a capture can confirm the lead
        u = unquote(str(req.url))
        if req.url.host == "web.archive.org" and ("matchType=domain" in u or "policies" in u):
            return httpx.Response(200, json=[["original", "timestamp"]]) if "/cdx/" in u else httpx.Response(404)
        return base(req)
    rid, _, _ = await run(db, s, {}, homepage_only)
    assert db.scalar("SELECT status FROM ai_leads WHERE quote=?", (REAL_QUOTE,)) == "unconfirmed"
    assert any(q["id"] == eid for q in capture_queue(db))
    res = add_capture(db, s, entity_id=eid, url="https://www.dailyex.com/page/policies", captured_by="KH",
                      title="Policies", text="Archive policy\n\n" + REAL_QUOTE + " In certain instances, we may choose to "
                      "update a story if it is found to be wildly inaccurate.")
    assert res["first_party"] and res["evidence"] >= 1 and res["ai_leads_confirmed"] == 1 and res["run_id"] == rid
    assert res["stance"] in {"STRICT_ARCHIVE", "UPDATE_ONLY"}
    ev = db.execute("SELECT * FROM evidence_items WHERE page_id=(SELECT page_id FROM captures WHERE id=?)", (res["capture_id"],)).fetchall()
    assert ev and all(r["acquisition"] == "capture" for r in ev)
    assert db.scalar("SELECT confirmed_via FROM ai_leads WHERE quote=?", (REAL_QUOTE,)) == "capture"
    again = add_capture(db, s, entity_id=eid, url="https://www.dailyex.com/page/policies", captured_by="KH",
                        text="Archive policy\n\n" + REAL_QUOTE + " In certain instances, we may choose to update a story "
                        "if it is found to be wildly inaccurate.")
    assert again["duplicate"] is True
    with pytest.raises(CaptureError):
        add_capture(db, s, entity_id=eid, url="not a url", captured_by="KH", text="x" * 100)
    with pytest.raises(CaptureError):
        add_capture(db, s, entity_id=eid, url="https://www.dailyex.com/", captured_by="", text="x" * 100)


async def test_capture_without_prior_run_creates_capture_run(tmp_path, db):
    s = make_settings(tmp_path)
    eid = entity(db, tmp_path)
    res = add_capture(db, s, entity_id=eid, url="https://www.dailyex.com/page/policies", captured_by="KH",
                      text="We do not remove stories from our online archive. Editors may add an update when charges are dismissed.")
    assert res["run_id"].startswith("capture-") and res["evidence"] >= 1


async def test_gui_capture_routes(tmp_path, db):
    from test_gui import req

    from nsmpa.gui import make_server
    s = make_settings(tmp_path)
    eid = entity(db, tmp_path)
    httpd, token = make_server(s, port=0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        body = {"entity_id": eid, "url": "https://www.dailyex.com/page/policies", "captured_by": "KH",
                "text": "We do not remove stories from our online archive. Editors may add an update when charges are dismissed."}
        assert req(port, "/api/capture", method="POST", body=body)[0] == 403                       # token required
        st, out, _ = req(port, "/api/capture", method="POST", body=body, headers={"X-NSMPA-Token": token})
        assert st == 200 and json.loads(out)["ok"] is True
        st, out, _ = req(port, "/api/capture", method="POST", body={**body, "url": "ftp://x"}, headers={"X-NSMPA-Token": token})
        assert st == 400
        st, out, _ = req(port, "/api/capture")
        d = json.loads(out)
        assert st == 200 and d["captures"][0]["entity"] == "The Daily" and "queue" in d and "leads" in d
        st, out, _ = req(port, "/api/entities?q=Dail")
        assert json.loads(out)["items"][0]["id"] == eid
        st, html, _ = req(port, "/")
        assert 'data-tab="capture"' in html
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_redaction_covers_lead_urls_and_notes():
    from nsmpa.redact import TEXT_FIELDS, URL_FIELDS
    assert {"claimed_url", "confirmed_url"} <= URL_FIELDS and "note" in TEXT_FIELDS



def test_sibling_domains_are_first_party():
    from nsmpa.research import is_first_party
    ent = {"homepage_url": "https://apnews.com/", "domain": "apnews.com", "metadata_json": json.dumps({"alt_domains": ["ap.org"]})}
    assert is_first_party(ent, "https://www.ap.org/about/news-values-and-principles/")
    assert is_first_party(ent, "https://apnews.com/hub/x") and not is_first_party(ent, "https://example.org/ap.org")


async def test_news_story_snippets_are_not_kept_and_recitation_falls_back(tmp_path, db, monkeypatch):
    import nsmpa.ai_review as ar
    calls = []

    def recites(client, settings, user_text, system, schema, *, grounded=False):
        calls.append(user_text)
        if len(calls) == 1:
            raise ar.AIUnavailable("Gemini declined this page (finish_reason=RECITATION)")
        return ({"policy_urls": ["https://www.dailyex.com/page/policies"], "quotes": [], "summary": "Archive policy."},
                {"model": "fake", "input_tokens": 1, "output_tokens": 1, "stop_reason": "STOP"})
    monkeypatch.setattr(ar, "make_client", lambda settings: object())
    monkeypatch.setattr(ar, "call_json", recites)
    s = make_settings(tmp_path, research_recover_stale_sites=False, max_retries=0)
    entity(db, tmp_path)
    routes = {"site:dailyex.com (unpublish": [
        ("https://www.dailyex.com/2022/04/27/google-removal-demands", "Google now takes removal demands",
         "Apr 27, 2022 — Google will not remove information that appears as part of a news article.")]}
    rid, _, _ = await run(db, s, routes, make_handler())
    assert len(calls) == 2 and "Do not quote" in calls[1]
    assert not db.execute("SELECT 1 FROM evidence_items WHERE acquisition='snippet' AND excerpt LIKE '%Google%'").fetchone()
    # The URL-only answer still led to the archived policy page.
    assert db.execute("SELECT 1 FROM research_pages WHERE run_id=? AND final_url='https://www.dailyex.com/page/policies' "
                      "AND acquisition='archive'", (rid,)).fetchone()


async def test_single_blocked_policy_page_is_read_from_archive(tmp_path, db):
    s = make_settings(tmp_path, research_recover_stale_sites=False, max_retries=0, research_practice_dig="never")
    p = tmp_path / "e.csv"
    p.write_text("name,url,parent_name\nThe Daily,https://www.dailyopen.com/,University of Example\n", encoding="utf-8")
    import_entities_csv(db, p, "student_media", "test")
    home = HOME.replace("dailyex.com", "dailyopen.com")

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        if req.url.host == "help.dailyopen.com":
            return httpx.Response(403)
        if req.url.host == "www.dailyopen.com":
            return httpx.Response(200, headers={"content-type": "text/html"}, content=home.encode()) if req.url.path == "/" \
                else httpx.Response(404)
        if req.url.host == "web.archive.org" and "help.dailyopen.com" in unquote(str(req.url)):
            return httpx.Response(200, headers={"content-type": "text/html"}, content=POLICY.encode())
        return httpx.Response(404)
    routes = {"site:dailyopen.com (unpublish": [("https://help.dailyopen.com/policies/removal", "Removal policy", "remove")]}
    rid, _, _ = await run(db, s, routes, handler)
    row = db.execute("SELECT * FROM research_pages WHERE run_id=? AND final_url='https://help.dailyopen.com/policies/removal' "
                     "AND acquisition='archive'", (rid,)).fetchone()
    assert row is not None
    assert db.scalar("SELECT status FROM research_targets WHERE run_id=? AND url LIKE '%help.dailyopen.com%'", (rid,)) == "archive_fallback"


def test_candidate_cap_keeps_highest_scores(tmp_path, db):
    from nsmpa.research import EntityResearcher
    from nsmpa.runs import create_or_resume_run
    s = make_settings(tmp_path, research_max_targets_per_entity=2)
    eid = entity(db, tmp_path)
    create_or_resume_run(db, s, "full_research", "run-cap")
    for i, score in enumerate([0.3, 0.9, 0.5, 0.7]):
        db.execute("INSERT INTO research_targets(run_id,entity_id,purpose,topic,url,domain,score,status) "
                   "VALUES('run-cap',?,?,?,?,?,?,'candidate')", (eid, "policy", "t", f"https://x.example/{i}", "x.example", score))
    ent = db.execute("SELECT * FROM research_entities WHERE id=?", (eid,)).fetchone()
    EntityResearcher.offline(db, s, "run-cap")._trim_candidates(ent)
    kept = [r[0] for r in db.execute("SELECT score FROM research_targets WHERE status='candidate' ORDER BY score DESC")]
    assert kept == [0.9, 0.7]

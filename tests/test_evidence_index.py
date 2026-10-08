"""Researcher evidence-index import: precedents, cohort placement, and no bias in percentages."""
from __future__ import annotations

import csv
import json

import httpx
import pytest

from conftest import make_settings, public_resolver
from nsmpa.evidence_index import import_evidence_index, index_report
from nsmpa.fetch import HardenedFetcher
from nsmpa.research import import_entities_csv
from nsmpa.seeds import run_seeds
from nsmpa.validate import cohort_metrics
from test_pipeline import PAGE, FakeSearch

COLS = ["index_id", "organization", "tier", "evidence_focus", "source_url", "kind", "homepage", "group", "country", "state",
        "source_kind"]
ROWS = [
    ["001", "Example TV", "A", "Shared digital-removal standards", "https://www.exampletv.com/standards/", "broadcast",
     "https://www.exampletv.com/", "Example Group", "US", "TX", "own_publication"],
    ["002", "Example Daily", "A", "Direct de-indexing policy", "https://exampledaily.com/up-for-review/", "newspaper",
     "https://exampledaily.com/", "", "US", "PA", "own_publication"],
    ["003", "Campus Paper", "B", "Student-media review", "https://casestudy.example/ten-papers/", "student_college",
     "https://campuspaper.example/", "", "US", "WA", "secondary_report"],
    ["004", "Other Campus Paper", "B", "Student-media review", "https://casestudy.example/ten-papers/", "student_college",
     "https://othercampus.example/", "", "US", "OR", "secondary_report"],
    ["005", "Maple Gazette", "B", "Unpublishing policy", "https://maplegazette.example/policy/", "student_college",
     "https://maplegazette.example/", "", "CA", "", "own_publication"],
]
WEB = {
    "https://www.exampletv.com/standards/": PAGE.format(title="Standards", body="<p>If the subject of a story provides proof "
        "charges were dropped, they were found not guilty or their conviction was expunged, we will consider removing the story.</p>"),
    "https://exampledaily.com/up-for-review/": PAGE.format(title="Up for Review", body="<p>Our main remedy is to deindex an "
        "article so it remains online but is not findable via search engines.</p>"),
    "https://casestudy.example/ten-papers/": PAGE.format(title="Ten campus papers", body="<p>Campus Paper editors said they "
        "will remove names from old crime stories when charges are dismissed.</p><p>Another paper never unpublishes.</p>"),
    "https://maplegazette.example/policy/": PAGE.format(title="Policy", body="<p>We may unpublish an article in rare cases.</p>"),
}


def handler(req):
    if req.url.path == "/robots.txt":
        return httpx.Response(404)
    body = WEB.get(str(req.url))
    return httpx.Response(200 if body else 404, headers={"content-type": "text/html"}, content=(body or "nf").encode())


def write_index(tmp_path):
    p = tmp_path / "index.csv"
    with p.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLS)
        w.writerows(ROWS)
    return p


def test_import_places_entities_and_never_biases_rates(tmp_path, db):
    s = make_settings(tmp_path)
    existing = tmp_path / "existing.csv"   # Example Daily is already in the sourced professional panel
    existing.write_text("name,url\nExample Daily,https://exampledaily.com/\nPanel Paper,https://panelpaper.example/\n")
    import_entities_csv(db, existing, "professional_newsroom", "directory")
    res = import_evidence_index(db, write_index(tmp_path), name="Test index")
    assert res["precedent_seeds"] == 5 and res["precedent_only"] == 1             # Canadian paper: precedent only
    assert res["new_broadcast_newsroom"] == 1 and res["matched_existing_professional_newsroom"] == 1
    tv = db.execute("SELECT * FROM research_entities WHERE name='Example TV'").fetchone()
    meta = json.loads(tv["metadata_json"])
    assert tv["cohort"] == "broadcast_newsroom" and meta["excluded_from_rates"] and meta["group"] == "Example Group"
    daily = json.loads(db.scalar("SELECT metadata_json FROM research_entities WHERE name='Example Daily'"))
    assert not daily.get("excluded_from_rates") and daily["evidence_index"][0]["index_id"] == "002"   # stays counted
    # Denominators ignore organizations selected from the index.
    assert cohort_metrics(db, s, "professional_newsroom").denominator == 2
    assert cohort_metrics(db, s, "broadcast_newsroom").denominator == 0
    assert db.scalar("SELECT COUNT(*) FROM precedent_seeds WHERE source_list='Test index' AND kind='broadcast'") == 1
    again = import_evidence_index(db, write_index(tmp_path), name="Test index")      # idempotent
    assert again["precedent_seeds"] == 5 and "new_broadcast_newsroom" not in again


async def test_verify_index_reads_sources_and_secondary_reports_must_name_the_org(tmp_path, db):
    s = make_settings(tmp_path)
    import_evidence_index(db, write_index(tmp_path), name="Test index")
    f = HardenedFetcher(s, transport=httpx.MockTransport(handler), resolver=public_resolver)
    provider = FakeSearch({})
    res = await run_seeds(db, s, "precedents", quiet=True, provider=provider, fetcher=f, only_index=True, search=False)
    assert provider.calls == [] and res["credits_estimated"] == 0                    # no searches
    st = {r["index_id"]: r["status"] for r in db.execute("SELECT index_id, status FROM precedent_seeds WHERE index_id IS NOT NULL")}
    assert st["001"] == "sources_found" and st["002"] == "sources_found"
    assert st["003"] == "sources_found"          # the case study names Campus Paper
    assert st["004"] == "not_found"              # same page, but Other Campus Paper is never named
    rep = {r["index_id"]: r for r in index_report(db, "Test index")}
    assert rep["002"]["tier"] == "A" and rep["001"]["status"] == "sources_found"


def test_opening_the_database_never_waits_on_a_running_job(tmp_path):
    import sqlite3
    import threading
    from nsmpa.db import Database
    path = tmp_path / "busy.sqlite3"
    Database(path).close()                     # create + migrate + record the views fingerprint
    holder = sqlite3.connect(str(path))
    holder.execute("BEGIN IMMEDIATE")          # a long job holding the write lock
    opened = []
    def open_and_close():
        d = Database(path)
        opened.append(d.scalar("SELECT COUNT(*) FROM schema_migrations"))
        d.close()
    t = threading.Thread(target=open_and_close, daemon=True)
    t.start()
    t.join(timeout=5)
    try:
        assert opened, "Database() blocked behind a writer: opening must be read-only when nothing is pending"
    finally:
        holder.rollback()
        holder.close()


async def test_reclassify_updates_stored_evidence_and_stances(tmp_path, db):
    from nsmpa.reclassify import reclassify_run
    s = make_settings(tmp_path)
    import_evidence_index(db, write_index(tmp_path), name="Test index")
    f = HardenedFetcher(s, transport=httpx.MockTransport(handler), resolver=public_resolver)
    res = await run_seeds(db, s, "precedents", quiet=True, provider=FakeSearch({}), fetcher=f, only_index=True, search=False)
    ev = db.execute("SELECT id FROM evidence_items WHERE run_id=? AND excerpt LIKE 'Our main remedy%'", (res["run_id"],)).fetchone()
    db.execute("UPDATE evidence_items SET statement_type='mention', direction='neutral' WHERE id=?", (ev["id"],))  # stale label
    out = reclassify_run(db, s, res["run_id"], out_dir=tmp_path)
    assert out["changed"] >= 1 and out["direction_changed"] >= 1
    assert db.scalar("SELECT direction FROM evidence_items WHERE id=?", (ev["id"],)) == "supportive"
    assert reclassify_run(db, s, res["run_id"], out_dir=tmp_path)["changed"] == 0          # idempotent


@pytest.mark.parametrize("sentence,stype", [
    ("If the story is deemed to not meet our standards or is so substantially wrong as to require removal, we will replace "
     "it with a detailed and transparent explanation.", "relief_narrow_exceptions"),
    ("Staff will consider requests to remove posted content if a reader provides a written request that the content in "
     "question is factually inaccurate.", "relief_narrow_exceptions"),
    ("We may remove a name from an old story to prevent ongoing harm, even when the story contains no errors.", "relief_permitted"),
])
def test_errors_only_permission_is_a_narrow_exception(sentence, stype):
    from nsmpa.evidence import classify_statement
    assert classify_statement(sentence).statement_type == stype


def test_news_story_about_others_is_not_the_newsrooms_policy(tmp_path, db):
    from nsmpa.research import EntityResearcher
    from nsmpa.capture import _QuietDash, _Page
    s = make_settings(tmp_path)
    existing = tmp_path / "e.csv"
    existing.write_text("name,url\nAtlanta Paper,https://atl.example/\n")
    import_entities_csv(db, existing, "professional_newsroom", "directory")
    ent = db.execute("SELECT * FROM research_entities WHERE name='Atlanta Paper'").fetchone()
    r = EntityResearcher(db, s, None, None, "run-x", _QuietDash(), None)
    page = _Page("https://atl.example/education/emory-to-remove-honors", "Emory to remove honors",
                 "Emory University announced Thursday it will remove the names of two men from buildings and professorships. "
                 "Editor's note: This story has been updated to remove the name of a student after charges were dismissed.")
    page.published = "2022-04-21"
    from nsmpa.runs import create_or_resume_run
    create_or_resume_run(db, s, 'full_research', 'run-x')
    db.execute("INSERT INTO research_pages(run_id,entity_id,requested_url,final_url,status,access_class) VALUES('run-x',?,?,?,'fetched','ok')",
               (ent["id"], page.url, page.url))
    pid = db.scalar("SELECT id FROM research_pages WHERE run_id='run-x'")
    r._store_evidence(ent, page, pid, None, "t", True, "other", None, None)
    rows = {row["excerpt"][:20]: row for row in db.execute("SELECT * FROM evidence_items WHERE run_id='run-x'")}
    emory = next(v for k, v in rows.items() if k.startswith("Emory"))
    note = next(v for k, v in rows.items() if k.startswith("Editor"))
    assert emory["about_entity"] == 0 and emory["evidence_class"] == "secondary_report"
    assert note["about_entity"] == 1 and note["evidence_class"] == "documented_practice"


@pytest.mark.parametrize("sentence,expected", [
    ("The Bangor Daily News reviews requests to remove old crime stories from Google.", {"deindex": "conditional"}),
    ("We consider article removal requests on a case-by-case basis.", {"unpublish": "conditional"}),
    ("If you'd like to be considered for our Fresh Start program, send an email along with a link to the story you'd like "
     "removed and any court documents.", {"unpublish": "conditional"}),
])
def test_recall_test_action_positions(sentence, expected):
    from nsmpa.evidence import action_positions
    assert action_positions(sentence) == expected


def test_caveat_is_not_a_refusal():
    from nsmpa.evidence import action_positions
    pos = action_positions("Removing the article from our site does not necessarily remove it from Google searches.")
    assert "rejected" not in pos.values()

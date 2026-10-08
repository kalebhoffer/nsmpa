"""Wayback comparison, AI second opinion and accuracy audit (no network, no credits, no API calls)."""
from __future__ import annotations

import csv
import json
from types import SimpleNamespace

import httpx
from openpyxl import load_workbook

from conftest import make_settings, public_resolver
from nsmpa.ai_review import run_ai_review, verify_quote
from nsmpa.audit import cohen_kappa, create_sample, export_csv, import_csv, report, wilson
from nsmpa.fetch import HardenedFetcher
from nsmpa.packet import build_packet
from nsmpa.research import import_entities_csv, research_all, start_research
from nsmpa.utils import normalize_for_hash
from nsmpa.wayback import compare_versions, person_names
import test_pipeline as tp
import test_practice as pr

OLD = ("<html><head><title>Student John Smithers arrested for assault</title></head><body><main>"
       "<p>Police arrested John Smithers, a junior, on Tuesday near campus. Smithers was charged with assault.</p>"
       "</main></body></html>").encode()
NEW_NOINDEX = ("<html><head><title>Student arrested for assault</title><meta name='robots' content='noindex'></head><body><main>"
               "<p>Editor's note: charges were dismissed. This story has been updated to remove the student's name.</p>"
               "<p>Police arrested a junior on Tuesday near campus. The student was charged with assault.</p></main></body></html>").encode()


# ============================================================================ Wayback

def test_compare_versions_detects_changes_without_storing_names():
    obs = compare_versions(OLD, NEW_NOINDEX, url="https://daily.example/a", current_status=200, threshold=0.85)
    types = {o["type"] for o in obs}
    assert {"names_removed", "noindex_added", "title_changed"} <= types
    blob = json.dumps(obs)
    assert "Smithers" not in blob and "John" not in blob  # privacy: names never stored
    nr = next(o for o in obs if o["type"] == "names_removed")
    assert nr["count"] >= 1 and nr["update_note_present"] is True
    assert compare_versions(OLD, None, url="u", current_status=410, threshold=0.85) == [{"type": "unpublished", "current_status": 410}]
    assert compare_versions(OLD, OLD, url="u", current_status=200, threshold=0.85) == []


def test_person_names_skips_institutions():
    names = person_names("John Smithers met Police Chief Brown at Washington State University and Mary Ann O'Neil.")
    assert "John Smithers" in names and not any("University" in n for n in names)


CRIME_URL = "https://daily.example/2014/03/student-arrested-for-assault-near-campus/"


def archive_handler(req: httpx.Request) -> httpx.Response:
    if req.url.host == "web.archive.org":
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        if req.url.path.startswith("/cdx/"):
            if "student-arrested" in str(req.url):
                body = [["timestamp", "statuscode", "digest"], ["20140305000000", "200", "A"], ["20200101000000", "200", "B"]]
            else:
                body = [["timestamp", "statuscode", "digest"]]
            return httpx.Response(200, headers={"content-type": "application/json"}, content=json.dumps(body).encode())
        if req.url.path.startswith("/web/20140305000000id_/"):
            return httpx.Response(200, headers={"content-type": "text/html"}, content=OLD)
        return httpx.Response(404)
    if str(req.url) == CRIME_URL:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=NEW_NOINDEX)
    return pr.handler(req)


async def test_wayback_in_research_records_archive_practice(tmp_path, db):
    p = tmp_path / "e.csv"
    p.write_text("name,url\nCampus Daily,https://daily.example/\n", encoding="utf-8")
    import_entities_csv(db, p, "student_media", "t")
    s = make_settings(tmp_path, research_crime_article_sample=5)
    rid, _ = start_research(db, s, cohort="student_media", limit=None, run_id=None, max_searches=None, command="t")
    f = HardenedFetcher(s, transport=httpx.MockTransport(archive_handler), resolver=public_resolver)
    await research_all(db, s, rid, cohort="student_media", quiet=True, provider=tp.FakeSearch({}), fetcher=f)
    chk = db.execute("SELECT * FROM wayback_checks WHERE url=?", (CRIME_URL,)).fetchone()
    assert chk["status"] == "changed" and chk["earliest_ts"] == "20140305000000"
    ev = db.execute("SELECT * FROM evidence_items WHERE statement_type='wayback_names_removed'").fetchone()
    assert ev and "Smithers" not in ev["excerpt"] and "Names withheld" in ev["excerpt"]
    assert db.scalar("SELECT COUNT(*) FROM evidence_items WHERE statement_type='wayback_noindex_added'") == 1
    assert db.scalar("SELECT COUNT(*) FROM evidence_items WHERE excerpt LIKE '%Smithers%' OR context LIKE '%Smithers%'") == 0
    st = db.execute("SELECT action_positions_json FROM entity_stances WHERE run_id=?", (rid,)).fetchone()
    assert json.loads(st["action_positions_json"])["anonymize"]["technical"] == "archive_change_observed"


# ============================================================================ AI second opinion

class FakeAI:
    """Stands in for anthropic.Anthropic(); returns canned structured output keyed by page content."""
    def __init__(self):
        self.calls = 0
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw):
        self.calls += 1
        page = kw["messages"][0]["content"]
        findings = []
        if "We do not remove stories" in page:
            findings = [
                dict(quote="We do not remove stories from our archive.", kind="policy", action="unpublish",
                     position="rejected", direction="adverse", conditions="", speaker="", speaker_role=""),
                dict(quote="We happily delete any story on request.", kind="policy", action="unpublish",  # fabricated
                     position="permitted", direction="supportive", conditions="", speaker="", speaker_role=""),
            ]
        if "served the valley since 1901" in page:
            findings = [dict(quote="The Strict Times has served the valley since 1901", kind="other", action="none",
                             position="mentioned", direction="adverse", conditions="", speaker="", speaker_role="")]
        assert "my case" not in kw["system"].lower()  # neutral prompt: requester's goal never sent
        return SimpleNamespace(stop_reason="end_turn", model=kw["model"], usage=SimpleNamespace(input_tokens=100, output_tokens=50),
                               content=[SimpleNamespace(type="text", text=json.dumps({"relevant": bool(findings), "summary": "s",
                                                                                     "findings": findings}))])


async def test_ai_review_verifies_quotes_compares_and_caches(tmp_path, db):
    s = make_settings(tmp_path)
    tp.seed_newsrooms(tmp_path, db)
    rid, _ = await tp.run_research(db, s, tp.FakeSearch(tp.SEARCH_ROUTES))
    ai = FakeAI()
    res = await run_ai_review(db, s, run_id=rid, quiet=True, client=ai)
    assert res["status"] == "completed" and res["live_calls"] == ai.calls > 0
    rows = {r["quote"]: r for r in db.execute("SELECT * FROM ai_findings")}
    assert rows["We do not remove stories from our archive."]["quote_verified"] == 1
    assert rows["We do not remove stories from our archive."]["agreement"] == "agree"
    assert rows["We happily delete any story on request."]["quote_verified"] == 0  # fabricated quote rejected
    assert rows["We happily delete any story on request."]["agreement"] == "unverified_quote"
    assert db.scalar("SELECT COUNT(*) FROM review_queue WHERE reasons_json LIKE '%ai_rule%'") >= 1
    # Stances are untouched by AI output.
    assert tp.stances(db, rid)["Strict Times"] == "UPDATE_ONLY"
    ai2 = FakeAI()
    res2 = await run_ai_review(db, s, run_id=rid, quiet=True, client=ai2)
    assert ai2.calls == 0 and res2["cached"] == res["live_calls"]


async def test_ai_review_call_cap(tmp_path, db):
    s = make_settings(tmp_path)
    tp.seed_newsrooms(tmp_path, db)
    rid, _ = await tp.run_research(db, s, tp.FakeSearch(tp.SEARCH_ROUTES))
    ai = FakeAI()
    res = await run_ai_review(db, s, run_id=rid, quiet=True, client=ai, max_calls=1)
    assert ai.calls <= 1 and res["status"] == "budget_exhausted" and res["remaining"] > 0


def test_verify_quote_is_strict_but_whitespace_tolerant():
    page = normalize_for_hash("We do  not remove\nstories from our archive.")
    assert verify_quote("We do not remove stories from our archive", page)
    assert not verify_quote("We do not delete stories", page)
    assert not verify_quote("We do", page)  # too short to be meaningful


# ============================================================================ audit

def test_wilson_and_kappa():
    p, lo, hi = wilson(45, 50)
    assert p == 0.9 and lo < 0.9 < hi and lo > 0.75
    assert wilson(0, 0) is None
    assert cohen_kappa([("a", "a"), ("b", "b"), ("a", "b"), ("b", "b")]) == 0.5
    assert cohen_kappa([("a", "a")] * 5) == 1.0


async def test_audit_sample_label_report(tmp_path, db):
    s = make_settings(tmp_path)
    tp.seed_newsrooms(tmp_path, db)
    await tp.run_research(db, s, tp.FakeSearch(tp.SEARCH_ROUTES))
    smp = create_sample(db, 10, seed=7)
    assert smp["sample_size"] > 0 and set(smp["strata"]) <= {"supportive", "adverse", "neutral"}
    assert create_sample(db, 10, seed=7)["strata"] == smp["strata"]  # reproducible
    out = tmp_path / "audit.csv"
    export_csv(db, smp["audit_id"], out)
    rows = list(csv.DictReader(open(out, encoding="utf-8")))
    assert "machine_direction" not in rows[0]  # blind by default
    machine = {r["audit_item_id"]: r for r in db.execute(
        "SELECT ai.id AS audit_item_id, e.direction FROM audit_items ai JOIN evidence_items e ON e.id=ai.evidence_id WHERE ai.audit_id=?",
        (smp["audit_id"],))}
    for labeler, flip in (("alice", False), ("bob", True)):
        p = tmp_path / f"{labeler}.csv"
        with open(p, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            for i, r in enumerate(rows):
                d = machine[int(r["audit_item_id"])]["direction"]
                if flip and i == 0:
                    d = "neutral" if d != "neutral" else "adverse"
                r = dict(r, **{"relevant (y/n)": "y", "direction (supportive/adverse/neutral)": d})
                w.writerow(r)
        import_csv(db, p, labeler)
    rep = report(db, smp["audit_id"])
    assert rep["direction_accuracy"][0] == 1.0  # alice agreed with every machine label
    assert rep["inter_rater"]["items"] == len(rows) and rep["inter_rater"]["cohen_kappa_direction"] < 1.0
    res = build_packet(db, s, s.output_dir)
    wb = load_workbook(res["workbook"])
    assert {"Archive Changes", "AI Second Opinion", "Accuracy"} <= set(wb.sheetnames)
    assert "Direction accuracy" in [c.value for c in wb["Accuracy"]["A"]]

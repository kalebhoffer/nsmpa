"""AI veto on decisive excerpts: veto-only, cached, auditable, and respected by reclassification."""
from __future__ import annotations

import pytest
from conftest import make_settings

from nsmpa.ai_gate import gate_run
from nsmpa.research import import_entities_csv
from nsmpa.runs import create_or_resume_run
from nsmpa.stance import classify_entity, store_stance

STATUE = "Not everyone agrees, however, that the Monaghan statue should be taken down."
POLICY = "We will consider removing an article when the subject was never charged."


def setup(db, tmp_path, excerpts):
    s = make_settings(tmp_path, ai_gate_enabled=True)
    p = tmp_path / "e.csv"
    p.write_text("name,url\nExample Herald,https://herald.example/\n")
    import_entities_csv(db, p, "professional_newsroom", "t")
    ent = db.execute("SELECT * FROM research_entities WHERE name='Example Herald'").fetchone()
    create_or_resume_run(db, s, "full_research", "run-g")
    for i, (text, stype, direction) in enumerate(excerpts):
        db.execute("INSERT INTO evidence_items(run_id,entity_id,cohort,source_url,excerpt,context,excerpt_sha256,near_dup_key,"
                   "first_party,about_entity,evidence_class,statement_type,direction,extraction_confidence,relevance_score,"
                   "authority_score) VALUES('run-g',?,?,?,?,?,?,?,1,1,'written_policy',?,?,0.8,1.0,1.0)",
                   (ent["id"], "professional_newsroom", f"https://herald.example/policy/{i}", text, "", f"h{i}", f"n{i}",
                    stype, direction))
    store_stance(db, "run-g", ent["id"], classify_entity(db, s, "run-g", ent))
    db.conn.commit()
    return s, ent


@pytest.fixture
def fake_ai(monkeypatch):
    import nsmpa.ai_review as ar
    calls = []

    def call_json(client, settings, user_text, system, schema, *, grounded=False):
        calls.append(user_text)
        if "statue" in user_text:
            return {"verdict": "reject", "direction": "neutral", "reason": "About a statue, not published journalism."}, {}
        if "BOOM" in user_text:
            raise RuntimeError("model unavailable")
        return {"verdict": "accept", "direction": "adverse", "reason": "The paper's own removal policy."}, {}
    monkeypatch.setattr(ar, "make_client", lambda settings: object())
    monkeypatch.setattr(ar, "call_json", call_json)
    return calls


async def test_veto_removes_a_false_finding_and_records_why(tmp_path, db, fake_ai):
    s, _ent = setup(db, tmp_path, [(STATUE, "relief_permitted", "supportive")])
    assert db.scalar("SELECT stance FROM entity_stances WHERE run_id='run-g'") == "SUPPORTS_RELIEF"
    res = await gate_run(db, s, "run-g")
    assert res["rejected"] == 1 and res["changed"] == [{"entity": "Example Herald", "from": "SUPPORTS_RELIEF",
                                                         "to": res["changed"][0]["to"]}]
    assert db.scalar("SELECT stance FROM entity_stances WHERE run_id='run-g'") not in {"SUPPORTS_RELIEF"}
    row = db.execute("SELECT ai_gate, ai_gate_reason FROM evidence_items WHERE excerpt=?", (STATUE,)).fetchone()
    assert row["ai_gate"] == "rejected" and "statue" in row["ai_gate_reason"]
    assert db.scalar("SELECT COUNT(*) FROM ai_gate_checks WHERE verdict='rejected'") == 1


async def test_accepted_finding_keeps_its_stance_and_ai_cannot_change_direction(tmp_path, db, fake_ai):
    s, _ent = setup(db, tmp_path, [(POLICY, "changed_circumstance_relief", "supportive")])
    res = await gate_run(db, s, "run-g")
    assert res["rejected"] == 0 and res["changed"] == []
    # The fake AI called it "adverse"; our direction and stance are untouched (veto only).
    assert db.scalar("SELECT direction FROM evidence_items WHERE excerpt=?", (POLICY,)) == "supportive"
    assert db.scalar("SELECT stance FROM entity_stances WHERE run_id='run-g'") == "SUPPORTS_CHANGED_CIRCUMSTANCES"


async def test_veto_loop_checks_newly_decisive_excerpts_and_caches(tmp_path, db, fake_ai):
    s, _ent = setup(db, tmp_path, [(STATUE, "relief_permitted", "supportive"), (POLICY, "changed_circumstance_relief", "supportive")])
    await gate_run(db, s, "run-g")
    assert db.scalar("SELECT stance FROM entity_stances WHERE run_id='run-g'") == "SUPPORTS_CHANGED_CIRCUMSTANCES"
    n = len(fake_ai)
    db.execute("UPDATE evidence_items SET ai_gate=NULL")
    await gate_run(db, s, "run-g")
    assert len(fake_ai) == n                     # second pass served from the cache


async def test_ai_errors_never_reject(tmp_path, db, fake_ai):
    s, _ent = setup(db, tmp_path, [("BOOM We will consider removing an article when charges are dismissed.",
                                   "changed_circumstance_relief", "supportive")])
    res = await gate_run(db, s, "run-g")
    assert res["rejected"] == 0
    assert db.execute("SELECT ai_gate FROM evidence_items").fetchone()[0] is None
    assert db.scalar("SELECT verdict FROM ai_gate_checks") == "error"


async def test_reclassify_respects_vetoes(tmp_path, db, fake_ai):
    from nsmpa.reclassify import reclassify_run
    s, _ent = setup(db, tmp_path, [(STATUE, "relief_permitted", "supportive")])
    await gate_run(db, s, "run-g")
    reclassify_run(db, s, "run-g", out_dir=tmp_path)
    assert db.scalar("SELECT stance FROM entity_stances WHERE run_id='run-g'") != "SUPPORTS_RELIEF"


async def test_no_ai_available_changes_nothing(tmp_path, db, monkeypatch):
    import nsmpa.ai_review as ar
    def unavailable(settings):
        raise ar.AIUnavailable("no key")
    monkeypatch.setattr(ar, "make_client", unavailable)
    s, _ent = setup(db, tmp_path, [(STATUE, "relief_permitted", "supportive")])
    res = await gate_run(db, s, "run-g")
    assert res.get("unavailable") and res["rejected"] == 0
    assert db.scalar("SELECT stance FROM entity_stances WHERE run_id='run-g'") == "SUPPORTS_RELIEF"

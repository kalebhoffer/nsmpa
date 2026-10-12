"""A duplicate merged into its survivor never reappears in counts, the evidence snapshot or run exports."""
from __future__ import annotations

from conftest import make_settings

from nsmpa.export import export_research
from nsmpa.packet import gather
from nsmpa.research import import_entities_csv
from nsmpa.runs import create_or_resume_run
from nsmpa.stance import classify_entity, store_stance
from nsmpa.validate import cohort_metrics

POLICY = "We will consider removing an article when the subject was never charged."


def setup(db, tmp_path):
    s = make_settings(tmp_path, ai_gate_enabled=False)
    p = tmp_path / "e.csv"
    # The same paper entered twice (the directory's mistaken second listing), plus an unrelated paper.
    p.write_text("name,url\nThe Pioneer Log,https://piolog.example/\nPioneer Log,https://piolog-idaho.example/\n"
                 "Example Herald,https://herald.example/\n")
    import_entities_csv(db, p, "professional_newsroom", "t")
    ents = {r["name"]: r for r in db.execute("SELECT * FROM research_entities")}
    create_or_resume_run(db, s, "full_research", "run-m")
    for i, ent in enumerate(ents.values()):
        db.execute("INSERT INTO run_items(run_id,item_type,item_key,status) VALUES('run-m','entity',?,'done')", (str(ent["id"]),))
        db.execute("INSERT INTO evidence_items(run_id,entity_id,cohort,source_url,excerpt,context,excerpt_sha256,near_dup_key,"
                   "first_party,about_entity,evidence_class,statement_type,direction,extraction_confidence,relevance_score,"
                   "authority_score) VALUES('run-m',?,?,?,?,'',?,?,1,1,'written_policy','changed_circumstance_relief',"
                   "'supportive',0.8,1.0,1.0)",
                   (ent["id"], "professional_newsroom", f"https://site{i}.example/policy", POLICY, f"h{i}", f"n{i}"))
        store_stance(db, "run-m", ent["id"], classify_entity(db, s, "run-m", ent))
    db.conn.commit()
    return s, ents["The Pioneer Log"]["id"], ents["Pioneer Log"]["id"]


def merge(db, keep, dup):
    db.execute("UPDATE OR IGNORE entity_sources SET entity_id=? WHERE entity_id=?", (keep, dup))
    db.execute("UPDATE research_entities SET active=0, merged_into=? WHERE id=?", (keep, dup))
    db.conn.commit()


def test_merged_duplicate_leaves_counts_snapshot_and_exports(tmp_path, db):
    s, keep, dup = setup(db, tmp_path)
    assert cohort_metrics(db, s, "professional_newsroom").researched == 3
    merge(db, keep, dup)

    m = cohort_metrics(db, s, "professional_newsroom")
    assert m.researched == 3 - 1 and m.determinate == 2

    data = gather(db, s)
    ids = {e["entity_id"] for e in data["entities"]}
    assert keep in ids and dup not in ids
    names = [e["entity"] for e in data["all_evidence"]]
    assert names.count("The Pioneer Log") == 1 and "Pioneer Log" not in names    # survivor keeps its own finding

    counts = export_research(db, "run-m", tmp_path / "out")
    assert counts["entities"] == 2 and counts["evidence"] == 2 and counts["entity_stances"] == 2
    exported = (tmp_path / "out" / "research_run-m" / "entity_stances.csv").read_text()
    assert "The Pioneer Log" in exported and "\nPioneer Log" not in exported and ",Pioneer Log," not in exported
    # Provenance moved to the survivor, not lost.
    assert db.scalar("SELECT COUNT(*) FROM entity_sources WHERE entity_id=?", (dup,)) == 0
    assert db.scalar("SELECT COUNT(*) FROM entity_sources WHERE entity_id=?", (keep,)) == 2

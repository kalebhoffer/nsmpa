"""Re-apply the current sentence classifier to evidence already stored, then recompute stances. No fetching.

Classifier rules improve as validation finds errors. Stored excerpts keep their context, so they can be re-judged
in place: statement type, direction, action positions, cues and confidence are updated, and every affected
organization's stance is recomputed for that run. Technical observations (Wayback, noindex) and AI findings are left
untouched. Each change is logged to ``output/reclassify_<run>.csv`` so a reviewer can see what moved.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .config import Settings
from .db import Database
from .evidence import STATEMENT_DIRECTION, action_positions, classify_statement
from .review import enqueue_entity_review
from .stance import classify_entity, store_stance


def reclassify_run(db: Database, settings: Settings, run_id: str, *, out_dir: Path | None = None) -> dict:
    rows = db.execute("SELECT * FROM evidence_items WHERE run_id=? AND evidence_class!='technical'", (run_id,)).fetchall()
    changes = []
    for r in rows:
        ctx = (r["context"] or "")
        st = classify_statement(r["excerpt"], ctx.replace(r["excerpt"], " "))
        actions = json.dumps(action_positions(r["excerpt"]), sort_keys=True)
        if st.statement_type == r["statement_type"] and actions == (r["actions_json"] or "{}"):
            continue
        changes.append({"evidence_id": r["id"], "entity_id": r["entity_id"], "from": r["statement_type"],
                        "to": st.statement_type, "direction_from": r["direction"], "direction_to": st.direction,
                        "excerpt": r["excerpt"][:300]})
        db.execute("UPDATE evidence_items SET statement_type=?, direction=?, actions_json=?, rationale=?, extraction_confidence=? "
                   "WHERE id=?", (st.statement_type, STATEMENT_DIRECTION[st.statement_type],
                                  actions, "; ".join(st.cues)[:500],
                                  st.confidence, r["id"]))
    touched = sorted({c["entity_id"] for c in changes})
    for eid in touched:
        if not db.scalar("SELECT 1 FROM entity_stances WHERE run_id=? AND entity_id=?", (run_id, eid)):
            continue  # precedent/expert runs have no stance rows
        ent = db.execute("SELECT * FROM research_entities WHERE id=?", (eid,)).fetchone()
        result = classify_entity(db, settings, run_id, ent)
        store_stance(db, run_id, eid, result)
        enqueue_entity_review(db, run_id, ent, result)
    db.conn.commit()
    log = None
    if changes:
        out_dir = out_dir or Path(settings.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        log = out_dir / f"reclassify_{run_id}.csv"
        with log.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(changes[0]))
            w.writeheader()
            w.writerows(changes)
    flips = sum(1 for c in changes if c["direction_from"] != c["direction_to"])
    return {"run_id": run_id, "evidence_checked": len(rows), "changed": len(changes), "direction_changed": flips,
            "organizations_restanced": len(touched), "log": str(log) if log else None}

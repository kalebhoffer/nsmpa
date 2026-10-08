"""Human-review queue.

Priority is an additive, documented score (higher = review first):

  high-similarity precedent            +30 (scaled by similarity)
  changed-criminal-outcome evidence    +25
  documented practice / deindexing     +20
  policy vs practice contradiction     +25
  conflicting statements (MIXED)       +20
  major national organization          +15 (benchmark panel, support orgs)
  determinate finding (needs verify)   +10
  low confidence                       +10
  ambiguous publication identification +20

Reviewer decisions are stored in ``review_queue`` and propagate to ``entity_stances.review_status``
and ``evidence_items.verification_status``; they never delete machine output.
"""
from __future__ import annotations

import json

from .db import Database
from .utils import json_meta

DECISIONS = {"accept", "reject", "correct", "skip"}


def entity_priority(entity, result) -> tuple[float, list[str]]:
    reasons = list(result.review_reasons)
    meta = {}
    try:
        meta = json_meta(entity)
    except (ValueError, TypeError, KeyError):
        meta = {}
    p = 0.0
    if "high_similarity_precedent" in reasons:
        p += 15 + 15 * min(1.0, result.max_similarity / 100.0)
    if result.stance == "SUPPORTS_CHANGED_CIRCUMSTANCES":
        p += 25
        reasons.append("changed_criminal_outcome")
    if "documented_practice" in reasons:
        p += 20
    if any(r.startswith("policy_practice_contradiction") for r in reasons):
        p += 25
    if "conflicting_policy_statements" in reasons:
        p += 20
    if entity["source"] in {"builtin_benchmark", "builtin_seed"} or meta.get("benchmark"):
        p += 15
        reasons.append("major_national_organization")
    if "determinate_finding_requires_verification" in reasons:
        p += 10
    if "low_confidence" in reasons:
        p += 10
    if "relief_practiced_without_written_policy" in reasons:
        p += 20
    if meta.get("ambiguous"):
        p += 20
        reasons.append("ambiguous_publication_identification")
    return round(p, 2), sorted(set(reasons))


def enqueue_entity_review(db: Database, run_id: str, entity, result) -> None:
    if not result.requires_review:
        return
    priority, reasons = entity_priority(entity, result)
    stance_id = db.scalar("SELECT id FROM entity_stances WHERE run_id=? AND entity_id=?", (run_id, entity["id"]), None)
    if stance_id is None:
        return
    db.execute(
        """
        INSERT INTO review_queue(run_id,item_type,item_id,entity_id,cohort,priority,reasons_json)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(run_id,item_type,item_id) DO UPDATE SET priority=excluded.priority,reasons_json=excluded.reasons_json
        """,
        (run_id, "entity_stance", stance_id, entity["id"], entity["cohort"], priority, json.dumps(reasons)),
    )


def enqueue_publication_review(db: Database, publication_id: int, unitid: str, priority: float, reasons: list[str]) -> None:
    db.execute(
        """
        INSERT INTO review_queue(run_id,item_type,item_id,cohort,priority,reasons_json)
        VALUES('publications','publication',?, 'student_media',?,?)
        ON CONFLICT(run_id,item_type,item_id) DO UPDATE SET priority=excluded.priority,reasons_json=excluded.reasons_json
        """,
        (publication_id, priority, json.dumps(sorted(set(reasons)))),
    )


def open_items(db: Database, run_id: str | None = None, cohort: str | None = None, limit: int = 20) -> list:
    sql = """
      SELECT q.*, re.name AS entity_name, re.homepage_url, s.stance, s.confidence, s.rationale,
             s.strongest_supportive_id, s.strongest_adverse_id
      FROM review_queue q
      LEFT JOIN research_entities re ON re.id=q.entity_id
      LEFT JOIN entity_stances s ON q.item_type='entity_stance' AND s.id=q.item_id
      WHERE q.status='open' AND q.run_id NOT IN (SELECT id FROM v_excluded_runs)"""
    params: list = []
    if run_id:
        sql += " AND q.run_id=?"
        params.append(run_id)
    if cohort:
        sql += " AND q.cohort=?"
        params.append(cohort)
    sql += " ORDER BY q.priority DESC, q.id LIMIT ?"
    params.append(limit)
    return db.execute(sql, params).fetchall()


def record_decision(db: Database, review_id: int, decision: str, *, reviewer: str = "", note: str = "",
                    corrected_stance: str | None = None) -> None:
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {sorted(DECISIONS)}")
    item = db.execute("SELECT * FROM review_queue WHERE id=?", (review_id,)).fetchone()
    if not item:
        raise ValueError(f"No review item {review_id}")
    status = {"accept": "accepted", "reject": "rejected", "correct": "corrected", "skip": "skipped"}[decision]
    with db.transaction():
        db.conn.execute(
            "UPDATE review_queue SET status=?, decision=?, reviewer=?, note=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, corrected_stance or decision, reviewer, note, review_id))
        if item["item_type"] == "entity_stance" and decision != "skip":
            db.conn.execute("UPDATE entity_stances SET review_status=? WHERE id=?",
                            (f"{status}:{corrected_stance}" if corrected_stance else status, item["item_id"]))
            stance = db.conn.execute("SELECT * FROM entity_stances WHERE id=?", (item["item_id"],)).fetchone()
            if stance and decision in {"accept", "reject"}:
                ver = "verified" if decision == "accept" else "rejected"
                for col in ("strongest_supportive_id", "strongest_adverse_id"):
                    if stance[col]:
                        db.conn.execute("UPDATE evidence_items SET verification_status=?, reviewer_note=? WHERE id=?",
                                        (ver, note or None, stance[col]))
        elif item["item_type"] == "publication" and decision != "skip":
            db.conn.execute("UPDATE publications SET verification_status=?, verification_note=?, verified_at=CURRENT_TIMESTAMP WHERE id=?",
                            ("human_verified" if decision == "accept" else f"human_{status}", note or None, item["item_id"]))


def verify_evidence(db: Database, evidence_id: int, status: str, note: str = "") -> None:
    if status not in {"verified", "rejected", "disputed", "unverified"}:
        raise ValueError("status must be verified|rejected|disputed|unverified")
    db.execute("UPDATE evidence_items SET verification_status=?, reviewer_note=? WHERE id=?", (status, note or None, evidence_id))
    db.conn.commit()

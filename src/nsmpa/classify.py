from __future__ import annotations

import json
import re

from .db import Database
from .models import PolicyClass

RELIEF_ACTION = r"(?:de[- ]?index|anonymi[sz]e|unpublish|remove|delete|take[- ]?down|suppress(?: a)? name)"
POSITIVE_RELIEF = re.compile(
    rf"(?:\b(?:may|might|can|could|will|we consider|editors? may|requests? (?:are|will be) considered).{{0,100}}\b{RELIEF_ACTION}\b|"
    rf"\b{RELIEF_ACTION}\w*.{{0,100}}\b(?:case[- ]by[- ]case|circumstances|request|discretion|appropriate)\b)",
    re.I,
)
NEGATIVE_RELIEF = re.compile(
    rf"(?:\b(?:do not|does not|will not|won't|never|generally do not|policy is not to).{{0,80}}\b{RELIEF_ACTION}\b|"
    rf"\b{RELIEF_ACTION}\w*.{{0,80}}\b(?:not permitted|not allowed|only if required by law|only in extraordinary))",
    re.I,
)
CHANGED = re.compile(
    r"\b(chang(?:e|ed|ing) circumstances|charges? (?:were |was )?(?:dismissed|dropped)|"
    r"expung(?:e|ed|ement)|seal(?:ed|ing) record|acquitt(?:ed|al)|rehabilitat(?:e|ed|ion)|"
    r"reputational? harm|privacy concern|disproportionate harm)\b",
    re.I,
)
CASE_BY_CASE = re.compile(r"\bcase[- ]by[- ]case\b|\b(?:editor|editorial board).{0,80}\bdiscretion\b", re.I)
UPDATE_ONLY = re.compile(
    r"\b(?:update|editor'?s note|correction|addendum|follow[- ]up|contextualize)\b",
    re.I,
)
STRICT_ARCHIVE = re.compile(
    r"\b(?:historical record|archive integrity|permanent archive|record of publication)\b|"
    r"\b(?:do not|will not|never).{0,80}\b(?:remove|delete|unpublish|de[- ]?index)\b",
    re.I,
)


def _support_value(text: str, tag: str) -> int | None:
    if tag in {"deindex", "anonymize", "unpublish", "remove", "takedown"}:
        if NEGATIVE_RELIEF.search(text):
            return 0
        if POSITIVE_RELIEF.search(text):
            return 1
    if tag in {"changed_circumstances", "dismissed_charges", "expunged", "sealed", "acquitted", "rehabilitation", "reputational_harm"}:
        return 1 if not NEGATIVE_RELIEF.search(text) else 0
    if tag in {"case_by_case", "privacy", "safety", "minimize_harm"}:
        return 1
    if tag == "archive_integrity" and STRICT_ARCHIVE.search(text):
        return 0
    return None


def classify_publication(db: Database, publication_id: int, run_id: str) -> dict:
    pages = db.execute(
        "SELECT * FROM pages WHERE publication_id=? AND run_id=?",
        (publication_id, run_id),
    ).fetchall()
    evidence = db.execute(
        "SELECT * FROM evidence WHERE publication_id=? AND run_id=? AND evidence_type='written_policy'",
        (publication_id, run_id),
    ).fetchall()

    attempted = len(pages)
    fetched = sum(1 for p in pages if p["status"] == "fetched")
    policy_pages = sum(1 for p in pages if p["status"] == "fetched" and float(p["policy_score"] or 0) >= 8)
    blocked = [p for p in pages if p["status"] in {"blocked", "skipped_robots"}]
    failures = [p for p in pages if p["status"] in {"failed", "too_large"}]
    homepage_ok = any(p["status"] == "fetched" and int(p["depth"] or 0) == 0 for p in pages)

    blockers: list[str] = []
    if not homepage_ok:
        blockers.append("homepage_not_fetched")
    if attempted and len(blocked) / attempted >= 0.35:
        blockers.append("substantial_access_blocking")
    if attempted and len(failures) / attempted >= 0.50:
        blockers.append("high_fetch_failure_rate")
    if attempted == 0:
        blockers.append("no_pages_attempted")

    inspection_complete = bool(
        homepage_ok
        and attempted >= 15
        and fetched >= 8
        and (len(blocked) + len(failures)) <= max(8, int(attempted * 0.50))
    )

    relief_pos = relief_neg = changed = case = update = strict = 0
    rationale_bits: list[str] = []
    for e in evidence:
        text = e["excerpt"]
        tag = e["tag"]
        support = _support_value(text, tag)
        if support is not None:
            db.execute("UPDATE evidence SET supports_relief=? WHERE id=?", (support, e["id"]))
        if POSITIVE_RELIEF.search(text):
            relief_pos += 1
        if NEGATIVE_RELIEF.search(text):
            relief_neg += 1
        if CHANGED.search(text):
            changed += 1
        if CASE_BY_CASE.search(text):
            case += 1
        if UPDATE_ONLY.search(text):
            update += 1
        if STRICT_ARCHIVE.search(text):
            strict += 1
    db.conn.commit()

    if relief_pos:
        primary = PolicyClass.A_EXPLICIT_RELIEF
        confidence = min(0.98, 0.72 + relief_pos * 0.05)
        rationale_bits.append(f"{relief_pos} excerpt(s) contain affirmative post-publication relief language")
    elif changed:
        primary = PolicyClass.B_CHANGED_CIRCUMSTANCES
        confidence = min(0.95, 0.70 + changed * 0.04)
        rationale_bits.append(f"{changed} excerpt(s) recognize changed-outcome/harm factors")
    elif case:
        primary = PolicyClass.C_CASE_BY_CASE
        confidence = min(0.92, 0.68 + case * 0.04)
        rationale_bits.append(f"{case} excerpt(s) describe case-by-case or discretionary review")
    elif strict or relief_neg:
        primary = PolicyClass.E_STRICT_ARCHIVE
        confidence = min(0.96, 0.72 + max(strict, relief_neg) * 0.04)
        rationale_bits.append(f"{max(strict, relief_neg)} excerpt(s) support a restrictive archive/removal policy")
    elif update and policy_pages:
        primary = PolicyClass.D_UPDATE_ONLY
        confidence = min(0.90, 0.66 + update * 0.03)
        rationale_bits.append("Policy evidence emphasizes updates/corrections without affirmative removal relief")
    elif inspection_complete and not evidence:
        primary = PolicyClass.F_NO_PUBLIC_POLICY_FOUND
        confidence = 0.75
        rationale_bits.append("Configured inspection threshold met and no qualifying public policy evidence was located")
    elif inspection_complete and policy_pages:
        primary = PolicyClass.F_NO_PUBLIC_POLICY_FOUND
        confidence = 0.65
        rationale_bits.append("Inspection threshold met but extracted policy material did not address post-publication relief")
    else:
        primary = PolicyClass.U_UNDETERMINED
        confidence = 0.90 if blockers else 0.60
        rationale_bits.append("Inspection did not meet the threshold required for a no-policy conclusion")

    if relief_pos and relief_neg:
        rationale_bits.append("Contradictory permissive and restrictive language detected")
        confidence = min(confidence, 0.72)
    if blockers:
        rationale_bits.append("Blockers: " + ", ".join(blockers))

    requires_review = primary != PolicyClass.F_NO_PUBLIC_POLICY_FOUND or bool(relief_pos and relief_neg) or confidence < 0.8
    rationale = "; ".join(rationale_bits)
    db.execute(
        """
        INSERT INTO classifications(
          publication_id,run_id,primary_class,confidence,rationale,inspection_complete,
          pages_attempted,pages_fetched,policy_pages_found,blockers_json,requires_human_review
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(publication_id,run_id) DO UPDATE SET
          primary_class=excluded.primary_class,confidence=excluded.confidence,rationale=excluded.rationale,
          inspection_complete=excluded.inspection_complete,pages_attempted=excluded.pages_attempted,
          pages_fetched=excluded.pages_fetched,policy_pages_found=excluded.policy_pages_found,
          blockers_json=excluded.blockers_json,requires_human_review=excluded.requires_human_review,
          created_at=CURRENT_TIMESTAMP
        """,
        (
            publication_id, run_id, primary.value, confidence, rationale, int(inspection_complete),
            attempted, fetched, policy_pages, json.dumps(blockers), int(requires_review),
        ),
    )
    db.conn.commit()
    return {
        "publication_id": publication_id,
        "primary_class": primary.value,
        "confidence": confidence,
        "inspection_complete": inspection_complete,
        "attempted": attempted,
        "fetched": fetched,
        "policy_pages": policy_pages,
        "blockers": blockers,
    }


def classify_all(db: Database, run_id: str) -> dict[str, int]:
    pubs = db.execute(
        "SELECT DISTINCT publication_id FROM pages WHERE run_id=? ORDER BY publication_id",
        (run_id,),
    ).fetchall()
    counts: dict[str, int] = {p.value: 0 for p in PolicyClass}
    for row in pubs:
        result = classify_publication(db, int(row["publication_id"]), run_id)
        counts[result["primary_class"]] += 1
    return counts

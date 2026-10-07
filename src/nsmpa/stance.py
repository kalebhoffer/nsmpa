"""Entity-level stance classification (v0.3).

Inputs are *unique, substantive, entity-attributable* evidence items only:
- written policy / professional guidance found on the entity's own site (``first_party=1``),
- documented practice (first-party or third-party text that names the entity),
- technical observations are summarised separately and never drive the stance.

Policy, practice, technical implementation and professional guidance are kept as separate
concepts; a stance is a statement about *written policy/guidance*. Practice that contradicts
the policy stance is flagged for human review rather than merged into it.

``NO_RELEVANT_GUIDANCE`` requires demonstrated inspection coverage; anything less is
``UNDETERMINED``. Inaccessible sources are never treated as negative findings.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field

from .config import Settings
from .db import Database
from .fetch import INACCESSIBLE_CLASSES

STANCE_VERSION = "0.3"
# Editor's columns/notes on the entity's own site state policy too (authority 0.8 vs 1.0 for policy pages).
POLICY_CLASSES = ("written_policy", "professional_guidance", "editorial_statement")
DETERMINATE = {"SUPPORTS_RELIEF", "SUPPORTS_CHANGED_CIRCUMSTANCES", "CASE_BY_CASE", "UPDATE_ONLY", "STRICT_ARCHIVE", "MIXED"}


@dataclass
class StanceResult:
    stance: str
    confidence: float
    rationale: str
    supportive: int = 0
    adverse: int = 0
    evidence_count: int = 0
    strongest_supportive_id: int | None = None
    strongest_adverse_id: int | None = None
    max_similarity: float = 0.0
    coverage: dict = field(default_factory=dict)
    review_reasons: list[str] = field(default_factory=list)
    practice_summary: str = ""
    technical_summary: str = ""

    @property
    def requires_review(self) -> bool:
        return bool(self.review_reasons)


def _distinct(items: list, types: set[str]) -> list:
    """One item per near-duplicate group, best (highest authority/confidence) first."""
    seen: set[str] = set()
    out = []
    for it in sorted(items, key=lambda e: (-(e["authority_score"] or 0), -(e["extraction_confidence"] or 0), e["id"])):
        if it["statement_type"] not in types or it["near_dup_key"] in seen:
            continue
        seen.add(it["near_dup_key"])
        out.append(it)
    return out


def coverage_for(db: Database, run_id: str, entity_id: int) -> dict:
    rows = db.execute(
        "SELECT access_class, page_kind, first_party FROM research_pages WHERE run_id=? AND entity_id=?",
        (run_id, entity_id)).fetchall()
    fp = [r for r in rows if r["first_party"]]
    fp_ok = [r for r in fp if r["access_class"] == "ok"]
    fp_bad = [r for r in fp if (r["access_class"] or "") in INACCESSIBLE_CLASSES]
    # Only real search providers count toward inspection coverage.
    q = db.execute(
        "SELECT status, purpose FROM search_queries WHERE run_id=? AND entity_id=? AND provider!='none'",
        (run_id, entity_id)).fetchall()
    tier1 = [r for r in q if ":t1:" in (r["purpose"] or "")]
    return {
        "first_party_pages_attempted": len(fp),
        "first_party_pages_ok": len(fp_ok),
        "first_party_inaccessible": len(fp_bad),
        "first_party_policy_pages_ok": sum(1 for r in fp_ok if r["page_kind"] in {"policy", "about"}),
        "homepage_ok": any(r["page_kind"] == "homepage" for r in fp_ok),
        "pages_ok_total": sum(1 for r in rows if r["access_class"] == "ok"),
        "queries": len(q),
        "queries_failed": sum(1 for r in q if r["status"] != "completed"),
        "tier1_queries": len(tier1),
        "tier1_completed": sum(1 for r in tier1 if r["status"] == "completed"),
    }


def classify_entity(db: Database, settings: Settings, run_id: str, entity) -> StanceResult:
    entity_id = int(entity["id"])
    items = db.execute(
        "SELECT * FROM evidence_items WHERE run_id=? AND entity_id=? AND duplicate_of IS NULL", (run_id, entity_id)
    ).fetchall()
    cov = coverage_for(db, run_id, entity_id)
    policy = [e for e in items if e["evidence_class"] in POLICY_CLASSES and e["about_entity"] and e["statement_type"] != "mention"
              and (e["extraction_confidence"] or 0) >= 0.5]
    practice = [e for e in items if e["evidence_class"] == "documented_practice" and e["about_entity"]]

    t_changed = _distinct(policy, {"changed_circumstance_relief"})
    t_relief = _distinct(policy, {"relief_permitted"})
    t_case = _distinct(policy, {"case_by_case"})
    t_reject = _distinct(policy, {"relief_rejected", "relief_narrow_exceptions"})
    t_archive = _distinct(policy, {"archive_principle"})
    t_update = _distinct(policy, {"update_remedy", "changed_circumstance_update"})
    supportive = t_changed + t_relief + t_case
    adverse = t_reject + t_archive

    def conf(base: float, n_support: int, conflict: bool = False) -> float:
        c = base + 0.04 * min(n_support - 1, 4)
        if conflict:
            c -= 0.12
        return round(max(0.3, min(c, 0.95)), 3)

    reasons: list[str] = []
    if t_changed:
        stance = "SUPPORTS_CHANGED_CIRCUMSTANCES"
        confidence = conf(0.78, len(t_changed), conflict=bool(t_reject))
        rationale = f"{len(t_changed)} distinct first-party statement(s) permit relief when outcomes/circumstances change"
        if t_reject:
            rationale += f"; {len(t_reject)} statement(s) state a general no-removal rule (treated as the default the exception modifies)"
    elif t_relief and t_reject:
        stance = "MIXED"
        confidence = conf(0.6, len(t_relief) + len(t_reject), conflict=True)
        rationale = f"{len(t_relief)} statement(s) permit relief and {len(t_reject)} reject it, with no changed-circumstance bridge"
        reasons.append("conflicting_policy_statements")
    elif t_relief:
        stance = "SUPPORTS_RELIEF"
        confidence = conf(0.78, len(t_relief))
        rationale = f"{len(t_relief)} distinct first-party statement(s) permit removal/de-indexing/anonymization"
    elif t_case:
        stance = "CASE_BY_CASE"
        confidence = conf(0.72, len(t_case), conflict=bool(t_reject))
        rationale = f"{len(t_case)} statement(s) describe discretionary/case-by-case review of requests"
        if t_reject:
            rationale += f"; {len(t_reject)} restrictive statement(s) also present"
    elif t_reject and t_update:
        stance = "UPDATE_ONLY"
        confidence = conf(0.75, len(t_reject) + len(t_update))
        rationale = f"Removal rejected ({len(t_reject)}) with updates/editor's notes offered as the remedy ({len(t_update)})"
    elif t_reject:
        stance = "STRICT_ARCHIVE"
        confidence = conf(0.78, len(t_reject))
        rationale = f"{len(t_reject)} statement(s) reject removal/unpublishing (narrow exceptions only)"
    elif t_update:
        stance = "UPDATE_ONLY"
        confidence = conf(0.62, len(t_update))
        rationale = f"{len(t_update)} statement(s) describe updates/corrections as the post-publication remedy; removal not addressed"
        reasons.append("update_only_without_explicit_removal_language")
    elif t_archive:
        stance = "STRICT_ARCHIVE"
        confidence = 0.55
        rationale = f"Only general archive-integrity language ({len(t_archive)}); no explicit removal rule"
        reasons.append("archive_principle_only")
    else:
        enough = (
            cov["first_party_pages_ok"] >= settings.no_guidance_min_first_party_pages
            and (cov["homepage_ok"] or cov["first_party_policy_pages_ok"] > 0)
            and cov["tier1_queries"] > 0 and cov["tier1_completed"] == cov["tier1_queries"]
            and (cov["first_party_inaccessible"] <= settings.no_guidance_max_inaccessible_ratio * max(1, cov["first_party_pages_attempted"]))
        )
        if enough:
            stance = "NO_RELEVANT_GUIDANCE"
            confidence = 0.7 if cov["first_party_policy_pages_ok"] else 0.6
            rationale = (f"Targeted searches completed and {cov['first_party_pages_ok']} first-party page(s) inspected "
                         f"({cov['first_party_policy_pages_ok']} policy/about) without qualifying post-publication guidance")
        else:
            stance = "UNDETERMINED"
            confidence = 0.0
            missing = []
            if cov["first_party_pages_ok"] < settings.no_guidance_min_first_party_pages:
                missing.append(f"only {cov['first_party_pages_ok']} first-party page(s) fetched")
            if cov["first_party_inaccessible"]:
                missing.append(f"{cov['first_party_inaccessible']} first-party page(s) inaccessible")
            if not cov["tier1_queries"]:
                missing.append("no targeted searches were run (no search provider configured)")
            elif cov["tier1_completed"] < cov["tier1_queries"]:
                missing.append("core searches incomplete")
            rationale = "Insufficient inspection for a no-guidance finding: " + ("; ".join(missing) or "coverage threshold not met")

    # ---- practice vs policy ------------------------------------------------------------
    pc = Counter(e["statement_type"] for e in practice)
    practice_summary = ", ".join(f"{k}={v}" for k, v in sorted(pc.items()))
    if stance in {"STRICT_ARCHIVE", "UPDATE_ONLY"} and pc.get("practice_relief_granted"):
        reasons.append("policy_practice_contradiction:restrictive_policy_but_relief_granted")
    if stance in {"SUPPORTS_RELIEF", "SUPPORTS_CHANGED_CIRCUMSTANCES"} and pc.get("practice_relief_denied"):
        reasons.append("policy_practice_contradiction:supportive_policy_but_relief_denied")

    tech = db.execute(
        "SELECT COUNT(*) n, SUM(noindex) ni FROM research_pages WHERE run_id=? AND entity_id=? AND access_class='ok'",
        (run_id, entity_id)).fetchone()
    technical_summary = f"pages_ok={tech['n'] or 0}, noindex_pages={tech['ni'] or 0}"

    all_sup = [e for e in items if e["direction"] == "supportive" and e["about_entity"]]
    all_adv = [e for e in items if e["direction"] == "adverse" and e["about_entity"]]
    best = lambda xs: max(xs, key=lambda e: ((e["relevance_score"] or 0) * (e["authority_score"] or 0), e["similarity_score"] or 0))["id"] if xs else None  # noqa: E731
    max_sim = max([float(e["similarity_score"] or 0) for e in items] or [0.0])

    if stance in DETERMINATE:
        reasons.append("determinate_finding_requires_verification")
    if stance in DETERMINATE and confidence < 0.7:
        reasons.append("low_confidence")
    if max_sim >= settings.case_profile.high_similarity_threshold:
        reasons.append("high_similarity_precedent")
    if any(e["statement_type"] in {"practice_relief_granted", "practice_relief_denied"} for e in practice):
        reasons.append("documented_practice")

    return StanceResult(
        stance=stance, confidence=confidence, rationale=rationale,
        supportive=len(supportive), adverse=len(adverse), evidence_count=len(items),
        strongest_supportive_id=best(all_sup), strongest_adverse_id=best(all_adv), max_similarity=max_sim,
        coverage=cov, review_reasons=sorted(set(reasons)), practice_summary=practice_summary,
        technical_summary=technical_summary,
    )


def store_stance(db: Database, run_id: str, entity_id: int, r: StanceResult) -> None:
    db.execute(
        """
        INSERT INTO entity_stances(run_id,entity_id,stance,confidence,rationale,evidence_count,supportive_count,adverse_count,
          max_similarity_score,requires_human_review,stance_version,strongest_supportive_id,strongest_adverse_id,coverage_json,
          review_reasons_json,practice_summary,technical_summary)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(run_id,entity_id) DO UPDATE SET stance=excluded.stance,confidence=excluded.confidence,rationale=excluded.rationale,
          evidence_count=excluded.evidence_count,supportive_count=excluded.supportive_count,adverse_count=excluded.adverse_count,
          max_similarity_score=excluded.max_similarity_score,requires_human_review=excluded.requires_human_review,
          stance_version=excluded.stance_version,strongest_supportive_id=excluded.strongest_supportive_id,
          strongest_adverse_id=excluded.strongest_adverse_id,coverage_json=excluded.coverage_json,
          review_reasons_json=excluded.review_reasons_json,practice_summary=excluded.practice_summary,
          technical_summary=excluded.technical_summary,created_at=CURRENT_TIMESTAMP
        """,
        (run_id, entity_id, r.stance, r.confidence, r.rationale, r.evidence_count, r.supportive, r.adverse, r.max_similarity,
         int(r.requires_review), STANCE_VERSION, r.strongest_supportive_id, r.strongest_adverse_id, json.dumps(r.coverage),
         json.dumps(r.review_reasons), r.practice_summary, r.technical_summary),
    )

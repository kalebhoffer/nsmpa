"""Cohort quality metrics and validation gates.

Reports may print cohort percentages only when every gate for that cohort passes. Otherwise
the cohort is labelled PRELIMINARY and only counts are shown, with the failing gates listed.
Each cohort has its own denominator; cohorts are never pooled.

By default the latest v0.3 stance per entity (across runs) is used; ``run_id`` restricts to one run.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import Settings
from .db import Database
from .stance import DETERMINATE

COHORT_LABELS = {
    "student_media": "Student journalism",
    "professional_newsroom": "Professional journalism",
    "support_org": "Support, standards, legal & advisory organizations",
    "press_association": "Press associations",
    "journalism_school": "Journalism schools",
    "other": "Other",
}


def latest_stances_sql(run_id: str | None) -> tuple[str, list]:
    """SQL (as a subquery) selecting one current stance row per entity."""
    if run_id:
        return ("SELECT s.* FROM entity_stances s WHERE s.run_id=? AND s.stance_version='0.3'", [run_id])
    return ("""SELECT s.* FROM entity_stances s
               JOIN (SELECT entity_id, MAX(id) mid FROM entity_stances
                     WHERE stance_version='0.3' AND run_id NOT IN (SELECT id FROM research_runs WHERE status='excluded')
                     GROUP BY entity_id) m
                 ON m.mid=s.id""", [])


@dataclass
class Gate:
    name: str
    value: float | None
    threshold: float
    comparator: str  # ">=" or "<="
    passed: bool
    note: str = ""


@dataclass
class CohortMetrics:
    cohort: str
    label: str
    denominator: int
    denominator_note: str
    researched: int = 0
    determinate: int = 0
    stance_counts: dict[str, int] = field(default_factory=dict)
    metrics: dict[str, float | int | None] = field(default_factory=dict)
    gates: list[Gate] = field(default_factory=list)

    @property
    def valid_for_percentages(self) -> bool:
        return bool(self.gates) and all(g.passed for g in self.gates)

    @property
    def failing(self) -> list[Gate]:
        return [g for g in self.gates if not g.passed]


def _ratio(n: float, d: float) -> float | None:
    return None if not d else round(n / d, 4)


def _gate(name: str, value: float | None, threshold: float, comparator: str, note: str = "") -> Gate:
    if value is None:
        return Gate(name, None, threshold, comparator, False, note or "no data")
    ok = value >= threshold if comparator == ">=" else value <= threshold
    return Gate(name, value, threshold, comparator, ok, note)


def student_discovery_metrics(db: Database) -> dict:
    total = db.scalar("SELECT COUNT(*) FROM institutions WHERE included=1")
    core = db.scalar("SELECT COUNT(*) FROM v_institution_peer WHERE included=1 AND core_stratum=1", default=None)
    attempted = db.scalar(
        "SELECT COUNT(DISTINCT item_key) FROM run_items ri JOIN research_runs rr ON rr.id=ri.run_id "
        "WHERE ri.item_type='institution' AND ri.status='done' AND rr.status!='excluded'")
    identified = db.scalar(
        "SELECT COUNT(DISTINCT p.unitid) FROM publications p JOIN institutions i ON i.unitid=p.unitid "
        "WHERE i.included=1 AND p.is_primary=1 AND p.verification_status NOT LIKE 'human_rej%'")
    verified = db.scalar(
        "SELECT COUNT(DISTINCT p.unitid) FROM publications p JOIN institutions i ON i.unitid=p.unitid "
        "WHERE i.included=1 AND p.verification_status IN ('human_verified','manual')")
    ambiguous = db.scalar("SELECT COUNT(*) FROM publications WHERE ambiguous=1 AND is_primary=1")
    no_site = db.scalar("SELECT COUNT(*) FROM institutions WHERE included=1 AND (website IS NULL OR website='')")
    core_identified = db.scalar(
        "SELECT COUNT(DISTINCT p.unitid) FROM publications p JOIN v_institution_peer v ON v.unitid=p.unitid "
        "WHERE v.included=1 AND v.core_stratum=1 AND p.is_primary=1", default=None)
    return {
        "institutions_in_denominator": total, "core_stratum_institutions": core,
        "discovery_attempted": attempted, "publications_identified": identified,
        "publications_human_verified": verified, "ambiguous_identifications": ambiguous,
        "institutions_without_website": no_site,
        "discovery_success_rate": _ratio(identified, total),
        "discovery_success_rate_core": _ratio(core_identified or 0, core or 0),
        "unresolved_rate_of_attempted": _ratio(max(0, attempted - identified), attempted) if attempted else None,
        "publication_verification_rate": _ratio(verified, identified),
    }


def cohort_metrics(db: Database, settings: Settings, cohort: str, run_id: str | None = None) -> CohortMetrics:
    th = settings.validation
    if cohort == "student_media":
        disc = student_discovery_metrics(db)
        denominator = int(disc["institutions_in_denominator"] or 0)
        note = ("IPEDS included institutions (ICLEVEL=1 four-year+, configured control filters). "
                "A core stratum excluding special-focus and very small institutions is reported alongside.")
    else:
        disc = {}
        denominator = int(db.scalar("SELECT COUNT(*) FROM research_entities WHERE cohort=? AND active=1 AND merged_into IS NULL", (cohort,)))
        sources = db.execute(
            "SELECT es.source, COUNT(DISTINCT es.entity_id) n FROM entity_sources es JOIN research_entities re ON re.id=es.entity_id "
            "WHERE re.cohort=? AND re.active=1 GROUP BY es.source ORDER BY n DESC", (cohort,)).fetchall()
        note = "Union of sourced lists, deduplicated by domain: " + ", ".join(f"{r['source']} ({r['n']})" for r in sources)
        note += ". This is a sourced panel, not a census, unless the sources are demonstrably exhaustive."
    sub, params = latest_stances_sql(run_id)
    # Student percentages are per IPEDS institution: count primary publications only. Secondary and unmatched student
    # papers are researched and reported separately ("additional_student_publications_researched").
    coverage_filter = ("AND COALESCE(json_extract(re.metadata_json,'$.excluded_from_ipeds_coverage'), 0) IN (0, 'false')"
                       if cohort == "student_media" else "")
    rows = db.execute(
        f"SELECT s.* FROM ({sub}) s JOIN research_entities re ON re.id=s.entity_id WHERE re.cohort=? AND re.active=1 {coverage_filter}",
        params + [cohort]).fetchall()
    extra_student = db.scalar(
        f"SELECT COUNT(*) FROM ({sub}) s JOIN research_entities re ON re.id=s.entity_id WHERE re.cohort='student_media' AND re.active=1 "
        f"AND json_extract(re.metadata_json,'$.excluded_from_ipeds_coverage') IN (1, 'true')", params) if cohort == "student_media" else None
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["stance"]] = counts.get(r["stance"], 0) + 1
    researched = len(rows)
    determinate = sum(n for s, n in counts.items() if s in DETERMINATE)
    undetermined = counts.get("UNDETERMINED", 0)
    reviewed = sum(1 for r in rows if r["stance"] in DETERMINATE and (r["review_status"] or "unreviewed") != "unreviewed")
    mean_conf = (sum(r["confidence"] for r in rows if r["stance"] in DETERMINATE) / determinate) if determinate else None
    excluded = "(SELECT id FROM research_runs WHERE status='excluded')"
    run_filter = "AND e.run_id=?" if run_id else f"AND e.run_id NOT IN {excluded}"
    rp = [run_id] if run_id else []
    ev_total = db.scalar(f"SELECT COUNT(*) FROM evidence_items e WHERE e.cohort=? {run_filter}", [cohort] + rp)
    ev_dup = db.scalar(f"SELECT COUNT(*) FROM evidence_items e WHERE e.cohort=? AND e.duplicate_of IS NOT NULL {run_filter}", [cohort] + rp)
    ev_sub = db.scalar(f"SELECT COUNT(*) FROM evidence_items e WHERE e.cohort=? AND e.statement_type!='mention' {run_filter}", [cohort] + rp)
    ev_ver = db.scalar(f"SELECT COUNT(*) FROM evidence_items e WHERE e.cohort=? AND e.statement_type!='mention' "
                       f"AND e.verification_status='verified' {run_filter}", [cohort] + rp)
    pg = db.execute(
        f"SELECT COUNT(*) n, SUM(p.access_class='ok') ok FROM research_pages p JOIN research_entities re ON re.id=p.entity_id "
        f"WHERE re.cohort=? AND p.first_party=1 {'AND p.run_id=?' if run_id else f'AND p.run_id NOT IN {excluded}'}",
        [cohort] + rp).fetchone()
    crawl_success = _ratio(pg["ok"] or 0, pg["n"] or 0)
    m = CohortMetrics(cohort, COHORT_LABELS.get(cohort, cohort), denominator, note, researched, determinate, counts)
    m.metrics = {
        **disc,
        "entities_in_universe": denominator,
        **({"additional_student_publications_researched": extra_student} if cohort == "student_media" else {}),
        "entities_researched": researched,
        "research_coverage": _ratio(researched, denominator),
        "policy_discovery_rate": _ratio(determinate, researched),
        "undetermined_rate": _ratio(undetermined, researched),
        "human_review_rate_of_findings": _ratio(reviewed, determinate),
        "mean_stance_confidence": round(mean_conf, 3) if mean_conf is not None else None,
        "first_party_fetch_success": crawl_success,
        "evidence_items": ev_total, "substantive_evidence_items": ev_sub,
        "duplicate_evidence_rate": _ratio(ev_dup, ev_total),
        "source_verification_rate": _ratio(ev_ver, ev_sub),
    }
    gates = [
        _gate("minimum researched entities", float(researched), float(th.min_entities_for_percentages), ">="),
        _gate("research coverage of universe", m.metrics["research_coverage"], th.min_discovery_coverage, ">="),
        _gate("first-party fetch success", crawl_success, th.min_crawl_success, ">="),
        _gate("undetermined rate", m.metrics["undetermined_rate"], th.max_undetermined_rate, "<="),
        _gate("human review of findings", m.metrics["human_review_rate_of_findings"], th.min_human_review_of_findings, ">="),
        _gate("source verification of evidence", m.metrics["source_verification_rate"], th.min_source_verification, ">="),
        _gate("mean stance confidence", m.metrics["mean_stance_confidence"], th.min_mean_stance_confidence, ">="),
        _gate("duplicate evidence rate", m.metrics["duplicate_evidence_rate"], th.max_duplicate_evidence_rate, "<="),
    ]
    if cohort == "student_media":
        gates.insert(0, _gate("publication discovery coverage", disc.get("discovery_success_rate"), th.min_discovery_coverage, ">="))
    m.gates = gates
    return m


def all_cohorts(db: Database) -> list[str]:
    present = [r[0] for r in db.execute("SELECT DISTINCT cohort FROM research_entities WHERE active=1 ORDER BY cohort")]
    order = ["student_media", "professional_newsroom", "support_org", "press_association", "journalism_school", "other"]
    if "student_media" not in present and db.scalar("SELECT COUNT(*) FROM institutions WHERE included=1"):
        present.append("student_media")
    return [c for c in order if c in present]

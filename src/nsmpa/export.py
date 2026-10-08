from __future__ import annotations

import csv
import json

from pathlib import Path

from .db import Database


def _write_query_csv(db: Database, path: Path, sql: str, params: tuple = (), *, json_path: Path | None = None) -> int:
    """Stream a query to CSV (and optionally JSON Lines) without loading the result set into memory."""
    cur = db.execute(sql, params)
    headers = [d[0] for d in cur.description or []]
    n = 0
    jf = open(json_path, "w", encoding="utf-8") if json_path else None
    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            while True:
                batch = cur.fetchmany(2000)
                if not batch:
                    break
                for row in batch:
                    vals = [row[h] for h in headers]
                    writer.writerow(vals)
                    if jf:
                        jf.write(json.dumps(dict(zip(headers, vals)), ensure_ascii=False, default=str) + "\n")
                    n += 1
    finally:
        if jf:
            jf.close()
    return n


def export_run(db: Database, run_id: str, out_dir: str | Path) -> dict[str, int]:
    out = Path(out_dir) / run_id
    out.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}

    counts["institutions"] = _write_query_csv(
        db, out / "institutions.csv",
        "SELECT unitid,name,city,state,website,control,level,source_year,included FROM institutions ORDER BY name",
    )
    counts["publications"] = _write_query_csv(
        db, out / "publications.csv",
        """
        SELECT p.id,p.unitid,i.name AS institution,p.name AS publication,p.homepage_url,p.domain,
               p.confidence,p.verification_status,p.is_primary
        FROM publications p JOIN institutions i ON i.unitid=p.unitid ORDER BY i.name,p.id
        """,
    )
    counts["classifications"] = _write_query_csv(
        db, out / "classifications.csv",
        """
        SELECT c.publication_id,i.unitid,i.name AS institution,p.name AS publication,p.homepage_url,
               c.primary_class,c.confidence,c.rationale,c.inspection_complete,c.pages_attempted,
               c.pages_fetched,c.policy_pages_found,c.blockers_json,c.requires_human_review
        FROM classifications c JOIN publications p ON p.id=c.publication_id
        JOIN institutions i ON i.unitid=p.unitid WHERE c.run_id=? ORDER BY c.primary_class,i.name
        """, (run_id,),
    )
    counts["evidence"] = _write_query_csv(
        db, out / "evidence.csv",
        """
        SELECT e.publication_id,i.name AS institution,p.name AS publication,e.tag,e.evidence_type,
               e.supports_relief,e.confidence,e.excerpt,e.source_url,e.created_at
        FROM evidence e JOIN publications p ON p.id=e.publication_id
        JOIN institutions i ON i.unitid=p.unitid WHERE e.run_id=?
        ORDER BY i.name,e.evidence_type,e.tag,e.id
        """, (run_id,),
    )
    counts["noindex"] = _write_query_csv(
        db, out / "noindex_observations.csv",
        """
        SELECT pg.publication_id,i.name AS institution,p.name AS publication,pg.final_url,pg.title,
               pg.meta_robots,pg.x_robots_tag,pg.content_sha256,pg.fetched_at
        FROM pages pg JOIN publications p ON p.id=pg.publication_id
        JOIN institutions i ON i.unitid=p.unitid
        WHERE pg.run_id=? AND pg.noindex=1 ORDER BY i.name,pg.final_url
        """, (run_id,),
    )
    counts["unresolved"] = _write_query_csv(
        db, out / "human_review_queue.csv",
        """
        SELECT c.publication_id,i.name AS institution,p.name AS publication,p.homepage_url,
               c.primary_class,c.confidence,c.rationale,c.blockers_json
        FROM classifications c JOIN publications p ON p.id=c.publication_id
        JOIN institutions i ON i.unitid=p.unitid
        WHERE c.run_id=? AND c.requires_human_review=1
        ORDER BY CASE WHEN c.primary_class='U_UNDETERMINED' THEN 0 ELSE 1 END,c.confidence,i.name
        """, (run_id,),
    )

    class_rows = db.execute(
        "SELECT primary_class,COUNT(*) AS n FROM classifications WHERE run_id=? GROUP BY primary_class",
        (run_id,),
    ).fetchall()
    class_counts = {r["primary_class"]: r["n"] for r in class_rows}
    pub_count = db.execute("SELECT COUNT(*) AS n FROM publications").fetchone()["n"]
    classified = sum(class_counts.values())
    determinate = classified - class_counts.get("U_UNDETERMINED", 0)
    written_policy = sum(class_counts.get(k, 0) for k in [
        "A_EXPLICIT_RELIEF", "B_CHANGED_CIRCUMSTANCES", "C_CASE_BY_CASE", "D_UPDATE_ONLY", "E_STRICT_ARCHIVE"
    ])
    supportive = sum(class_counts.get(k, 0) for k in ["A_EXPLICIT_RELIEF", "B_CHANGED_CIRCUMSTANCES", "C_CASE_BY_CASE"])

    def pct(n: int, d: int) -> str:
        return "n/a" if not d else f"{100*n/d:.1f}%"

    summary = [
        "# NSMPA run summary",
        "",
        f"Run ID: `{run_id}`",
        "",
        f"- Publications in database: {pub_count}",
        f"- Publications classified in this run: {classified}",
        f"- Determinate classifications: {determinate}",
        f"- Publications with identified written-policy evidence: {written_policy}",
        f"- A/B/C classifications among determinate cases: {supportive} ({pct(supportive, determinate)})",
        "",
        "## Class counts",
        "",
    ]
    for key in sorted(class_counts):
        summary.append(f"- {key}: {class_counts[key]}")
    summary.extend([
        "",
        "## Interpretation guardrails",
        "",
        "Do not publish national percentages until discovery coverage and human-review rates are acceptable.",
        "`F_NO_PUBLIC_POLICY_FOUND` is only assigned when the configured inspection threshold is met.",
        "`U_UNDETERMINED` is excluded from determinate denominators rather than silently counted as no-policy.",
        "All externally cited examples should be verified against the captured snapshot and live source.",
        "",
    ])
    (out / "summary.md").write_text("\n".join(summary), encoding="utf-8")
    (out / "run_manifest.json").write_text(
        json.dumps({"run_id": run_id, "counts": counts, "class_counts": class_counts}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return counts


# --------------------------------------------------------------------------- v0.3 research exports

RESEARCH_EXPORTS: dict[str, str] = {
    "entities": """
        SELECT re.id AS entity_id, re.cohort, re.name, re.parent_name, re.homepage_url, re.domain, re.state, re.source,
               re.verification_status, re.metadata_json,
               (SELECT group_concat(es.source, ';') FROM entity_sources es WHERE es.entity_id=re.id) AS provenance_sources
        FROM research_entities re
        WHERE re.id IN (SELECT CAST(item_key AS INTEGER) FROM run_items WHERE run_id=:run AND item_type='entity')
        ORDER BY re.cohort, re.name""",
    "search_queries": """
        SELECT q.id AS query_id, q.created_at, q.run_id, q.entity_id, re.name AS entity_name, q.unitid, q.provider, q.purpose,
               q.query, q.query_hash AS cache_key, q.status, q.was_cached, q.refresh, q.credits_estimated, q.result_count,
               q.produced_evidence, q.latency_ms, q.error,
               (SELECT group_concat(sr.url, ' ') FROM search_results sr WHERE sr.query_id=q.id) AS result_urls
        FROM search_queries q LEFT JOIN research_entities re ON re.id=q.entity_id
        WHERE q.run_id=:run ORDER BY q.id""",
    "research_targets": """
        SELECT t.*, re.name AS entity_name FROM research_targets t JOIN research_entities re ON re.id=t.entity_id
        WHERE t.run_id=:run ORDER BY t.entity_id, t.score DESC""",
    "research_pages": """
        SELECT p.id AS page_id, p.entity_id, re.name AS entity_name, p.requested_url, p.final_url, p.canonical_url,
               p.http_status, p.access_class, p.page_kind, p.first_party, p.title, p.content_type, p.text_length,
               p.content_sha256, p.text_sha256, p.snapshot_path, p.meta_robots, p.x_robots_tag, p.noindex,
               p.redirect_chain_json, p.error, p.fetched_at, COALESCE(p.acquisition,'live') AS acquisition, p.archive_ts,
               p.archive_url
        FROM research_pages p JOIN research_entities re ON re.id=p.entity_id WHERE p.run_id=:run ORDER BY p.entity_id, p.id""",
    "evidence": """
        SELECT e.id AS evidence_id, e.entity_id, re.name AS entity_name, re.parent_name, e.cohort, e.evidence_class,
               e.statement_type, e.direction, e.first_party, e.about_entity,
               (SELECT group_concat(tag, ';') FROM evidence_item_tags t WHERE t.evidence_id=e.id) AS tags,
               e.topic, e.authority_score, e.relevance_score, e.similarity_score, e.similarity_factors_json,
               e.extraction_confidence, e.verification_status, e.duplicate_of, e.near_dup_key,
               e.excerpt, e.source_url, e.source_title, e.source_domain, e.fetched_at, e.page_sha256, e.text_sha256,
               COALESCE(e.acquisition,'live') AS acquisition, e.archive_ts, e.archive_url,
               e.query_id, q.query AS search_query, e.rationale AS classifier_cues, e.run_id
        FROM evidence_items e JOIN research_entities re ON re.id=e.entity_id
        LEFT JOIN search_queries q ON q.id=e.query_id
        WHERE e.run_id=:run ORDER BY e.similarity_score DESC, e.id""",
    "entity_stances": """
        SELECT s.entity_id, re.cohort, re.name, re.parent_name, re.homepage_url, s.stance, s.confidence, s.rationale,
               s.evidence_count, s.supportive_count, s.adverse_count, s.max_similarity_score,
               sup.excerpt AS strongest_supportive_excerpt, sup.source_url AS strongest_supportive_url,
               adv.excerpt AS strongest_adverse_excerpt, adv.source_url AS strongest_adverse_url,
               s.practice_summary, s.technical_summary, s.coverage_json, s.requires_human_review, s.review_reasons_json,
               s.review_status
        FROM entity_stances s JOIN research_entities re ON re.id=s.entity_id
        LEFT JOIN evidence_items sup ON sup.id=s.strongest_supportive_id
        LEFT JOIN evidence_items adv ON adv.id=s.strongest_adverse_id
        WHERE s.run_id=:run ORDER BY re.cohort, s.stance, re.name""",
    "human_review_queue": """
        SELECT q.id AS review_id, q.priority, q.status, q.cohort, re.name AS entity_name, s.stance, s.confidence,
               q.reasons_json, s.rationale, re.homepage_url
        FROM review_queue q LEFT JOIN research_entities re ON re.id=q.entity_id
        LEFT JOIN entity_stances s ON q.item_type='entity_stance' AND s.id=q.item_id
        WHERE q.run_id=:run ORDER BY q.priority DESC""",
}


def export_research(db: Database, run_id: str, out_dir: str | Path) -> dict[str, int]:
    out = Path(out_dir) / f"research_{run_id}"
    out.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for name, sql in RESEARCH_EXPORTS.items():
        counts[name] = _write_query_csv(db, out / f"{name}.csv", sql.replace(":run", "?"), (run_id,) * sql.count(":run"),
                                        json_path=out / f"{name}.jsonl")
    run = db.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()
    stance_rows = db.execute(
        "SELECT re.cohort, s.stance, COUNT(*) n FROM entity_stances s JOIN research_entities re ON re.id=s.entity_id "
        "WHERE s.run_id=? GROUP BY re.cohort, s.stance ORDER BY re.cohort, s.stance", (run_id,)).fetchall()
    by_cohort: dict[str, dict[str, int]] = {}
    for r in stance_rows:
        by_cohort.setdefault(r["cohort"], {})[r["stance"]] = r["n"]
    unique_ev = db.scalar("SELECT COUNT(DISTINCT near_dup_key) FROM evidence_items WHERE run_id=? AND statement_type!='mention'", (run_id,))
    tag_links = db.scalar("SELECT COUNT(*) FROM evidence_item_tags t JOIN evidence_items e ON e.id=t.evidence_id WHERE e.run_id=?", (run_id,))
    manifest = {
        "run_id": run_id, "engine_version": run["engine_version"] if run else None, "status": run["status"] if run else None,
        "status_reason": run["status_reason"] if run else None, "started_at": run["started_at"] if run else None,
        "completed_at": run["completed_at"] if run else None,
        "searches_live": run["searches_live"] if run else None, "searches_cached": run["searches_cached"] if run else None,
        "credits_estimated": run["credits_estimated"] if run else None,
        "params": json.loads(run["params_json"] or "{}") if run and run["params_json"] else {},
        "counts": counts, "stances_by_cohort": by_cohort,
        "unique_substantive_excerpts": unique_ev, "excerpt_tag_links": tag_links,
    }
    (out / "run_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str), encoding="utf-8")
    lines = [f"# NSMPA research run `{run_id}`", "",
             f"Status: **{manifest['status']}**" + (f" ({manifest['status_reason']})" if manifest["status_reason"] else ""),
             f"Searches: {manifest['searches_live']} live, {manifest['searches_cached']} cached, ~{manifest['credits_estimated']} credits", "",
             f"Unique substantive excerpts: {unique_ev} (linked to {tag_links} tag assignments; tags are not independent evidence)", "",
             "## Stances by cohort (separate denominators — never pool across cohorts)", ""]
    for cohort, stances in by_cohort.items():
        total = sum(stances.values())
        lines += [f"### {cohort} (n={total} researched entities)", ""]
        lines += [f"- {k}: {v}" for k, v in sorted(stances.items())] + [""]
    lines += ["## Guardrails", "",
              "- Counts above are from this run only and are not national estimates; use `nsmpa report` for validated figures.",
              "- `UNDETERMINED` means the entity could not be adequately inspected; it is never a negative finding.",
              "- Every excerpt in `evidence.csv` links to its source URL, page SHA-256 and the query that found it.", ""]
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    return counts

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

from .db import Database


def _write_query_csv(db: Database, path: Path, sql: str, params: tuple = ()) -> int:
    cur = db.execute(sql, params)
    rows = cur.fetchall()
    headers = [d[0] for d in cur.description or []]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for row in rows:
            writer.writerow([row[h] for h in headers])
    return len(rows)


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

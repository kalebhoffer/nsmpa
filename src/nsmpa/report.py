"""National report: Markdown + JSON + CSV tables, gated by validation metrics.

Every figure is computed from database rows that carry source URLs, hashes and query ids, so
each aggregate can be traced back to its evidence. Cohorts are reported separately.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import Settings
from .db import Database
from .stance import DETERMINATE
from .validate import CohortMetrics, all_cohorts, cohort_metrics, latest_stances_sql

STANCE_ORDER = ["SUPPORTS_RELIEF", "SUPPORTS_CHANGED_CIRCUMSTANCES", "CASE_BY_CASE", "UPDATE_ONLY", "STRICT_ARCHIVE",
                "MIXED", "NO_RELEVANT_GUIDANCE", "UNDETERMINED"]

OBE_REGIONS = {0: "US service schools", 1: "New England", 2: "Mid East", 3: "Great Lakes", 4: "Plains", 5: "Southeast",
               6: "Southwest", 7: "Rocky Mountains", 8: "Far West", 9: "Outlying areas"}
SIZE_LABELS = {1: "<1,000", 2: "1,000-4,999", 3: "5,000-9,999", 4: "10,000-19,999", 5: "20,000+"}

PRECEDENT_TABLES: dict[str, tuple[str, str]] = {
    "strongest_supportive": ("Strongest supportive evidence", "e.direction='supportive' AND e.statement_type!='mention'"),
    "strongest_adverse": ("Strongest adverse evidence", "e.direction='adverse' AND e.statement_type!='mention'"),
    "changed_outcome_precedents": ("Changed criminal-outcome evidence",
        "e.statement_type IN ('changed_circumstance_relief','changed_circumstance_update','practice_relief_granted','practice_relief_denied','practice_update') "
        "AND EXISTS (SELECT 1 FROM evidence_item_tags t WHERE t.evidence_id=e.id AND t.tag IN "
        "('dismissed_charges','acquitted','exonerated','vacated','expunged_sealed','plea_withdrawn'))"),
    "deindexing_precedents": ("De-indexing / noindex evidence",
        "e.statement_type!='mention' AND EXISTS (SELECT 1 FROM evidence_item_tags t WHERE t.evidence_id=e.id AND t.tag IN ('deindex','noindex'))"),
    "anonymization_precedents": ("Anonymization / name-removal evidence",
        "e.statement_type!='mention' AND EXISTS (SELECT 1 FROM evidence_item_tags t WHERE t.evidence_id=e.id AND t.tag='anonymize')"),
    "archive_integrity_arguments": ("Archive-integrity arguments", "e.statement_type IN ('archive_principle','relief_rejected','relief_narrow_exceptions')"),
    "documented_practice": ("Documented practice (what newsrooms actually did)", "e.statement_type LIKE 'practice_%'"),
    "high_similarity": ("High fact-pattern similarity evidence", "e.similarity_score >= :hs AND e.statement_type!='mention'"),
}


def _pct(n: int, d: int) -> str:
    return "n/a" if not d else f"{100.0 * n / d:.1f}%"


def _fmt(v) -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{v:.3f}" if v < 1.0 or v == 0 else f"{v:,.2f}"
    return f"{v:,}" if isinstance(v, int) else str(v)


def _evidence_sql(where: str, latest_only: bool) -> str:
    run_clause = ("AND e.run_id=(SELECT s2.run_id FROM entity_stances s2 WHERE s2.entity_id=e.entity_id "
                  "AND s2.stance_version='0.3' ORDER BY s2.id DESC LIMIT 1)") if latest_only else "AND e.run_id=:run"
    return f"""
      SELECT e.id AS evidence_id, e.cohort, re.name AS entity, re.parent_name, e.evidence_class, e.statement_type,
             e.direction, e.similarity_score, e.authority_score, e.verification_status, e.excerpt, e.source_url,
             e.page_sha256, e.fetched_at, e.run_id
      FROM evidence_items e JOIN research_entities re ON re.id=e.entity_id
      WHERE e.duplicate_of IS NULL AND {where} {run_clause}
      ORDER BY (e.relevance_score * e.authority_score) DESC, e.similarity_score DESC, e.id
      LIMIT 50"""


def peer_group_rows(db: Database, settings: Settings, run_id: str | None) -> list[dict]:
    sub, params = latest_stances_sql(run_id)
    out = []
    groups: list[tuple[str, str, str, list]] = []
    for key, g in settings.peer_groups.items():
        conds, p = ["v.included=1"], []
        for col, vals in (("v.state", g.states), ("v.county", g.counties), ("v.carnegie_basic", g.carnegie_basic),
                          ("v.size_category", g.size_categories), ("v.obe_region", g.obe_regions)):
            if vals:
                conds.append(f"{col} IN ({','.join('?' * len(vals))})")
                p += list(vals)
        if g.control:
            conds.append(f"i.control IN ({','.join('?' * len(g.control))})")
            p += list(g.control)
        where = " AND ".join(conds)
        if g.unitids:
            where = f"({where}) OR v.unitid IN ({','.join('?' * len(g.unitids))})"
            p += list(g.unitids)
        groups.append((key, g.label, where, p))
    for label_col, labels, name in (("v.control_label", None, "control"), ("v.obe_region", OBE_REGIONS, "region"),
                                    ("v.size_category", SIZE_LABELS, "enrollment size"),
                                    ("v.research_university", {1: "R1/R2 research university", 0: "not R1/R2"}, "research university"),
                                    ("v.core_stratum", {1: "core stratum (Carnegie 15-23)", 0: "outside core stratum"}, "stratum")):
        vals = [r[0] for r in db.execute(f"SELECT DISTINCT {label_col} FROM v_institution_peer v WHERE v.included=1 ORDER BY 1")]
        for v in vals:
            lbl = labels.get(v, str(v)) if labels else str(v)
            groups.append((f"{name}:{v}", f"{name}: {lbl}", f"v.included=1 AND {label_col} IS ?", [v]))
    attr_keys = [r[0] for r in db.execute("SELECT DISTINCT key FROM institution_attributes ORDER BY key")]
    for k in attr_keys:
        groups.append((f"attr:{k}", f"attribute: {k}", "v.included=1 AND v.unitid IN (SELECT unitid FROM institution_attributes WHERE key=? AND value IN ('1','true','yes','Y'))", [k]))
    for key, label, where, p in groups:
        inst = db.scalar(f"SELECT COUNT(*) FROM v_institution_peer v JOIN institutions i ON i.unitid=v.unitid WHERE {where}", p)
        if not inst:
            continue
        pubs = db.scalar(f"SELECT COUNT(DISTINCT pu.unitid) FROM publications pu JOIN v_institution_peer v ON v.unitid=pu.unitid "
                         f"JOIN institutions i ON i.unitid=v.unitid WHERE pu.is_primary=1 AND ({where})", p)
        stance_rows = db.execute(
            f"""SELECT s.stance, COUNT(*) n FROM ({sub}) s JOIN research_entities re ON re.id=s.entity_id
                JOIN v_institution_peer v ON v.unitid=json_extract(re.metadata_json,'$.unitid')
                JOIN institutions i ON i.unitid=v.unitid
                WHERE re.cohort='student_media' AND ({where}) GROUP BY s.stance""", params + p).fetchall()
        sc = {r["stance"]: r["n"] for r in stance_rows}
        out.append({"group": key, "label": label, "institutions": inst, "publications_identified": pubs,
                    "researched": sum(sc.values()), "determinate": sum(n for s, n in sc.items() if s in DETERMINATE), "stances": sc})
    return out


def build_report(db: Database, settings: Settings, out_dir: Path, run_id: str | None = None) -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(out_dir) / f"report_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    cohorts = all_cohorts(db)
    metrics: list[CohortMetrics] = [cohort_metrics(db, settings, c, run_id) for c in cohorts]
    peers = peer_group_rows(db, settings, run_id)

    tables: dict[str, list[dict]] = {}
    for key, (_, where) in PRECEDENT_TABLES.items():
        sql = _evidence_sql(where, latest_only=run_id is None)
        params: dict = {"hs": settings.case_profile.high_similarity_threshold}
        if run_id:
            params["run"] = run_id
        rows = db.execute(sql, params).fetchall()
        tables[key] = [dict(r) for r in rows]
        with open(out / f"{key}.csv", "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            cols = list(tables[key][0].keys()) if tables[key] else ["evidence_id"]
            w.writerow(cols)
            for r in tables[key]:
                w.writerow([r[c] for c in cols])
    sub, sp = latest_stances_sql(run_id)
    contradictions = [dict(r) for r in db.execute(
        f"""SELECT re.cohort, re.name, s.stance, s.practice_summary, s.review_reasons_json, s.run_id
            FROM ({sub}) s JOIN research_entities re ON re.id=s.entity_id
            WHERE s.review_reasons_json LIKE '%policy_practice_contradiction%' ORDER BY re.cohort, re.name""", sp)]
    tables["policy_practice_contradictions"] = contradictions

    runs = [dict(r) for r in db.execute(
        "SELECT id, mode, status, status_reason, engine_version, started_at, completed_at, searches_live, searches_cached, credits_estimated "
        "FROM research_runs WHERE engine_version!='0.2' ORDER BY started_at")]

    # ------------------------------------------------------------------ markdown
    L: list[str] = [
        "# NSMPA national journalism post-publication policy report", "",
        f"Generated {stamp} by NSMPA {__version__}. Scope: {'run ' + run_id if run_id else 'latest v0.3 stance per entity across all runs'}.", "",
        "> **Read this first.** Cohorts are reported separately and their percentages must never be combined. "
        "A cohort's percentages are shown only when every validation gate passes; otherwise the cohort is marked "
        "**PRELIMINARY** and only counts are given. `UNDETERMINED` means *not adequately inspected* and is never a negative "
        "finding. Search-result snippets are discovery aids only; all findings come from fetched source text.", "",
    ]
    for m in metrics:
        status = "VALIDATED" if m.valid_for_percentages else "PRELIMINARY — not valid for national claims"
        L += [f"## {m.label}", "", f"**Status: {status}**", "", f"Denominator: **{m.denominator:,}**. {m.denominator_note}", ""]
        L += ["| Metric | Value |", "|---|---|"]
        for k, v in m.metrics.items():
            L.append(f"| {k.replace('_', ' ')} | {_fmt(v)} |")
        L += ["", "### Validation gates", "", "| Gate | Value | Threshold | Result |", "|---|---|---|---|"]
        for g in m.gates:
            L.append(f"| {g.name} | {_fmt(g.value)} | {g.comparator} {g.threshold} | {'pass' if g.passed else '**FAIL**'} |")
        L += ["", f"### Stance distribution (researched entities: {m.researched:,})", "",
              "| Stance | Count | Share of researched |", "|---|---|---|"]
        for s in STANCE_ORDER:
            n = m.stance_counts.get(s, 0)
            share = _pct(n, m.researched) if m.valid_for_percentages else "withheld (gates failing)"
            L.append(f"| {s} | {n:,} | {share} |")
        L.append("")

    if peers:
        L += ["## Student-media peer groups", "",
              "Peer groups are defined in `config.yml` (`peer_groups`) and IPEDS fields; percentages are withheld here — "
              "use counts with the cohort's validation status above.", "",
              "| Group | Institutions | Publications identified | Researched | Determinate | Stances |", "|---|---|---|---|---|---|"]
        for p in peers:
            st = ", ".join(f"{k}={v}" for k, v in sorted(p["stances"].items())) or "–"
            L.append(f"| {p['label']} | {p['institutions']:,} | {p['publications_identified']:,} | {p['researched']:,} | {p['determinate']:,} | {st} |")
        L.append("")

    for key, (title, _) in PRECEDENT_TABLES.items():
        rows = tables[key][:12]
        L += [f"## {title}", "", f"Top {len(rows)} of {len(tables[key])} (full table: `{key}.csv`). Verification status shown; "
              "unverified items must be checked against the live source and snapshot before citation.", ""]
        if not rows:
            L += ["_None found in scope._", ""]
            continue
        L += ["| Cohort | Entity | Type | Sim. | Verified | Excerpt | Source |", "|---|---|---|---|---|---|---|"]
        for r in rows:
            ex = (r["excerpt"] or "").replace("|", "\\|")[:260]
            L.append(f"| {r['cohort']} | {r['entity']} | {r['statement_type']} | {r['similarity_score']:.0f} | "
                     f"{r['verification_status']} | {ex} | {r['source_url']} |")
        L.append("")
    L += ["## Policy vs. practice contradictions", ""]
    if contradictions:
        L += ["| Cohort | Entity | Policy stance | Practice | Run |", "|---|---|---|---|---|"]
        L += [f"| {c['cohort']} | {c['name']} | {c['stance']} | {c['practice_summary']} | {c['run_id']} |" for c in contradictions]
    else:
        L.append("_None detected in scope._")
    L += ["", "## Runs included", "", "| Run | Mode | Status | Live searches | Cached | Credits |", "|---|---|---|---|---|---|"]
    L += [f"| {r['id']} | {r['mode']} | {r['status']} | {r['searches_live']} | {r['searches_cached']} | {r['credits_estimated']} |" for r in runs]
    L += ["", "## Reproducing these figures", "",
          "Each table row carries `evidence_id`, `source_url`, `page_sha256` and `run_id`. Snapshots are content-addressed under "
          f"`{settings.research_snapshot_dir}/raw/<sha[:2]>/<sha>`. SQL views: `v_evidence_unique`, `v_cohort_stance_counts`, "
          "`v_query_usefulness`, `v_institution_peer`.", ""]
    (out / "report.md").write_text("\n".join(L), encoding="utf-8")

    payload = {
        "generated": stamp, "engine_version": __version__, "scope_run_id": run_id,
        "cohorts": [{"cohort": m.cohort, "label": m.label, "status": "validated" if m.valid_for_percentages else "preliminary",
                     "denominator": m.denominator, "denominator_note": m.denominator_note, "researched": m.researched,
                     "determinate": m.determinate, "stance_counts": m.stance_counts, "metrics": m.metrics,
                     "gates": [g.__dict__ for g in m.gates]} for m in metrics],
        "peer_groups": peers, "tables": tables, "runs": runs,
    }
    (out / "report.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return {"out_dir": str(out), "cohorts": {m.cohort: ("validated" if m.valid_for_percentages else "preliminary") for m in metrics}}

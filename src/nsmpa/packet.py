"""Case packet: an evidence workbook (Excel) and a presentation (PowerPoint) built from the database.

Design rules:
- Hand people the evidence, not conclusions: every excerpt row carries the source URL, fetch time,
  page SHA-256, snapshot path and the query that found it.
- Opposing evidence is included in its own sheet/slide; it is never filtered out.
- Cohort percentages appear only for cohorts that pass validation gates; otherwise counts only.
- Anything not human-verified is labelled "UNVERIFIED" wherever it is quoted.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import Settings
from .db import Database
from .utils import sha256_text
from .validate import all_cohorts, cohort_metrics, latest_stances_sql

# Validated categorical slots (dataviz reference palette, light mode; first three validate all-pairs).
SERIES = ["2A78D6", "EB6834", "1BAF7A"]
INK, INK_2, MUTED, SURFACE = "0B0B0B", "52514E", "8A8984", "FCFCFB"
FONT = "Arial"
FILL = {"supportive": "E4F2EA", "adverse": "FBE4E2", "neutral": "F1F1EF"}
RELIEF_MODES = ["DEINDEX_OR_ANONYMIZE_PRESERVING_ARCHIVE", "UNPUBLISHING_PERMITTED", "UPDATE_ONLY", "NO_RELIEF", "UNADDRESSED"]
RELIEF_LABEL = {"DEINDEX_OR_ANONYMIZE_PRESERVING_ARCHIVE": "De-index/anonymize, archive kept",
                "UNPUBLISHING_PERMITTED": "Unpublishing permitted", "UPDATE_ONLY": "Update / editor's note only",
                "NO_RELIEF": "No relief", "UNADDRESSED": "Not addressed"}
COHORT_SHORT = {"student_media": "Student media", "professional_newsroom": "Professional newsrooms",
                "support_org": "Standards & support orgs", "press_association": "Press associations",
                "journalism_school": "Journalism schools", "other": "Other"}
SEED_COHORTS = ("expert", "precedent_case")


# =========================================================================== data

def _evidence_scope(alias: str = "e") -> str:
    """Evidence from non-excluded runs; for population cohorts only the run that produced the entity's current stance."""
    return (f"{alias}.run_id NOT IN (SELECT id FROM research_runs WHERE status='excluded') AND ({alias}.cohort IN ('expert','precedent_case') "
            f"OR {alias}.run_id=(SELECT s2.run_id FROM entity_stances s2 WHERE s2.entity_id={alias}.entity_id AND s2.stance_version='0.3' "
            f"AND s2.run_id NOT IN (SELECT id FROM research_runs WHERE status='excluded') ORDER BY s2.id DESC LIMIT 1))")


EVIDENCE_COLS = """e.id AS evidence_id, e.cohort, re.name AS entity, re.parent_name AS parent_or_institution, e.evidence_class,
  e.statement_type, e.direction, e.actions_json AS action_positions, e.case_match_score, e.similarity_score, e.authority_score,
  e.verification_status, e.excerpt, e.source_url, e.source_title, e.fetched_at, COALESCE(e.page_sha256, p.content_sha256) AS page_sha256,
  p.snapshot_path,
  q.query AS found_by_query, e.case_match_factors_json AS case_match_factors, e.rationale AS classifier_cues, e.run_id"""
EVIDENCE_FROM = """FROM evidence_items e JOIN research_entities re ON re.id=e.entity_id
  LEFT JOIN research_pages p ON p.id=e.page_id LEFT JOIN search_queries q ON q.id=e.query_id"""


def gather(db: Database, settings: Settings, run_id: str | None = None) -> dict:
    sub, sp = latest_stances_sql(run_id)
    scope = _evidence_scope()
    if run_id:
        scope = "e.run_id=? OR e.cohort IN ('expert','precedent_case')"
    sparams = [run_id] if run_id else []
    lim = settings.packet_max_rows_per_sheet

    def ev(where: str, order: str, limit: int = lim, extra: list | None = None) -> list[dict]:
        sql = (f"SELECT {EVIDENCE_COLS} {EVIDENCE_FROM} WHERE e.duplicate_of IS NULL AND ({scope}) AND ({where}) "
               f"ORDER BY {order} LIMIT ?")
        return [dict(r) for r in db.execute(sql, sparams + (extra or []) + [limit])]

    cohorts = [c for c in all_cohorts(db)]
    metrics = {c: cohort_metrics(db, settings, c, run_id) for c in cohorts}
    entities = [dict(r) for r in db.execute(
        f"""SELECT re.id AS entity_id, re.cohort, re.name, re.parent_name, re.state, re.homepage_url, s.stance, s.confidence,
                   s.relief_mode, s.preserves_archive_relief, s.action_positions_json, s.rationale, s.practice_summary,
                   s.technical_summary, s.review_status, s.review_reasons_json, s.run_id,
                   sup.excerpt AS strongest_supportive, sup.source_url AS strongest_supportive_url,
                   adv.excerpt AS strongest_adverse, adv.source_url AS strongest_adverse_url
            FROM ({sub}) s JOIN research_entities re ON re.id=s.entity_id
            LEFT JOIN evidence_items sup ON sup.id=s.strongest_supportive_id
            LEFT JOIN evidence_items adv ON adv.id=s.strongest_adverse_id
            WHERE re.cohort NOT IN ('expert','precedent_case')
            ORDER BY re.cohort, s.relief_mode, re.name""", sp)]
    modes: dict[str, dict[str, int]] = {}
    actions: dict[str, dict[str, int]] = {}
    for e in entities:
        modes.setdefault(e["cohort"], {}).setdefault(e["relief_mode"] or "UNADDRESSED", 0)
        modes[e["cohort"]][e["relief_mode"] or "UNADDRESSED"] += 1
        a = json.loads(e["action_positions_json"] or "{}")
        c = actions.setdefault(e["cohort"], {"deindex_or_anonymize_policy": 0, "deindex_or_anonymize_practice": 0,
                                             "unpublish_rejected": 0, "unpublish_permitted": 0, "preserving_relief": 0,
                                             "relief_without_written_policy": 0, "researched": 0})
        c["researched"] += 1
        da = [a.get(k, {}) for k in ("deindex", "anonymize")]
        if any(x.get("policy") in {"permitted", "conditional", "mixed"} for x in da):
            c["deindex_or_anonymize_policy"] += 1
        if any(x.get("practice") == "granted" or x.get("technical", "none") != "none" for x in da):
            c["deindex_or_anonymize_practice"] += 1
        if a.get("unpublish", {}).get("policy") == "rejected":
            c["unpublish_rejected"] += 1
        if a.get("unpublish", {}).get("policy") in {"permitted", "conditional"}:
            c["unpublish_permitted"] += 1
        c["preserving_relief"] += int(bool(e["preserves_archive_relief"]))
        if e["stance"] in {"NO_RELEVANT_GUIDANCE", "UNDETERMINED"} and e["relief_mode"] in {
                "DEINDEX_OR_ANONYMIZE_PRESERVING_ARCHIVE", "UNPUBLISHING_PERMITTED"}:
            c["relief_without_written_policy"] += 1

    substantive = "e.statement_type NOT IN ('mention','technical_sitewide_noindex')"
    data = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "version": __version__, "scope": run_id or "latest stance per entity (excluded runs omitted)",
        "my_case": settings.my_case, "cohorts": cohorts, "metrics": metrics, "entities": entities,
        "modes": modes, "actions": actions,
        "closest": ev(f"{substantive} AND (e.about_entity=1 OR e.cohort IN ('precedent_case','expert'))",
                      "CASE e.verification_status WHEN 'verified' THEN 0 ELSE 1 END, e.case_match_score DESC, e.authority_score DESC", 1000),
        "guidance": ev(f"{substantive} AND (e.evidence_class='professional_guidance' OR e.cohort='precedent_case')",
                       "e.case_match_score DESC, e.authority_score DESC"),
        "practice": ev(f"{substantive} AND e.evidence_class IN ('documented_practice','technical') AND e.about_entity=1",
                       "CASE e.direction WHEN 'supportive' THEN 0 ELSE 1 END, e.case_match_score DESC"),
        "opposing": ev(f"{substantive} AND e.direction='adverse'", "e.authority_score DESC, e.case_match_score DESC"),
        "all_evidence": ev(substantive, "e.cohort, re.name, e.id"),
        "precedents": [dict(r) for r in db.execute(
            """SELECT ps.*, (SELECT COUNT(*) FROM evidence_items e WHERE e.entity_id=ps.entity_id AND e.about_entity=1
                              AND e.statement_type!='mention') AS excerpts,
                      (SELECT e.excerpt FROM evidence_items e WHERE e.entity_id=ps.entity_id AND e.about_entity=1
                        AND e.statement_type!='mention' ORDER BY e.authority_score DESC, e.case_match_score DESC LIMIT 1) AS best_excerpt,
                      (SELECT e.source_url FROM evidence_items e WHERE e.entity_id=ps.entity_id AND e.about_entity=1
                        AND e.statement_type!='mention' ORDER BY e.authority_score DESC, e.case_match_score DESC LIMIT 1) AS best_source
               FROM precedent_seeds ps
               ORDER BY CASE ps.status WHEN 'human_verified' THEN 0 WHEN 'sources_found' THEN 1 WHEN 'unverified' THEN 2
                        WHEN 'not_found' THEN 3 ELSE 4 END, ps.id""")],
        "voices": [dict(r) for r in db.execute(
            """SELECT v.id AS voice_id, v.person_name, COALESCE(x.role, v.role) AS role, COALESCE(x.affiliation, v.affiliation) AS affiliation,
                      CASE WHEN x.id IS NULL THEN 'captured from coverage (credentials unverified)' ELSE 'seeded expert (role to verify)' END AS basis,
                      v.direction, v.statement_type, v.actions_json AS action_positions, v.case_match_score, v.verification_status,
                      v.quote, v.source_url, v.source_title, v.attribution_method, v.attribution_confidence, v.run_id
               FROM voices v LEFT JOIN experts x ON x.id=v.expert_id
               WHERE v.run_id IS NULL OR v.run_id NOT IN (SELECT id FROM research_runs WHERE status='excluded')
               ORDER BY CASE WHEN x.id IS NULL THEN 1 ELSE 0 END, CASE v.verification_status WHEN 'verified' THEN 0 ELSE 1 END,
                        CASE v.direction WHEN 'supportive' THEN 0 WHEN 'adverse' THEN 1 ELSE 2 END, v.case_match_score DESC""")],
        "sources": [dict(r) for r in db.execute(
            """SELECT p.id AS page_id, re.cohort, re.name AS entity, p.page_kind, p.first_party, p.access_class, p.http_status,
                      p.requested_url, p.final_url, p.title, p.author, p.published_date, p.fetched_at, p.content_sha256, p.text_sha256,
                      p.snapshot_path, p.meta_robots, p.x_robots_tag, p.noindex, p.run_id
               FROM research_pages p JOIN research_entities re ON re.id=p.entity_id
               WHERE p.run_id NOT IN (SELECT id FROM research_runs WHERE status='excluded') ORDER BY re.cohort, re.name, p.id LIMIT ?""", (lim,))],
        "legal": [dict(r) for r in db.execute(
            """SELECT jurisdiction, topic, title, citation, status, prior_confidence, claim, best_excerpt, best_source, primary_url,
                      status_note, last_checked_at FROM legal_context
               ORDER BY CASE status WHEN 'human_verified' THEN 0 WHEN 'sources_found' THEN 1 ELSE 2 END,
                        CASE WHEN key LIKE 'state_%' THEN 1 ELSE 0 END, id""")],
        "wayback": [dict(r) for r in db.execute(
            """SELECT re.cohort, re.name AS entity, w.url, w.status, w.snapshots, w.earliest_ts, w.compared_ts, w.archive_url,
                      w.observations_json, w.run_id
               FROM wayback_checks w JOIN research_entities re ON re.id=w.entity_id
               WHERE w.run_id NOT IN (SELECT id FROM research_runs WHERE status='excluded')
               ORDER BY CASE w.status WHEN 'changed' THEN 0 ELSE 1 END, re.name LIMIT ?""", (lim,))],
        "ai": [dict(r) for r in db.execute(
            """SELECT re.cohort, re.name AS entity, f.agreement, f.kind, f.action, f.position, f.direction, f.conditions,
                      f.speaker, f.speaker_role, f.quote, p.final_url AS source_url, f.matched_evidence_id, r.model, r.prompt_version,
                      r.run_id AS ai_run_id
               FROM ai_findings f JOIN ai_reviews r ON r.id=f.review_id JOIN research_entities re ON re.id=f.entity_id
               LEFT JOIN research_pages p ON p.id=f.page_id
               WHERE f.quote_verified=1
               ORDER BY CASE f.agreement WHEN 'disagree' THEN 0 WHEN 'ai_only' THEN 1 ELSE 2 END, re.name LIMIT ?""", (lim,))],
        "ledger": [dict(r) for r in db.execute(
            """SELECT q.id AS query_id, q.created_at, q.run_id, re.name AS entity, q.purpose, q.query, q.status, q.was_cached,
                      q.credits_estimated, q.result_count, q.produced_evidence
               FROM search_queries q LEFT JOIN research_entities re ON re.id=q.entity_id
               WHERE q.run_id NOT IN (SELECT id FROM research_runs WHERE status='excluded') ORDER BY q.id LIMIT ?""", (lim,))],
    }
    from .audit import summary_for_packet
    data["audit"] = summary_for_packet(db)
    return data


def _obs_text(js: str | None) -> str:
    try:
        obs = json.loads(js or "[]")
    except ValueError:
        return ""
    parts = []
    for o in obs:
        if o["type"] == "names_removed":
            parts.append(f"{o['count']} name(s) removed (names withheld)")
        elif o["type"] == "noindex_added":
            parts.append("noindex added")
        elif o["type"] == "unpublished":
            parts.append(f"now HTTP {o['current_status']}")
        elif o["type"] == "content_altered":
            parts.append(f"text changed (similarity {o['text_similarity']})")
        elif o["type"] == "title_changed":
            parts.append("headline changed")
    return "; ".join(parts)


def _fmt_actions(js: str | None) -> str:
    try:
        a = json.loads(js or "{}")
    except ValueError:
        return ""
    return "; ".join(f"{k}: {v}" for k, v in sorted(a.items()))


# =========================================================================== Excel

def build_workbook(data: dict, path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="2B2B2B")
    wrap = Alignment(wrap_text=True, vertical="top")
    link_font = Font(color="1F5FBF", underline="single")

    def sheet(title: str, rows: list[dict], cols: list[tuple[str, str, int]], *, first: bool = False, note: str = "") -> None:
        ws = wb.active if first else wb.create_sheet()
        ws.title = title[:31]
        start = 1
        if note:
            ws.cell(row=1, column=1, value=note).font = Font(italic=True, color=INK_2)
            start = 3
        for j, (_, label, width) in enumerate(cols, start=1):
            c = ws.cell(row=start, column=j, value=label)
            c.font, c.fill = head_font, head_fill
            c.alignment = Alignment(wrap_text=True, vertical="center")
            ws.column_dimensions[get_column_letter(j)].width = width
        for i, r in enumerate(rows, start=start + 1):
            direction = r.get("direction")
            for j, (key, _, width) in enumerate(cols, start=1):
                v = r.get(key)
                if key in {"action_positions", "action_positions_json"}:
                    v = _fmt_actions(v)
                if isinstance(v, float):
                    v = round(v, 2)
                c = ws.cell(row=i, column=j, value=v)
                if width >= 40:
                    c.alignment = wrap
                if isinstance(v, str) and v.startswith(("http://", "https://")) and len(v) < 2000:
                    c.hyperlink = v
                    c.font = link_font
                if direction in FILL and key == "direction":
                    c.fill = PatternFill("solid", fgColor=FILL[direction])
        ws.freeze_panes = ws.cell(row=start + 1, column=1)
        if rows:
            ws.auto_filter.ref = f"A{start}:{get_column_letter(len(cols))}{start + len(rows)}"

    # ---- Read Me
    ws = wb.active
    ws.title = "Read Me"
    lines = [
        ("NSMPA evidence workbook", True),
        (f"Generated {data['generated']} by NSMPA {data['version']}. Scope: {data['scope']}.", False),
        ("", False),
        ("What this is", True),
        ("A database of published policies, documented newsroom practice, technical observations and professional opinion on "
         "post-publication relief (de-indexing, anonymization, unpublishing, updates) after criminal allegations end in dismissal, "
         "acquittal, expungement or similar outcomes. Supportive and opposing evidence were searched for with equal effort.", False),
        ("", False),
        ("How to check any row", True),
        ("Every excerpt row includes the source URL (clickable), the time it was fetched, the SHA-256 of the exact page that was "
         "analysed and the local snapshot path, so anyone can confirm the quote against the live page or the archived copy.", False),
        ("Rows marked UNVERIFIED have not yet been checked by a person. Verify before quoting publicly.", False),
        (("Names of private individuals have been replaced with [name withheld] by automated redaction "
          f"({data.get('redaction_count', 0)} replacements). Organizations, institutions, cited experts and quoted speakers are "
          "kept. URLs containing a redacted name and archived-copy links are withheld; originals are retained privately and "
          "available on request by evidence ID.") if data.get("redacted") else
         "This copy is NOT redacted: excerpts may contain names of private individuals. Use `nsmpa packet --redact-names` "
         "before sharing outside your review team.", data.get("redacted", False)),
        ("", False),
        ("Rules used", True),
        ("• Student media, professional newsrooms and standards organizations are separate groups with separate denominators; "
         "their percentages are never combined.", False),
        ("• 'Undetermined' means the source could not be adequately inspected. It is never counted as 'no policy'.", False),
        ("• Written policy, documented practice (what a newsroom actually did), technical observations (e.g. noindex tags) and "
         "professional guidance are reported separately.", False),
        ("• A noindex tag on an archived crime article counts as de-indexing practice only when ordinary articles on the same site "
         "are indexable (site-wide noindex is excluded).", False),
        ("• Percentages are shown only for groups that pass the validation gates (coverage, review, verification).", False),
        ("", False),
        ("Sheets", True),
        ("Summary · Closest to My Case · De-index vs Unpublish · Named Precedents · Professional Guidance · Expert Voices · "
         "Documented Practice · Opposing Evidence · Legal Context · Archive Changes · AI Second Opinion · Accuracy · Entities · All Evidence · "
         "Sources · Search Ledger", False),
    ]
    ws.column_dimensions["A"].width = 130
    for i, (t, bold) in enumerate(lines, start=1):
        c = ws.cell(row=i, column=1, value=t)
        c.font = Font(bold=bold, size=14 if i == 1 else 11)
        c.alignment = Alignment(wrap_text=True, vertical="top")

    # ---- Summary
    summary = []
    for c in data["cohorts"]:
        m = data["metrics"][c]
        a = data["actions"].get(c, {})
        valid = m.valid_for_percentages
        n = a.get("researched", 0)

        def pct(x: int) -> str:
            return f"{100 * x / n:.1f}%" if valid and n else "withheld"
        summary.append({
            "cohort": COHORT_SHORT.get(c, c), "status": "VALIDATED" if valid else "PRELIMINARY (counts only)",
            "denominator": m.denominator, "researched": n,
            "deindex_policy": a.get("deindex_or_anonymize_policy", 0), "deindex_policy_pct": pct(a.get("deindex_or_anonymize_policy", 0)),
            "deindex_practice": a.get("deindex_or_anonymize_practice", 0),
            "preserving": a.get("preserving_relief", 0), "preserving_pct": pct(a.get("preserving_relief", 0)),
            "without_policy": a.get("relief_without_written_policy", 0),
            "unpub_rejected": a.get("unpublish_rejected", 0), "unpub_permitted": a.get("unpublish_permitted", 0),
            "failing_gates": "; ".join(g.name for g in m.failing) or "none",
        })
    sheet("Summary", summary, [
        ("cohort", "Group", 24), ("status", "Status", 22), ("denominator", "Universe size", 12), ("researched", "Researched", 11),
        ("deindex_policy", "Written policy permits de-index/anonymize", 18), ("deindex_policy_pct", "% of researched", 11),
        ("deindex_practice", "Practice/technical evidence of de-index/anonymize", 20),
        ("preserving", "Relief that keeps the archive", 14), ("preserving_pct", "% of researched", 11),
        ("without_policy", "Relief practiced with no written policy", 16),
        ("unpub_rejected", "Policy rejects unpublishing", 14), ("unpub_permitted", "Policy permits unpublishing", 14),
        ("failing_gates", "Validation gates not yet met", 60)],
        note="Percentages appear only for groups that pass every validation gate. 'Relief that keeps the archive' = de-indexing "
             "or anonymization permitted or practiced while the article stays published.")

    ev_cols = [("evidence_id", "ID", 8), ("cohort", "Group", 16), ("entity", "Organization", 26), ("parent_or_institution", "Institution / topic", 22),
               ("direction", "Direction", 11), ("statement_type", "Statement type", 24), ("evidence_class", "Evidence class", 20),
               ("action_positions", "Positions by action", 28), ("case_match_score", "Match to my case (0-100)", 11),
               ("verification_status", "Verified?", 12), ("excerpt", "Exact excerpt", 80), ("source_url", "Source", 40),
               ("source_title", "Page title", 30), ("fetched_at", "Fetched (UTC)", 18), ("page_sha256", "Page SHA-256", 20),
               ("snapshot_path", "Snapshot", 30), ("found_by_query", "Found by search", 36), ("case_match_factors", "Matched facts", 40),
               ("classifier_cues", "Classifier cues", 30), ("run_id", "Run", 16)]
    sheet("Closest to My Case", data["closest"], ev_cols,
          note="All directions included, ranked by how many elements of my_case.yml each excerpt matches. Verified rows first.")

    ent_rows = []
    for e in data["entities"]:
        a = json.loads(e["action_positions_json"] or "{}")
        row = dict(e)
        for act in ("deindex", "anonymize", "unpublish", "update"):
            x = a.get(act, {})
            row[f"{act}_policy"], row[f"{act}_practice"] = x.get("policy"), x.get("practice")
        row["deindex_technical"] = a.get("deindex", {}).get("technical")
        row["relief_mode"] = RELIEF_LABEL.get(e["relief_mode"] or "", e["relief_mode"])
        ent_rows.append(row)
    sheet("De-index vs Unpublish", ent_rows, [
        ("cohort", "Group", 16), ("name", "Organization", 28), ("parent_name", "Institution", 26), ("relief_mode", "Overall position", 26),
        ("deindex_policy", "De-index: policy", 13), ("deindex_practice", "De-index: practice", 13), ("deindex_technical", "De-index: noindex seen", 15),
        ("anonymize_policy", "Anonymize: policy", 13), ("anonymize_practice", "Anonymize: practice", 13),
        ("unpublish_policy", "Unpublish: policy", 13), ("unpublish_practice", "Unpublish: practice", 13),
        ("update_policy", "Update: policy", 13), ("stance", "Written-policy stance", 24), ("review_status", "Human review", 14)],
        note="Separates relief that keeps the article published (de-index, anonymize) from deleting it (unpublish).")

    sheet("Named Precedents", data["precedents"], [
        ("organization", "Organization", 24), ("title", "Program / document", 34), ("approx_year", "Year", 9),
        ("status", "Status", 15), ("prior_confidence", "Lead quality", 13), ("claim", "Claim being checked", 60),
        ("excerpts", "Excerpts found", 9), ("best_excerpt", "Best excerpt found", 70), ("best_source", "Source", 40),
        ("status_note", "Note", 30), ("last_checked_at", "Checked", 18)],
        note="Seeded leads. 'sources_found' means fetched text supports the claim; only 'human_verified' rows should be presented as fact.")
    sheet("Professional Guidance", data["guidance"], ev_cols)
    sheet("Expert Voices", data["voices"], [
        ("person_name", "Person", 22), ("role", "Role (verify)", 24), ("affiliation", "Affiliation", 26), ("basis", "Basis", 22),
        ("direction", "Direction", 11), ("quote", "Quote", 80), ("source_url", "Source", 40), ("source_title", "Page title", 30),
        ("verification_status", "Verified?", 12), ("action_positions", "Positions by action", 26),
        ("case_match_score", "Match to my case", 10), ("attribution_method", "How attributed", 18), ("voice_id", "ID", 7)],
        note="Opinions in both directions. Attribution is automatic; confirm speaker, role and wording against the source before use.")
    sheet("Documented Practice", data["practice"], ev_cols,
          note="What newsrooms actually did: editor's notes, name removals, noindex on archived crime articles, documented refusals.")
    sheet("Opposing Evidence", data["opposing"], ev_cols,
          note="Policies and statements against removal or de-indexing. Included deliberately; a credible case answers these.")
    sheet("Legal Context", data["legal"], [
        ("jurisdiction", "Jurisdiction", 22), ("title", "Law / decision", 40), ("citation", "Citation (verify)", 24),
        ("status", "Status", 15), ("claim", "What it is said to do (to verify)", 60), ("best_excerpt", "Best excerpt found", 70),
        ("best_source", "Source", 40), ("primary_url", "Primary source", 36), ("last_checked_at", "Checked", 18)],
        note="Context, not legal advice. Includes authority on both sides. Only 'human_verified' rows should be presented as fact.")
    wb_rows = [dict(r, observations=_obs_text(r["observations_json"])) for r in data["wayback"]]
    sheet("Archive Changes", wb_rows, [
        ("cohort", "Group", 16), ("entity", "Organization", 26), ("status", "Result", 16), ("observations", "What changed", 50),
        ("earliest_ts", "Earliest capture", 16), ("snapshots", "Distinct captures", 10), ("url", "Current URL", 50),
        ("archive_url", "Archived copy", 50), ("run_id", "Run", 16)],
        note="Wayback Machine comparisons of archived crime/arrest articles. Names of people are never recorded; "
             "the archived copy may still show them.")
    sheet("AI Second Opinion", data["ai"], [
        ("cohort", "Group", 16), ("entity", "Organization", 26), ("agreement", "vs. rule-based classifier", 16),
        ("kind", "Kind", 11), ("action", "Action", 11), ("position", "Position", 12), ("direction", "Direction", 11),
        ("quote", "Verbatim quote (verified in page text)", 80), ("conditions", "Conditions", 30), ("speaker", "Speaker", 18),
        ("speaker_role", "Role", 20), ("source_url", "Source", 40), ("model", "Model", 16), ("prompt_version", "Prompt", 12)],
        note="Independent AI reading of the same pages. Only quotes found word-for-word in the saved page are listed. "
             "Disagreements are review items, not findings.")
    audit = data.get("audit")
    acc_rows = []
    if audit:
        def w(x):
            return "–" if not x else f"{x[0]:.0%} (95% CI {x[1]:.0%}–{x[2]:.0%})"
        acc_rows = [{"metric": "Items hand-checked", "value": f"{audit['labeled_items']} of {audit['sample_size']}"},
                    {"metric": "Relevance precision", "value": w(audit.get("relevance_precision"))},
                    {"metric": "Direction accuracy", "value": w(audit.get("direction_accuracy"))}]
        acc_rows += [{"metric": f"Accuracy when machine said '{d}'", "value": w(x)}
                     for d, x in (audit.get("direction_accuracy_by_machine_label") or {}).items()]
        if audit.get("inter_rater"):
            ir = audit["inter_rater"]
            acc_rows.append({"metric": f"Agreement between {ir['labelers'][0]} and {ir['labelers'][1]} (Cohen's kappa)",
                             "value": f"{ir['cohen_kappa_direction']} over {ir['items']} items"})
        if audit.get("ai_direction_accuracy"):
            acc_rows.append({"metric": "AI second-opinion direction accuracy", "value": w(audit["ai_direction_accuracy"])})
    else:
        acc_rows = [{"metric": "Accuracy audit", "value": "Not yet measured — run `nsmpa audit sample` and label the sample"}]
    sheet("Accuracy", acc_rows, [("metric", "Measure", 60), ("value", "Result", 50)],
          note="Measured by people hand-checking a random, stratified sample of the tool's classifications.")
    if data.get("ai_summaries"):
        srows = [{"section": v["title"], "sentence": snt["text"], "evidence_ids": ", ".join(f"E{i}" for i in snt["evidence_ids"]),
                  "model": v.get("model", ""), "dropped": v.get("dropped", 0)}
                 for v in data["ai_summaries"].values() for snt in v["sentences"]]
        sheet("AI Summaries", srows, [("section", "Section", 28), ("sentence", "AI-drafted sentence (edit before use)", 80),
                                      ("evidence_ids", "Cites evidence IDs", 20), ("model", "Model", 22),
                                      ("dropped", "Sentences dropped by validation", 12)],
              note="Drafted by AI from the listed evidence only. Sentences without valid evidence citations or with "
                   "numbers not present in the evidence were removed automatically.")
    sheet("Entities", data["entities"], [
        ("entity_id", "ID", 7), ("cohort", "Group", 16), ("name", "Organization", 28), ("parent_name", "Institution", 26),
        ("state", "State", 7), ("homepage_url", "Website", 32), ("stance", "Written-policy stance", 24), ("confidence", "Confidence", 10),
        ("rationale", "Rationale", 60), ("practice_summary", "Practice found", 30), ("technical_summary", "Technical", 40),
        ("strongest_supportive", "Strongest supportive", 60), ("strongest_supportive_url", "Source", 36),
        ("strongest_adverse", "Strongest opposing", 60), ("strongest_adverse_url", "Source", 36),
        ("review_status", "Human review", 14), ("run_id", "Run", 16)])
    sheet("All Evidence", data["all_evidence"], ev_cols)
    sheet("Sources", data["sources"], [
        ("page_id", "Page ID", 8), ("cohort", "Group", 16), ("entity", "Organization", 26), ("page_kind", "Page type", 14),
        ("first_party", "Own site?", 9), ("access_class", "Access", 14), ("http_status", "HTTP", 7), ("final_url", "URL", 50),
        ("title", "Title", 36), ("author", "Author", 18), ("published_date", "Published", 18), ("fetched_at", "Fetched (UTC)", 18),
        ("content_sha256", "Page SHA-256", 22), ("snapshot_path", "Snapshot", 36), ("meta_robots", "meta robots", 18),
        ("x_robots_tag", "X-Robots-Tag", 14), ("noindex", "noindex", 8)])
    sheet("Search Ledger", data["ledger"], [
        ("query_id", "ID", 8), ("created_at", "When", 18), ("entity", "Organization", 24), ("purpose", "Purpose", 30),
        ("query", "Query", 70), ("status", "Status", 12), ("was_cached", "Cached", 8), ("credits_estimated", "Credits", 8),
        ("result_count", "Results", 8), ("produced_evidence", "Useful", 8), ("run_id", "Run", 16)])
    wb.save(path)


# =========================================================================== PowerPoint

def build_deck(data: dict, path: Path, *, title: str, max_precedent_slides: int) -> int:
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.dml.color import RGBColor
    from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION, XL_LABEL_POSITION
    from pptx.util import Inches, Pt

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    blank = prs.slide_layouts[6]
    rgb = RGBColor.from_string

    def text(slide, x, y, w, h, lines, size=18, color=INK, bold_first=False, italic=False):
        tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        tf = tb.text_frame
        tf.word_wrap = True
        for i, line in enumerate(lines if isinstance(lines, list) else [lines]):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            run = p.add_run()
            run.text = line
            run.font.size = Pt(size)
            run.font.name = FONT
            run.font.color.rgb = rgb(color)
            run.font.bold = bold_first and i == 0
            run.font.italic = italic
            p.space_after = Pt(6)
        return tb

    def new(title_text: str, kicker: str = ""):
        s = prs.slides.add_slide(blank)
        bg = s.background.fill
        bg.solid()
        bg.fore_color.rgb = rgb(SURFACE)
        if kicker:
            text(s, 0.6, 0.35, 12, 0.4, kicker.upper(), size=12, color=MUTED)
        text(s, 0.6, 0.65, 12.1, 0.9, title_text, size=30, color=INK, bold_first=True)
        return s

    def footer(s, msg: str):
        text(s, 0.6, 6.85, 12.1, 0.4, msg, size=10, color=MUTED)

    def style_chart(ch, max_value: int) -> None:
        from pptx.enum.chart import XL_TICK_LABEL_POSITION  # noqa: F401
        ch.category_axis.reverse_order = True          # first category on top, matching reading order
        ch.category_axis.tick_labels.font.size = Pt(13)
        ch.category_axis.format.line.color.rgb = rgb("C9C8C3")
        va = ch.value_axis
        va.has_major_gridlines = True
        va.major_gridlines.format.line.color.rgb = rgb("ECEBE7")
        va.format.line.fill.background()
        va.tick_labels.font.size = Pt(11)
        va.tick_labels.font.color.rgb = rgb(MUTED)
        va.minimum_scale = 0
        if max_value <= 10:
            va.major_unit = 1
        if ch.has_legend:
            ch.legend.font.size = Pt(13)

    def verified_tag(status: str | None) -> str:
        return "VERIFIED" if status in {"verified", "human_verified"} else "UNVERIFIED — confirm against source before quoting"

    # 1. Title
    s = new(title, f"Evidence briefing · {data['generated']}")
    text(s, 0.6, 2.2, 12, 2.5, [
        "How U.S. journalism organizations handle requests to de-index, anonymize, update or remove truthful archived "
        "reporting after circumstances change.",
        "Every statement in this deck is drawn from a source listed in the accompanying workbook, with its URL, fetch time "
        "and page hash.",
    ], size=20, color=INK_2)
    footer(s, f"Prepared with NSMPA {data['version']} · scope: {data['scope']}"
              + (" · names of private individuals withheld (automated redaction)" if data.get("redacted") else ""))

    # 2. My situation
    mc = data["my_case"]
    facts = [label for key, label in (
        ("student_or_university_setting", "Student / university setting"), ("criminal_allegation_or_arrest", "Criminal allegation or arrest"),
        ("charges_dismissed_or_dropped", "Charges dismissed or dropped"), ("acquittal_or_exoneration", "Acquittal or exoneration"),
        ("conviction_vacated_or_plea_withdrawn", "Conviction vacated or plea withdrawn"), ("record_expunged_or_sealed", "Record expunged or sealed"),
        ("substantial_time_passed", "Substantial time has passed"), ("private_individual_no_public_role", "Private individual, no public role"),
        ("search_engine_prominence", "Article surfaces prominently in name searches"), ("reputational_consequences", "Continuing reputational harm"),
    ) if getattr(mc, key)]
    s = new("The situation this evidence is matched against", "Case profile")
    text(s, 0.6, 1.8, 6.2, 4.8, ["Facts:"] + [f"• {f}" for f in facts], size=18, bold_first=True)
    text(s, 7.0, 1.8, 5.7, 4.8, ["Relief requested:"] + [f"• {r}" for r in mc.relief_sought]
         + ([""] + [mc.description] if mc.description else []), size=18, bold_first=True)
    if not mc.configured:
        footer(s, "Default profile — edit my_case.yml to describe the actual case before presenting.")

    # 3. Method
    s = new("How the evidence was gathered", "Method")
    text(s, 0.6, 1.8, 12.1, 4.9, [
        "• Three separate groups, never pooled: student newspapers (IPEDS four-year institutions), professional newsrooms, "
        "and journalism standards / legal / ethics organizations.",
        "• Each organization's own site was searched and read for written policy; its archive was sampled for what it actually "
        "did (editor's notes, name removals, noindex tags on old crime stories).",
        "• Supportive and opposing language were searched for with equal effort; opposing evidence is reported in full.",
        "• Sites that could not be inspected are 'undetermined', never counted as 'no policy'.",
        "• Every excerpt is stored with its URL, fetch time and SHA-256 hash; snapshots are kept unchanged.",
    ], size=18)

    # 4. Chart: de-index/anonymize vs unpublish by cohort
    cohorts = [c for c in data["cohorts"] if data["actions"].get(c, {}).get("researched")]
    s = new("De-indexing and anonymization vs. unpublishing", "Which kind of relief, by group")
    if cohorts:
        cd = CategoryChartData()
        cd.categories = [COHORT_SHORT.get(c, c) for c in cohorts]
        series = [("Policy: de-index/anonymize", "deindex_or_anonymize_policy"),
                  ("Practice: de-index/anonymize", "deindex_or_anonymize_practice"),
                  ("Policy: unpublish", "unpublish_permitted")]
        for label, key in series:
            cd.add_series(label, [data["actions"][c].get(key, 0) for c in cohorts])
        gf = s.shapes.add_chart(XL_CHART_TYPE.BAR_CLUSTERED, Inches(0.6), Inches(1.7), Inches(8.4), Inches(4.9), cd)
        ch = gf.chart
        ch.has_legend = True
        ch.legend.position = XL_LEGEND_POSITION.BOTTOM
        ch.legend.include_in_layout = False
        ch.legend.font.size = Pt(12)
        ch.plots[0].gap_width = 60
        ch.plots[0].overlap = -8
        ch.value_axis.has_major_gridlines = True
        ch.value_axis.major_gridlines.format.line.color.rgb = rgb("E4E3DF")
        ch.value_axis.tick_labels.font.size = Pt(11)
        ch.category_axis.tick_labels.font.size = Pt(12)
        for i, ser in enumerate(ch.series):
            ser.format.fill.solid()
            ser.format.fill.fore_color.rgb = rgb(SERIES[i])
            ser.data_labels.show_value = True
            ser.data_labels.number_format = "0;;;"   # hide zero labels
            ser.data_labels.number_format_is_linked = False
            ser.data_labels.position = XL_LABEL_POSITION.OUTSIDE_END
            ser.data_labels.font.size = Pt(11)
            ser.data_labels.font.color.rgb = rgb(INK_2)
        style_chart(ch, max(max(data["actions"][c].get(k, 0) for _, k in series) for c in cohorts))
        notes = []
        for c in cohorts:
            a = data["actions"][c]
            status = "validated" if data["metrics"][c].valid_for_percentages else "preliminary — counts only"
            notes.append(f"{COHORT_SHORT.get(c, c)}: {a['researched']} researched ({status}); "
                         f"{a['preserving_relief']} allow relief that keeps the archive; {a['unpublish_rejected']} reject unpublishing.")
        text(s, 9.2, 1.8, 3.6, 4.8, notes, size=13, color=INK_2)
    else:
        text(s, 0.6, 2.2, 12, 1, "No organizations have been researched yet in this scope.", size=20, color=INK_2)
    footer(s, "Counts of organizations. Source rows: workbook sheet 'De-index vs Unpublish'.")

    # 5. Chart: overall position by cohort
    if cohorts:
        s = new("Where each group stands", "Overall position by group")
        cd = CategoryChartData()
        modes = [m for m in RELIEF_MODES if any(data["modes"].get(c, {}).get(m) for c in cohorts)]
        cd.categories = [RELIEF_LABEL[m] for m in modes]
        for c in cohorts[:3]:
            cd.add_series(COHORT_SHORT.get(c, c), [data["modes"].get(c, {}).get(m, 0) for m in modes])
        ch = s.shapes.add_chart(XL_CHART_TYPE.BAR_CLUSTERED, Inches(0.6), Inches(1.7), Inches(12.1), Inches(4.9), cd).chart
        ch.has_legend = len(cohorts) > 1
        if ch.has_legend:
            ch.legend.position = XL_LEGEND_POSITION.BOTTOM
            ch.legend.include_in_layout = False
        ch.plots[0].gap_width = 60
        ch.value_axis.major_gridlines.format.line.color.rgb = rgb("E4E3DF")
        for i, ser in enumerate(ch.series):
            ser.format.fill.solid()
            ser.format.fill.fore_color.rgb = rgb(SERIES[i])
            ser.data_labels.show_value = True
            ser.data_labels.number_format = "0;;;"
            ser.data_labels.number_format_is_linked = False
            ser.data_labels.position = XL_LABEL_POSITION.OUTSIDE_END
            ser.data_labels.font.size = Pt(11)
        style_chart(ch, max([data["modes"].get(c, {}).get(m, 0) for c in cohorts for m in modes] or [0]))
        footer(s, "Each organization counted once, in its own group. 'Not addressed' includes organizations whose sites could not be fully inspected.")

    # 6. Named precedents table
    precs = [p for p in data["precedents"] if p["status"] in {"human_verified", "sources_found"}]
    s = new("Named programs and precedents", "Known initiatives")
    if precs:
        rows = min(len(precs), 8) + 1
        tbl = s.shapes.add_table(rows, 4, Inches(0.6), Inches(1.7), Inches(12.1), Inches(0.5 * rows)).table
        for j, (h, w) in enumerate((("Organization", 2.6), ("Program / document", 3.4), ("Year", 0.9), ("Status", 5.2))):
            tbl.columns[j].width = Inches(w)
            tbl.cell(0, j).text = h
        for i, p in enumerate(precs[:8], start=1):
            vals = (p["organization"], p["title"], p["approx_year"] or "", verified_tag(p["status"]))
            for j, v in enumerate(vals):
                cell = tbl.cell(i, j)
                cell.text = v
                for para in cell.text_frame.paragraphs:
                    for r in para.runs:
                        r.font.size = Pt(13)
    else:
        text(s, 0.6, 2.2, 12, 1, "Run `nsmpa verify-precedents` to locate sources for the seeded programs.", size=20, color=INK_2)
    footer(s, "Workbook sheet 'Named Precedents' lists every lead, including those not found.")

    # 7+. Closest precedents, one per slide
    shown = 0
    for r in data["closest"]:
        if shown >= max_precedent_slides:
            break
        if r["direction"] == "adverse" or r["statement_type"] in {"harm_consideration"}:
            continue
        shown += 1
        s = new(r["entity"] + (f" — {r['parent_or_institution']}" if r["parent_or_institution"] else ""),
                f"Precedent {shown} · {COHORT_SHORT.get(r['cohort'], r['cohort'])} · match to case {r['case_match_score']:.0f}/100")
        observation = r["evidence_class"] == "technical"
        body = ("Observed: " + r["excerpt"][:600]) if observation else f"“{r['excerpt'][:600]}”"
        text(s, 0.9, 1.9, 11.5, 3.2, body, size=22, color=INK, italic=not observation)
        factors = ", ".join(f["factor"].replace("_", " ") for f in json.loads(r["case_match_factors"] or "[]"))
        text(s, 0.9, 5.2, 11.5, 1.4, [f"Source: {r['source_url']}", f"Fetched {r['fetched_at']} · page SHA-256 {str(r['page_sha256'])[:16]}…",
                                       f"Matches: {factors or '—'}", verified_tag(r["verification_status"])], size=12, color=INK_2)

    # AI-drafted section summaries (optional)
    for key, summ in (data.get("ai_summaries") or {}).items():
        if not summ.get("sentences"):
            continue
        s = new(summ["title"], "AI-drafted summary — edit before presenting")
        text(s, 0.6, 1.9, 12.1, 4.6, [f"{x['text']}  [{', '.join('E' + str(i) for i in x['evidence_ids'])}]"
                                      for x in summ["sentences"]], size=18)
        footer(s, f"Drafted by {summ.get('model')} from the cited evidence only (IDs refer to the workbook). "
                  f"{summ.get('dropped', 0)} unsupported sentence(s) removed automatically.")

    # Expert voices
    voices = data["voices"]
    for direction, heading in (("supportive", "What respected practitioners say"), ("adverse", "Practitioners urging caution")):
        vs = [v for v in voices if v["direction"] == direction][:4]
        if not vs:
            continue
        s = new(heading, "Expert voices")
        for i, v in enumerate(vs):
            x, y = 0.6 + (i % 2) * 6.15, 1.7 + (i // 2) * 2.55
            card = s.shapes.add_shape(1, Inches(x), Inches(y), Inches(5.95), Inches(2.4))
            card.fill.solid()
            card.fill.fore_color.rgb = rgb(FILL["supportive" if direction == "supportive" else "adverse"])
            card.line.color.rgb = rgb("E4E3DF")
            tb = text(s, x + 0.15, y + 0.1, 5.65, 2.2, [
                f"“{v['quote'][:260]}”",
                f"— {v['person_name']}" + (f", {v['role']}" if v['role'] else "") + (f", {v['affiliation']}" if v['affiliation'] else ""),
                f"{v['source_url'][:90]} · {verified_tag(v['verification_status'])[:10]}"], size=12, color=INK)
            tb.text_frame.paragraphs[0].runs[0].font.italic = True
        footer(s, "Roles as commonly reported; attribution extracted automatically. Full list: workbook sheet 'Expert Voices'.")

    # Opposing evidence
    opp = data["opposing"][:3]
    s = new("The main objection: the archive is the historical record", "Opposing evidence")
    lines = [f"“{o['excerpt'][:220]}” — {o['entity']}" for o in opp] or ["No opposing statements found in scope."]
    text(s, 0.6, 1.8, 12.1, 3.4, lines, size=16, color=INK)
    pres = sum(a.get("preserving_relief", 0) for a in data["actions"].values())
    rej = sum(a.get("unpublish_rejected", 0) for a in data["actions"].values())
    text(s, 0.6, 5.3, 12.1, 1.4, [
        f"In this dataset, {pres} organization(s) allow or practice de-indexing or anonymization while keeping the article "
        f"published; {rej} organization(s) explicitly reject unpublishing.",
        "De-indexing and anonymization leave the published record in place."], size=16, color=INK_2)
    footer(s, f"All {len(data['opposing'])} opposing excerpts: workbook sheet 'Opposing Evidence'.")

    # Legal context
    legal = [r for r in data.get("legal", []) if r["status"] in {"human_verified", "sources_found"} and not r["topic"] == "record_clearing"]
    if legal:
        s = new("Legal context", "Not legal advice — the law sets the floor; relief is editorial discretion")
        text(s, 0.6, 1.8, 12.1, 4.8, [f"• {r['title']}{' — ' + r['citation'] if r['citation'] else ''}: {r['claim'][:220]}"
                                      + ("" if r["status"] == "human_verified" else "  (UNVERIFIED)") for r in legal[:6]], size=15)
        footer(s, "Workbook sheet 'Legal Context' lists every lead, including state record-clearing laws and authority against a legal duty to remove.")

    # Accuracy
    s = new("How accurate is the automated classification?", "Measured, not assumed")
    audit = data.get("audit")
    if audit and audit.get("direction_accuracy"):
        da, rp = audit["direction_accuracy"], audit.get("relevance_precision")
        lines = [f"{audit['labeled_items']} randomly sampled items were checked by hand.",
                 f"Direction correct: {da[0]:.0%} (95% confidence interval {da[1]:.0%}–{da[2]:.0%})."]
        if rp:
            lines.append(f"Items that were genuine evidence: {rp[0]:.0%} (95% CI {rp[1]:.0%}–{rp[2]:.0%}).")
        if audit.get("inter_rater"):
            ir = audit["inter_rater"]
            lines.append(f"Two independent reviewers agreed with Cohen's kappa = {ir['cohen_kappa_direction']} ({ir['items']} items).")
        text(s, 0.6, 1.9, 12.1, 4.6, lines, size=22)
    else:
        text(s, 0.6, 1.9, 12.1, 2, "Not yet measured. Run `nsmpa audit sample`, label the sample, and rebuild the packet.",
             size=20, color=INK_2)
    footer(s, "Workbook sheet 'Accuracy'.")

    # Limitations
    s = new("Limits of this evidence", "Read before relying on it")
    fails = []
    for c in data["cohorts"]:
        m = data["metrics"][c]
        if not m.valid_for_percentages:
            fails.append(f"• {COHORT_SHORT.get(c, c)}: preliminary — " + "; ".join(g.name for g in m.failing[:3]))
    text(s, 0.6, 1.8, 12.1, 4.9, [
        "• Automated extraction; every quote used publicly should be confirmed against the live page or snapshot.",
        "• Professional newsroom group is a sourced panel, not a census of U.S. journalism.",
        "• Absence of a written policy is not evidence of a practice either way.",
        "• Absence from Google is never treated as de-indexing; only noindex directives on the page itself are counted.",
    ] + fails, size=16)
    prs.save(path)
    return len(prs.slides)


# =========================================================================== AI summaries

SUMMARY_PROMPT_VERSION = "ai-summary-v1"
SUMMARY_SYSTEM = """You draft short, neutral summaries of evidence for a research presentation about how news organizations handle
requests to remove, de-index, anonymize, or update old articles. Use ONLY the numbered evidence provided. Write 2-3 plain
sentences. Every sentence must cite the evidence IDs it relies on. Do not generalize beyond the evidence, do not state
counts or percentages that are not given in the input, do not characterize the evidence as proving anything, and include
opposing evidence if it is provided."""
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"sentences": {"type": "array", "items": {
        "type": "object",
        "properties": {"text": {"type": "string"}, "evidence_ids": {"type": "array", "items": {"type": "integer"}}},
        "required": ["text", "evidence_ids"], "additionalProperties": False}}},
    "required": ["sentences"], "additionalProperties": False,
}
SUMMARY_SECTIONS = {
    "guidance": ("What professional guidance says", "guidance"),
    "practice": ("What newsrooms have actually done", "practice"),
    "opposing": ("The case against removal", "opposing"),
}


def validate_summary(sentences: list[dict], allowed_ids: set[int], payload: str) -> tuple[list[dict], int]:
    """Keep only sentences that cite at least one provided evidence ID, cite no unknown IDs, and state no new numbers."""
    import re as _re
    numbers_in = set(_re.findall(r"\d+(?:\.\d+)?", payload))
    kept, dropped = [], 0
    for snt in sentences or []:
        ids = [i for i in snt.get("evidence_ids", []) if isinstance(i, int)]
        text = str(snt.get("text", "")).strip()
        cleaned = _re.sub(r"\[E\d+\]", "", text)
        bad_numbers = set(_re.findall(r"\d+(?:\.\d+)?", cleaned)) - numbers_in
        if not text or not ids or not set(ids) <= allowed_ids or bad_numbers:
            dropped += 1
            continue
        kept.append({"text": text, "evidence_ids": ids})
    return kept, dropped


def ai_summaries(db: Database, settings: Settings, data: dict, client=None) -> dict:
    from .ai_review import AIUnavailable, cached_call, make_client
    out: dict = {}
    for key, (title, data_key) in SUMMARY_SECTIONS.items():
        rows = data.get(data_key, [])[:25]
        if not rows:
            continue
        lines = [f"Section: {title}", "Evidence:"]
        for r in rows:
            lines.append(f"[E{r['evidence_id']}] ({r['entity']}, {r['cohort']}, {r['direction']}) {r['excerpt'][:500]}")
        payload = "\n".join(lines)
        try:
            client = client or make_client(settings)
            resp, cached = cached_call(db, client, settings, SUMMARY_PROMPT_VERSION, sha256_text(payload), payload,
                                       SUMMARY_SYSTEM, SUMMARY_SCHEMA)
        except AIUnavailable as exc:
            out[key] = {"title": title, "sentences": [], "dropped": 0, "error": str(exc)}
            continue
        kept, dropped = validate_summary(resp.get("sentences", []), {r["evidence_id"] for r in rows}, payload)
        out[key] = {"title": title, "sentences": kept, "dropped": dropped, "cached": cached,
                    "model": f"{settings.ai_provider}:{settings.ai_model}"}
    return out


# =========================================================================== entry point

def build_packet(db: Database, settings: Settings, out_dir: Path, *, run_id: str | None = None,
                 title: str = "Post-publication relief in U.S. journalism: the evidence", ai_summaries_on: bool = False,
                 ai_client=None, redact: bool = False) -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(out_dir) / f"packet_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    data = gather(db, settings, run_id)
    data["ai_summaries"] = ai_summaries(db, settings, data, ai_client) if ai_summaries_on else {}
    if redact:
        from .redact import Redactor, redact_packet_data
        redact_packet_data(data, Redactor.from_db(db))
    xlsx = out / "NSMPA_evidence.xlsx"
    pptx = out / "NSMPA_presentation.pptx"
    build_workbook(data, xlsx)
    slides = build_deck(data, pptx, title=title, max_precedent_slides=settings.packet_precedent_slides)
    md = [f"# {title}", "", f"Generated {data['generated']} (NSMPA {data['version']}); scope: {data['scope']}."
          + (f" Names of private individuals withheld ({data.get('redaction_count', 0)} automated replacements)." if data.get("redacted") else
             " NOT redacted — may contain names of private individuals."), "",
          f"- Workbook: `{xlsx.name}` ({len(data['all_evidence'])} evidence rows, {len(data['voices'])} voices, "
          f"{len(data['precedents'])} precedent leads, {len(data['opposing'])} opposing excerpts)",
          f"- Presentation: `{pptx.name}` ({slides} slides)", ""]
    for c in data["cohorts"]:
        a = data["actions"].get(c, {})
        m = data["metrics"][c]
        md.append(f"- {COHORT_SHORT.get(c, c)} ({'validated' if m.valid_for_percentages else 'preliminary'}): "
                  f"{a.get('researched', 0)} researched; {a.get('preserving_relief', 0)} archive-preserving relief; "
                  f"{a.get('relief_without_written_policy', 0)} practice without written policy; "
                  f"{a.get('unpublish_rejected', 0)} reject unpublishing")
    (out / "case_packet.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return {"out_dir": str(out), "workbook": str(xlsx), "presentation": str(pptx), "slides": slides,
            "redacted": bool(data.get("redacted")), "redactions": data.get("redaction_count", 0),
            "evidence_rows": len(data["all_evidence"]), "voices": len(data["voices"]), "precedents": len(data["precedents"])}

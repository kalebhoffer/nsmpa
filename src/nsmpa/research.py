from __future__ import annotations

import csv
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .classify import CASE_BY_CASE, CHANGED, NEGATIVE_RELIEF, POSITIVE_RELIEF, STRICT_ARCHIVE, UPDATE_ONLY, _support_value
from .config import Settings
from .db import Database
from .extract import analyze_page
from .fetch import HardenedFetcher
from .progress import RunDashboard
from .search import SearchBroker, SearchBudgetExceeded, get_search_provider
from .utils import (
    is_blocked_social_or_aggregator,
    normalize_url,
    registrableish_domain,
    safe_snapshot_path,
    sha256_bytes,
    sha256_text,
    same_site,
)

COHORTS = {"student_media", "professional_newsroom", "support_org", "press_association", "journalism_school", "other"}

POLICY_TERMS_RE = re.compile(
    r"\b(unpublish|de[- ]?index|anonymi[sz]|take[- ]?down|content removal|editorial policy|ethics|"
    r"corrections? policy|archive policy|historical record|right to be forgotten|changed circumstances|"
    r"charges? dismissed|charges? dropped|expung|vacat|acquitt|sealed record|reputational harm)\b",
    re.I,
)
STRICT_TERMS_RE = re.compile(r"\b(do not remove|will not remove|never remove|never unpublish|archive integrity|permanent archive|historical record)\b", re.I)
CASE_TERMS_RE = re.compile(r"\b(case[- ]by[- ]case|editorial discretion|individual circumstances)\b", re.I)
CRIMINAL_TERMS_RE = re.compile(r"\b(arrest|criminal|charge|charged|conviction|plea|prosecution|defendant|suspect)\b", re.I)
VACATED_RE = re.compile(r"\b(vacat(?:e|ed|ur)|set aside|overturned conviction|withdrawn plea)\b", re.I)
TIME_RE = re.compile(r"\b(?:after|over|more than)\s+(\d{1,2})\s+years?\b", re.I)


@dataclass(frozen=True)
class QuerySpec:
    purpose: str
    topic: str
    template: str
    min_score: float = 0.28


BASE_QUERY_SPECS = [
    QuerySpec("policy", "relief", 'site:{domain} (unpublish OR deindex OR "de-index" OR takedown OR "content removal")'),
    QuerySpec("policy", "editorial_policy", 'site:{domain} ("editorial policy" OR "editorial policies" OR ethics OR standards OR "corrections policy")'),
    QuerySpec("policy", "changed_circumstances", 'site:{domain} ("charges dismissed" OR "charges dropped" OR expunged OR vacated OR acquitted OR "changed circumstances")'),
    QuerySpec("adverse", "archive_restriction", 'site:{domain} ("do not remove" OR "will not remove" OR "never unpublish" OR "historical record" OR "archive integrity")'),
    QuerySpec("precedent", "criminal_outcome", 'site:{domain} ("charges dismissed" OR "charges dropped" OR expunged OR vacated OR acquitted) (article OR story OR archive OR update)'),
    QuerySpec("precedent", "removal_request", '"{name}" ("unpublish request" OR "remove article" OR "remove story" OR deindex OR anonymize)'),
    QuerySpec("precedent", "search_prominence", 'site:{domain} (Google OR "search engine" OR "search results") (deindex OR archive OR remove OR anonymize)'),
    QuerySpec("adverse", "rejection", '"{name}" ("will not remove" OR "do not remove" OR "declined to remove" OR "refused to remove")'),
]

SUPPORT_EXTRA_SPECS = [
    QuerySpec("guidance", "ethics_guidance", 'site:{domain} (unpublishing OR deindexing OR archives OR "right to be forgotten") journalism ethics'),
    QuerySpec("guidance", "criminal_records", 'site:{domain} (arrest OR "criminal record" OR expunged OR dismissed) (privacy OR harm OR archive OR journalism)'),
    QuerySpec("guidance", "minimize_harm", 'site:{domain} ("minimize harm" OR "long-term implications" OR permanence) journalism'),
]


def create_research_run(db: Database, settings: Settings, mode: str = "full", run_id: str | None = None) -> str:
    rid = run_id or uuid.uuid4().hex
    db.execute(
        "INSERT OR IGNORE INTO research_runs(id,mode,config_json,status) VALUES(?,?,?,'running')",
        (rid, mode, settings.model_dump_json()),
    )
    db.conn.commit()
    return rid


def complete_research_run(db: Database, run_id: str, status: str = "completed") -> None:
    db.execute("UPDATE research_runs SET completed_at=CURRENT_TIMESTAMP,status=? WHERE id=?", (status, run_id))
    db.conn.commit()


def sync_student_entities(db: Database) -> dict[str, int]:
    rows = db.execute(
        """
        SELECT p.id AS publication_id,p.name AS publication,p.homepage_url,p.domain,p.confidence,
               i.unitid,i.name AS institution,i.state
        FROM publications p JOIN institutions i ON i.unitid=p.unitid
        """
    ).fetchall()
    n = 0
    with db.transaction():
        for r in rows:
            key = f"student_publication:{r['publication_id']}"
            db.conn.execute(
                """
                INSERT INTO research_entities(cohort,source_key,name,homepage_url,domain,state,parent_name,source,verification_status,metadata_json)
                VALUES('student_media',?,?,?,?,?,?, 'publications_table','synced',?)
                ON CONFLICT(cohort,source_key) DO UPDATE SET name=excluded.name,homepage_url=excluded.homepage_url,
                  domain=excluded.domain,state=excluded.state,parent_name=excluded.parent_name,metadata_json=excluded.metadata_json,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (key, r["publication"], r["homepage_url"], r["domain"], r["state"], r["institution"],
                 db.json({"unitid": r["unitid"], "publication_id": r["publication_id"], "confidence": r["confidence"]})),
            )
            n += 1
    return {"synced": n}


def import_entities_csv(db: Database, path: str | Path, cohort: str, source: str = "csv") -> dict[str, int]:
    if cohort not in COHORTS:
        raise ValueError(f"Unknown cohort {cohort!r}; choose from {sorted(COHORTS)}")
    inserted = updated = skipped = 0
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader, start=2):
            name = (row.get("name") or row.get("publication") or "").strip()
            url = normalize_url((row.get("url") or row.get("homepage_url") or "").strip())
            if not name or not url:
                skipped += 1
                continue
            source_key = (row.get("source_key") or f"{source}:{registrableish_domain(url)}:{name.lower()}").strip()
            existing = db.execute("SELECT id FROM research_entities WHERE cohort=? AND source_key=?", (cohort, source_key)).fetchone()
            metadata = {k: v for k, v in row.items() if k not in {"name", "publication", "url", "homepage_url", "source_key", "state", "parent_name"} and v}
            db.execute(
                """
                INSERT INTO research_entities(cohort,source_key,name,homepage_url,domain,state,parent_name,source,verification_status,metadata_json)
                VALUES(?,?,?,?,?,?,?,?, 'imported',?)
                ON CONFLICT(cohort,source_key) DO UPDATE SET name=excluded.name,homepage_url=excluded.homepage_url,
                  domain=excluded.domain,state=excluded.state,parent_name=excluded.parent_name,metadata_json=excluded.metadata_json,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (cohort, source_key, name, url, registrableish_domain(url), row.get("state"), row.get("parent_name"), source, db.json(metadata)),
            )
            if existing: updated += 1
            else: inserted += 1
    db.conn.commit()
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


def _score_target(entity, result, topic: str) -> tuple[float, list[str]]:
    url = normalize_url(result.url)
    if not url or is_blocked_social_or_aggregator(url):
        return 0.0, ["blocked_or_invalid"]
    hay = f"{result.title} {result.snippet} {url}"
    score = 0.0
    reasons: list[str] = []
    if entity["homepage_url"] and same_site(entity["homepage_url"], url):
        score += 0.36
        reasons.append("first_party_domain")
    else:
        score += 0.06
        reasons.append("third_party_result")
    if POLICY_TERMS_RE.search(hay):
        score += 0.30
        reasons.append("policy_terms")
    if STRICT_TERMS_RE.search(hay):
        score += 0.22
        reasons.append("restrictive_terms")
    if CASE_TERMS_RE.search(hay):
        score += 0.18
        reasons.append("case_by_case_terms")
    if topic in {"criminal_outcome", "changed_circumstances", "criminal_records"} and (CRIMINAL_TERMS_RE.search(hay) or re.search(r"dismiss|expung|vacat|acquitt|seal", hay, re.I)):
        score += 0.24
        reasons.append("criminal_outcome_terms")
    if result.rank:
        score += max(0.0, 0.10 - (result.rank - 1) * 0.012)
        reasons.append(f"rank:{result.rank}")
    if re.search(r"/tag/|/category/|/search/|\?s=", url, re.I):
        score -= 0.08
        reasons.append("listing_page_penalty")
    return max(0.0, min(score, 1.0)), reasons


def similarity_score(settings: Settings, excerpt: str, cohort: str) -> float:
    p = settings.case_profile
    score = 0.0
    possible = 0.0
    def add(weight: float, cond: bool):
        nonlocal score, possible
        possible += weight
        if cond: score += weight
    add(p.student_context_weight, cohort == "student_media" or bool(re.search(r"\b(student|campus|university|college)\b", excerpt, re.I)))
    add(p.criminal_allegation_weight, bool(CRIMINAL_TERMS_RE.search(excerpt)))
    add(p.dismissed_charges_weight, bool(re.search(r"\b(charges? (?:were |was )?(?:dismissed|dropped)|case (?:was )?dismissed)\b", excerpt, re.I)))
    add(p.vacated_conviction_weight, bool(VACATED_RE.search(excerpt)))
    add(p.sealed_or_expunged_weight, bool(re.search(r"\b(expung|seal(?:ed|ing)? record)\b", excerpt, re.I)))
    add(p.long_time_passed_weight, bool(TIME_RE.search(excerpt)))
    add(p.search_prominence_weight, bool(re.search(r"\b(search engine|google|search results?|search prominence)\b", excerpt, re.I)))
    add(p.deindex_relief_weight, bool(re.search(r"\bde[- ]?index", excerpt, re.I)))
    add(p.anonymization_weight, bool(re.search(r"\banonymi[sz]", excerpt, re.I)))
    add(p.update_context_weight, bool(UPDATE_ONLY.search(excerpt)))
    return round(100.0 * score / possible, 2) if possible else 0.0


def _store_target(db: Database, run_id: str, entity, qid: int, spec: QuerySpec, result, threshold: float) -> tuple[bool, float]:
    url = normalize_url(result.url)
    if not url:
        return False, 0.0
    score, reasons = _score_target(entity, result, spec.topic)
    if score < max(threshold, spec.min_score):
        return False, score
    db.execute(
        """
        INSERT INTO research_targets(run_id,entity_id,query_id,purpose,topic,url,domain,title,snippet,score,score_reasons_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(run_id,entity_id,purpose,url) DO UPDATE SET score=MAX(research_targets.score,excluded.score),
          title=excluded.title,snippet=excluded.snippet,query_id=excluded.query_id,score_reasons_json=excluded.score_reasons_json,
          updated_at=CURRENT_TIMESTAMP
        """,
        (run_id, entity["id"], qid, spec.purpose, spec.topic, url, registrableish_domain(url), result.title, result.snippet, score, json.dumps(reasons)),
    )
    return True, score


def _stance_from_evidence(db: Database, run_id: str, entity_id: int) -> dict:
    ev = db.execute("SELECT * FROM research_evidence WHERE run_id=? AND entity_id=?", (run_id, entity_id)).fetchall()
    pages = db.execute("SELECT * FROM research_pages WHERE run_id=? AND entity_id=?", (run_id, entity_id)).fetchall()
    queries = db.execute("SELECT COUNT(*) n FROM search_queries WHERE run_id=? AND entity_id=? AND status='completed'", (run_id, entity_id)).fetchone()["n"]
    pos = neg = changed = case = update = strict = 0
    for e in ev:
        text, tag = e["excerpt"], e["tag"]
        support = _support_value(text, tag)
        if support is not None:
            db.execute("UPDATE research_evidence SET supports_relief=? WHERE id=?", (support, e["id"]))
            pos += int(support == 1)
            neg += int(support == 0)
        changed += int(bool(CHANGED.search(text)))
        case += int(bool(CASE_BY_CASE.search(text)))
        update += int(bool(UPDATE_ONLY.search(text)))
        strict += int(bool(STRICT_ARCHIVE.search(text) or NEGATIVE_RELIEF.search(text)))
    fetched = sum(1 for p in pages if p["status"] == "fetched")
    failed = sum(1 for p in pages if p["status"] != "fetched")
    if pos and neg:
        stance, conf, rationale = "MIXED", 0.72, f"Both supportive ({pos}) and adverse ({neg}) evidence located"
    elif any(POSITIVE_RELIEF.search(e["excerpt"]) for e in ev):
        stance, conf, rationale = "SUPPORTS_RELIEF", min(0.98, 0.74 + pos * .03), "Affirmative unpublishing/deindexing/anonymization/removal relief located"
    elif changed:
        stance, conf, rationale = "SUPPORTS_CHANGED_CIRCUMSTANCES", min(0.95, .70 + changed * .03), "Guidance recognizes changed outcomes or circumstances"
    elif case:
        stance, conf, rationale = "CASE_BY_CASE", min(0.92, .68 + case * .03), "Case-by-case or discretionary review language located"
    elif strict or neg:
        stance, conf, rationale = "STRICT_ARCHIVE", min(0.96, .72 + max(strict, neg) * .03), "Restrictive archive/removal language located"
    elif update:
        stance, conf, rationale = "UPDATE_ONLY", min(0.90, .66 + update * .03), "Update/correction language found without affirmative removal relief"
    elif queries >= 4 and fetched >= 3 and failed <= fetched:
        stance, conf, rationale = "NO_RELEVANT_GUIDANCE", 0.67, "Broad targeted search completed without qualifying post-publication guidance"
    else:
        stance, conf, rationale = "UNDETERMINED", 0.60, "Insufficient fetched evidence for a defensible stance classification"
    max_sim = max([float(e["similarity_score"] or 0) for e in ev] or [0.0])
    review = stance not in {"NO_RELEVANT_GUIDANCE"} or conf < .80 or stance == "MIXED"
    db.execute(
        """
        INSERT INTO entity_stances(run_id,entity_id,stance,confidence,rationale,evidence_count,supportive_count,adverse_count,max_similarity_score,requires_human_review)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(run_id,entity_id) DO UPDATE SET stance=excluded.stance,confidence=excluded.confidence,rationale=excluded.rationale,
          evidence_count=excluded.evidence_count,supportive_count=excluded.supportive_count,adverse_count=excluded.adverse_count,
          max_similarity_score=excluded.max_similarity_score,requires_human_review=excluded.requires_human_review,created_at=CURRENT_TIMESTAMP
        """,
        (run_id, entity_id, stance, conf, rationale, len(ev), pos, neg, max_sim, int(review)),
    )
    db.conn.commit()
    return {"stance": stance, "confidence": conf, "evidence": len(ev), "max_similarity": max_sim}


async def _fetch_targets(db: Database, settings: Settings, fetcher: HardenedFetcher, run_id: str, entity, dashboard: RunDashboard | None = None) -> dict[str, int]:
    rows = db.execute(
        """
        SELECT * FROM research_targets WHERE run_id=? AND entity_id=? AND status='candidate'
        ORDER BY score DESC,id LIMIT ?
        """,
        (run_id, entity["id"], settings.research_fetch_top_targets),
    ).fetchall()
    fetched = failed = skipped = 0
    for target in rows:
        url = target["url"]
        if dashboard: dashboard.update(phase=f"fetching {target['topic']}")
        try:
            r = await fetcher.fetch(url)
            if not r.robots_allowed:
                status = "skipped_robots"
                analysis = None
                skipped += 1
                if dashboard: dashboard.increment(robots_blocked=1, skipped=1)
            elif r.status_code >= 400:
                status = "failed"
                analysis = None
                failed += 1
                if dashboard: dashboard.increment(errors=1)
            else:
                analysis = analyze_page(r.final_url, r.content, r.content_type, r.headers)
                status = "fetched"
                fetched += 1
                if dashboard: dashboard.increment(pages_fetched=1)
            snapshot_path = None
            if analysis and settings.save_html_snapshots and r.content:
                suffix = ".pdf" if r.content_type == "application/pdf" else ".html"
                sp = safe_snapshot_path(settings.research_snapshot_dir / run_id, int(entity["id"]), r.final_url, suffix)
                sp.parent.mkdir(parents=True, exist_ok=True)
                sp.write_bytes(r.content)
                snapshot_path = str(sp)
            cur = db.execute(
                """
                INSERT INTO research_pages(run_id,entity_id,target_id,requested_url,final_url,http_status,status,content_type,title,text_length,
                  content_sha256,meta_robots,x_robots_tag,noindex,policy_score,evidence_tags_json,headers_json,snapshot_path,error)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id,entity_id,requested_url) DO UPDATE SET final_url=excluded.final_url,http_status=excluded.http_status,
                  status=excluded.status,content_type=excluded.content_type,title=excluded.title,text_length=excluded.text_length,
                  content_sha256=excluded.content_sha256,meta_robots=excluded.meta_robots,x_robots_tag=excluded.x_robots_tag,
                  noindex=excluded.noindex,policy_score=excluded.policy_score,evidence_tags_json=excluded.evidence_tags_json,
                  headers_json=excluded.headers_json,snapshot_path=excluded.snapshot_path,error=excluded.error,fetched_at=CURRENT_TIMESTAMP
                """,
                (run_id, entity["id"], target["id"], url, r.final_url, r.status_code, status, r.content_type,
                 analysis.title if analysis else None, len(analysis.text) if analysis else 0, sha256_bytes(r.content) if r.content else None,
                 analysis.meta_robots if analysis else None, analysis.x_robots_tag if analysis else None, int(analysis.noindex) if analysis else 0,
                 analysis.policy_score if analysis else 0, json.dumps(analysis.evidence_tags if analysis else []), json.dumps(r.headers), snapshot_path, r.error),
            )
            page_id = int(cur.lastrowid) if cur.lastrowid else int(db.execute("SELECT id FROM research_pages WHERE run_id=? AND entity_id=? AND requested_url=?", (run_id, entity["id"], url)).fetchone()["id"])
            if analysis:
                first_party = bool(entity["homepage_url"] and same_site(entity["homepage_url"], r.final_url))
                evidence_type = "first_party_guidance" if first_party else "secondary_reporting"
                for tag, excerpt in analysis.excerpts:
                    support = _support_value(excerpt, tag)
                    sim = similarity_score(settings, excerpt, entity["cohort"])
                    db.execute(
                        """
                        INSERT OR IGNORE INTO research_evidence(run_id,entity_id,page_id,topic,tag,excerpt,source_url,evidence_type,supports_relief,confidence,similarity_score,excerpt_sha256)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (run_id, entity["id"], page_id, target["topic"], tag, excerpt, r.final_url, evidence_type, support, .92 if first_party else .68, sim, sha256_text(excerpt)),
                    )
                if dashboard and analysis.excerpts:
                    dashboard.add_recent(f"✓ {entity['name']}: {target['topic']} evidence ({len(analysis.excerpts)} excerpts)")
            db.execute("UPDATE research_targets SET status=? WHERE id=?", (status, target["id"]))
            db.conn.commit()
        except Exception as exc:
            failed += 1
            db.execute(
                "INSERT INTO research_pages(run_id,entity_id,target_id,requested_url,status,error) VALUES(?,?,?,?, 'failed',?) ON CONFLICT(run_id,entity_id,requested_url) DO UPDATE SET status='failed',error=excluded.error,fetched_at=CURRENT_TIMESTAMP",
                (run_id, entity["id"], target["id"], url, f"{type(exc).__name__}: {exc}"),
            )
            db.execute("UPDATE research_targets SET status='failed' WHERE id=?", (target["id"],))
            db.conn.commit()
            if dashboard: dashboard.increment(errors=1)
    return {"fetched": fetched, "failed": failed, "skipped": skipped}


async def research_entity(db: Database, settings: Settings, broker: SearchBroker, fetcher: HardenedFetcher, run_id: str, entity, dashboard: RunDashboard | None = None) -> dict:
    domain = entity["domain"] or (registrableish_domain(entity["homepage_url"]) if entity["homepage_url"] else "")
    if not domain:
        return {"entity_id": entity["id"], "queries": 0, "targets": 0, "stance": "UNDETERMINED"}
    specs = list(BASE_QUERY_SPECS)
    if entity["cohort"] in {"support_org", "press_association", "journalism_school"}:
        specs += SUPPORT_EXTRA_SPECS
    specs = specs[: settings.research_max_searches_per_entity]
    targets = queries = 0
    for spec in specs:
        query = spec.template.format(domain=domain, name=entity["name"])
        if dashboard: dashboard.update(phase=f"searching {spec.purpose}/{spec.topic}")
        try:
            results, qid, cached = await broker.search(query, purpose=f"research:{spec.purpose}:{spec.topic}", entity_id=int(entity["id"]))
            queries += 1
            if dashboard:
                if cached: dashboard.increment(searches_cached=1)
                else: dashboard.increment(searches_live=1, credits_estimated=0 if broker.provider.name == "none" else 1)
            for result in results:
                ok, score = _store_target(db, run_id, entity, qid, spec, result, settings.research_candidate_threshold)
                if ok: targets += 1
            db.conn.commit()
        except SearchBudgetExceeded:
            raise
        except Exception as exc:
            if dashboard: dashboard.increment(errors=1); dashboard.log(f"search error {entity['name']}: {exc}")
    fetch_stats = await _fetch_targets(db, settings, fetcher, run_id, entity, dashboard)
    stance = _stance_from_evidence(db, run_id, int(entity["id"]))
    return {"entity_id": entity["id"], "queries": queries, "targets": targets, **fetch_stats, **stance}


async def research_all(db: Database, settings: Settings, run_id: str, cohort: str | None = None, limit: int | None = None, quiet: bool = False, verbose: bool = False) -> dict:
    provider = get_search_provider(settings.search_provider, settings.user_agent)
    broker = SearchBroker(db, settings, run_id, provider)
    sql = "SELECT * FROM research_entities WHERE active=1"
    params: list = []
    if cohort:
        sql += " AND cohort=?"
        params.append(cohort)
    sql += " ORDER BY cohort,name"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    rows = db.execute(sql, tuple(params)).fetchall()
    counts: dict[str, int] = {"entities": len(rows), "completed": 0, "targets": 0, "fetched": 0, "errors": 0}
    with RunDashboard("NSMPA v0.2 Research Engine", len(rows), quiet=quiet, verbose=verbose) as dash:
        async with HardenedFetcher(settings) as fetcher:
            for idx, entity in enumerate(rows, start=1):
                dash.update(current=f"{entity['name']} [{entity['cohort']}]", phase="planning searches")
                try:
                    result = await research_entity(db, settings, broker, fetcher, run_id, entity, dash)
                    counts["targets"] += int(result.get("targets", 0))
                    counts["fetched"] += int(result.get("fetched", 0))
                    if result.get("stance") not in {"UNDETERMINED", "NO_RELEVANT_GUIDANCE"}:
                        dash.add_recent(f"★ {entity['name']} → {result.get('stance')} | similarity {result.get('max_similarity', 0):.1f}")
                except SearchBudgetExceeded as exc:
                    db.execute("UPDATE research_runs SET notes=? WHERE id=?", (str(exc), run_id)); db.conn.commit()
                    dash.add_recent(f"Search budget reached: {exc}")
                    break
                except Exception as exc:
                    counts["errors"] += 1
                    dash.increment(errors=1)
                    dash.log(f"entity failure {entity['name']}: {type(exc).__name__}: {exc}")
                counts["completed"] += 1
                db.execute("UPDATE research_runs SET entities_completed=? WHERE id=?", (counts["completed"], run_id)); db.conn.commit()
                high = db.execute("SELECT COUNT(*) n FROM entity_stances WHERE run_id=? AND stance IN ('SUPPORTS_RELIEF','SUPPORTS_CHANGED_CIRCUMSTANCES','CASE_BY_CASE')", (run_id,)).fetchone()["n"]
                target_count = db.execute("SELECT COUNT(*) n FROM research_targets WHERE run_id=?", (run_id,)).fetchone()["n"]
                dash.update(completed=idx, candidates=target_count, high_confidence=high)
    return {**counts, "provider": provider.name, "searches_live": broker.live_calls, "searches_cached": broker.cached_calls, "credits_estimated": broker.credits_used}


def export_research(db: Database, run_id: str, out_dir: str | Path) -> dict[str, int]:
    out = Path(out_dir) / f"research_{run_id}"
    out.mkdir(parents=True, exist_ok=True)
    def write_csv(name: str, sql: str, params: tuple = ()) -> int:
        cur = db.execute(sql, params); rows = cur.fetchall(); headers = [d[0] for d in cur.description or []]
        with open(out / name, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f); w.writerow(headers); [w.writerow([r[h] for h in headers]) for r in rows]
        return len(rows)
    counts = {}
    counts["entities"] = write_csv("entities.csv", "SELECT * FROM research_entities ORDER BY cohort,name")
    counts["search_queries"] = write_csv("search_queries.csv", "SELECT * FROM search_queries WHERE run_id=? ORDER BY id", (run_id,))
    counts["targets"] = write_csv("research_targets.csv", "SELECT * FROM research_targets WHERE run_id=? ORDER BY entity_id,score DESC", (run_id,))
    counts["evidence"] = write_csv("research_evidence.csv", """
        SELECT e.cohort,e.name,e.parent_name,s.stance,s.confidence AS stance_confidence,
               r.topic,r.tag,r.supports_relief,r.confidence AS evidence_confidence,r.similarity_score,r.excerpt,r.source_url
        FROM research_evidence r JOIN research_entities e ON e.id=r.entity_id
        LEFT JOIN entity_stances s ON s.entity_id=e.id AND s.run_id=r.run_id
        WHERE r.run_id=? ORDER BY r.similarity_score DESC,e.cohort,e.name
    """, (run_id,))
    counts["stances"] = write_csv("entity_stances.csv", """
        SELECT e.cohort,e.name,e.parent_name,e.homepage_url,e.source,s.stance,s.confidence,s.rationale,
               s.evidence_count,s.supportive_count,s.adverse_count,s.max_similarity_score,s.requires_human_review
        FROM entity_stances s JOIN research_entities e ON e.id=s.entity_id
        WHERE s.run_id=? ORDER BY e.cohort,s.max_similarity_score DESC,e.name
    """, (run_id,))
    counts["review"] = write_csv("human_review_queue.csv", """
        SELECT e.cohort,e.name,e.homepage_url,s.stance,s.confidence,s.rationale,s.max_similarity_score
        FROM entity_stances s JOIN research_entities e ON e.id=s.entity_id
        WHERE s.run_id=? AND s.requires_human_review=1 ORDER BY s.max_similarity_score DESC,e.cohort,e.name
    """, (run_id,))
    summary_rows = db.execute("""
        SELECT e.cohort,s.stance,COUNT(*) n FROM entity_stances s JOIN research_entities e ON e.id=s.entity_id
        WHERE s.run_id=? GROUP BY e.cohort,s.stance ORDER BY e.cohort,s.stance
    """, (run_id,)).fetchall()
    lines = ["# NSMPA v0.2 research summary", "", f"Run ID: `{run_id}`", "", "## Cohort stance counts", ""]
    current = None
    for r in summary_rows:
        if r["cohort"] != current:
            current = r["cohort"]; lines += [f"### {current}", ""]
        lines.append(f"- {r['stance']}: {r['n']}")
    lines += ["", "## Methodological guardrails", "", "Student media, professional newsrooms, and support/advice organizations are separate cohorts and must not share a denominator.", "Search-result snippets are discovery evidence only; substantive external claims require fetched source text and human verification.", "Adverse evidence is searched and retained alongside supportive evidence.", "UNDETERMINED is never converted to NO_RELEVANT_GUIDANCE merely because a source was inaccessible.", "Similarity scores rank review priority; they do not establish legal or ethical authority."]
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    return counts

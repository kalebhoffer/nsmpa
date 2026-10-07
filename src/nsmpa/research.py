"""Cross-cohort policy, precedent and guidance research engine (v0.3).

Per entity:
  1. Tier-1 searches (always): relief policy, editorial/standards policy, adverse archive language.
  2. First-party link discovery from the entity homepage (free: no search credits).
  3. Adaptive escalation to tier 2 (changed-circumstance precedents, right-to-be-forgotten /
     search-engine harm, removal-request handling, third-party coverage of the entity) when
     tier 1 shows signal, for guidance organizations, or at ``research_depth: deep``.
     Tier 3 (deep only) issues the individual exact-phrase queries.
  4. Fetch top first-party targets and a few third-party targets that name the entity.
  5. Extract sentence-level evidence, score similarity, classify stance, enqueue review.
Every entity is a checkpointed ``run_items`` row; Ctrl+C finishes in-flight entities.
"""
from __future__ import annotations

import asyncio
import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .config import Settings
from .db import Database
from .evidence import STATEMENT_RELEVANCE, extract_evidence
from .extract import extract_main_text
from .fetch import HardenedFetcher
from .progress import RunDashboard
from .review import enqueue_entity_review
from .runs import (StopController, create_or_resume_run, done_keys, finish_run, mark_item, register_items)
from .search import SearchAuthError, SearchBroker, SearchBudgetExceeded, get_search_provider
from .similarity import score_similarity
from .snapshots import store_raw, store_text
from .stance import classify_entity, store_stance
from .utils import (is_blocked_social_or_aggregator, normalize_url, prepare_request_url, registrableish_domain,
                    same_site)

COHORTS = {"student_media", "professional_newsroom", "support_org", "press_association", "journalism_school", "other"}
GUIDANCE_COHORTS = {"support_org", "press_association", "journalism_school"}

POLICY_PAGE_RE = re.compile(
    r"(?:polic(?:y|ies)|ethic|standards|guidelines|code[-_ ]of|corrections?|unpublish|takedown|take-down|removal|"
    r"remove|de-?index|anonymi|archive[-_ ]polic|editorial[-_ ]polic|handbook|bylaws|faq|principles|"
    r"right[-_ ]to[-_ ]be[-_ ]forgotten|fresh[-_ ]start|privacy)", re.I)
ABOUT_PAGE_RE = re.compile(r"(?:^|/)(?:about|about-us|who-we-are|masthead|staff|contact|mission|our-team)(?:/|$|\.)", re.I)
ARTICLE_PATH_RE = re.compile(r"/(?:19|20)\d{2}/\d{1,2}/|/\d{4}-\d{2}-\d{2}|/article_|/story/|/news/[^/]{25,}", re.I)
SIGNAL_RE = re.compile(r"\b(?:unpublish|de-?index|anonymi[sz]|take ?down|removal|remove (?:an? |the )?(?:article|story|name)|"
                       r"editorial polic|ethics|corrections polic|archive polic|historical record|case[- ]by[- ]case|"
                       r"right to be forgotten|expung|charges (?:were )?dismissed)\b", re.I)
KNOWN_JOURNALISM_DOMAINS = {
    "poynter.org", "splc.org", "rcfp.org", "niemanlab.org", "niemanreports.org", "cjr.org", "spj.org",
    "journalists.org", "americanpressinstitute.org", "rjionline.org", "studentpress.org", "collegemedia.org",
    "inn.org", "lionpublishers.com", "ap.org", "apnews.com", "nytimes.com", "washingtonpost.com", "trustingnews.org",
    "mediaengagement.org", "freedom.press", "freepress.net", "pressgazette.co.uk",
}


@dataclass(frozen=True)
class QuerySpec:
    tier: int
    purpose: str
    topic: str
    template: str
    third_party: bool = False


TIER1 = [
    QuerySpec(1, "policy", "relief",
              'site:{site} (unpublish OR unpublishing OR deindex OR "de-index" OR takedown OR "remove an article" OR anonymize OR "removal request")'),
    QuerySpec(1, "policy", "editorial_policy",
              'site:{site} ("editorial policy" OR "ethics policy" OR "corrections policy" OR "archive policy" OR "code of ethics" OR standards)'),
    QuerySpec(1, "adverse", "archive_restriction",
              'site:{site} ("never unpublish" OR "do not remove" OR "will not remove" OR "historical record" OR "archive integrity" OR "requests to remove")'),
]
TIER2 = [
    QuerySpec(2, "precedent", "changed_outcome",
              'site:{site} ("charges dismissed" OR "charges dropped" OR "case dismissed" OR expunged OR sealed OR acquitted OR exonerated OR vacated) '
              '("editor\'s note" OR update OR removed OR anonymized OR unpublished)'),
    QuerySpec(2, "policy", "digital_permanence",
              'site:{site} ("right to be forgotten" OR "digital permanence" OR "search engines" OR "reputational harm" OR "case by case") '
              '(remove OR unpublish OR archive OR name)'),
    QuerySpec(2, "adverse", "request_handling",
              'site:{site} ("requests to remove" OR "removal requests" OR "decline removal" OR embarrassment OR reputation) (remove OR unpublish OR delete)'),
    QuerySpec(2, "precedent", "third_party_practice",
              '"{name}" (unpublished OR "removed the article" OR "removed the story" OR anonymized OR deindexed OR "declined to remove" OR "refused to remove")',
              third_party=True),
]
GUIDANCE_SPECS = [
    QuerySpec(2, "guidance", "unpublishing_guidance",
              'site:{site} (unpublishing OR deindexing OR "right to be forgotten" OR "unpublish requests") journalism'),
    QuerySpec(2, "guidance", "criminal_records_guidance",
              'site:{site} (arrest OR mugshot OR "criminal record" OR expunged OR "charges dismissed") (archive OR unpublish OR update OR name)'),
    QuerySpec(2, "guidance", "minimize_harm_guidance",
              'site:{site} ("minimize harm" OR "digital permanence" OR "long-term" OR "search engine") (archive OR unpublish OR remove)'),
]
TIER3_TERMS = [
    ("policy", "unpublish"), ("policy", "unpublishing"), ("policy", "deindex"), ("policy", '"de-index"'),
    ("policy", "takedown"), ("policy", "removal"), ("policy", "anonymize"), ("policy", '"archive policy"'),
    ("policy", '"editorial policy"'), ("policy", '"corrections policy"'), ("policy", '"right to be forgotten"'),
    ("policy", '"case by case" removal'), ("policy", '"reputational harm"'), ("policy", '"digital permanence"'),
    ("policy", '"search engines"'),
    ("adverse", '"never unpublish"'), ("adverse", '"do not remove"'), ("adverse", '"historical record"'),
    ("adverse", '"archive integrity"'), ("adverse", '"requests to remove"'), ("adverse", '"decline removal"'),
    ("adverse", "embarrassment removal"), ("adverse", "reputation removal"),
    ("precedent", '"charges dismissed"'), ("precedent", '"charges dropped"'), ("precedent", '"case dismissed"'),
    ("precedent", '"conviction vacated"'), ("precedent", '"record expunged"'), ("precedent", '"record sealed"'),
    ("precedent", "acquitted"), ("precedent", "exonerated"), ("precedent", '"plea withdrawn"'),
    ("precedent", '"arrest record"'), ("precedent", '"changed circumstances"'), ("precedent", "rehabilitation"),
    ("precedent", '"request to remove"'), ("precedent", '"request to unpublish"'), ("precedent", '"request to deindex"'),
    ("precedent", '"name removed"'), ("precedent", '"name anonymized"'),
]
TIER3 = [QuerySpec(3, p, re.sub(r"\W+", "_", t.strip('"')).strip("_"), f"site:{{site}} {t}") for p, t in TIER3_TERMS]


# --------------------------------------------------------------------------- entity helpers

def entity_site(entity) -> str:
    """Host (+ path prefix for publications hosted under a university path) for site: queries."""
    url = entity["homepage_url"] or ""
    try:
        p = urlsplit(url)
    except ValueError:
        return entity["domain"] or ""
    host = (p.hostname or entity["domain"] or "").lower()
    host = host[4:] if host.startswith("www.") else host
    path = (p.path or "/").rstrip("/")
    if path and path != "/" and not re.search(r"\.(?:html?|php|aspx?|cshtml)$", path, re.I):
        return f"{host}{path}"
    return host


def is_first_party(entity, url: str) -> bool:
    home = entity["homepage_url"]
    if not home:
        return bool(entity["domain"]) and registrableish_domain(url).endswith(entity["domain"])
    if not same_site(home, url):
        return False
    try:
        hp = (urlsplit(home).path or "/").rstrip("/")
        up = urlsplit(url).path or "/"
    except ValueError:
        return False
    if hp and hp != "/" and not re.search(r"\.(?:html?|php|aspx?|cshtml)$", hp, re.I):
        # Path-scoped publication (e.g. university.edu/student-media): only pages under that path.
        return up.startswith(hp)
    return True


def entity_terms(entity) -> list[str]:
    name = (entity["name"] or "").strip()
    terms = [name]
    if name.lower().startswith("the "):
        terms.append(name[4:])
    if entity["domain"]:
        terms.append(entity["domain"])
    return [t for t in terms if len(t) >= 4]


def page_kind(url: str, title: str, is_pdf: bool, is_listing: bool) -> str:
    try:
        path = urlsplit(url).path or "/"
    except ValueError:
        path = "/"
    if path in {"", "/"}:
        return "homepage"
    if is_listing:
        return "listing"
    hay = f"{path} {title}"
    if POLICY_PAGE_RE.search(hay):
        return "policy"
    if ABOUT_PAGE_RE.search(path):
        return "about"
    if is_pdf:
        return "pdf"
    if ARTICLE_PATH_RE.search(path):
        return "article"
    return "other"


def evidence_class_for(cohort: str, first_party: bool, about_entity: bool, kind: str, statement_type: str) -> str:
    practice = statement_type.startswith("practice_")
    if first_party:
        if cohort in GUIDANCE_COHORTS:
            # Guidance orgs describing what *other* newsrooms did are precedent reports, not their own practice.
            return "secondary_report" if practice else "professional_guidance"
        if practice:
            return "documented_practice"
        return "written_policy" if kind in {"policy", "about"} else "editorial_statement"
    if about_entity:
        return "documented_practice" if practice else "secondary_report"
    return "secondary_report"


def authority_for(evidence_class: str, kind: str, url: str) -> float:
    if evidence_class == "written_policy":
        return 1.0 if kind == "policy" else 0.9
    if evidence_class == "professional_guidance":
        return 1.0 if kind == "policy" else 0.9
    if evidence_class == "editorial_statement":
        return 0.8
    if evidence_class == "documented_practice":
        return 0.85
    dom = registrableish_domain(url)
    if any(dom == d or dom.endswith("." + d) for d in KNOWN_JOURNALISM_DOMAINS):
        return 0.6
    if dom.endswith(".edu") or dom.endswith(".gov"):
        return 0.5
    return 0.35


# --------------------------------------------------------------------------- universe management

def _record_source(db: Database, entity_id: int, source: str, source_key: str, url: str | None, raw: dict,
                   membership_label: str | None = None) -> None:
    db.execute(
        "INSERT INTO entity_sources(entity_id,source,source_key,source_url,membership_label,raw_json) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(entity_id,source,source_key) DO UPDATE SET source_url=excluded.source_url,raw_json=excluded.raw_json,"
        "membership_label=COALESCE(excluded.membership_label,entity_sources.membership_label),imported_at=CURRENT_TIMESTAMP",
        (entity_id, source, source_key, url, membership_label, json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)),
    )


def sync_student_entities(db: Database) -> dict[str, int]:
    rows = db.execute(
        """
        SELECT p.id AS publication_id,p.name AS publication,p.homepage_url,p.domain,p.confidence,p.verification_status,
               p.relationship,p.discovery_method,p.ambiguous,i.unitid,i.name AS institution,i.state
        FROM publications p JOIN institutions i ON i.unitid=p.unitid WHERE p.is_primary=1
        """
    ).fetchall()
    n = 0
    with db.transaction():
        for r in rows:
            key = f"student_publication:{r['publication_id']}"
            meta = {"unitid": r["unitid"], "publication_id": r["publication_id"], "confidence": r["confidence"],
                    "relationship": r["relationship"], "discovery_method": r["discovery_method"], "ambiguous": r["ambiguous"]}
            db.conn.execute(
                """
                INSERT INTO research_entities(cohort,source_key,name,homepage_url,domain,state,parent_name,source,verification_status,metadata_json,entity_type)
                VALUES('student_media',?,?,?,?,?,?,'publications_table',?,?,'student_publication')
                ON CONFLICT(cohort,source_key) DO UPDATE SET name=excluded.name,homepage_url=excluded.homepage_url,
                  domain=excluded.domain,state=excluded.state,parent_name=excluded.parent_name,metadata_json=excluded.metadata_json,
                  verification_status=excluded.verification_status,updated_at=CURRENT_TIMESTAMP
                """,
                (key, r["publication"], r["homepage_url"], r["domain"], r["state"], r["institution"],
                 r["verification_status"] or "auto", db.json(meta)),
            )
            eid = db.conn.execute("SELECT id FROM research_entities WHERE cohort='student_media' AND source_key=?", (key,)).fetchone()[0]
            _record_source(db, eid, "publications_table", key, r["homepage_url"], meta)
            n += 1
    return {"synced": n}


def import_entities_csv(db: Database, path: str | Path, cohort: str, source: str = "csv",
                        membership_label: str | None = None) -> dict[str, int]:
    """Import a directory/membership list. Rows whose domain already exists in the cohort are
    merged into the existing entity as an additional provenance source (no duplicate entity)."""
    if cohort not in COHORTS:
        raise ValueError(f"Unknown cohort {cohort!r}; choose from {sorted(COHORTS)}")
    inserted = merged = updated = skipped = 0
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
            name = row.get("name") or row.get("publication") or ""
            raw_url = row.get("url") or row.get("homepage_url") or row.get("website") or ""
            url = prepare_request_url(raw_url if raw_url.startswith("http") else f"https://{raw_url}") if raw_url else None
            if not name or not url:
                skipped += 1
                continue
            domain = registrableish_domain(url)
            source_key = row.get("source_key") or f"{source}:{domain}:{name.lower()}"
            metadata = {k: v for k, v in row.items() if k not in {"name", "publication", "url", "homepage_url", "website",
                                                                  "source_key", "state", "parent_name"} and v}
            by_key = db.execute("SELECT id FROM research_entities WHERE cohort=? AND source_key=?", (cohort, source_key)).fetchone()
            by_domain = None if by_key else db.execute(
                "SELECT id FROM research_entities WHERE cohort=? AND domain=? AND merged_into IS NULL ORDER BY id LIMIT 1",
                (cohort, domain)).fetchone()
            if by_domain:
                _record_source(db, int(by_domain["id"]), source, source_key, url, row, membership_label)
                merged += 1
                continue
            db.execute(
                """
                INSERT INTO research_entities(cohort,source_key,name,homepage_url,domain,state,parent_name,source,verification_status,metadata_json)
                VALUES(?,?,?,?,?,?,?,?, 'imported',?)
                ON CONFLICT(cohort,source_key) DO UPDATE SET name=excluded.name,homepage_url=excluded.homepage_url,
                  domain=excluded.domain,state=excluded.state,parent_name=excluded.parent_name,metadata_json=excluded.metadata_json,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (cohort, source_key, name, url, domain, row.get("state") or None, row.get("parent_name") or None, source, db.json(metadata)),
            )
            eid = db.execute("SELECT id FROM research_entities WHERE cohort=? AND source_key=?", (cohort, source_key)).fetchone()["id"]
            _record_source(db, int(eid), source, source_key, url, row, membership_label)
            if by_key:
                updated += 1
            else:
                inserted += 1
    db.conn.commit()
    return {"inserted": inserted, "updated": updated, "merged_as_additional_source": merged, "skipped": skipped}


def merge_duplicate_entities(db: Database, cohort: str | None = None) -> dict[str, int]:
    """Merge active entities sharing a domain within a cohort; provenance moves to the survivor."""
    sql = ("SELECT cohort, domain, MIN(id) keep, GROUP_CONCAT(id) ids, COUNT(*) n FROM research_entities "
           "WHERE active=1 AND merged_into IS NULL AND domain IS NOT NULL AND domain!=''")
    params: list = []
    if cohort:
        sql += " AND cohort=?"
        params.append(cohort)
    sql += " GROUP BY cohort, domain HAVING COUNT(*)>1"
    merged = 0
    with db.transaction():
        for r in db.execute(sql, params).fetchall():
            keep = int(r["keep"])
            for other in (int(x) for x in str(r["ids"]).split(",") if int(x) != keep):
                db.conn.execute("UPDATE OR IGNORE entity_sources SET entity_id=? WHERE entity_id=?", (keep, other))
                db.conn.execute("UPDATE research_entities SET active=0, merged_into=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (keep, other))
                merged += 1
    return {"merged": merged}


# --------------------------------------------------------------------------- legacy shims

def create_research_run(db: Database, settings: Settings, mode: str = "full", run_id: str | None = None) -> str:
    rid, _ = create_or_resume_run(db, settings, mode, run_id)
    return rid


def complete_research_run(db: Database, run_id: str, status: str = "completed") -> None:
    finish_run(db, run_id, status)


def similarity_score(settings: Settings, excerpt: str, cohort: str) -> float:
    return score_similarity(settings.case_profile, excerpt, cohort=cohort).score


# --------------------------------------------------------------------------- per-entity research

def plan_queries(settings: Settings, entity, tier: int) -> list[QuerySpec]:
    if tier == 1:
        return list(TIER1)
    if tier == 2:
        specs = list(TIER2)
        if entity["cohort"] in GUIDANCE_COHORTS:
            specs = [s for s in specs if not s.third_party] + GUIDANCE_SPECS
        return specs
    return list(TIER3)


def render_query(spec: QuerySpec, entity) -> str:
    name = re.sub(r'["“”]', "", entity["name"] or "").strip()
    return spec.template.format(site=entity_site(entity), name=name)


def score_target(entity, url: str, title: str, snippet: str, rank: int, spec: QuerySpec | None) -> tuple[float, list[str]]:
    reasons: list[str] = []
    if is_blocked_social_or_aggregator(url):
        return 0.0, ["blocked_domain"]
    hay = f"{title} {snippet} {url}"
    fp = is_first_party(entity, url)
    score = 0.0
    if fp:
        score += 0.4
        reasons.append("first_party")
    else:
        mentions = any(t.lower() in hay.lower() for t in entity_terms(entity))
        if not mentions:
            return 0.0, ["third_party_not_about_entity"]
        score += 0.15
        reasons.append("third_party_mentions_entity")
    if SIGNAL_RE.search(hay):
        score += 0.3
        reasons.append("policy_signal")
    kind = page_kind(url, title, url.lower().endswith(".pdf"), False)
    if kind == "policy":
        score += 0.2
        reasons.append("policy_page")
    if spec and spec.purpose == "precedent":
        score += 0.05
    if rank:
        score += max(0.0, 0.08 - (rank - 1) * 0.01)
        reasons.append(f"rank:{rank}")
    if re.search(r"/(?:tag|tags|category|author|search|page/\d+)(?:/|$)|[?&](?:s|q)=", url, re.I):
        score -= 0.25
        reasons.append("listing_page_penalty")
    return round(max(0.0, min(score, 1.0)), 3), reasons


class EntityResearcher:
    def __init__(self, db: Database, settings: Settings, broker: SearchBroker, fetcher: HardenedFetcher, run_id: str,
                 dash: RunDashboard, stop: StopController):
        self.db, self.settings, self.broker, self.fetcher = db, settings, broker, fetcher
        self.run_id, self.dash, self.stop = run_id, dash, stop
        self.snapshot_root = settings.research_snapshot_dir

    # ---------------------------------------------------------------- targets
    def _store_target(self, entity, url: str, title: str, snippet: str, rank: int, spec: QuerySpec | None,
                      query_id: int | None, purpose: str, topic: str) -> bool:
        req = prepare_request_url(url)
        key = normalize_url(url)
        if not req or not key:
            return False
        score, reasons = score_target(entity, req, title, snippet, rank, spec)
        if score < self.settings.research_candidate_threshold:
            self.dash.log(f"  reject target {score:.2f} {req} {reasons}")
            return False
        self.db.execute(
            """
            INSERT INTO research_targets(run_id,entity_id,query_id,purpose,topic,url,domain,title,snippet,score,score_reasons_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id,entity_id,purpose,url) DO UPDATE SET score=MAX(research_targets.score,excluded.score),
              title=excluded.title,snippet=excluded.snippet,score_reasons_json=excluded.score_reasons_json,updated_at=CURRENT_TIMESTAMP
            """,
            (self.run_id, entity["id"], query_id, purpose, topic, req, registrableish_domain(req), title[:500], snippet[:2000],
             score, json.dumps(reasons)),
        )
        return True

    async def _search_tier(self, entity, specs: list[QuerySpec], budget_left: int) -> tuple[int, bool]:
        used = 0
        signal = False
        for spec in specs:
            if used >= budget_left or self.stop.force:
                break
            query = render_query(spec, entity)
            purpose = f"research:t{spec.tier}:{spec.purpose}:{spec.topic}"
            self.dash.update(phase=f"search t{spec.tier} {spec.purpose}/{spec.topic}")
            self.dash.log(f"  query [{purpose}] {query}")
            results, qid, cached = await self.broker.search(query, purpose=purpose, entity_id=int(entity["id"]))
            used += 1
            self._sync_search_counters()
            for r in results:
                if self._store_target(entity, r.url, r.title, r.snippet, r.rank, spec, qid or None, spec.purpose, spec.topic):
                    if is_first_party(entity, r.url) and SIGNAL_RE.search(f"{r.title} {r.snippet} {r.url}"):
                        signal = True
            self.db.conn.commit()
        return used, signal

    def _sync_search_counters(self) -> None:
        b = self.broker
        self.dash.update(searches_live=b.live_calls, searches_cached=b.cached_calls, searches_failed=b.failed_calls,
                         credits_estimated=b.credits_used)

    def _sync_fetch_counters(self) -> None:
        st = self.fetcher.stats
        self.dash.update(robots_blocked=st.robots_blocked, access_blocked=st.access_blocked,
                         malformed_skipped=st.malformed_skipped, retries=st.retries)

    async def _homepage_links(self, entity) -> None:
        """Free first-party discovery: homepage plus policy/about links found on it."""
        home = prepare_request_url(entity["homepage_url"] or "")
        if not home:
            return
        self.dash.update(phase="homepage + policy links")
        self._store_target_direct(entity, home, "homepage", 0.9)
        r = await self.fetcher.fetch_safe(home)
        await self._record_page(entity, r, target_id=None, query_id=None, topic="homepage", forced_kind="homepage")
        if r.access_class != "ok":
            return
        page = extract_main_text(r.content, r.content_type, r.final_url, r.headers)
        picked = 0
        for text, href in page.links:
            if picked >= self.settings.research_first_party_link_pages:
                break
            if not is_first_party(entity, href) or href == r.final_url:
                continue
            if POLICY_PAGE_RE.search(f"{text} {urlsplit(href).path}") or ABOUT_PAGE_RE.search(urlsplit(href).path):
                if self._store_target_direct(entity, href, "first_party_link", 0.85 if POLICY_PAGE_RE.search(text + href) else 0.6):
                    picked += 1

    def _store_target_direct(self, entity, url: str, topic: str, score: float) -> bool:
        req = prepare_request_url(url)
        if not req:
            return False
        cur = self.db.execute(
            "INSERT OR IGNORE INTO research_targets(run_id,entity_id,purpose,topic,url,domain,score,score_reasons_json,status) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (self.run_id, entity["id"], "first_party_discovery", topic, req, registrableish_domain(req), score,
             json.dumps(["first_party", topic]), "fetched" if topic == "homepage" else "candidate"))
        return cur.rowcount > 0

    async def _fetch_targets(self, entity) -> None:
        rows = self.db.execute(
            "SELECT * FROM research_targets WHERE run_id=? AND entity_id=? AND status='candidate' ORDER BY score DESC, id",
            (self.run_id, entity["id"])).fetchall()
        seen: set[str] = set(r["requested_url"] for r in self.db.execute(
            "SELECT requested_url FROM research_pages WHERE run_id=? AND entity_id=?", (self.run_id, entity["id"])))
        fp_budget = self.settings.research_fetch_top_targets
        tp_budget = self.settings.research_third_party_fetch_limit
        for t in rows:
            if self.stop.force:
                break
            first = is_first_party(entity, t["url"])
            if t["url"] in seen:
                self.db.execute("UPDATE research_targets SET status='duplicate' WHERE id=?", (t["id"],))
                continue
            if first and fp_budget <= 0 or (not first and tp_budget <= 0):
                self.db.execute("UPDATE research_targets SET status='not_fetched_budget' WHERE id=?", (t["id"],))
                continue
            seen.add(t["url"])
            if first:
                fp_budget -= 1
            else:
                tp_budget -= 1
            self.dash.update(phase=f"fetch {t['topic']}")
            self.dash.log(f"  fetch [{t['score']:.2f}] {t['url']}")
            r = await self.fetcher.fetch_safe(t["url"])
            status = await self._record_page(entity, r, target_id=t["id"], query_id=t["query_id"], topic=t["topic"])
            self.db.execute("UPDATE research_targets SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, t["id"]))
            self.db.conn.commit()
            self._sync_fetch_counters()

    async def _record_page(self, entity, r, *, target_id: int | None, query_id: int | None, topic: str,
                           forced_kind: str | None = None) -> str:
        first = is_first_party(entity, r.final_url or r.requested_url)
        ok = r.access_class == "ok" and bool(r.content)
        page = extract_main_text(r.content, r.content_type, r.final_url, r.headers) if ok else None
        kind = forced_kind or (page_kind(r.final_url, page.title, page.is_pdf, page.is_listing) if page else "unknown")
        raw_sha = text_sha = snap_path = None
        if page and self.settings.save_html_snapshots:
            raw_sha, snap_path = store_raw(self.db, self.snapshot_root, r.content, r.content_type, r.final_url)
            if page.main_text:
                text_sha = store_text(self.db, self.snapshot_root, page.main_text, r.final_url)
        if not ok:
            self.dash.increment(errors=1 if r.access_class not in {"robots_disallowed", "not_found", "gone"} else 0)
            self.dash.log(f"  ! {r.access_class} {r.requested_url} {r.error or ''}")
        else:
            self.dash.increment(pages_fetched=1)
        self.db.execute(
            """
            INSERT INTO research_pages(run_id,entity_id,target_id,requested_url,final_url,http_status,status,content_type,title,
              text_length,content_sha256,meta_robots,x_robots_tag,noindex,policy_score,evidence_tags_json,headers_json,snapshot_path,
              error,canonical_url,access_class,page_kind,text_sha256,redirect_chain_json,query_id,first_party)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id,entity_id,requested_url) DO UPDATE SET final_url=excluded.final_url,http_status=excluded.http_status,
              status=excluded.status,content_type=excluded.content_type,title=excluded.title,text_length=excluded.text_length,
              content_sha256=excluded.content_sha256,meta_robots=excluded.meta_robots,x_robots_tag=excluded.x_robots_tag,
              noindex=excluded.noindex,headers_json=excluded.headers_json,snapshot_path=excluded.snapshot_path,error=excluded.error,
              canonical_url=excluded.canonical_url,access_class=excluded.access_class,page_kind=excluded.page_kind,
              text_sha256=excluded.text_sha256,redirect_chain_json=excluded.redirect_chain_json,first_party=excluded.first_party,
              fetched_at=CURRENT_TIMESTAMP
            """,
            (self.run_id, entity["id"], target_id, r.requested_url, r.final_url, r.status_code or None,
             "fetched" if ok else "failed", r.content_type, page.title if page else None,
             len(page.main_text) if page else 0, raw_sha, page.meta_robots if page else None, r.headers.get("x-robots-tag"),
             int(page.noindex) if page else 0, 0.0, "[]",
             json.dumps({k: v for k, v in r.headers.items() if k in {"content-type", "x-robots-tag", "last-modified", "server", "link"}}),
             snap_path, r.error, page.canonical_url if page else None, r.access_class, kind, text_sha,
             json.dumps(r.redirect_chain), query_id, int(first)),
        )
        page_id = self.db.execute("SELECT id FROM research_pages WHERE run_id=? AND entity_id=? AND requested_url=?",
                                  (self.run_id, entity["id"], r.requested_url)).fetchone()["id"]
        if page and page.main_text:
            n_useful = self._store_evidence(entity, page, int(page_id), query_id, topic, first, kind, raw_sha, text_sha)
            if n_useful:
                self.broker.mark_useful(query_id)
        return "fetched" if ok else r.access_class

    def _store_evidence(self, entity, page, page_id: int, query_id: int | None, topic: str, first: bool, kind: str,
                        raw_sha: str | None, text_sha: str | None) -> int:
        items = extract_evidence(page.main_text, entity_terms=entity_terms(entity),
                                 max_items=self.settings.research_max_evidence_per_page,
                                 require_entity_mention=not first)
        profile = self.settings.case_profile
        useful = 0
        fetched_at = self.db.scalar("SELECT fetched_at FROM research_pages WHERE id=?", (page_id,), None)
        for it in items:
            st = it.statement
            low = it.context.lower()
            about = first or any(t.lower() in low for t in entity_terms(entity))
            ev_class = evidence_class_for(entity["cohort"], first, about and not first, kind, st.statement_type)
            if ev_class == "secondary_report" and entity["cohort"] in GUIDANCE_COHORTS and first:
                about = False  # a guidance org describing another newsroom's decision
            authority = authority_for(ev_class, kind, page.url)
            sim = score_similarity(profile, it.excerpt, it.context, cohort=entity["cohort"] if first else None)
            dup = self.db.execute(
                "SELECT id FROM evidence_items WHERE run_id=? AND entity_id=? AND near_dup_key=? AND duplicate_of IS NULL "
                "AND excerpt_sha256!=? ORDER BY id LIMIT 1",
                (self.run_id, entity["id"], it.near_dup_key, it.excerpt_sha256)).fetchone()
            cur = self.db.execute(
                """
                INSERT OR IGNORE INTO evidence_items(run_id,entity_id,page_id,query_id,cohort,source_url,source_title,source_domain,
                  fetched_at,page_sha256,text_sha256,excerpt,context,excerpt_sha256,near_dup_key,duplicate_of,first_party,about_entity,
                  evidence_class,statement_type,direction,topic,authority_score,relevance_score,similarity_score,similarity_factors_json,
                  extraction_confidence,rationale)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (self.run_id, entity["id"], page_id, query_id, entity["cohort"], page.url, page.title[:500],
                 registrableish_domain(page.url), fetched_at, raw_sha, text_sha, it.excerpt, it.context, it.excerpt_sha256,
                 it.near_dup_key, dup["id"] if dup else None, int(first), int(about), ev_class, st.statement_type, st.direction,
                 topic, authority, STATEMENT_RELEVANCE[st.statement_type], sim.score, json.dumps(sim.factors),
                 st.confidence, "; ".join(st.cues)[:500]),
            )
            if cur.rowcount and cur.lastrowid:
                self.db.conn.executemany("INSERT OR IGNORE INTO evidence_item_tags(evidence_id,tag) VALUES(?,?)",
                                         [(cur.lastrowid, t) for t in st.tags])
                if not dup:
                    self.dash.increment(evidence_unique=1)
                    if sim.score >= profile.high_similarity_threshold:
                        self.dash.increment(high_similarity=1)
                if st.statement_type != "mention":
                    useful += 1
        return useful

    async def research(self, entity, budget_per_entity: int) -> dict:
        eid = int(entity["id"])
        if not entity_site(entity):
            return {"stance": "UNDETERMINED", "reason": "no homepage/domain"}
        depth = self.settings.research_depth
        searching = self.broker.provider.name != "none"
        used, signal = 0, False
        if searching:
            used, signal = await self._search_tier(entity, plan_queries(self.settings, entity, 1), budget_per_entity)
        await self._homepage_links(entity)
        escalate = searching and (depth == "deep" or signal or entity["cohort"] in GUIDANCE_COHORTS)
        if depth != "quick" and escalate and not self.stop.force:
            u2, _ = await self._search_tier(entity, plan_queries(self.settings, entity, 2), budget_per_entity - used)
            used += u2
        if searching and depth == "deep" and not self.stop.force:
            u3, _ = await self._search_tier(entity, plan_queries(self.settings, entity, 3), budget_per_entity - used)
            used += u3
        await self._fetch_targets(entity)
        self.dash.update(phase="classifying")
        result = classify_entity(self.db, self.settings, self.run_id, entity)
        store_stance(self.db, self.run_id, eid, result)
        enqueue_entity_review(self.db, self.run_id, entity, result)
        self.db.conn.commit()
        return {"stance": result.stance, "confidence": result.confidence, "queries": used, "escalated": escalate,
                "max_similarity": result.max_similarity, "supportive": result.supportive, "adverse": result.adverse}


STANCE_BADGE = {
    "SUPPORTS_RELIEF": "✓ Relief policy", "SUPPORTS_CHANGED_CIRCUMSTANCES": "✓ Changed-circumstance policy",
    "CASE_BY_CASE": "✓ Case-by-case review", "UPDATE_ONLY": "· Update-only policy", "STRICT_ARCHIVE": "! Strict archive policy",
    "MIXED": "? Mixed signals",
}


def select_entities(db: Database, cohort: str | None, limit: int | None, entity_ids: list[int] | None = None) -> list:
    sql = "SELECT * FROM research_entities WHERE active=1 AND merged_into IS NULL"
    params: list = []
    if cohort:
        sql += " AND cohort=?"
        params.append(cohort)
    if entity_ids:
        sql += f" AND id IN ({','.join('?' * len(entity_ids))})"
        params += entity_ids
    sql += " ORDER BY cohort, id"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return db.execute(sql, params).fetchall()


async def research_all(db: Database, settings: Settings, run_id: str, cohort: str | None = None, limit: int | None = None,
                       quiet: bool = False, verbose: bool = False, *, max_searches: int | None = None,
                       refresh_search: bool = False, fresh: bool = False, entity_ids: list[int] | None = None,
                       provider=None, fetcher: HardenedFetcher | None = None, stop: StopController | None = None,
                       dashboard: RunDashboard | None = None) -> dict:
    provider = provider or get_search_provider(settings.search_provider, settings.user_agent)
    broker = SearchBroker(db, settings, run_id, provider, max_searches=max_searches, refresh=refresh_search)
    rows = select_entities(db, cohort, limit, entity_ids)
    keys = [str(r["id"]) for r in rows]
    register_items(db, run_id, "entity", keys)
    if fresh:
        db.execute("UPDATE run_items SET status='pending' WHERE run_id=? AND item_type='entity'", (run_id,))
        db.conn.commit()
    completed = done_keys(db, run_id, "entity")
    todo = [r for r in rows if str(r["id"]) not in completed]
    stop = stop or StopController()
    universe = {"student_media": "Student Journalism", "professional_newsroom": "Professional Newsrooms",
                "support_org": "Support / Standards Organizations"}.get(cohort or "", cohort or "All cohorts")
    dash = dashboard or RunDashboard("NSMPA National Research", len(rows), quiet=quiet, verbose=verbose, universe=universe)
    counts = {"entities": len(rows), "already_done": len(rows) - len(todo), "completed": 0, "failed": 0}
    stop_reason = ""
    own_fetcher = fetcher is None
    fetcher = fetcher or HardenedFetcher(settings, on_event=dash.log)
    queue: asyncio.Queue = asyncio.Queue()
    for r in todo:
        queue.put_nowait(r)
    per_entity = settings.research_max_searches_per_entity
    with dash:
        dash.update(completed=len(rows) - len(todo), skipped_done=len(rows) - len(todo), budget_limit=max_searches)
        stop.on_stop(dash.notice)
        uninstall = stop.install()
        researcher = EntityResearcher(db, settings, broker, fetcher, run_id, dash, stop)
        active: dict[int, str] = {}

        async def worker(wid: int) -> None:
            nonlocal stop_reason
            while not stop.stop_requested and not queue.empty():
                entity = queue.get_nowait()
                key = str(entity["id"])
                active[wid] = entity["name"]
                dash.update(current=f"{entity['name']}", publication=entity["parent_name"] or "", active=len(active))
                mark_item(db, run_id, "entity", key, "running")
                try:
                    res = await researcher.research(entity, per_entity)
                    mark_item(db, run_id, "entity", key, "done", result=res)
                    counts["completed"] += 1
                    dash.stance(res["stance"])
                    if res["stance"] not in {"UNDETERMINED", "NO_RELEVANT_GUIDANCE"}:
                        dash.increment(policies_found=1)
                        dash.add_recent(f"{STANCE_BADGE.get(res['stance'], res['stance'])}  {entity['name']}")
                except SearchBudgetExceeded as exc:
                    mark_item(db, run_id, "entity", key, "pending", error=str(exc))
                    stop_reason = f"budget_exhausted: {exc}"
                    stop.stop_requested = True
                    dash.notice(f"Search budget reached ({exc}). Finishing in-flight entities and checkpointing.")
                except SearchAuthError as exc:
                    mark_item(db, run_id, "entity", key, "pending", error=str(exc))
                    stop_reason = f"search_auth_error: {exc}"
                    stop.stop_requested = True
                    dash.notice(f"Search provider rejected the API key/account: {exc}")
                except asyncio.CancelledError:
                    mark_item(db, run_id, "entity", key, "pending", error="cancelled")
                    raise
                except Exception as exc:  # one broken site never terminates the run
                    counts["failed"] += 1
                    mark_item(db, run_id, "entity", key, "failed", error=f"{type(exc).__name__}: {exc}"[:500])
                    db.execute("INSERT INTO errors(research_run_id,entity_id,stage,url,error_type,message,retryable) VALUES(?,?,?,?,?,?,1)",
                               (run_id, entity["id"], "research_entity", entity["homepage_url"], type(exc).__name__, str(exc)[:1000]))
                    db.conn.commit()
                    dash.increment(errors=1)
                    dash.log(f"entity failure {entity['name']}: {type(exc).__name__}: {exc}")
                finally:
                    active.pop(wid, None)
                    dash.increment(completed=1)
                    dash.update(active=len(active))
                    dash.checkpoint()

        tasks = [asyncio.create_task(worker(i)) for i in range(min(settings.research_concurrency, max(1, len(todo))))]
        for t in tasks:
            stop.track(t)
        try:
            await asyncio.gather(*tasks, return_exceptions=False)
        except asyncio.CancelledError:
            stop_reason = stop_reason or "force-cancelled by user"
        finally:
            dash.update(phase="stopped" if stop.stop_requested else "complete", current="-", active=0)
            uninstall()
            if own_fetcher:
                await fetcher.close()
            await broker.aclose()
    remaining = len(rows) - len(done_keys(db, run_id, "entity"))
    if stop_reason.startswith("budget"):
        status = "budget_exhausted"
    elif stop_reason.startswith("search_auth"):
        status = "failed"
    elif stop.stop_requested and remaining:
        status, stop_reason = "interrupted", stop_reason or stop.reason
    else:
        status = "completed"
    finish_run(db, run_id, status, stop_reason or None)
    return {**counts, "remaining": remaining, "status": status, "stop_reason": stop_reason, "provider": provider.name,
            "searches_live": broker.live_calls, "searches_cached": broker.cached_calls, "searches_failed": broker.failed_calls,
            "credits_estimated": broker.credits_used, "run_credits_total": broker.run_credits_total,
            "pages_fetched": fetcher.stats.fetched_ok, "robots_blocked": fetcher.stats.robots_blocked}


def start_research(db: Database, settings: Settings, *, cohort: str | None, limit: int | None, run_id: str | None,
                   max_searches: int | None, command: str) -> tuple[str, bool]:
    params = {"cohort": cohort, "limit": limit, "depth": settings.research_depth}
    return create_or_resume_run(db, settings, "full_research", run_id, params=params, command=command, max_searches=max_searches)


from .export import export_research  # noqa: E402,F401  (re-export for backwards compatibility)

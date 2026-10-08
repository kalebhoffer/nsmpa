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
from .evidence import (
    STATEMENT_DIRECTION,
    STATEMENT_RELEVANCE,
    action_positions,
    extract_evidence,
    extract_voices,
    person_key,
)
from .extract import extract_main_text
from .fetch import HardenedFetcher
from .identity import check_identity
from .practice import choose_samples, site_article_urls
from .progress import RunDashboard
from .review import enqueue_entity_review
from .runs import StopController, create_or_resume_run, done_keys, finish_run, mark_item, register_items
from .search import SearchAuthError, SearchBroker, SearchBudgetExceeded, get_search_provider
from .similarity import score_case_match, score_similarity
from .snapshots import store_raw, store_text
from .stance import classify_entity, store_stance
from .utils import (
    is_blocked_social_or_aggregator,
    json_meta,
    normalize_for_hash,
    normalize_url,
    prepare_request_url,
    registrableish_domain,
    same_site,
    sha256_text,
)
from .wayback import wayback_for_entity

COHORTS = {"student_media", "professional_newsroom", "broadcast_newsroom", "support_org", "press_association",
           "journalism_school", "other"}
# Lead lists, not populations: never part of any denominator and skipped by `research` unless named explicitly.
SEED_COHORTS = {"expert", "precedent_case"}
GUIDANCE_COHORTS = {"support_org", "press_association", "journalism_school"}

POLICY_PAGE_RE = re.compile(
    r"(?:polic(?:y|ies)|ethic|standards|guidelines|code[-_ ]of|corrections?|unpublish|takedown|take-down|removal|"
    r"remove|de-?index|anonymi|archive[-_ ]polic|editorial[-_ ]polic|handbook|bylaws|faq|principles|"
    r"right[-_ ]to[-_ ]be[-_ ]forgotten|fresh[-_ ]start)", re.I)
# Website legal boilerplate (data privacy, cookies, terms of use) is about the site's handling of user data, not about
# journalism: never treated as editorial policy evidence.
LEGAL_BOILERPLATE_RE = re.compile(
    r"(?:privacy|cookie|terms[-_ ]of[-_ ](?:use|service)|terms[-_ ]and[-_ ]conditions|\bterms\b|gdpr|ccpa|do[-_ ]not[-_ ]sell|"
    r"accessibility|disclaimer|copyright[-_ ]policy|dmca|user[-_ ]agreement|advertis)", re.I)
ABOUT_PAGE_RE = re.compile(r"(?:^|/)(?:about|about-us|who-we-are|masthead|staff|contact|mission|our-team)(?:/|$|\.)", re.I)
ARTICLE_PATH_RE = re.compile(r"/(?:19|20)\d{2}/\d{1,2}/|/\d{4}-\d{2}-\d{2}|/article_|/story/|/news/[^/]{25,}", re.I)
SIGNAL_RE = re.compile(r"\b(?:unpublish\w*|de-?index\w*|anonymi[sz]\w*|take ?downs?|removal|"
                       r"(?:remove|delete)[sd]? (?:an? |the |old |our )?(?:articles?|stor(?:y|ies)|names?|content|posts?)|"
                       r"editorial polic\w*|ethics|corrections polic\w*|archive polic\w*|historical record|case[- ]by[- ]case|"
                       r"right to be forgotten|expung\w*|charges (?:were )?(?:dismissed|dropped)|do not remove|never unpublish|"
                       r"editor'?s note|from the editor)\b", re.I)
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
              'site:{site} (unpublish OR unpublishing OR deindex OR "de-index" OR "de-indexed" OR takedown OR "remove an article" OR '
              'anonymize OR "removal request" OR "fresh start" OR "right to be forgotten" OR "old crime stories" OR "remove old")'),
    QuerySpec(1, "policy", "editorial_policy",
              'site:{site} ("editorial policy" OR "ethics policy" OR "corrections policy" OR "archive policy" OR "code of ethics" OR '
              'standards OR "publishing principles" OR "reporting policy" OR "community guide" OR "crime coverage" OR guidelines)'),
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
PRACTICE_SPECS = [
    QuerySpec(4, "practice", "editors_note_outcome",
              'site:{site} "editor\'s note" ("charges were dismissed" OR "charges were dropped" OR acquitted OR expunged OR "found not guilty")'),
    QuerySpec(4, "practice", "name_removed_note",
              'site:{site} ("name has been removed" OR "removed the name" OR "no longer identifies" OR "has been updated to remove" OR "at the request of")'),
    QuerySpec(4, "practice", "updated_outcome",
              'site:{site} ("this story has been updated" OR "this article has been updated") (dismissed OR dropped OR acquitted OR cleared OR expunged)'),
    QuerySpec(4, "practice", "unpublish_deindex_note",
              'site:{site} (unpublished OR "removed this story" OR "removed this article" OR "de-indexed" OR "search engines") editor'),
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


def alt_domains(entity) -> list[str]:
    """Other domains the organization itself publishes on (metadata ``alt_domains``), e.g. ap.org for AP News."""
    try:
        meta = json_meta(entity)
    except (KeyError, IndexError, TypeError, ValueError):
        return []
    return [d.lower().removeprefix("www.") for d in meta.get("alt_domains") or [] if d]


def is_first_party(entity, url: str) -> bool:
    dom = registrableish_domain(url)
    if dom and any(dom == d or dom.endswith("." + d) for d in alt_domains(entity)):
        return True
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
    for part in [name, *re.split(r"\s+/\s+", name)]:
        part = part.strip()
        terms.append(part)
        if part.lower().startswith("the "):
            terms.append(part[4:])
    terms = list(dict.fromkeys(terms))
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
    if LEGAL_BOILERPLATE_RE.search(f"{path} {title[:120]}"):
        return "legal_boilerplate"
    if is_listing:
        return "listing"
    # Dated or /article/ URLs are stories even when the slug mentions ethics or policy (coverage *of* a topic).
    if re.search(r"/(?:19|20)\d{2}/\d{1,2}/|/\d{4}-\d{2}-\d{2}|/article[s]?/|/story/", path, re.I):
        return "article"
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


POLICY_URL_PATH = re.compile(r"/(?:about(?:-us)?|policies|policy|standards|ethics|corrections|help|faq|contact|"
                             r"who-we-are|editorial-(?:policy|standards|guidelines)|guidelines|principles|hc/)", re.I)
# The newsroom speaking for itself inside a story: first person, an editor's note, or "this story/article was ...".
NEWSROOM_VOICE = re.compile(r"editor'?s'? note|\bthis (?:story|article|report|post) (?:has been|was|is)\b|"
                            r"\bupdated to (?:remove|reflect|include|correct)\b|"
                            # "we" only with an action on published content ("we have removed the document"); a bare
                            # "we/our" in a story is often a letter writer, columnist or quoted source.
                            r"\bwe\s+(?:have\s+|had\s+|\'ve\s+)?(?:removed|updated|deleted|unpublished|redacted|withheld|"
                            r"corrected|changed|taken down|de-?indexed|anonymi[sz]ed)\b|"
                            # ...or a stated policy about the paper's own content ("we do not remove stories").
                            r"\bwe\s+(?:do\s+not|don'?t|will\s+not|won'?t|never|may|will|can|generally|typically|rarely|"
                            r"do|also|only)?\s*(?:\w+\s+)?(?:remov\w*|unpublish\w*|delet\w*|tak\w* down|de-?index\w*|"
                            r"anonymi[sz]\w*|updat\w*|correct\w*|alter\w*|chang\w*)\s+(?:\w+\s+){0,2}(?:stor(?:y|ies)|"
                            r"articles?|archives?|names?|content|coverage|posts?)\b",
                            re.I)
# Pages where the newsroom speaks in the first person by design.
EDITOR_COLUMN = re.compile(r"\b(?:from the editor|editor'?s'? (?:note|column|letter|desk|corner)|letter from the editor|"
                           r"note to (?:our )?readers|to our readers|publisher'?s'? (?:note|column)|our policy)\b", re.I)
# Article-style URLs: /2024/05/..., /stories/..., /news/..., /article..., numeric story ids.
ARTICLE_URL = re.compile(r"/(?:19|20)\d\d/|/stor(?:y|ies)/|/news/|/articles?[/_-]|/\d{5,}|,\d{5,}$|/opinion/|/sports/|"
                         r"/documents?/|/letters?[/-]|"
                         r"/local/|/business/|/obituar", re.I)
_SUBJECT_VERB = (r"(?:\s+\w+){0,2}?\s+(?:has|have|had|will|may|can|does|did|is|reviews?|considers?|decided|removed|"
                 r"unpublishe[sd]|agreed|declined|grants?|denies|policy|editors?)\b")


_POLICY_NAMED = re.compile(r"(?:standards|principles|polic(?:y|ies)|ethics|guidelines|removal|unpublish|takedown|"
                           r"right-to-be-forgotten|fresh-start|corrections|faq)", re.I)


def is_news_story(kind: str, url: str, published: str | None, author: str | None, title: str = "") -> bool:
    """A dated, bylined or article-addressed page that is not a policy/about page.

    A page whose address or title names a policy ("…-news-standards-and-publishing-principles/3069505/") is a policy
    page even when it lives under /news/ with a numeric id."""
    path = urlsplit(url).path
    if re.search(r"/(?:19|20)\d\d/", path):
        return True  # a dated address is a story, whatever its slug says ("…-on-removal-of-…-from-ballot")
    if POLICY_URL_PATH.search(path) or _POLICY_NAMED.search(path) or _POLICY_NAMED.search(title or ""):
        return False
    if kind in {"article", "crime_article", "baseline_article"}:
        return True
    if kind in {"policy", "about", "homepage", "legal_boilerplate"}:
        return False
    return bool(published or author or ARTICLE_URL.search(urlsplit(url).path))


def speaks_for_newsroom(entity, excerpt: str, title: str = "") -> bool:
    """First person, an editor's note, or the newsroom's own name as the subject of a verb ("The Bangor Daily News
    reviews…"). A byline or masthead that merely contains the name ("By Luke Caputo, Island Review") does not count."""
    if NEWSROOM_VOICE.search(excerpt) or (EDITOR_COLUMN.search(title or "") and re.search(r"\b(?:we|our)\b", excerpt, re.I)):
        return True
    return any(len(t) >= 6 and re.search(rf"\b{re.escape(t)}{_SUBJECT_VERB}", excerpt, re.I) for t in entity_terms(entity))


def attribute_statement(entity, *, first: bool, kind: str, url: str, published: str | None, author: str | None,
                        statement_type: str, excerpt: str, context: str, title: str = "") -> tuple[bool, str]:
    """(about_entity, evidence_class) for one extracted statement. Shared by research and ``nsmpa reclassify``."""
    about = first or any(t.lower() in context.lower() for t in entity_terms(entity))
    if (first and statement_type != "mention" and entity["cohort"] not in SEED_COHORTS
            and is_news_story(kind, url, published, author, title) and not speaks_for_newsroom(entity, excerpt, title)):
        # A news story *about* someone else's removal (Emory renaming buildings, a school removing DEI pages)
        # is not this newsroom's policy or practice.
        about = False
    ev_class = evidence_class_for(entity["cohort"], first, about and not first, kind, statement_type)
    if first and not about:
        ev_class = "secondary_report"
    if ev_class == "secondary_report" and entity["cohort"] in GUIDANCE_COHORTS and first:
        about = False  # a guidance org describing another newsroom's decision
    return about, ev_class


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
    with open(path, encoding="utf-8-sig", newline="") as f:
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
    kind_guess = page_kind(url, title, url.lower().endswith(".pdf"), False)
    if kind_guess == "legal_boilerplate":
        return 0.0, ["legal_boilerplate_page"]
    if fp:
        # Being on the organization's own site is necessary, not sufficient: require a policy signal or policy page.
        score += 0.25 if (SIGNAL_RE.search(hay) or kind_guess in {"policy", "about"}) else 0.05
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
    # A dated news story found by a policy/adverse query is usually coverage *of* someone else's ethics, not the
    # organization's own policy, unless it speaks in the first person or names the organization.
    if (fp and spec and spec.purpose in {"policy", "adverse"} and kind_guess == "article"
            and not re.search(r"\b(?:our|we|editor'?s note|from the editor|letter from the editor)\b", f"{title} {snippet}", re.I)
            and not any(t.lower() in title.lower() for t in entity_terms(entity))):
        reasons.append("news_story_not_policy")
        return round(min(score, 0.15), 3), reasons
    if rank:
        score += max(0.0, 0.08 - (rank - 1) * 0.01)
        reasons.append(f"rank:{rank}")
    if re.search(r"/(?:tag|tags|category|author|search|page/\d+)(?:/|$)|[?&](?:s|q)=", url, re.I):
        score -= 0.25
        reasons.append("listing_page_penalty")
    return round(max(0.0, min(score, 1.0)), 3), reasons


# Result sites that describe a publication but are never its home: encyclopedias, library/archive collections,
# university news offices, rankings and podcast hosts.
RECOVERY_SKIP = re.compile(r"(?:wikipedia\.org|//(?:news|library|libraries|content\.libraries|mabel|calendar|today)\.|"
                           r"onlinebooks\.|niche\.com|podbean\.com|archive\.org|/digital/collection/)", re.I)


class _SilentDashboard:
    """Stands in for RunDashboard when nothing is displayed: every call is a no-op."""

    def __getattr__(self, name):
        return lambda *a, **k: None


class EntityResearcher:
    @classmethod
    def offline(cls, db: Database, settings: Settings, run_id: str) -> EntityResearcher:
        """A researcher that only classifies and stores text it is given (no search, no fetch, no dashboard)."""
        return cls(db, settings, None, None, run_id, _SilentDashboard(), None)

    def __init__(self, db: Database, settings: Settings, broker: SearchBroker, fetcher: HardenedFetcher, run_id: str,
                 dash: RunDashboard, stop: StopController):
        self.db, self.settings, self.broker, self.fetcher = db, settings, broker, fetcher
        self.run_id, self.dash, self.stop = run_id, dash, stop
        self.snapshot_root = settings.research_snapshot_dir
        self.ai_fallback_calls = 0
        self._gate = None

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
            results, qid, _cached = await self.broker.search(query, purpose=purpose, entity_id=int(entity["id"]))
            used += 1
            self._sync_search_counters()
            for r in results:
                stored = self._store_target(entity, r.url, r.title, r.snippet, r.rank, spec, qid or None, spec.purpose, spec.topic)
                if stored and is_first_party(entity, r.url) and SIGNAL_RE.search(f"{r.title} {r.snippet} {r.url}"):
                    signal = True
            self._trim_candidates(entity)
            self.db.conn.commit()
        return used, signal

    def _trim_candidates(self, entity) -> None:
        """Keep at most ``research_max_targets_per_entity`` unfetched candidates, highest scores first."""
        self.db.execute(
            """UPDATE research_targets SET status='not_fetched_cap' WHERE id IN (
                 SELECT id FROM research_targets WHERE run_id=? AND entity_id=? AND status='candidate'
                 ORDER BY score DESC, id LIMIT -1 OFFSET ?)""",
            (self.run_id, entity["id"], self.settings.research_max_targets_per_entity))

    def _sync_search_counters(self) -> None:
        b = self.broker
        self.dash.update(searches_live=b.live_calls, searches_cached=b.cached_calls, searches_failed=b.failed_calls,
                         credits_estimated=b.credits_used)

    def _sync_fetch_counters(self) -> None:
        st = self.fetcher.stats
        self.dash.update(robots_blocked=st.robots_blocked, access_blocked=st.access_blocked,
                         malformed_skipped=st.malformed_skipped, retries=st.retries)

    async def _homepage_links(self, entity):
        """Free first-party discovery: homepage plus policy/about links found on it. Returns (fetch result, page)."""
        home = prepare_request_url(entity["homepage_url"] or "")
        if not home:
            return None, None
        self.dash.update(phase="homepage + identity check")
        self._store_target_direct(entity, home, "homepage", 0.9)
        r = await self.fetcher.fetch_safe(home)
        await self._record_page(entity, r, target_id=None, query_id=None, topic="homepage", forced_kind="homepage")
        if r.access_class != "ok":
            return r, None
        page = extract_main_text(r.content, r.content_type, r.final_url, r.headers)
        picked = 0
        for text, href in page.links:
            if picked >= self.settings.research_first_party_link_pages:
                break
            if not is_first_party(entity, href) or href == r.final_url:
                continue
            if LEGAL_BOILERPLATE_RE.search(f"{text} {urlsplit(href).path}"):
                continue
            path = urlsplit(href).path
            wanted = POLICY_PAGE_RE.search(f"{text} {path}") or ABOUT_PAGE_RE.search(path)
            if wanted and self._store_target_direct(entity, href, "first_party_link",
                                                    0.85 if POLICY_PAGE_RE.search(text + href) else 0.6):
                picked += 1
        return r, page

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
        seen: set[str] = {r["requested_url"] for r in self.db.execute(
            "SELECT requested_url FROM research_pages WHERE run_id=? AND entity_id=?", (self.run_id, entity["id"]))}
        fp_budget = self.settings.research_fetch_top_targets
        tp_budget = self.settings.research_third_party_fetch_limit
        for t in rows:
            if self.stop.force:
                break
            first = is_first_party(entity, t["url"])
            if t["url"] in seen:
                self.db.execute("UPDATE research_targets SET status='duplicate' WHERE id=?", (t["id"],))
                continue
            if (first and fp_budget <= 0) or (not first and tp_budget <= 0):
                self.db.execute("UPDATE research_targets SET status='not_fetched_budget' WHERE id=?", (t["id"],))
                continue
            host = (urlsplit(t["url"]).hostname or "").lower()
            if self.fetcher.host_failures.get(host, 0) >= self.settings.host_failure_threshold:
                self.db.execute("UPDATE research_targets SET status='skipped_host_unavailable' WHERE id=?", (t["id"],))
                continue
            seen.add(t["url"])
            if first:
                fp_budget -= 1
            else:
                tp_budget -= 1
            self.dash.update(phase=f"fetch {t['topic']}")
            self.dash.log(f"  fetch [{t['score']:.2f}] {t['url']}")
            r = await self.fetcher.fetch_safe(t["url"])
            status, _, _ = await self._record_page(entity, r, target_id=t["id"], query_id=t["query_id"], topic=t["topic"])
            if (first and r.access_class in {"blocked", "robots_disallowed", "rate_limited"} and self.settings.blocked_fallback
                    and (POLICY_URL_PATH.search(urlsplit(t["url"]).path) or POLICY_PAGE_RE.search(f"{t['title'] or ''} {t['url']}"))):
                from .fallback import BlockedSiteFallback
                page, _ = await BlockedSiteFallback(self, entity, 0).read_archived(t["url"], topic=t["topic"])
                if page is not None:
                    status = "archive_fallback"
            self.db.execute("UPDATE research_targets SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, t["id"]))
            self.db.conn.commit()
            self._sync_fetch_counters()

    async def _record_page(self, entity, r, *, target_id: int | None, query_id: int | None, topic: str,
                           forced_kind: str | None = None, substantive_only: bool = False):
        """Persist one fetched page plus its evidence and attributed voices. Returns (status, page, page_id)."""
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
            self.dash.increment(errors=1 if r.access_class not in {"robots_disallowed", "not_found", "gone", "host_unavailable",
                                                                    "blocked", "unsupported_content"} else 0)
            self.dash.log(f"  ! {r.access_class} {r.requested_url} {r.error or ''}")
        else:
            self.dash.increment(pages_fetched=1)
        self.db.execute(
            """
            INSERT INTO research_pages(run_id,entity_id,target_id,requested_url,final_url,http_status,status,content_type,title,
              text_length,content_sha256,meta_robots,x_robots_tag,noindex,policy_score,evidence_tags_json,headers_json,snapshot_path,
              error,canonical_url,access_class,page_kind,text_sha256,redirect_chain_json,query_id,first_party,author,published_date)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id,entity_id,requested_url) DO UPDATE SET final_url=excluded.final_url,http_status=excluded.http_status,
              status=excluded.status,content_type=excluded.content_type,title=excluded.title,text_length=excluded.text_length,
              content_sha256=excluded.content_sha256,meta_robots=excluded.meta_robots,x_robots_tag=excluded.x_robots_tag,
              noindex=excluded.noindex,headers_json=excluded.headers_json,snapshot_path=excluded.snapshot_path,error=excluded.error,
              canonical_url=excluded.canonical_url,access_class=excluded.access_class,page_kind=excluded.page_kind,
              text_sha256=excluded.text_sha256,redirect_chain_json=excluded.redirect_chain_json,first_party=excluded.first_party,
              author=excluded.author,published_date=excluded.published_date,fetched_at=CURRENT_TIMESTAMP
            """,
            (self.run_id, entity["id"], target_id, r.requested_url, r.final_url, r.status_code or None,
             "fetched" if ok else "failed", r.content_type, page.title if page else None,
             len(page.main_text) if page else 0, raw_sha, page.meta_robots if page else None, r.headers.get("x-robots-tag"),
             int(page.noindex) if page else 0, 0.0, "[]",
             json.dumps({k: v for k, v in r.headers.items() if k in {"content-type", "x-robots-tag", "last-modified", "server", "link"}}),
             snap_path, r.error, page.canonical_url if page else None, r.access_class, kind, text_sha,
             json.dumps(r.redirect_chain), query_id, int(first), page.author if page else None, page.published if page else None),
        )
        page_id = int(self.db.execute("SELECT id FROM research_pages WHERE run_id=? AND entity_id=? AND requested_url=?",
                                      (self.run_id, entity["id"], r.requested_url)).fetchone()["id"])
        if page and page.main_text and kind != "legal_boilerplate":
            n_useful = self._store_evidence(entity, page, page_id, query_id, topic, first, kind, raw_sha, text_sha,
                                            substantive_only=substantive_only)
            n_useful += self._store_voices(entity, page, page_id)
            if n_useful:
                self.broker.mark_useful(query_id)
        self.db.conn.commit()  # page + evidence are durable before the next network wait (and the lock is released)
        return ("fetched" if ok else r.access_class), page, page_id

    def _insert_item(self, entity, *, page, page_id: int | None, query_id: int | None, topic: str, first: bool, about: bool,
                     ev_class: str, statement_type: str, direction: str, excerpt: str, context: str, excerpt_sha: str,
                     near_dup: str, tags: list[str], actions: dict, authority: float, confidence: float, cues: str,
                     raw_sha: str | None, text_sha: str | None) -> int | None:
        profile = self.settings.case_profile
        cohort_basis = entity["cohort"] if first else None
        sim = score_similarity(profile, excerpt, context, cohort=cohort_basis)
        cm = score_case_match(profile, self.settings.my_case, excerpt, context, cohort=cohort_basis)
        fetched_at = self.db.scalar("SELECT fetched_at FROM research_pages WHERE id=?", (page_id,), None) if page_id else None
        dup = self.db.execute(
            "SELECT id FROM evidence_items WHERE run_id=? AND entity_id=? AND near_dup_key=? AND duplicate_of IS NULL "
            "AND excerpt_sha256!=? ORDER BY id LIMIT 1", (self.run_id, entity["id"], near_dup, excerpt_sha)).fetchone()
        cur = self.db.execute(
            """
            INSERT OR IGNORE INTO evidence_items(run_id,entity_id,page_id,query_id,cohort,source_url,source_title,source_domain,
              fetched_at,page_sha256,text_sha256,excerpt,context,excerpt_sha256,near_dup_key,duplicate_of,first_party,about_entity,
              evidence_class,statement_type,direction,topic,authority_score,relevance_score,similarity_score,similarity_factors_json,
              extraction_confidence,rationale,actions_json,case_match_score,case_match_factors_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (self.run_id, entity["id"], page_id, query_id, entity["cohort"], page.url, (page.title or "")[:500],
             registrableish_domain(page.url), fetched_at, raw_sha, text_sha, excerpt, context, excerpt_sha, near_dup,
             dup["id"] if dup else None, int(first), int(about), ev_class, statement_type, direction, topic, authority,
             STATEMENT_RELEVANCE[statement_type], sim.score, json.dumps(sim.factors), confidence, cues[:500],
             json.dumps(actions, sort_keys=True), cm.score, json.dumps(cm.factors)),
        )
        if not (cur.rowcount and cur.lastrowid):
            return None
        self.db.conn.executemany("INSERT OR IGNORE INTO evidence_item_tags(evidence_id,tag) VALUES(?,?)",
                                 [(cur.lastrowid, t) for t in tags])
        if not dup:
            self.dash.increment(evidence_unique=1)
            if sim.score >= profile.high_similarity_threshold:
                self.dash.increment(high_similarity=1)
        return int(cur.lastrowid)

    def _store_evidence(self, entity, page, page_id: int, query_id: int | None, topic: str, first: bool, kind: str,
                        raw_sha: str | None, text_sha: str | None, *, substantive_only: bool = False) -> int:
        items = extract_evidence(page.main_text, entity_terms=entity_terms(entity),
                                 max_items=self.settings.research_max_evidence_per_page,
                                 require_entity_mention=not first)
        useful = 0
        for it in items:
            st = it.statement
            if substantive_only and st.statement_type in {"mention", "harm_consideration"}:
                continue  # archived crime stories: keep only what the newsroom said or did, not the crime narrative
            about, ev_class = attribute_statement(
                entity, first=first, kind=kind, url=page.url, published=getattr(page, "published", None),
                author=getattr(page, "author", None), statement_type=st.statement_type, excerpt=it.excerpt,
                context=it.context, title=getattr(page, "title", "") or "")
            new_id = self._insert_item(
                entity, page=page, page_id=page_id, query_id=query_id, topic=topic, first=first, about=about, ev_class=ev_class,
                statement_type=st.statement_type, direction=st.direction, excerpt=it.excerpt, context=it.context,
                excerpt_sha=it.excerpt_sha256, near_dup=it.near_dup_key, tags=st.tags, actions=action_positions(it.excerpt),
                authority=authority_for(ev_class, kind, page.url), confidence=st.confidence, cues="; ".join(st.cues),
                raw_sha=raw_sha, text_sha=text_sha)
            if new_id and st.statement_type != "mention":
                useful += 1
                if st.statement_type.startswith("practice_relief_granted") and about:
                    self.dash.add_recent(f"✓ Documented practice  {entity['name']}: {it.excerpt[:60]}")
        return useful

    def _store_voices(self, entity, page, page_id: int) -> int:
        n = 0
        for v in extract_voices(page.main_text, author=page.author):
            key = person_key(v.person_name)
            expert = self.db.execute("SELECT id, role, affiliation FROM experts WHERE person_key=?", (key,)).fetchone()
            cm = score_case_match(self.settings.case_profile, self.settings.my_case, v.quote, v.context)
            cur = self.db.execute(
                """
                INSERT OR IGNORE INTO voices(person_key,person_name,role,affiliation,expert_id,quote,quote_sha256,context,source_url,
                  source_title,source_domain,page_id,run_id,entity_id,statement_type,direction,actions_json,attribution_method,
                  attribution_confidence,case_match_score)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (key, v.person_name, v.role or (expert["role"] if expert else None), expert["affiliation"] if expert else None,
                 expert["id"] if expert else None, v.quote, sha256_text(normalize_for_hash(v.quote)), v.context, page.url,
                 (page.title or "")[:300], registrableish_domain(page.url), page_id, self.run_id, entity["id"],
                 v.statement.statement_type, v.statement.direction, json.dumps(v.actions, sort_keys=True), v.method,
                 v.confidence + (0.15 if expert else 0.0), cm.score),
            )
            if cur.rowcount:
                n += 1
                if expert:
                    self.dash.add_recent(f"✎ Voice  {v.person_name}: {v.quote[:60]}")
        return n

    async def _dig_practice(self, entity, budget_left: int) -> int:
        """Search for and sample the entity's own archive for evidence of what it actually did."""
        used = 0
        if self.broker.provider.name != "none" and self.settings.research_practice_queries and budget_left > 0:
            u, _ = await self._search_tier(entity, list(PRACTICE_SPECS), budget_left)
            used += u
            await self._fetch_targets(entity)
        home = prepare_request_url(entity["homepage_url"] or "")
        if not home or self.stop.force or (entity_site(entity) != registrableish_domain(home) and "/" in entity_site(entity)):
            return used  # path-scoped hubs on a university site are not newsroom archives
        if self.settings.research_crime_article_sample <= 0:
            return used
        self.dash.update(phase="archive: reading sitemaps")
        urls = await site_article_urls(self.fetcher, home)
        crime, baseline = choose_samples(urls, self.settings.research_crime_article_sample,
                                         self.settings.research_baseline_article_sample)
        self.dash.log(f"  archive sample: {len(urls)} sitemap URLs -> {len(crime)} crime/arrest, {len(baseline)} baseline")
        baseline_noindex = baseline_ok = 0
        for u in baseline:
            if self.stop.force:
                return used
            self.dash.update(phase="archive: baseline articles")
            r = await self.fetcher.fetch_safe(u)
            _status, page, _ = await self._record_page(entity, r, target_id=None, query_id=None, topic="baseline_article",
                                                      forced_kind="baseline_article", substantive_only=True)
            if page:
                baseline_ok += 1
                baseline_noindex += int(page.noindex)
        sitewide = baseline_ok > 0 and baseline_noindex == baseline_ok
        for u in crime:
            if self.stop.force:
                return used
            self.dash.update(phase="archive: crime/arrest articles")
            r = await self.fetcher.fetch_safe(u)
            _status, page, page_id = await self._record_page(entity, r, target_id=None, query_id=None, topic="crime_article",
                                                            forced_kind="crime_article", substantive_only=True)
            if page and page.noindex:
                directive = "; ".join(x for x in (page.meta_robots and f"meta robots={page.meta_robots}",
                                                  page.x_robots_tag and f"X-Robots-Tag={page.x_robots_tag}") if x)
                if sitewide:
                    stype, text = "technical_sitewide_noindex", (
                        f'Archived crime/arrest article "{page.title}" carries noindex ({directive}), but so do all '
                        f"{baseline_ok} sampled comparison articles on this site; not evidence of targeted de-indexing.")
                else:
                    stype, text = "technical_noindex", (
                        f'Archived crime/arrest article "{page.title}" carries a noindex directive ({directive}); '
                        f"{baseline_ok - baseline_noindex} of {baseline_ok} sampled comparison articles on the same site are indexable.")
                self._insert_item(
                    entity, page=page, page_id=page_id, query_id=None, topic="crime_article", first=True, about=True,
                    ev_class="technical", statement_type=stype, direction=STATEMENT_DIRECTION[stype], excerpt=text,
                    context=f"{page.title}. {page.main_text[:1200]}", excerpt_sha=sha256_text(normalize_for_hash(text + page.url)),
                    near_dup=sha256_text(page.url)[:24], tags=["noindex"],
                    actions={"deindex": "practiced"} if stype == "technical_noindex" else {},
                    authority=0.9, confidence=0.9 if baseline_ok else 0.6, cues=f"noindex;baseline={baseline_noindex}/{baseline_ok}",
                    raw_sha=None, text_sha=None)
                if stype == "technical_noindex":
                    self.dash.add_recent(f"✓ De-indexed crime article  {entity['name']}: {page.title[:50]}")
        self.db.conn.commit()
        return used

    def _note_identity(self, entity, ident) -> None:
        meta = json_meta(entity)
        meta["website_identity"] = {"status": ident.status, "reason": ident.reason, "signals": ident.signals}
        self.db.execute("UPDATE research_entities SET metadata_json=? WHERE id=?", (json.dumps(meta), entity["id"]))

    def _stop_for_identity(self, entity, ident) -> dict:
        """No credits are spent and nothing from the site is attributed to the organization."""
        from .stance import StanceResult
        self.db.execute("UPDATE research_targets SET status='skipped_identity' WHERE run_id=? AND entity_id=? AND status='candidate'",
                        (self.run_id, entity["id"]))
        self.db.execute("UPDATE evidence_items SET about_entity=0, evidence_class='unattributable_site' WHERE run_id=? AND entity_id=?",
                        (self.run_id, entity["id"]))
        label = {"mismatch": "website_no_longer_this_publication", "unreachable": "homepage_unreachable",
                 "blocked": "site_blocks_automated_access"}[ident.status]
        result = StanceResult(stance="UNDETERMINED", confidence=0.0, rationale=f"Not researched: {ident.reason}.",
                              coverage={"identity": ident.status, **ident.signals}, review_reasons=[label])
        store_stance(self.db, self.run_id, int(entity["id"]), result)
        enqueue_entity_review(self.db, self.run_id, entity, result)
        self.db.conn.commit()
        self.dash.update(step_done=8, step_total=8)
        self.dash.add_recent(f"· {entity['name']}: {label.replace('_', ' ')}")
        return {"stance": "UNDETERMINED", "confidence": 0.0, "queries": 0, "escalated": False, "max_similarity": 0.0,
                "supportive": 0, "adverse": 0, "relief_mode": "UNADDRESSED", "identity": ident.status}

    async def _recover_site(self, entity, ident, budget: int):
        """The directory URL is dead or repurposed: look for where the publication lives now (1 search).

        Up to three plausible result sites are fetched (free) and each must pass the identity check; a site that also
        carries the publication's own name beats an umbrella page (e.g. a university's student-media hub).
        """
        from .identity import institution_base_name
        meta = json_meta(entity)
        old_dom = entity["domain"] or registrableish_domain(entity["homepage_url"] or "")
        pub = re.sub(r"\(\d+ titles\)$", "", entity["name"] or "").strip()
        if entity["cohort"] == "student_media" and entity["parent_name"]:
            query = f'"{institution_base_name(entity["parent_name"])}" student newspaper'
        elif entity["cohort"] in {"professional_newsroom", "broadcast_newsroom"}:
            if "." in pub:
                return None
            query = f'"{pub}" newspaper {entity["state"] or ""}'.strip()
        else:
            return None
        self.dash.update(phase="finding the publication's current website")
        results, qid, _ = await self.broker.search(query, purpose="research:recover_site", entity_id=int(entity["id"]))
        self._sync_search_counters()
        pub_toks = [t for t in re.findall(r"[a-z]{3,}", pub.lower()) if t not in {"the", "online", "daily", "news", "univ"}] or \
            [t for t in re.findall(r"[a-z]{4,}", pub.lower()) if t not in {"online", "univ"}]  # "The Univ of WA Daily" -> daily
        seen, cands = set(), []
        for res in results:
            url = prepare_request_url(res.url)
            if not url or is_blocked_social_or_aggregator(url) or RECOVERY_SKIP.search(url):
                continue
            dom = registrableish_domain(url)
            host = urlsplit(url).netloc.lower()
            if dom == old_dom or host in seen:
                continue
            seen.add(host)
            blob = f"{res.title} {res.snippet}".lower()
            score = (2 if pub_toks and any(t in blob for t in pub_toks) else 0) + (1 if "student" in blob else 0)
            cands.append((-score, res.rank, url))
        # Best-named candidate first (title/snippet naming the publication beats an umbrella or committee page that
        # merely mentions it); a site that blocks robots is accepted, unverified, only when the result names it.
        chosen = None
        for _neg, _, url in sorted(cands)[:3]:
            root = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}/"
            r = await self.fetcher.fetch_safe(root)
            page = extract_main_text(r.content, r.content_type, r.final_url, r.headers) if r.access_class == "ok" else None
            trial = dict(entity)
            trial["homepage_url"], trial["domain"] = root, registrableish_domain(root)
            got = check_identity(trial, meta, r.access_class, page)
            if got.status == "ok" and (-_neg >= 2 or got.signals.get("name_on_page")):
                chosen = (root, trial, True)
            elif got.status == "blocked" and -_neg >= 2:
                chosen = (root, trial, False)
            if chosen:
                break
        if chosen is None:
            return None
        root, trial, confirmed = chosen
        meta.setdefault("previous_urls", []).append(entity["homepage_url"])
        meta["recovered"] = {"from": entity["homepage_url"], "to": root, "query_id": qid, "reason": ident.reason,
                             "verified": confirmed}
        self.db.execute("UPDATE research_entities SET homepage_url=?, domain=?, metadata_json=?, updated_at=CURRENT_TIMESTAMP "
                        "WHERE id=?", (root, trial["domain"], json.dumps(meta), entity["id"]))
        if (entity["source_key"] or "").startswith("student_publication:"):
            try:
                self.db.execute("UPDATE publications SET homepage_url=?, domain=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                (root, trial["domain"], int(entity["source_key"].split(":")[1])))
            except Exception as exc:  # unique (unitid, domain) clash: the new site is already recorded
                self.dash.log(f"publication update skipped: {exc}")
        self.db.execute("UPDATE evidence_items SET about_entity=0, evidence_class='unattributable_site' WHERE run_id=? AND entity_id=?",
                        (self.run_id, entity["id"]))
        self.db.conn.commit()
        self.dash.add_recent(f"↪ {entity['name']}: moved to {trial['domain']}" + ("" if confirmed else " (site blocks robots; unverified)"))
        return self.db.execute("SELECT * FROM research_entities WHERE id=?", (entity["id"],)).fetchone()

    async def _decide(self, entity):
        """Classify the entity; when the stance is determinate, let the AI veto check its decisive excerpts first."""
        from .stance import DETERMINATE
        result = classify_entity(self.db, self.settings, self.run_id, entity)
        if not self.settings.ai_gate_enabled or result.stance not in DETERMINATE or (self.stop and self.stop.force):
            return result
        if self._gate is None:
            from .ai_gate import Gate
            self._gate = Gate(self.db, self.settings)
        self.dash.update(phase="AI check of decisive excerpts")
        out = await self._gate.check_entity(self.run_id, entity)
        if out["rejected"]:
            self.dash.add_recent(f"· AI check vetoed {out['rejected']} excerpt(s) for {entity['name']}")
        return out["result"]

    async def _research_blocked(self, entity, ident, budget: int) -> dict:
        """The live site refuses robots: archived copies, snippets, AI search (confirmed) and other sources instead."""
        from .fallback import BlockedSiteFallback
        fb = BlockedSiteFallback(self, entity, budget)
        out = await fb.run(ident)
        if out.get("identity") == "mismatch":
            from .identity import Identity
            return self._stop_for_identity(entity, Identity("mismatch", out["reason"], {"source": "archive"}))
        self.dash.update(phase="classifying")
        result = await self._decide(entity)
        result.coverage.update({"live_site": "blocks_automated_access", "fallback": {k: v for k, v in out.items() if k != "guidance"}})
        result.review_reasons.append("site_blocks_robots_evidence_from_archive_or_leads")
        if not out.get("archived_pages") and result.stance not in {"UNDETERMINED"}:
            result.review_reasons.append("no_archived_pages")
        if result.stance == "NO_RELEVANT_GUIDANCE":
            # Silence from copies and snippets is weaker than silence from the live site: never claim "no policy".
            result.stance, result.confidence = "UNDETERMINED", 0.0
            result.rationale = ("Site blocks automated access; archived copies, search snippets and AI search found no "
                                "confirmed policy text. " + (result.rationale or ""))
        store_stance(self.db, self.run_id, int(entity["id"]), result)
        enqueue_entity_review(self.db, self.run_id, entity, result)
        self.db.conn.commit()
        self.dash.update(step_done=8, step_total=8)
        leads = self.db.scalar("SELECT COUNT(*) FROM ai_leads WHERE run_id=? AND entity_id=? AND status='unconfirmed'",
                               (self.run_id, entity["id"]))
        self.dash.add_recent(f"· {entity['name']}: blocked → {out.get('archived_pages', 0)} archived page(s), {leads} AI lead(s) to confirm")
        return {"stance": result.stance, "confidence": result.confidence, "queries": out.get("searches", 0), "escalated": False,
                "max_similarity": result.max_similarity, "supportive": result.supportive, "adverse": result.adverse,
                "relief_mode": result.relief_mode, "identity": "blocked_fallback", "archived_pages": out.get("archived_pages", 0)}

    async def research(self, entity, budget_per_entity: int, _recovered: bool = False) -> dict:
        eid = int(entity["id"])
        if not entity_site(entity):
            return {"stance": "UNDETERMINED", "reason": "no homepage/domain"}
        depth = self.settings.research_depth
        searching = self.broker.provider.name != "none"
        used, signal = 0, False
        steps = iter(range(1, 9))
        step = lambda: self.dash.update(step_done=next(steps, 8), step_total=8)  # noqa: E731
        self.dash.update(step_done=0, step_total=8)
        # 1. Free homepage fetch + identity check BEFORE any search credits are spent.
        r_home, home_page = await self._homepage_links(entity)
        meta = json_meta(entity)
        ident = check_identity(entity, meta, r_home.access_class if r_home else "invalid_url", home_page)
        self._note_identity(entity, ident)
        step()
        if ident.status == "blocked" and self.settings.blocked_fallback:
            return await self._research_blocked(entity, ident, budget_per_entity)
        if not ident.proceed:
            recovered = None
            if searching and self.settings.research_recover_stale_sites and not _recovered and ident.status in {"mismatch", "unreachable"}:
                recovered = await self._recover_site(entity, ident, budget_per_entity)
            if recovered is not None:
                return await self.research(recovered, budget_per_entity, _recovered=True)
            return self._stop_for_identity(entity, ident)
        if searching:
            used, signal = await self._search_tier(entity, plan_queries(self.settings, entity, 1), budget_per_entity)
        step()
        escalate = searching and (depth == "deep" or signal or entity["cohort"] in GUIDANCE_COHORTS)
        if depth != "quick" and escalate and not self.stop.force:
            u2, _ = await self._search_tier(entity, plan_queries(self.settings, entity, 2), budget_per_entity - used)
            used += u2
        if searching and depth == "deep" and not self.stop.force:
            u3, _ = await self._search_tier(entity, plan_queries(self.settings, entity, 3), budget_per_entity - used)
            used += u3
        step()
        await self._fetch_targets(entity)
        step()
        dig = self.settings.research_practice_dig
        if entity["cohort"] not in GUIDANCE_COHORTS and dig != "never" and not self.stop.force:
            has_policy = bool(self.db.scalar(
                "SELECT COUNT(*) FROM evidence_items WHERE run_id=? AND entity_id=? AND first_party=1 "
                "AND evidence_class IN ('written_policy','editorial_statement') AND statement_type NOT IN ('mention','harm_consideration')",
                (self.run_id, eid)))
            if dig == "always" or not has_policy:
                used += await self._dig_practice(entity, budget_per_entity - used)
        step()
        if entity["cohort"] not in GUIDANCE_COHORTS and not self.stop.force:
            await wayback_for_entity(self, entity)
        step()
        self.dash.update(phase="classifying")
        result = await self._decide(entity)
        store_stance(self.db, self.run_id, eid, result)
        enqueue_entity_review(self.db, self.run_id, entity, result)
        self.db.conn.commit()
        self.dash.update(step_done=8, step_total=8)
        return {"stance": result.stance, "confidence": result.confidence, "queries": used, "escalated": escalate,
                "max_similarity": result.max_similarity, "supportive": result.supportive, "adverse": result.adverse,
                "relief_mode": result.relief_mode}

STANCE_BADGE = {
    "SUPPORTS_RELIEF": "✓ Relief policy", "SUPPORTS_CHANGED_CIRCUMSTANCES": "✓ Changed-circumstance policy",
    "CASE_BY_CASE": "✓ Case-by-case review", "UPDATE_ONLY": "· Update-only policy", "STRICT_ARCHIVE": "! Strict archive policy",
    "MIXED": "? Mixed signals",
}


def select_entities(db: Database, cohort: str | None, limit: int | None, entity_ids: list[int] | None = None,
                    max_priority: int | None = None) -> list:
    sql = "SELECT * FROM research_entities WHERE active=1 AND merged_into IS NULL"
    params: list = []
    if cohort:
        sql += " AND cohort=?"
        params.append(cohort)
    elif not entity_ids:
        sql += f" AND cohort NOT IN ({','.join('?' * len(SEED_COHORTS))})"
        params += sorted(SEED_COHORTS)
    if entity_ids:
        sql += f" AND id IN ({','.join('?' * len(entity_ids))})"
        params += entity_ids
    if max_priority is not None:
        sql += " AND CAST(json_extract(metadata_json,'$.research_priority') AS INTEGER) <= ?"
        params.append(max_priority)
    # Directory research priority first (1 = highest), then stable order.
    sql += " ORDER BY COALESCE(CAST(json_extract(metadata_json,'$.research_priority') AS INTEGER), 9), cohort, id"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return db.execute(sql, params).fetchall()


async def research_all(db: Database, settings: Settings, run_id: str, cohort: str | None = None, limit: int | None = None,
                       quiet: bool = False, verbose: bool = False, *, max_searches: int | None = None,
                       refresh_search: bool = False, fresh: bool = False, entity_ids: list[int] | None = None,
                       provider=None, fetcher: HardenedFetcher | None = None, stop: StopController | None = None,
                       dashboard: RunDashboard | None = None, max_priority: int | None = None) -> dict:
    provider = provider or get_search_provider(settings.search_provider, settings.user_agent)
    broker = SearchBroker(db, settings, run_id, provider, max_searches=max_searches, refresh=refresh_search)
    rows = select_entities(db, cohort, limit, entity_ids, max_priority)
    keys = [str(r["id"]) for r in rows]
    register_items(db, run_id, "entity", keys)
    if fresh:
        db.execute("UPDATE run_items SET status='pending' WHERE run_id=? AND item_type='entity'", (run_id,))
        db.conn.commit()
    completed = done_keys(db, run_id, "entity")
    todo = [r for r in rows if str(r["id"]) not in completed]
    stop = stop or StopController()
    universe = {"student_media": "Student Journalism", "professional_newsroom": "Professional Newsrooms",
                "broadcast_newsroom": "Broadcast Newsrooms",
                "support_org": "Support / Standards Organizations"}.get(cohort or "", cohort or "All cohorts")
    dash = dashboard or RunDashboard("NSMPA National Research", len(rows), quiet=quiet, verbose=verbose, universe=universe,
                                     db=db, run_id=run_id, persist_seconds=settings.heartbeat_seconds)
    counts = {"entities": len(rows), "already_done": len(rows) - len(todo), "completed": 0, "failed": 0}
    stop_reason = ""
    own_fetcher = fetcher is None
    fetcher = fetcher or HardenedFetcher(settings, on_event=dash.log)
    queue: asyncio.Queue = asyncio.Queue()
    for r in todo:
        queue.put_nowait(r)
    per_entity = (max(settings.research_max_searches_per_entity, settings.research_deep_max_searches_per_entity)
                  if settings.research_depth == "deep" else settings.research_max_searches_per_entity)
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

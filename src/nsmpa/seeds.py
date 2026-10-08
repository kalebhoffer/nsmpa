"""Seeded precedents and expert voices.

These lists are *search leads*, not findings. Each seed's claim and each expert's role are
recorded as "to verify". The tool searches, fetches and snapshots sources, extracts what was
actually said (supportive or adverse), and sets ``status='sources_found'`` only when fetched
text about the seed contains a substantive relief/update statement. A human must then mark it
``human_verified`` (or ``refuted``) with ``nsmpa precedent --key K --status ...`` before it is
presented as established fact.
"""
from __future__ import annotations

import asyncio
import json
import re

from .config import Settings
from .db import Database
from .evidence import person_key
from .fetch import HardenedFetcher
from .progress import RunDashboard
from .research import EntityResearcher, QuerySpec
from .runs import StopController, create_or_resume_run, done_keys, finish_run, mark_item, register_items
from .search import SearchAuthError, SearchBroker, SearchBudgetExceeded, get_search_provider
from .utils import prepare_request_url, registrableish_domain

# (key, organization, title, approx_year, claim to verify, actions, verification query, primary_url, prior)
PRECEDENT_SEEDS: list[dict] = [
    dict(key="spj_code_minimize_harm", organization="Society of Professional Journalists", title="SPJ Code of Ethics: Minimize Harm",
         year="2014", actions="update",
         claim="The SPJ Code asks journalists to consider the long-term implications of the extended reach and permanence of "
               "publication, and to provide updated and more complete information as appropriate.",
         query='"SPJ Code of Ethics" "permanence of publication"', url="https://www.spj.org/ethicscode.asp", prior="well_documented"),
    dict(key="apme_longtail_2009", organization="Associated Press Managing Editors", title="The Longtail of News: To Unpublish or Not to Unpublish",
         year="2009", actions="unpublish;anonymize;update",
         claim="APME report (Kathy English) surveying how editors handle requests to unpublish archived stories.",
         query='"The Longtail of News" unpublish', url=None, prior="well_documented"),
    dict(key="boston_globe_fresh_start", organization="The Boston Globe", title="Fresh Start initiative",
         year="2021", actions="anonymize;deindex;update",
         claim="The Globe invited people to request review of past coverage of minor matters, with possible updates, "
               "anonymization or reduced search visibility.",
         query='"Boston Globe" "Fresh Start" past coverage', url=None, prior="well_documented"),
    dict(key="cleveland_names_2018", organization="cleveland.com", title="Removing names from old minor-crime stories",
         year="2018", actions="anonymize;deindex",
         claim="cleveland.com's editor announced the site would remove names from some old stories about minor crimes.",
         query='cleveland.com remove names old stories minor crimes "Chris Quinn"', url=None, prior="well_documented"),
    dict(key="ap_minor_crimes_2021", organization="The Associated Press", title="No longer naming suspects in minor crimes",
         year="2021", actions="anonymize",
         claim="AP said it would generally stop naming suspects in minor crimes because stories follow people online.",
         query='"Associated Press" no longer name suspects minor crimes', url=None, prior="well_documented"),
    dict(key="dwyer_unpublishing_research", organization="Deborah Dwyer", title="Unpublishing the News (research)",
         year="2019-2021", actions="unpublish;deindex;anonymize;update",
         claim="Academic/RJI research on how U.S. newsrooms handle unpublishing requests.",
         query='"Deborah Dwyer" unpublishing newsrooms', url=None, prior="well_documented"),
    dict(key="splc_takedown_guidance", organization="Student Press Law Center", title="Guidance on takedown/unpublishing requests",
         year="", actions="unpublish;deindex;anonymize;update",
         claim="SPLC guidance for student media handling requests to remove or alter archived content.",
         query='"Student Press Law Center" takedown requests unpublish student media', url=None, prior="candidate"),
    dict(key="poynter_unpublishing_guidance", organization="Poynter", title="Guidance on unpublishing and old crime stories",
         year="", actions="unpublish;deindex;anonymize;update",
         claim="Poynter commentary/guidance on whether and how to unpublish or update old crime coverage.",
         query='Poynter unpublishing old crime stories names', url=None, prior="candidate"),
    dict(key="bangor_daily_news_policy", organization="Bangor Daily News", title="Policy on old minor-crime stories (candidate)",
         year="", actions="unpublish;anonymize",
         claim="Candidate lead: a published policy on unpublishing or anonymizing old minor-crime stories. Unconfirmed.",
         query='"Bangor Daily News" unpublish policy minor crimes', url=None, prior="candidate"),
    dict(key="mugshot_galleries_ended", organization="mugshot galleries", title="Newsrooms ending mugshot galleries (candidate)",
         year="c. 2020", actions="unpublish;deindex",
         claim="Candidate lead: newsrooms that discontinued mugshot galleries citing lasting harm. Unconfirmed.",
         query='newspaper ends "mugshot galleries" harm', url=None, prior="candidate"),
]

# Roles are as commonly reported and must be verified before presentation.
EXPERT_SEEDS: list[dict] = [
    dict(name="Kathy English", role="Former public editor", affiliation="Toronto Star",
         note="Author of the 2009 APME report on unpublishing"),
    dict(name="Deborah Dwyer", role="Researcher", affiliation="Unpublishing the News project / RJI",
         note="Researches newsroom unpublishing practices"),
    dict(name="Kelly McBride", role="Journalism ethicist", affiliation="Poynter", note="Poynter ethics leadership; former NPR public editor"),
    dict(name="Chris Quinn", role="Editor", affiliation="cleveland.com / The Plain Dealer", note="Led cleveland.com name-removal initiative"),
    dict(name="Brian McGrory", role="Former editor", affiliation="The Boston Globe", note="Editor when Fresh Start was launched"),
    dict(name="John Daniszewski", role="Standards editor", affiliation="The Associated Press", note="AP standards"),
    dict(name="Frank LoMonte", role="Media-law scholar", affiliation="former Student Press Law Center", note="Former SPLC executive director"),
    dict(name="Mike Hiestand", role="Legal counsel", affiliation="Student Press Law Center", note="Student-media legal adviser"),
    dict(name="Bob Steele", role="Journalism ethicist", affiliation="Poynter / DePauw University", note="Ethics scholar"),
    dict(name="Al Tompkins", role="Senior faculty", affiliation="Poynter", note="Broadcast/ethics trainer"),
    dict(name="Andrew Seaman", role="Former ethics committee chair", affiliation="Society of Professional Journalists", note="SPJ ethics"),
    dict(name="Margaret Sullivan", role="Media columnist; former public editor", affiliation="The New York Times / The Washington Post",
         note="Public-editor perspective (may be adverse)"),
]

RELIEF_TERMS = '(unpublish OR unpublishing OR "de-index" OR deindex OR "right to be forgotten" OR "old stories" OR mugshot OR "remove names")'


def seed_precedents(db: Database) -> dict:
    n = 0
    with db.transaction():
        for s in PRECEDENT_SEEDS:
            db.conn.execute(
                """INSERT INTO precedent_seeds(seed_key,organization,title,approx_year,claim,actions,verification_query,primary_url,prior_confidence)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(seed_key) DO UPDATE SET organization=excluded.organization,title=excluded.title,approx_year=excluded.approx_year,
                     claim=excluded.claim,actions=excluded.actions,verification_query=excluded.verification_query,
                     primary_url=excluded.primary_url,prior_confidence=excluded.prior_confidence""",
                (s["key"], s["organization"], s["title"], s["year"], s["claim"], s["actions"], s["query"], s["url"], s["prior"]))
            n += 1
    return {"precedent_seeds": n}


def seed_experts(db: Database) -> dict:
    n = 0
    with db.transaction():
        for e in EXPERT_SEEDS:
            db.conn.execute(
                """INSERT INTO experts(person_key,name,role,affiliation,credential_note,source) VALUES(?,?,?,?,?,'builtin_seed')
                   ON CONFLICT(person_key) DO UPDATE SET name=excluded.name,role=excluded.role,affiliation=excluded.affiliation,
                     credential_note=excluded.credential_note""",
                (person_key(e["name"]), e["name"], e["role"], e["affiliation"], e["note"]))
            n += 1
        # Link voices captured earlier to newly seeded experts.
        db.conn.execute("UPDATE voices SET expert_id=(SELECT id FROM experts WHERE experts.person_key=voices.person_key) "
                        "WHERE expert_id IS NULL")
    return {"experts": n}


def add_expert(db: Database, name: str, role: str, affiliation: str, note: str) -> int:
    db.execute("""INSERT INTO experts(person_key,name,role,affiliation,credential_note,source) VALUES(?,?,?,?,?,'manual')
                  ON CONFLICT(person_key) DO UPDATE SET role=excluded.role,affiliation=excluded.affiliation,credential_note=excluded.credential_note""",
               (person_key(name), name, role, affiliation, note))
    db.execute("UPDATE voices SET expert_id=(SELECT id FROM experts WHERE experts.person_key=voices.person_key) WHERE expert_id IS NULL")
    db.conn.commit()
    return int(db.scalar("SELECT id FROM experts WHERE person_key=?", (person_key(name),)))


def _ensure_entity(db: Database, cohort: str, key: str, name: str, url: str | None, parent: str | None, meta: dict) -> int:
    clean = prepare_request_url(url) if url else None
    db.execute(
        """INSERT INTO research_entities(cohort,source_key,name,homepage_url,domain,parent_name,source,verification_status,metadata_json,entity_type)
           VALUES(?,?,?,?,?,?,'builtin_seed','seeded',?,?)
           ON CONFLICT(cohort,source_key) DO UPDATE SET name=excluded.name,homepage_url=excluded.homepage_url,domain=excluded.domain,
             parent_name=excluded.parent_name,metadata_json=excluded.metadata_json""",
        (cohort, key, name, clean, registrableish_domain(clean) if clean else None, parent, json.dumps(meta), cohort))
    return int(db.scalar("SELECT id FROM research_entities WHERE cohort=? AND source_key=?", (cohort, key)))


async def _research_seed(researcher: EntityResearcher, entity, query: str | None, primary_url: str | None, max_pages: int,
                         purpose: str, *, search: bool = True) -> None:
    if primary_url:
        r = await researcher.fetcher.fetch_safe(primary_url)
        await researcher._record_page(entity, r, target_id=None, query_id=None, topic=purpose, forced_kind="policy")
        researcher.db.conn.commit()
        if r.access_class != "ok" and researcher.settings.blocked_fallback:
            from .fallback import BlockedSiteFallback  # the source blocks robots or is gone: read its archived copy
            await BlockedSiteFallback(researcher, entity, 0).read_archived(primary_url, topic=purpose, forced_kind="policy")
    if query and search and researcher.broker.provider.name != "none":
        spec = QuerySpec(1, "seed", purpose, query, third_party=True)
        results, qid, _ = await researcher.broker.search(query, purpose=f"seed:{purpose}", entity_id=int(entity["id"]))
        researcher._sync_search_counters()
        for r in results:
            researcher._store_target(entity, r.url, r.title, r.snippet, r.rank, spec, qid or None, "seed", purpose)
        researcher.db.conn.commit()
    s = researcher.settings
    saved = (s.research_fetch_top_targets, s.research_third_party_fetch_limit)
    s.research_fetch_top_targets, s.research_third_party_fetch_limit = max_pages, max_pages
    try:
        await researcher._fetch_targets(entity)
    finally:
        s.research_fetch_top_targets, s.research_third_party_fetch_limit = saved


async def run_seeds(db: Database, settings: Settings, kind: str, *, run_id: str | None = None, max_searches: int | None = None,
                    quiet: bool = False, verbose: bool = False, provider=None, fetcher: HardenedFetcher | None = None,
                    stop: StopController | None = None, command: str | None = None, only_index: bool = False,
                    search: bool = True) -> dict:
    """kind = 'precedents' or 'experts'. Resumable, budgeted, checkpointed like every other run."""
    if kind == "precedents":
        seed_precedents(db)
        rows = db.execute("SELECT * FROM precedent_seeds " + ("WHERE index_id IS NOT NULL " if only_index else "")
                          + "ORDER BY id").fetchall()
        keys = [r["seed_key"] for r in rows]
    else:
        seed_experts(db)
        rows = db.execute("SELECT * FROM experts WHERE active=1 ORDER BY id").fetchall()
        keys = [r["person_key"] for r in rows]
    provider = provider or get_search_provider(settings.search_provider, settings.user_agent)
    rid, _ = create_or_resume_run(db, settings, f"{kind}_verification", run_id, params={"kind": kind}, command=command,
                                  max_searches=max_searches)
    broker = SearchBroker(db, settings, rid, provider, max_searches=max_searches)
    register_items(db, rid, kind, keys)
    done = done_keys(db, rid, kind)
    stop = stop or StopController()
    dash = RunDashboard(f"NSMPA {'Precedent verification' if kind == 'precedents' else 'Expert voices'}", len(rows),
                        quiet=quiet, verbose=verbose, universe="Seeded leads (to verify)", db=db, run_id=rid,
                        persist_seconds=settings.heartbeat_seconds)
    own = fetcher is None
    fetcher = fetcher or HardenedFetcher(settings, on_event=dash.log)
    status, reason, found = "completed", None, 0
    with dash:
        dash.update(completed=len(done), skipped_done=len(done), budget_limit=max_searches)
        stop.on_stop(dash.notice)
        uninstall = stop.install()
        researcher = EntityResearcher(db, settings, broker, fetcher, rid, dash, stop)
        try:
            for row, key in zip(rows, keys, strict=True):
                if stop.stop_requested:
                    status, reason = "interrupted", stop.reason
                    break
                if key in done:
                    continue
                mark_item(db, rid, kind, key, "running")
                try:
                    if kind == "precedents":
                        eid = _ensure_entity(db, "precedent_case", key, row["organization"], row["primary_url"], row["title"],
                                             {"seed_key": key, "claim": row["claim"]})
                        db.execute("UPDATE precedent_seeds SET entity_id=? WHERE id=?", (eid, row["id"]))
                        db.conn.commit()  # never hold the write lock across network waits
                        entity = db.execute("SELECT * FROM research_entities WHERE id=?", (eid,)).fetchone()
                        dash.update(current=f"{row['organization']}: {row['title']}")
                        await _research_seed(researcher, entity, row["verification_query"], row["primary_url"],
                                             settings.precedent_max_pages_per_seed, key, search=search)
                        n = _substantive_about(db, rid, eid, row)
                        st = "sources_found" if n else "not_found"
                        db.execute("UPDATE precedent_seeds SET status=CASE WHEN status IN ('human_verified','refuted') THEN status ELSE ? END, "
                                   "status_note=?, last_checked_at=CURRENT_TIMESTAMP WHERE id=?",
                                   (st, f"{n} substantive excerpt(s) about the organization in run {rid}", row["id"]))
                        found += int(bool(n))
                        dash.add_recent(f"{'✓' if n else '·'} {row['organization']}: {st} ({n})")
                    else:
                        eid = _ensure_entity(db, "expert", key, row["name"], None, row["affiliation"],
                                             {"role": row["role"], "credential_note": row["credential_note"]})
                        db.execute("UPDATE experts SET entity_id=? WHERE id=?", (eid, row["id"]))
                        entity = db.execute("SELECT * FROM research_entities WHERE id=?", (eid,)).fetchone()
                        dash.update(current=row["name"])
                        q = f'"{row["name"]}" {RELIEF_TERMS}'
                        await _research_seed(researcher, entity, q, None, settings.expert_max_pages_per_person, key)
                        db.execute("UPDATE voices SET expert_id=? WHERE person_key=? AND expert_id IS NULL", (row["id"], key))
                        n = db.scalar("SELECT COUNT(*) FROM voices WHERE person_key=?", (key,))
                        found += int(bool(n))
                        dash.add_recent(f"{'✎' if n else '·'} {row['name']}: {n} attributed statement(s)")
                    mark_item(db, rid, kind, key, "done")
                    dash.increment(completed=1)
                    dash.checkpoint()
                except SearchBudgetExceeded as exc:
                    mark_item(db, rid, kind, key, "pending", error=str(exc))
                    status, reason = "budget_exhausted", str(exc)
                    break
                except SearchAuthError as exc:
                    mark_item(db, rid, kind, key, "pending", error=str(exc))
                    status, reason = "failed", f"search_auth_error: {exc}"
                    break
                except asyncio.CancelledError:
                    mark_item(db, rid, kind, key, "pending", error="cancelled")
                    status, reason = "interrupted", "force-cancelled"
                    break
                except Exception as exc:  # one bad lead never stops the run
                    mark_item(db, rid, kind, key, "failed", error=f"{type(exc).__name__}: {exc}"[:500])
                    dash.increment(errors=1, completed=1)
        finally:
            uninstall()
            if own:
                await fetcher.close()
            await broker.aclose()
    finish_run(db, rid, status, reason)
    remaining = len(keys) - len(done_keys(db, rid, kind))
    return {"run_id": rid, "kind": kind, "status": status, "stop_reason": reason, "with_sources": found, "remaining": remaining,
            "searches_live": broker.live_calls, "searches_cached": broker.cached_calls, "credits_estimated": broker.credits_used}


GENERIC_NAME_WORDS = {"daily", "news", "times", "student", "students", "newspaper", "media", "university", "college",
                      "network", "online", "digital", "archive", "press", "journal", "herald", "gazette", "tribune",
                      "post", "voice", "review", "weekly", "public", "radio", "television", "station", "group",
                      "campus", "paper", "papers", "other", "school", "community", "national", "american", "county", "local"}


def _org_terms(name: str, homepage: str | None = None) -> list[str]:
    """Ways a report may name the organization: core name, the part in parentheses, distinctive words, web name."""
    core = re.sub(r"^the\s+", "", name.split("/")[0].split("(")[0].strip(), flags=re.I)
    terms = {core}
    if "(" in name:
        terms.add(name.split("(")[1].rstrip(")").strip())
    terms |= {w for w in re.findall(r"[A-Za-z][A-Za-z'’-]{5,}", name) if w.lower() not in GENERIC_NAME_WORDS}
    if homepage:
        stem = re.sub(r"^www\.", "", re.sub(r"^https?://", "", homepage)).split("/")[0].split(".")[0]
        if len(stem) >= 5 and stem.lower() not in GENERIC_NAME_WORDS:
            terms.add(stem)
    return [t for t in terms if len(t) >= 3]


def _substantive_about(db: Database, run_id: str, entity_id: int, row) -> int:
    """Substantive excerpts that support this seed. A secondary report about many outlets (a case study of ten campus
    papers, a feature on several newsrooms) counts only where the excerpt or its context names this organization."""
    rows = db.execute("SELECT excerpt, context FROM evidence_items WHERE entity_id=? AND run_id=? AND about_entity=1 "
                      "AND statement_type NOT IN ('mention')", (entity_id, run_id)).fetchall()
    if row["source_kind"] != "secondary_report":
        return len(rows)
    home = db.scalar("SELECT homepage_url FROM research_entities WHERE cohort!='precedent_case' AND id IN "
                     "(SELECT entity_id FROM entity_sources WHERE source_key=?)", (row["seed_key"],), None) \
        if row["index_id"] else None
    terms = [t.lower() for t in _org_terms(row["organization"], home)]
    return sum(1 for r in rows if any(t in f"{r['excerpt']} {r['context']}".lower() for t in terms))


def recount_index_statuses(db: Database, run_id: str) -> dict:
    """Re-apply the counting rule to an existing verification run (no refetch)."""
    from collections import Counter
    out: Counter = Counter()
    for row in db.execute("SELECT * FROM precedent_seeds WHERE index_id IS NOT NULL AND entity_id IS NOT NULL").fetchall():
        if row["status"] in {"human_verified", "refuted"}:
            continue
        n = _substantive_about(db, run_id, row["entity_id"], row)
        st = "sources_found" if n else "not_found"
        db.execute("UPDATE precedent_seeds SET status=?, status_note=? WHERE id=?",
                   (st, f"{n} substantive excerpt(s) about the organization in run {run_id}", row["id"]))
        out[st] += 1
    db.conn.commit()
    return dict(out)


def set_precedent_status(db: Database, key: str, status: str, note: str = "") -> None:
    if status not in {"human_verified", "refuted", "sources_found", "not_found", "unverified"}:
        raise ValueError("status must be human_verified|refuted|sources_found|not_found|unverified")
    cur = db.execute("UPDATE precedent_seeds SET status=?, status_note=?, last_checked_at=CURRENT_TIMESTAMP WHERE seed_key=?",
                     (status, note or None, key))
    if not cur.rowcount:
        raise ValueError(f"unknown precedent key {key}")
    db.conn.commit()


def add_precedent(db: Database, key: str, organization: str, title: str, claim: str, *, url: str | None, query: str | None,
                  year: str = "", actions: str = "") -> None:
    db.execute("""INSERT INTO precedent_seeds(seed_key,organization,title,approx_year,claim,actions,verification_query,primary_url,prior_confidence)
                  VALUES(?,?,?,?,?,?,?,?,'user_supplied')
                  ON CONFLICT(seed_key) DO UPDATE SET organization=excluded.organization,title=excluded.title,claim=excluded.claim,
                    primary_url=excluded.primary_url,verification_query=excluded.verification_query""",
               (key, organization, title, year, claim, actions, query, url))
    db.conn.commit()

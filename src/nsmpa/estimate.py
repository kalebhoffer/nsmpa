"""Pre-run estimates: search credits, page requests, wall-clock time and AI calls.

Exact where possible: every deterministic query (tier 1, guidance tier, practice queries, the discovery ladder) is
rendered and checked against the search cache, so already-paid queries count as free. Escalation is data-dependent,
so results are a low/high range. Dollar figures appear only if you set prices in config
(``serper_usd_per_credit``, ``ai_usd_per_million_input``, ``ai_usd_per_million_output``).
"""
from __future__ import annotations

from .config import Settings
from .db import Database
from .discovery import DISCOVERY_LADDER
from .research import (
    GUIDANCE_COHORTS,
    PRACTICE_SPECS,
    TIER1,
    TIER3,
    entity_site,
    plan_queries,
    render_query,
    select_entities,
)
from .search import cache_key
from .utils import registrableish_domain


def _cached(db: Database, query: str, count: int) -> bool:
    return bool(db.scalar("SELECT COUNT(*) FROM search_cache WHERE provider='serper' AND query_hash=?", (cache_key(query, count),)))


def estimate_research(db: Database, settings: Settings, entities: list) -> dict:
    n = settings.search_results_per_query
    per_credit = 1 if n <= 10 else 2
    deep = settings.research_depth == "deep"
    cap = max(settings.research_max_searches_per_entity, settings.research_deep_max_searches_per_entity) if deep \
        else settings.research_max_searches_per_entity
    lo = hi = cached = 0
    fp_req_lo = fp_req_hi = wayback_lo = wayback_hi = 0
    for e in entities:
        if not entity_site(e):
            continue
        guidance = e["cohort"] in GUIDANCE_COHORTS
        always: list = list(TIER1)
        maybe: list = []
        if settings.research_depth != "quick":
            (always if guidance else maybe).extend(plan_queries(settings, e, 2))
        if deep:
            always.extend(TIER3)
        if not guidance and settings.research_practice_dig != "never" and settings.research_practice_queries:
            (always if settings.research_practice_dig == "always" else maybe).extend(PRACTICE_SPECS)
        a_live = sum(1 for q in always if not _cached(db, render_query(q, e), n))
        m_live = sum(1 for q in maybe if not _cached(db, render_query(q, e), n))
        cached += (len(always) - a_live) + (len(maybe) - m_live)
        lo += min(a_live, cap)
        hi += min(a_live + m_live, cap)
        fp_req_lo += 3
        fp_req_hi += 1 + settings.research_first_party_link_pages + settings.research_fetch_top_targets
        if not guidance and settings.research_practice_dig != "never":
            fp_req_hi += 17 + settings.research_crime_article_sample + settings.research_baseline_article_sample
            if settings.wayback_enabled:
                wayback_lo += min(settings.wayback_max_checks_per_entity, settings.research_crime_article_sample) // 2
                wayback_hi += settings.wayback_max_checks_per_entity * 3
    delay, conc = settings.per_host_delay_seconds, max(1, settings.research_concurrency)
    t_lo = max(fp_req_lo * delay / conc, wayback_lo * delay)
    t_hi = max(fp_req_hi * delay / conc, wayback_hi * delay)
    return {"entities": len(entities), "queries_low": lo, "queries_high": hi, "queries_already_cached": cached,
            "credits_low": lo * per_credit, "credits_high": hi * per_credit,
            "page_requests_low": fp_req_lo + wayback_lo, "page_requests_high": fp_req_hi + wayback_hi,
            "minutes_low": round(t_lo / 60, 1), "minutes_high": round(t_hi / 60, 1)}


def estimate_discovery(db: Database, settings: Settings, institutions: list) -> dict:
    n = settings.search_results_per_query
    per_credit = 1 if n <= 10 else 2
    ladder = DISCOVERY_LADDER[: settings.discovery_max_searches_per_institution]
    lo = hi = cached = 0
    for inst in institutions:
        qs = [t.format(name=inst["name"].replace('"', ""), domain=registrableish_domain(inst["website"] or ""))
              for t in ladder if "{domain}" not in t or inst["website"]]
        live = [q for q in qs if not _cached(db, q, n)]
        cached += len(qs) - len(live)
        lo += min(1, len(live))
        hi += len(live)
    req_hi = len(institutions) * (settings.discovery_site_pages + 8 + settings.discovery_verify_top_candidates)
    t_hi = req_hi * settings.per_host_delay_seconds / max(1, settings.discovery_concurrency)
    return {"institutions": len(institutions), "queries_low": lo, "queries_high": hi, "queries_already_cached": cached,
            "credits_low": lo * per_credit, "credits_high": hi * per_credit,
            "minutes_low": round(len(institutions) * 4 * settings.per_host_delay_seconds / max(1, settings.discovery_concurrency) / 60, 1),
            "minutes_high": round(t_hi / 60, 1)}


def estimate_ai(settings: Settings, entities: int, *, pages_low_per_entity: int = 2, pages_high_per_entity: int = 20) -> dict:
    lo, hi = entities * pages_low_per_entity, entities * pages_high_per_entity
    tok_in_hi = hi * settings.ai_max_page_chars // 4
    out = {"provider": settings.ai_provider, "model": settings.ai_model, "calls_low": lo, "calls_high": hi,
           "input_tokens_high": tok_in_hi, "output_tokens_high": hi * 1500}
    if settings.ai_usd_per_million_input is not None and settings.ai_usd_per_million_output is not None:
        out["usd_high"] = round(tok_in_hi / 1e6 * settings.ai_usd_per_million_input
                                + hi * 1500 / 1e6 * settings.ai_usd_per_million_output, 2)
    return out


def estimate_all(db: Database, settings: Settings, *, cohort: str | None = None, limit: int | None = None,
                 entity_ids: list[int] | None = None, institutions: int | None = None, states: list[str] | None = None) -> dict:
    ents = select_entities(db, cohort, limit, entity_ids)
    res = {"research": estimate_research(db, settings, ents), "depth": settings.research_depth,
           "practice_dig": settings.research_practice_dig}
    if institutions or states:
        sql = "SELECT unitid,name,website FROM institutions WHERE included=1"
        params: list = []
        if states:
            sql += f" AND state IN ({','.join('?' * len(states))})"
            params += [x.upper() for x in states]
        if institutions:
            sql += " LIMIT ?"
            params.append(institutions)
        res["discovery"] = estimate_discovery(db, settings, db.execute(sql, params).fetchall())
    res["ai_review"] = estimate_ai(settings, len(ents))
    credits_hi = res["research"]["credits_high"] + res.get("discovery", {}).get("credits_high", 0)
    credits_lo = res["research"]["credits_low"] + res.get("discovery", {}).get("credits_low", 0)
    res["total_credits_low"], res["total_credits_high"] = credits_lo, credits_hi
    if settings.serper_usd_per_credit is not None:
        res["serper_usd_low"] = round(credits_lo * settings.serper_usd_per_credit, 2)
        res["serper_usd_high"] = round(credits_hi * settings.serper_usd_per_credit, 2)
    return res

"""Legal context: the law around record clearing and online permanence, kept separate from newsroom evidence.

Seeds are *leads to verify*, not legal conclusions or advice. Only items the author is confident exist carry a
citation; everything else (including one lead per state) carries only a search query. `nsmpa legal-research`
finds and snapshots sources; status moves unverified -> sources_found / not_found automatically and only a person
can set human_verified or refuted. Both helpful and unhelpful authority is included (e.g. Martin v. Hearst).
"""
from __future__ import annotations

import asyncio
import re

from .config import Settings
from .db import Database
from .evidence import split_sentences
from .extract import extract_main_text
from .fetch import HardenedFetcher
from .progress import RunDashboard
from .runs import StopController, create_or_resume_run, done_keys, finish_run, mark_item, register_items
from .search import SearchAuthError, SearchBroker, SearchBudgetExceeded, get_search_provider
from .snapshots import store_raw
from .utils import registrableish_domain

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

CORE_SEEDS: list[dict] = [
    dict(key="eu_gdpr_art17", jurisdiction="European Union", topic="right_to_erasure",
         title="GDPR Article 17: right to erasure ('right to be forgotten')", citation="Regulation (EU) 2016/679, Art. 17",
         claim="Gives individuals a right to erasure of personal data in defined circumstances, with an exception where "
               "processing is necessary for exercising the right of freedom of expression and information (Art. 17(3)(a)).",
         url="https://eur-lex.europa.eu/eli/reg/2016/679/oj", query=None, prior="well_documented"),
    dict(key="eu_google_spain_2014", jurisdiction="European Union", topic="search_delisting",
         title="Google Spain v AEPD and Costeja González (CJEU, 2014)", citation="Case C-131/12",
         claim="The Court of Justice held that search engines can be required to delist results for a person's name in "
               "certain circumstances, while the underlying publication may remain online.",
         url=None, query='"C-131/12" Google Spain Costeja judgment', prior="well_documented"),
    dict(key="us_martin_v_hearst_2015", jurisdiction="United States (2d Cir.)", topic="news_archives_and_erasure",
         title="Martin v. Hearst Corp. (2d Cir. 2015)", citation="777 F.3d 546 (2d Cir. 2015)",
         claim="A state erasure statute did not make accurate news reports of an arrest false or defamatory; news "
               "organizations were not legally required to remove them. (Authority against a legal duty to remove.)",
         url=None, query='"Martin v. Hearst" erasure statute arrest news', prior="well_documented"),
    dict(key="us_first_amendment_context", jurisdiction="United States", topic="no_general_right",
         title="No general U.S. right to be forgotten for accurate news", citation=None,
         claim="Lead: U.S. law generally does not compel removal of accurate, lawfully published news reports; relief is "
               "therefore a matter of editorial discretion. Find authoritative commentary to cite.",
         url=None, query='"right to be forgotten" United States First Amendment news archives', prior="lead"),
    dict(key="us_clean_slate_laws", jurisdiction="United States (multi-state)", topic="automatic_record_clearing",
         title="State 'Clean Slate' automatic record-clearing laws", citation=None,
         claim="Lead: a number of states have enacted laws that automatically seal or clear certain records "
               "(Pennsylvania's 2018 law is commonly cited as the first). Verify each state and date.",
         url=None, query='"Clean Slate" law states automatic record sealing list', prior="lead"),
    dict(key="wa_vacation_statutes", jurisdiction="Washington", topic="record_vacation",
         title="Washington conviction vacation statutes", citation="RCW 9.94A.640; RCW 9.96.060 (verify)",
         claim="Lead: Washington allows vacating certain felony and misdemeanor convictions; verify current text and "
               "effect on public records.",
         url=None, query="RCW 9.94A.640 vacation felony conviction Washington", prior="lead"),
    dict(key="us_mugshot_removal_laws", jurisdiction="United States (multi-state)", topic="mugshot_websites",
         title="State laws on pay-for-removal mugshot websites", citation=None,
         claim="Lead: several states restrict websites that charge fees to remove booking photos. Distinct from news "
               "organizations, but relevant context.",
         url=None, query="state law mugshot website removal fee prohibited", prior="lead"),
]


def seed_legal(db: Database) -> dict:
    rows = list(CORE_SEEDS)
    for abbr, name in STATES.items():
        rows.append(dict(key=f"state_{abbr.lower()}_record_clearing", jurisdiction=name, topic="record_clearing",
                         title=f"{name}: expungement / sealing / vacation law", citation=None,
                         claim=f"Lead: identify {name}'s statute(s) on expunging, sealing or vacating arrest and dismissed-charge "
                               "records, and whether cleared records may be treated as not having occurred.",
                         url=None, query=f'{name} expungement OR "record sealing" statute dismissed charges eligibility site:.gov',
                         prior="lead"))
    with db.transaction():
        for r in rows:
            db.conn.execute(
                """INSERT INTO legal_context(key,jurisdiction,topic,title,claim,citation,primary_url,verification_query,prior_confidence)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET jurisdiction=excluded.jurisdiction,topic=excluded.topic,title=excluded.title,
                     claim=excluded.claim,citation=excluded.citation,primary_url=excluded.primary_url,
                     verification_query=excluded.verification_query,prior_confidence=excluded.prior_confidence""",
                (r["key"], r["jurisdiction"], r["topic"], r["title"], r["claim"], r["citation"], r["url"], r["query"], r["prior"]))
    return {"legal_leads": len(rows)}


LEGAL_TERMS = re.compile(r"\b(?:expunge\w*|seal\w*|vacat\w*|erasure|erase\w*|delist\w*|right to be forgotten|clean slate|"
                         r"dismiss\w*|nonconviction|records? (?:shall|may) be|deemed not to have occurred|personal data)\b", re.I)
AUTHORITATIVE = (".gov", ".us", "europa.eu", "courtlistener.com", "law.cornell.edu", "justia.com", "uscourts.gov",
                 "curia.europa.eu", "legislature", "leg.", "courts.", "ncsl.org")


def _authority(url: str) -> int:
    d = registrableish_domain(url)
    return 2 if any(a in d or d.endswith(a) for a in AUTHORITATIVE) else 1


def best_excerpt(text: str) -> str | None:
    best = None
    for snt in split_sentences(text):
        if 60 <= len(snt) <= 600 and LEGAL_TERMS.search(snt):
            score = len(LEGAL_TERMS.findall(snt))
            if best is None or score > best[0]:
                best = (score, snt)
    return best[1] if best else None


async def run_legal_research(db: Database, settings: Settings, *, max_searches: int | None = None, keys: list[str] | None = None,
                             quiet: bool = False, verbose: bool = False, provider=None, fetcher: HardenedFetcher | None = None,
                             run_id: str | None = None) -> dict:
    seed_legal(db)
    sql = "SELECT * FROM legal_context WHERE status NOT IN ('human_verified','refuted')"
    params: list = []
    if keys:
        sql += f" AND key IN ({','.join('?' * len(keys))})"
        params += keys
    rows = db.execute(sql + " ORDER BY id", params).fetchall()
    provider = provider or get_search_provider(settings.search_provider, settings.user_agent)
    rid, _ = create_or_resume_run(db, settings, "legal_research", run_id, params={"keys": keys}, max_searches=max_searches)
    broker = SearchBroker(db, settings, rid, provider, max_searches=max_searches)
    register_items(db, rid, "legal", [r["key"] for r in rows])
    done = done_keys(db, rid, "legal")
    stop = StopController()
    dash = RunDashboard("NSMPA Legal context", len(rows), quiet=quiet, verbose=verbose, universe="Leads to verify",
                        db=db, run_id=rid, persist_seconds=settings.heartbeat_seconds)
    own = fetcher is None
    fetcher = fetcher or HardenedFetcher(settings, on_event=dash.log)
    status, reason, found = "completed", None, 0
    with dash:
        dash.update(completed=len(done), skipped_done=len(done), budget_limit=max_searches)
        uninstall = stop.install()
        try:
            for r in rows:
                if stop.stop_requested:
                    status, reason = "interrupted", stop.reason
                    break
                if r["key"] in done:
                    continue
                mark_item(db, rid, "legal", r["key"], "running")
                dash.update(current=r["title"][:70])
                try:
                    urls = [r["primary_url"]] if r["primary_url"] else []
                    if r["verification_query"] and provider.name != "none":
                        results, _, _ = await broker.search(r["verification_query"], purpose="legal_research")
                        dash.update(searches_live=broker.live_calls, searches_cached=broker.cached_calls, credits_estimated=broker.credits_used)
                        urls += [x.url for x in sorted(results, key=lambda x: (-_authority(x.url), x.rank))[:3]]
                    best = None
                    for u in urls[:3]:
                        page = await fetcher.fetch_safe(u)
                        if page.access_class != "ok":
                            continue
                        dash.increment(pages_fetched=1)
                        mt = extract_main_text(page.content, page.content_type, page.final_url, page.headers)
                        ex = best_excerpt(mt.main_text or mt.full_text)
                        if ex and (best is None or _authority(page.final_url) > best[0]):
                            store_raw(db, settings.research_snapshot_dir, page.content, page.content_type, page.final_url)
                            best = (_authority(page.final_url), ex, page.final_url)
                    st = "sources_found" if best else "not_found"
                    db.execute("""UPDATE legal_context SET status=?, best_excerpt=?, best_source=?, status_note=?,
                                    last_checked_at=CURRENT_TIMESTAMP WHERE id=?""",
                               (st, best[1] if best else None, best[2] if best else None,
                                f"run {rid}; {'authoritative domain' if best and best[0] == 2 else 'secondary source' if best else 'nothing relevant fetched'}",
                                r["id"]))
                    found += int(bool(best))
                    mark_item(db, rid, "legal", r["key"], "done")
                    dash.add_recent(f"{'✓' if best else '·'} {r['jurisdiction']}: {st}")
                except SearchBudgetExceeded as exc:
                    mark_item(db, rid, "legal", r["key"], "pending", error=str(exc))
                    status, reason = "budget_exhausted", str(exc)
                    break
                except SearchAuthError as exc:
                    mark_item(db, rid, "legal", r["key"], "pending", error=str(exc))
                    status, reason = "failed", f"search_auth_error: {exc}"
                    break
                except asyncio.CancelledError:
                    mark_item(db, rid, "legal", r["key"], "pending", error="cancelled")
                    status, reason = "interrupted", "force-cancelled"
                    break
                except Exception as exc:  # one bad lead never stops the run
                    mark_item(db, rid, "legal", r["key"], "failed", error=f"{type(exc).__name__}: {exc}"[:500])
                    dash.increment(errors=1)
                finally:
                    dash.increment(completed=1)
                    db.conn.commit()
        finally:
            uninstall()
            if own:
                await fetcher.close()
            await broker.aclose()
    finish_run(db, rid, status, reason)
    return {"run_id": rid, "status": status, "stop_reason": reason, "with_sources": found,
            "remaining": len(rows) - len(done_keys(db, rid, "legal")), "searches_live": broker.live_calls,
            "credits_estimated": broker.credits_used}


def set_legal_status(db: Database, key: str, status: str, note: str = "") -> None:
    if status not in {"human_verified", "refuted", "sources_found", "not_found", "unverified"}:
        raise ValueError("status must be human_verified|refuted|sources_found|not_found|unverified")
    if not db.execute("UPDATE legal_context SET status=?, status_note=? WHERE key=?", (status, note or None, key)).rowcount:
        raise ValueError(f"unknown legal key {key}")
    db.conn.commit()

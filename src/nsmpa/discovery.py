from __future__ import annotations

import asyncio
import html
import json
import re
from collections import deque
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .extract import decode_html, extract_main_text

from .config import Settings
from .db import Database
from .fetch import HardenedFetcher
from .models import SearchResult
from .progress import RunDashboard
from .review import enqueue_publication_review
from .runs import StopController, create_or_resume_run, done_keys, finish_run, mark_item, register_items
from .search import SearchAuthError, SearchBroker, SearchBudgetExceeded, get_search_provider
from .utils import (is_blocked_social_or_aggregator, normalize_url, prepare_request_url, registrableish_domain,
                    same_site)

# Discovery intentionally distinguishes student journalism from institutional PR.
STRONG_STUDENT_TERMS = re.compile(
    r"\b(?:independent\s+student\s+(?:newspaper|news|media|press)|"
    r"student[-\s]+run\s+(?:newspaper|news|media|press)|"
    r"student\s+(?:newspaper|newspaper|news(?:paper)?|media|press|publication|publications)|"
    r"campus\s+newspaper|college\s+newspaper)\b",
    re.I,
)
NEWSPAPER_TERMS = re.compile(r"\b(?:newspaper|newspapers|student press|news publication)\b", re.I)
PAPER_NAME_TERMS = re.compile(
    r"\b(?:daily|times|review|chronicle|herald|sentinel|observer|gazette|tribune|"
    r"collegian|collegiate|oracle|lantern|beacon|echo|signal|spectator|journal)\b",
    re.I,
)
PR_TERMS = re.compile(
    r"\b(?:newsroom|pressroom|press room|media relations|public relations|"
    r"university news|college news|institutional news|press releases?|"
    r"marketing and communications|communications and marketing|news and stories|"
    r"news & stories|faculty news|staff news|alumni news)\b",
    re.I,
)
BAD_TERMS = re.compile(
    r"\b(?:athletics|admissions|alumni|bookstore|jobs|directory|library catalog|"
    r"emergency alerts?|research news|development office)\b",
    re.I,
)
# Pages worth inspecting to find the actual publication link. This is broader than
# candidate vocabulary because student newspapers are often linked from student-life hubs.
NAV_TERMS = re.compile(
    r"(?:student[-_/ ]?(?:media|newspaper|news|press|publication|publications|life|activities|"
    r"organizations?|clubs|involvement|engagement)|campus[-_/ ]?(?:media|newspaper|life)|journalism|"
    r"newspaper|publications?|clubs?[-_ &]+(?:and[-_ ])?org|get[-_ ]involved|involvement|registered[-_ ]student)",
    re.I,
)
GENERIC_NEWS_PATH = re.compile(r"/(?:news|newsroom|pressroom|press|media-relations)(?:/|$)", re.I)


def _name_tokens(name: str) -> list[str]:
    stop = {"university", "college", "the", "of", "at", "and", "campus", "system"}
    tokens = [t.lower() for t in re.findall(r"[A-Za-z0-9]+", name) if len(t) >= 3 and t.lower() not in stop]
    return tokens[:8]


def score_candidate(inst_name: str, inst_website: str | None, result: SearchResult) -> tuple[float, list[str]]:
    """Score a possible student-news publication conservatively.

    Institutional PR/newsroom pages are explicitly penalized. A same-site university
    page is not enough by itself; there must be a student-journalism signal.
    """
    url = normalize_url(result.url)
    if not url or is_blocked_social_or_aggregator(url):
        return 0.0, ["blocked_or_invalid_domain"]

    hay = f"{result.title} {result.snippet} {url}".lower()
    reasons: list[str] = []
    score = 0.0

    strong_student = bool(STRONG_STUDENT_TERMS.search(hay))
    newspaper = bool(NEWSPAPER_TERMS.search(hay))
    paper_name = bool(PAPER_NAME_TERMS.search(result.title or ""))
    pr_context = bool(PR_TERMS.search(hay)) or bool(GENERIC_NEWS_PATH.search(urlsplit(url).path))

    if strong_student:
        score += 0.50
        reasons.append("explicit_student_journalism_signal")
        reasons.append("student_news_terms")
    if newspaper:
        score += 0.12
        reasons.append("newspaper_term")
    if paper_name and (strong_student or newspaper or result.provider in {"brave", "serper"}):
        score += 0.08
        reasons.append("publication_name_signal")

    tokens = _name_tokens(inst_name)
    matched = sum(1 for t in tokens if t in hay)
    if tokens and matched:
        frac = matched / len(tokens)
        score += min(0.16, 0.05 + 0.16 * frac)
        reasons.append(f"institution_name_tokens:{matched}/{len(tokens)}")

    if inst_website and same_site(inst_website, url):
        # Same-site is weak corroboration, not strong evidence of student journalism.
        score += 0.06
        reasons.append("same_site_as_institution")
    elif inst_website and strong_student:
        # Student papers are frequently hosted on independent domains.
        score += 0.06
        reasons.append("independent_domain_with_student_signal")

    path = urlsplit(url).path.lower()
    if re.search(r"student[-_/]?(?:media|newspaper|news|press|publication)", path):
        score += 0.15
        reasons.append("student_media_url")
    elif "newspaper" in path:
        score += 0.10
        reasons.append("newspaper_url")

    # Search results are only lightly rewarded and only when the result itself has
    # a journalism-ish signal. The query text alone can never make a result valid.
    if result.provider in {"brave", "serper"} and (strong_student or newspaper or paper_name):
        if result.rank:
            score += max(0.0, 0.07 - (result.rank - 1) * 0.008)
            reasons.append(f"search_rank:{result.rank}")

    if BAD_TERMS.search(hay):
        score -= 0.30
        reasons.append("bad_context_terms")

    if pr_context:
        penalty = 0.25 if strong_student else 0.75
        score -= penalty
        reasons.append("institutional_pr_penalty")

    # A generic same-site news page with no student-journalism signal should never
    # survive as a plausible publication candidate.
    if not strong_student and not newspaper and not paper_name:
        score = min(score, 0.10)
        reasons.append("no_student_journalism_signal_cap")

    return max(0.0, min(score, 1.0)), reasons


def _html_links(content: bytes, base_url: str) -> tuple[str, list[tuple[str, str]]]:
    soup = BeautifulSoup(decode_html(content), "html.parser")
    title = ""
    if soup.title:
        title = " ".join(soup.title.stripped_strings)
    links: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        text = " ".join(a.stripped_strings)
        href = prepare_request_url(str(a.get("href")), base=base_url)
        if not href or is_blocked_social_or_aggregator(href):
            continue
        links.append((text, href))
    return title, links


def _link_is_direct_candidate(text: str, href: str) -> bool:
    hay = f"{text} {href}"
    return bool(STRONG_STUDENT_TERMS.search(hay) or NEWSPAPER_TERMS.search(hay))


def _page_is_student_media_context(url: str, title: str, content: bytes) -> bool:
    # Limit body context to avoid treating one unrelated footer link as page identity.
    text = BeautifulSoup(decode_html(content), "html.parser").get_text(" ", strip=True)[:4000]
    hay = f"{url} {title} {text}"
    return bool(STRONG_STUDENT_TERMS.search(hay)) and not (
        PR_TERMS.search(f"{title} {url}") and not re.search(r"student", f"{title} {url}", re.I)
    )


def _sitemap_locs(content: bytes) -> list[str]:
    # Regex is deliberately permissive because institutional sitemaps vary widely.
    text = content.decode("utf-8", errors="replace")
    return [html.unescape(x.strip()) for x in re.findall(r"<loc\b[^>]*>(.*?)</loc>", text, flags=re.I | re.S)]


async def _sitemap_navigation_urls(fetcher: HardenedFetcher, website: str, max_urls: int = 20) -> list[str]:
    """Find likely student-media navigation pages from one or two sitemap layers."""
    root = normalize_url(website)
    if not root:
        return []
    parts = urlsplit(root)
    origin = f"{parts.scheme}://{parts.netloc}"
    sitemap_seeds = [origin + "/sitemap.xml", origin + "/sitemap_index.xml"]
    seen_maps: set[str] = set()
    candidate_pages: list[str] = []
    nested_maps: list[str] = []

    async def inspect_map(map_url: str) -> None:
        if map_url in seen_maps or len(seen_maps) >= 8:
            return
        seen_maps.add(map_url)
        r = await fetcher.fetch_safe(map_url)
        if r.access_class != "ok" or not r.content:
            return
        for raw in _sitemap_locs(r.content)[:8000]:
            loc = normalize_url(raw)
            if not loc or not same_site(root, loc):
                continue
            path = urlsplit(loc).path.lower()
            if ("sitemap" in path or path.endswith(".xml")) and len(nested_maps) < 8:
                nested_maps.append(loc)
            elif NAV_TERMS.search(loc) and loc not in candidate_pages:
                candidate_pages.append(loc)
                if len(candidate_pages) >= max_urls:
                    return

    for seed in sitemap_seeds:
        await inspect_map(seed)
        if len(candidate_pages) >= max_urls:
            break
    for nested in nested_maps[:6]:
        if len(candidate_pages) >= max_urls:
            break
        await inspect_map(nested)
    return candidate_pages[:max_urls]


async def institution_site_candidates(fetcher: HardenedFetcher, website: str, inst_name: str, max_pages: int = 20) -> list[SearchResult]:
    """Bounded institution-site discovery with sitemap and student-life recovery passes.

    It inspects the homepage, a small number of likely student-life/media pages, and
    follows external links from pages that clearly describe student media. This allows
    discovery of independently hosted student newspapers without broad crawling.
    """
    root = normalize_url(website)
    if not root:
        return []

    queue: deque[tuple[str, int, str]] = deque([(root, 0, "institution_homepage")])
    queued: set[str] = {root}
    # Cheap probes of conventional student-media locations (404s are recorded, not errors).
    origin = f"{urlsplit(root).scheme}://{urlsplit(root).netloc}"
    for path in ("/student-media", "/studentmedia", "/student-media/", "/student-life/student-media"):
        probe = prepare_request_url(origin + path)
        if probe and probe not in queued:
            queued.add(probe)
            queue.append((probe, 1, "institution_path_probe"))
    visited: set[str] = set()
    out: list[SearchResult] = []

    # Sitemap URLs give us a recovery route when the homepage does not link student media.
    try:
        for u in await _sitemap_navigation_urls(fetcher, root, max_urls=16):
            if u not in queued:
                queued.add(u)
                queue.append((u, 1, "institution_sitemap"))
    except Exception as exc:
        fetcher._event(f"sitemap discovery failed for {root}: {type(exc).__name__}: {exc}")

    while queue and len(visited) < max_pages:
        page_url, depth, source = queue.popleft()
        ident = normalize_url(page_url) or page_url  # /a and /a/ are one page for budgeting purposes
        if ident in visited:
            continue
        visited.add(ident)
        r = await fetcher.fetch_safe(page_url)
        if r.access_class != "ok" or "html" not in (r.content_type or "html"):
            continue

        try:
            title, links = _html_links(r.content, r.final_url)
        except Exception:
            continue
        student_context = _page_is_student_media_context(r.final_url, title, r.content)

        # A student-media hub is itself useful as an intermediate candidate, but its
        # external publication links are even more valuable.
        if STRONG_STUDENT_TERMS.search(f"{title} {r.final_url}"):
            out.append(SearchResult(
                url=r.final_url,
                title=title or "Student Media",
                snippet=f"Student-media page discovered on {inst_name} site",
                rank=0,
                provider=source,
                query=root,
            ))

        for text, href in links:
            same = same_site(root, href)
            if _link_is_direct_candidate(text, href):
                out.append(SearchResult(
                    url=href,
                    title=text or href,
                    snippet=f"Link discovered from {r.final_url}",
                    rank=0,
                    provider=source,
                    query=root,
                ))
            elif student_context and not same:
                # Student-media pages often link to independently hosted publications
                # by brand name only, e.g. "The Northern Light".
                if text and 2 <= len(text.split()) <= 12:
                    out.append(SearchResult(
                        url=href,
                        title=text,
                        snippet=f"External link from student media page at {r.final_url}",
                        rank=0,
                        provider="institution_student_media_external",
                        query=root,
                    ))

            # Follow only a narrow set of same-site navigation pages and never exceed
            # depth 2. This keeps discovery bounded for a national run.
            if same and depth < 2 and href not in queued and NAV_TERMS.search(f"{text} {href}"):
                if not PR_TERMS.search(f"{text} {href}") or re.search(r"student", f"{text} {href}", re.I):
                    queued.add(href)
                    queue.append((href, depth + 1, "institution_navigation"))

    # Deduplicate by exact URL. Domain-level dedupe is intentionally avoided because
    # the first same-domain result may be a university hub rather than the publication.
    unique: dict[str, SearchResult] = {}
    for item in out:
        unique.setdefault(item.url, item)
    return list(unique.values())[:50]


DISCOVERY_LADDER = [
    '"{name}" student newspaper',
    '"{name}" student media',
    '"{name}" campus newspaper',
    '"{name}" independent student newspaper',
    "site:{domain} student newspaper",
]

VERIFY_STUDENT = re.compile(
    r"\b(?:student[-\s]run|student newspaper|student media|student journalists?|student[-\s]produced|"
    r"independent student|editor[-\s]in[-\s]chief|managing editor|news editor|staff (?:box|list|directory)|masthead|"
    r"campus newspaper|college newspaper|student publication|student voice)\b", re.I)
VERIFY_PR = re.compile(
    r"\b(?:office of (?:university |college )?(?:communications?|marketing|public affairs|media relations)|"
    r"university communications|marketing (?:and|&) communications|media relations|news releases?|press releases?|"
    r"for (?:the )?media|media contacts?|public affairs office|university relations)\b", re.I)
SECTION_LINK = re.compile(r"/(?:news|sports|opinion|opinions|arts|culture|features|campus|life|editorial)(?:/|$)", re.I)
DATE_LINK = re.compile(r"/(?:19|20)\d{2}/\d{1,2}/")


async def verify_candidate(fetcher: HardenedFetcher, inst_name: str, inst_website: str | None, url: str) -> dict:
    """Fetch a candidate (its site root when independently hosted) and look for student-newsroom identity."""
    target = url
    try:
        p = urlsplit(url)
        if inst_website and not same_site(inst_website, url):
            target = f"{p.scheme}://{p.netloc}/"
    except ValueError:
        return {"verified": False, "delta": 0.0, "reason": "invalid_url"}
    r = await fetcher.fetch_safe(target)
    if r.access_class != "ok" or not r.content:
        return {"verified": False, "delta": 0.0, "reason": f"unreachable:{r.access_class}", "url": target}
    page = extract_main_text(r.content, r.content_type, r.final_url, r.headers)
    top = f"{page.title} {page.full_text[:8000]}"
    tokens = _name_tokens(inst_name)
    inst_hits = sum(1 for t in tokens if t in top.lower())
    student = bool(VERIFY_STUDENT.search(top) or STRONG_STUDENT_TERMS.search(top))
    pr = bool(VERIFY_PR.search(f"{page.title} {page.full_text[:3000]}"))
    newsy = sum(1 for _, h in page.links if SECTION_LINK.search(h) or DATE_LINK.search(h))
    delta = 0.0
    signals = []
    if student:
        delta += 0.12
        signals.append("student_newsroom_identity")
    if tokens and inst_hits >= max(1, len(tokens) // 2):
        delta += 0.05
        signals.append("institution_named")
    if newsy >= 5:
        delta += 0.05
        signals.append(f"news_site_structure:{newsy}")
    if pr and not student:
        delta -= 0.45
        signals.append("institutional_pr_identity")
    return {"verified": True, "delta": round(delta, 3), "signals": signals, "url": r.final_url, "title": page.title[:200]}


def _domain_key(url: str) -> str:
    return registrableish_domain(url)


async def discover_institution(
    db: Database,
    settings: Settings,
    fetcher: HardenedFetcher,
    broker: SearchBroker,
    row,
    dashboard: RunDashboard | None = None,
    run_id: str | None = None,
) -> dict[str, int | float]:
    unitid, name, website = row["unitid"], row["name"], row["website"]
    early = settings.publication_early_stop_threshold
    results: list[SearchResult] = []
    scored: dict[str, tuple[SearchResult, float, list[str]]] = {}
    verification: dict[str, dict] = {}
    searches = 0

    def phase(msg: str) -> None:
        if dashboard:
            dashboard.update(phase=msg)

    def rescore(new: list[SearchResult]) -> None:
        for res in new:
            key = normalize_url(res.url)
            req = prepare_request_url(res.url)
            if not key or not req:
                continue
            res.url = req
            sc, reasons = score_candidate(name, website, res)
            if sc < 0.15:
                if dashboard:
                    dashboard.log(f"  reject {sc:.2f} {req} {reasons[-2:]}")
                continue
            prev = scored.get(key)
            if prev is None or sc > prev[1]:
                scored[key] = (res, sc, reasons)

    def effective(key: str) -> float:
        base = scored[key][1]
        v = verification.get(_domain_key(scored[key][0].url))
        return max(0.0, min(1.0, base + (v["delta"] if v else 0.0)))

    async def verify_top() -> float:
        if settings.discovery_verify_top_candidates <= 0:
            return max((s for _, s, _ in scored.values()), default=0.0)
        ranked = sorted(scored, key=lambda k: -scored[k][1])
        doms: list[str] = []
        for key in ranked:
            dom = _domain_key(scored[key][0].url)
            if dom in doms:
                continue
            doms.append(dom)
            if dom not in verification and scored[key][1] >= 0.35:
                phase(f"verify {dom}")
                verification[dom] = await verify_candidate(fetcher, name, website, scored[key][0].url)
                if dashboard:
                    dashboard.log(f"  verify {dom}: {verification[dom]}")
            if len(doms) >= settings.discovery_verify_top_candidates:
                break
        return max((effective(k) for k in scored), default=0.0)

    async def run_query(template: str) -> bool:
        nonlocal searches
        domain = registrableish_domain(website) if website else ""
        if "{domain}" in template and not domain:
            return False
        query = template.format(name=name.replace('"', ""), domain=domain)
        phase(f"search: {query[:60]}")
        found, _qid, cached = await broker.search(query, purpose="publication_discovery", unitid=unitid,
                                                   count=settings.search_results_per_query)
        searches += 1
        if dashboard:
            dashboard.update(searches_live=broker.live_calls, searches_cached=broker.cached_calls,
                             searches_failed=broker.failed_calls, credits_estimated=broker.credits_used)
            dashboard.log(f"  query {'(cached) ' if cached else ''}{query} -> {len(found)} results")
        results.extend(found)
        rescore(found)
        return True

    ladder = DISCOVERY_LADDER[: settings.discovery_max_searches_per_institution]
    use_search = broker.provider.name != "none"
    best = 0.0
    # 1) First ladder query (1 credit) usually finds the paper outright.
    if use_search and ladder:
        await run_query(ladder[0])
        best = await verify_top()
    # 2) Free: institution website, sitemap and student-life pages (also finds independent domains).
    if best < early and website and settings.discovery_site_pages > 0:
        phase("university site + sitemap")
        try:
            site = await institution_site_candidates(fetcher, website, name, max_pages=settings.discovery_site_pages)
            results.extend(site)
            rescore(site)
            best = await verify_top()
        except Exception as exc:  # one broken site never terminates the run
            db.execute("INSERT INTO errors(unitid,stage,error_type,message,retryable,research_run_id) VALUES(?,?,?,?,1,?)",
                       (unitid, "discovery_institution_site", type(exc).__name__, str(exc)[:1000], run_id))
            if dashboard:
                dashboard.increment(errors=1)
                dashboard.log(f"site discovery error {name}: {exc}")
    # 3) Escalate through the remaining ladder only while confidence stays below the stop threshold.
    if use_search:
        for template in ladder[1:]:
            if best >= early:
                break
            if await run_query(template):
                best = await verify_top()

    inserted = high = 0
    top_score = 0.0
    with db.transaction():
        for key, (res, sc, reasons) in scored.items():
            v = verification.get(_domain_key(res.url))
            eff = effective(key)
            top_score = max(top_score, eff)
            db.conn.execute(
                """
                INSERT INTO publication_candidates(unitid,url,domain,title,snippet,source,query,score,score_reasons_json,
                  discovery_run_id,verification_json,verified_score)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(unitid,url) DO UPDATE SET
                  title=excluded.title,snippet=excluded.snippet,source=excluded.source,query=excluded.query,
                  score=excluded.score,score_reasons_json=excluded.score_reasons_json,discovery_run_id=excluded.discovery_run_id,
                  verification_json=excluded.verification_json,verified_score=excluded.verified_score,updated_at=CURRENT_TIMESTAMP
                """,
                (unitid, res.url, registrableish_domain(res.url), res.title, res.snippet, res.provider, res.query, sc,
                 json.dumps(reasons), run_id, json.dumps(v or {}), eff),
            )
            inserted += 1
            if eff >= settings.publication_confidence_threshold:
                high += 1
    if dashboard and top_score >= settings.publication_confidence_threshold:
        best_key = max(scored, key=effective)
        dashboard.add_recent(f"✓ {name}: {scored[best_key][0].title[:50] or scored[best_key][0].url} [{top_score:.2f}]")
    elif dashboard:
        dashboard.add_recent(f"? {name}: unresolved (best {top_score:.2f})")
    return {"candidates": inserted, "high": high, "searches": searches, "top_score": round(top_score, 3)}


async def discover_all(
    db: Database,
    settings: Settings,
    limit: int | None = None,
    *,
    run_id: str | None = None,
    quiet: bool = False,
    verbose: bool = False,
    max_searches: int | None = None,
    refresh_search: bool = False,
    fresh: bool = False,
    states: list[str] | None = None,
    unitids: list[str] | None = None,
    provider=None,
    fetcher: HardenedFetcher | None = None,
    stop: StopController | None = None,
    command: str | None = None,
) -> dict[str, int | str]:
    provider = provider or get_search_provider(settings.search_provider, settings.user_agent)
    rid, resumed = create_or_resume_run(db, settings, "publication_discovery", run_id,
                                        params={"limit": limit, "states": states, "unitids": unitids},
                                        command=command, max_searches=max_searches)
    broker = SearchBroker(db, settings, rid, provider, max_searches=max_searches, refresh=refresh_search)
    sql = "SELECT unitid,name,website,state FROM institutions WHERE included=1"
    params: list = []
    if states:
        sql += f" AND state IN ({','.join('?' * len(states))})"
        params += [s.upper() for s in states]
    if unitids:
        sql += f" AND unitid IN ({','.join('?' * len(unitids))})"
        params += unitids
    sql += " ORDER BY unitid"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    rows = db.execute(sql, params).fetchall()
    register_items(db, rid, "institution", [r["unitid"] for r in rows])
    if fresh:
        db.execute("UPDATE run_items SET status='pending' WHERE run_id=? AND item_type='institution'", (rid,))
        db.conn.commit()
    done = done_keys(db, rid, "institution")
    todo = [r for r in rows if r["unitid"] not in done]
    stop = stop or StopController()
    stats = {"candidates": 0, "high": 0, "processed": 0, "failed": 0}
    stop_reason = ""
    dash = RunDashboard("NSMPA Publication Discovery", len(rows), quiet=quiet, verbose=verbose,
                        universe="Student Journalism (IPEDS four-year)", db=db, run_id=rid,
                        persist_seconds=settings.heartbeat_seconds)
    own_fetcher = fetcher is None
    fetcher = fetcher or HardenedFetcher(settings, on_event=dash.log)
    queue: deque = deque(todo)
    with dash:
        dash.update(completed=len(rows) - len(todo), skipped_done=len(rows) - len(todo), budget_limit=max_searches)
        if resumed:
            dash.add_recent(f"Resumed run {rid}: {len(rows) - len(todo)} institutions already complete")
        stop.on_stop(dash.notice)
        uninstall = stop.install()

        async def worker() -> None:
            nonlocal stop_reason
            while queue and not stop.stop_requested:
                row = queue.popleft()
                key = row["unitid"]
                dash.update(current=row["name"], publication="")
                mark_item(db, rid, "institution", key, "running")
                try:
                    res = await discover_institution(db, settings, fetcher, broker, row, dash, rid)
                    mark_item(db, rid, "institution", key, "done", result=res)
                    stats["candidates"] += int(res["candidates"])
                    stats["high"] += int(res["high"])
                    stats["processed"] += 1
                    dash.update(candidates=stats["candidates"], high_confidence=stats["high"])
                except SearchBudgetExceeded as exc:
                    mark_item(db, rid, "institution", key, "pending", error=str(exc))
                    stop_reason = f"budget_exhausted: {exc}"
                    stop.stop_requested = True
                    dash.notice(f"Search budget reached ({exc}). Checkpointing; rerun the same command to resume.")
                except SearchAuthError as exc:
                    mark_item(db, rid, "institution", key, "pending", error=str(exc))
                    stop_reason = f"search_auth_error: {exc}"
                    stop.stop_requested = True
                    dash.notice(f"Search provider rejected the API key/account: {exc}")
                except asyncio.CancelledError:
                    mark_item(db, rid, "institution", key, "pending", error="cancelled")
                    raise
                except Exception as exc:
                    stats["failed"] += 1
                    mark_item(db, rid, "institution", key, "failed", error=f"{type(exc).__name__}: {exc}"[:500])
                    db.execute("INSERT INTO errors(unitid,stage,error_type,message,retryable,research_run_id) VALUES(?,?,?,?,1,?)",
                               (key, "discovery", type(exc).__name__, str(exc)[:1000], rid))
                    db.conn.commit()
                    dash.increment(errors=1)
                    dash.log(f"discovery failure {row['name']}: {type(exc).__name__}: {exc}")
                finally:
                    st = fetcher.stats
                    dash.increment(completed=1)
                    dash.update(pages_fetched=st.fetched_ok, robots_blocked=st.robots_blocked, access_blocked=st.access_blocked,
                                malformed_skipped=st.malformed_skipped, retries=st.retries)
                    dash.checkpoint()

        tasks = [asyncio.create_task(worker()) for _ in range(min(settings.discovery_concurrency, max(1, len(todo))))]
        for t in tasks:
            stop.track(t)
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            stop_reason = stop_reason or "force-cancelled by user"
        finally:
            dash.update(phase="stopped" if stop.stop_requested else "complete", current="-")
            uninstall()
            if own_fetcher:
                await fetcher.close()
            await broker.aclose()
    remaining = len(rows) - len(done_keys(db, rid, "institution"))
    if stop_reason.startswith("budget"):
        status = "budget_exhausted"
    elif stop_reason.startswith("search_auth"):
        status = "failed"
    elif stop.stop_requested and remaining:
        status, stop_reason = "interrupted", stop_reason or stop.reason
    else:
        status = "completed"
    finish_run(db, rid, status, stop_reason or None)
    with_candidates = db.scalar(
        "SELECT COUNT(DISTINCT c.unitid) FROM publication_candidates c JOIN institutions i ON i.unitid=c.unitid "
        "WHERE i.included=1 AND COALESCE(c.verified_score,c.score)>=?", (settings.publication_confidence_threshold,))
    return {
        "run_id": rid, "status": status, "stop_reason": stop_reason, "institutions_selected": len(rows),
        "processed_this_invocation": stats["processed"], "failed": stats["failed"], "remaining": remaining,
        "candidates": stats["candidates"], "high_confidence_candidates": stats["high"],
        "institutions_with_high_confidence_candidate_total": int(with_candidates or 0),
        "provider": provider.name, "searches_live": broker.live_calls, "searches_cached": broker.cached_calls,
        "searches_failed": broker.failed_calls, "credits_estimated": broker.credits_used,
    }


_NAME_SPLIT = re.compile(r"\s+[|–—:-]\s+|\s+-\s+")
_GENERIC_NAME = re.compile(r"^(?:home|homepage|news|student media|student newspaper|the student newspaper|welcome|index|"
                           r"student publications?|campus newspaper|official site)$", re.I)


def clean_publication_name(title: str | None, url: str, inst_name: str) -> str:
    parts = [p.strip() for p in _NAME_SPLIT.split(title or "") if p.strip()]
    for p in parts:
        if _GENERIC_NAME.match(p) or STRONG_STUDENT_TERMS.fullmatch(p):
            continue
        if PAPER_NAME_TERMS.search(p) or p.lower().startswith("the "):
            return p[:120]
    for p in parts:
        if not _GENERIC_NAME.match(p) and len(p) <= 80 and p.lower() != inst_name.lower():
            return p[:120]
    host = registrableish_domain(url)
    return f"{inst_name} student publication ({host})"


def _relationship(inst_website: str | None, url: str) -> str:
    if not inst_website:
        return "unknown"
    if not same_site(inst_website, url):
        return "independent_domain"
    host = registrableish_domain(url)
    inst = registrableish_domain(inst_website)
    if host != inst:
        return "institution_subdomain"
    return "institution_path"


def _ai_pick(c) -> dict | None:
    try:
        return json.loads(c["ai_pick_json"]) if c["ai_pick_json"] else None
    except (ValueError, TypeError, IndexError, KeyError):
        return None


def promote_candidates(db: Database, threshold: float, margin: float = 0.08, *, use_ai: bool = False,
                       ai_min_confidence: float = 0.6) -> dict[str, int]:
    """Promote the best candidate per institution, preserving human/manual verifications.

    - Effective score = verified_score when available, else search score.
    - A student-media hub page on the university site loses to an independently hosted publication
      that scores within 0.10, because hubs usually link to the actual paper.
    - Two distinct domains above threshold within ``margin`` are flagged ``ambiguous`` and queued for review.
    - ``use_ai``: a confident AI pick (``nsmpa ai-discovery``) scoring within 0.15 of the threshold breaks ties or lifts a
      near-threshold candidate; a confident AI "none of these" blocks promotion. Every AI-assisted case is queued for review.
    """
    protected = {r[0] for r in db.execute(
        "SELECT unitid FROM publications WHERE verification_status IN ('manual','human_verified')")}
    rows = db.execute(
        """
        SELECT c.*, COALESCE(c.verified_score, c.score) AS eff, i.name AS institution_name, i.website
        FROM publication_candidates c JOIN institutions i ON i.unitid=c.unitid
        WHERE c.status NOT IN ('rejected') AND COALESCE(c.verified_score, c.score) >= ?
        ORDER BY c.unitid, eff DESC, c.id
        """, (threshold - (0.15 if use_ai else 0.10),)).fetchall()
    by_unit: dict[str, list] = {}
    for r in rows:
        by_unit.setdefault(r["unitid"], []).append(r)
    promoted = ambiguous = skipped_protected = ai_assisted = ai_blocked = 0
    with db.transaction():
        for unitid, cands in by_unit.items():
            if unitid in protected:
                skipped_protected += 1
                continue
            best_by_domain: dict[str, object] = {}
            for c in cands:
                best_by_domain.setdefault(c["domain"], c)
            ranked = sorted(best_by_domain.values(), key=lambda c: -c["eff"])
            best = ranked[0]
            ai_used = False
            if use_ai:
                picks = [(c, _ai_pick(c)) for c in cands if _ai_pick(c)]
                says_none = [p for _, p in picks if p.get("choice") == "none" and p.get("confidence", 0) >= max(ai_min_confidence, 0.7)]
                chosen = [c for c, p in picks if p.get("choice") == "this" and p.get("confidence", 0) >= ai_min_confidence
                          and c["eff"] >= threshold - 0.15]
                tied = len(ranked) > 1 and ranked[1]["eff"] >= threshold and ranked[0]["eff"] - ranked[1]["eff"] <= margin
                if says_none and (best["eff"] < threshold + 0.1 or tied):
                    ai_blocked += 1
                    continue
                if chosen:
                    best, ai_used = chosen[0], True
            if best["eff"] < threshold and not ai_used:
                continue
            if not ai_used and _relationship(best["website"], best["url"]) != "independent_domain":
                indep = [c for c in ranked[1:] if _relationship(c["website"], c["url"]) == "independent_domain"
                         and c["eff"] >= best["eff"] - 0.10]
                if indep:
                    best = indep[0]
            rivals = [c for c in ranked if c is not best and c["eff"] >= threshold and abs(best["eff"] - c["eff"]) <= margin]
            is_amb = int(bool(rivals)) if not ai_used else 0
            rel = _relationship(best["website"], best["url"])
            pub_name = clean_publication_name(best["title"], best["url"], best["institution_name"])
            db.conn.execute("UPDATE publications SET is_primary=0, updated_at=CURRENT_TIMESTAMP WHERE unitid=? AND domain!=? "
                            "AND verification_status='auto'", (unitid, best["domain"]))
            db.conn.execute(
                """
                INSERT INTO publications(unitid,name,homepage_url,domain,confidence,verification_status,is_primary,relationship,
                  discovery_method,candidate_id,ambiguous)
                VALUES(?,?,?,?,?,'auto',1,?,?,?,?)
                ON CONFLICT(unitid,domain) DO UPDATE SET name=excluded.name,homepage_url=excluded.homepage_url,
                  confidence=excluded.confidence,is_primary=1,relationship=excluded.relationship,
                  discovery_method=excluded.discovery_method,candidate_id=excluded.candidate_id,ambiguous=excluded.ambiguous,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (unitid, pub_name, best["url"], best["domain"], best["eff"], rel,
                 best["source"] + ("+ai_assist" if ai_used else ""), best["id"], is_amb),
            )
            db.conn.execute("UPDATE publication_candidates SET status='promoted' WHERE id=?", (best["id"],))
            pid = db.conn.execute("SELECT id FROM publications WHERE unitid=? AND domain=?", (unitid, best["domain"])).fetchone()[0]
            reasons = ["auto_promoted"]
            prio = 5.0
            if is_amb:
                ambiguous += 1
                prio += 20
                reasons.append("ambiguous_publication_identification")
            if best["eff"] < threshold + 0.1:
                prio += 10
                reasons.append("near_threshold_confidence")
            if ai_used:
                ai_assisted += 1
                prio += 15
                reasons.append("ai_assisted_identification")
            enqueue_publication_review(db, pid, unitid, prio, reasons)
            promoted += 1
    return {"promoted": promoted, "ambiguous": ambiguous, "skipped_human_verified": skipped_protected,
            "ai_assisted": ai_assisted, "ai_blocked": ai_blocked}

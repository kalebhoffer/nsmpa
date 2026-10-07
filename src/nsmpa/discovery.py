from __future__ import annotations

import asyncio
import html
import json
import re
from collections import deque
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .config import Settings
from .db import Database
from .fetch import HardenedFetcher
from .models import SearchResult
from .search import SearchBroker, SearchBudgetExceeded, get_search_provider
from .utils import is_blocked_social_or_aggregator, normalize_url, registrableish_domain, same_site
from .progress import RunDashboard

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
    r"organizations?|clubs)|campus[-_/ ]?(?:media|newspaper|life)|journalism|"
    r"newspaper|publications?)",
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
    soup = BeautifulSoup(content.decode("utf-8", errors="replace"), "html.parser")
    title = ""
    if soup.title:
        title = " ".join(soup.title.stripped_strings)
    links: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        text = " ".join(a.stripped_strings)
        href = normalize_url(str(a.get("href")), base_url)
        if not href or is_blocked_social_or_aggregator(href):
            continue
        links.append((text, href))
    return title, links


def _link_is_direct_candidate(text: str, href: str) -> bool:
    hay = f"{text} {href}"
    return bool(STRONG_STUDENT_TERMS.search(hay) or NEWSPAPER_TERMS.search(hay))


def _page_is_student_media_context(url: str, title: str, content: bytes) -> bool:
    # Limit body context to avoid treating one unrelated footer link as page identity.
    text = BeautifulSoup(content.decode("utf-8", errors="replace"), "html.parser").get_text(" ", strip=True)[:4000]
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
        try:
            r = await fetcher.fetch(map_url)
        except Exception:
            return
        if r.status_code >= 400 or not r.content:
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


async def institution_site_candidates(fetcher: HardenedFetcher, website: str, inst_name: str) -> list[SearchResult]:
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
    visited: set[str] = set()
    out: list[SearchResult] = []

    # Sitemap URLs give us a recovery route when the homepage does not link student media.
    try:
        for u in await _sitemap_navigation_urls(fetcher, root, max_urls=16):
            if u not in queued:
                queued.add(u)
                queue.append((u, 1, "institution_sitemap"))
    except Exception:
        pass

    max_pages = 20
    while queue and len(visited) < max_pages:
        page_url, depth, source = queue.popleft()
        if page_url in visited:
            continue
        visited.add(page_url)
        try:
            r = await fetcher.fetch(page_url)
        except Exception:
            continue
        if r.status_code >= 400 or "html" not in r.content_type:
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


async def discover_institution(
    db: Database,
    settings: Settings,
    fetcher: HardenedFetcher,
    broker: SearchBroker,
    row,
    dashboard: RunDashboard | None = None,
) -> dict[str, int | float]:
    unitid, name, website = row["unitid"], row["name"], row["website"]
    results: list[SearchResult] = []
    if website:
        try:
            if dashboard:
                dashboard.update(phase="university site + sitemap")
            results.extend(await institution_site_candidates(fetcher, website, name))
        except Exception as exc:
            db.execute(
                "INSERT INTO errors(unitid,stage,error_type,message,retryable) VALUES(?,?,?,?,1)",
                (unitid, "discovery_institution_site", type(exc).__name__, str(exc)),
            )
            db.conn.commit()
            if dashboard:
                dashboard.increment(errors=1)
                dashboard.log(f"site discovery error {name}: {exc}")

    def best_score(items: list[SearchResult]) -> float:
        return max((score_candidate(name, website, item)[0] for item in items), default=0.0)

    queries = [
        f'"{name}" student newspaper',
        f'"{name}" student media newspaper',
        f'"{name}" campus newspaper',
        f'"{name}" newspaper editorial policy',
        f'"{name}" independent student newspaper',
        f'"{name}" student-run news',
    ][: settings.discovery_max_searches_per_institution]

    searches = 0
    # Adaptive escalation: do not spend additional credits once a strong candidate is found.
    if broker.provider.name != "none" and best_score(results) < settings.publication_early_stop_threshold:
        for query in queries:
            try:
                if dashboard:
                    dashboard.update(phase=f"search: {query[-42:]}")
                found, _qid, cached = await broker.search(
                    query,
                    purpose="publication_discovery",
                    unitid=unitid,
                    count=settings.search_results_per_query,
                )
                results.extend(found)
                searches += 1
                if dashboard:
                    if cached:
                        dashboard.increment(searches_cached=1)
                    else:
                        dashboard.increment(searches_live=1, credits_estimated=1)
                if best_score(found) >= settings.publication_early_stop_threshold or best_score(results) >= settings.publication_early_stop_threshold:
                    break
            except SearchBudgetExceeded:
                raise
            except Exception as exc:
                db.execute(
                    "INSERT INTO errors(unitid,stage,error_type,message,retryable) VALUES(?,?,?,?,1)",
                    (unitid, "discovery_search", type(exc).__name__, str(exc)),
                )
                db.conn.commit()
                if dashboard:
                    dashboard.increment(errors=1)
                    dashboard.log(f"search error {name}: {exc}")

    inserted = 0
    high = 0
    top_score = 0.0
    best_by_url: dict[str, tuple[SearchResult, float, list[str]]] = {}
    for result in results:
        url = normalize_url(result.url)
        if not url:
            continue
        result.url = url
        score, reasons = score_candidate(name, website, result)
        top_score = max(top_score, score)
        if score < 0.15:
            continue
        prev = best_by_url.get(url)
        if prev is None or score > prev[1]:
            best_by_url[url] = (result, score, reasons)

    with db.transaction():
        for url, (result, score, reasons) in best_by_url.items():
            db.conn.execute(
                """
                INSERT INTO publication_candidates(unitid,url,domain,title,snippet,source,query,score,score_reasons_json)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(unitid,url) DO UPDATE SET
                  title=excluded.title,snippet=excluded.snippet,source=excluded.source,query=excluded.query,
                  score=excluded.score,score_reasons_json=excluded.score_reasons_json,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (unitid, url, registrableish_domain(url), result.title, result.snippet,
                 result.provider, result.query, score, json.dumps(reasons)),
            )
            inserted += 1
            if score >= settings.publication_confidence_threshold:
                high += 1
                if dashboard:
                    dashboard.add_recent(f"✓ {name}: {result.title or url} [{score:.2f}]")
    return {"candidates": inserted, "high": high, "searches": searches, "top_score": top_score}


async def discover_all(
    db: Database,
    settings: Settings,
    limit: int | None = None,
    *,
    run_id: str | None = None,
    quiet: bool = False,
    verbose: bool = False,
) -> dict[str, int | str]:
    import uuid

    provider = get_search_provider(settings.search_provider, settings.user_agent)
    rid = run_id or f"discovery-{uuid.uuid4().hex}"
    db.execute(
        "INSERT OR IGNORE INTO research_runs(id,mode,config_json,status) VALUES(?,?,?,'running')",
        (rid, "publication_discovery", settings.model_dump_json()),
    )
    db.conn.commit()
    broker = SearchBroker(db, settings, rid, provider)
    sql = "SELECT unitid,name,website FROM institutions WHERE included=1 ORDER BY unitid"
    params: tuple = ()
    if limit:
        sql += " LIMIT ?"
        params = (limit,)
    rows = db.execute(sql, params).fetchall()
    candidates = high = processed = 0
    try:
        with RunDashboard("NSMPA Publication Discovery", len(rows), quiet=quiet, verbose=verbose) as dash:
            async with HardenedFetcher(settings) as fetcher:
                for idx, row in enumerate(rows, start=1):
                    dash.update(current=row["name"], phase="starting institution")
                    try:
                        result = await discover_institution(db, settings, fetcher, broker, row, dash)
                        candidates += int(result["candidates"])
                        high += int(result["high"])
                    except SearchBudgetExceeded as exc:
                        dash.add_recent(f"Search budget reached: {exc}")
                        break
                    processed += 1
                    dash.update(completed=idx, candidates=candidates, high_confidence=high)
        db.execute("UPDATE research_runs SET completed_at=CURRENT_TIMESTAMP,status='completed',entities_completed=? WHERE id=?", (processed, rid))
        db.conn.commit()
    except Exception:
        db.execute("UPDATE research_runs SET completed_at=CURRENT_TIMESTAMP,status='failed' WHERE id=?", (rid,)); db.conn.commit()
        raise

    with_candidates = db.execute(
        """
        SELECT COUNT(DISTINCT c.unitid) AS n
        FROM publication_candidates c
        JOIN institutions i ON i.unitid=c.unitid
        WHERE i.included=1 AND c.score>=0.40
        """
    ).fetchone()["n"]
    return {
        "run_id": rid,
        "institutions": processed,
        "candidates": candidates,
        "high_confidence_candidates": high,
        "institutions_with_plausible_candidates_total": int(with_candidates or 0),
        "provider": provider.name,
        "searches_live": broker.live_calls,
        "searches_cached": broker.cached_calls,
        "credits_estimated": broker.credits_used,
    }


def promote_candidates(db: Database, threshold: float) -> dict[str, int]:
    rows = db.execute(
        """
        SELECT c.*, i.name AS institution_name
        FROM publication_candidates c JOIN institutions i ON i.unitid=c.unitid
        WHERE c.score>=? AND c.status!='rejected'
        ORDER BY c.unitid,c.score DESC,c.id
        """, (threshold,)
    ).fetchall()
    promoted = 0
    seen_unitids: set[str] = set()
    with db.transaction():
        for row in rows:
            if row["unitid"] in seen_unitids:
                continue
            seen_unitids.add(row["unitid"])
            db.conn.execute(
                """
                INSERT INTO publications(unitid,name,homepage_url,domain,confidence,verification_status,is_primary)
                VALUES(?,?,?,?,?,'auto',1)
                ON CONFLICT(unitid,domain) DO UPDATE SET
                  homepage_url=excluded.homepage_url,confidence=MAX(publications.confidence,excluded.confidence),
                  updated_at=CURRENT_TIMESTAMP
                """,
                (row["unitid"], row["title"] or row["institution_name"], row["url"], row["domain"], row["score"]),
            )
            db.conn.execute("UPDATE publication_candidates SET status='promoted' WHERE id=?", (row["id"],))
            promoted += 1
    return {"promoted": promoted}

from __future__ import annotations

import heapq
import json
import re
import uuid
from urllib.parse import urljoin, urlsplit

from .config import Settings
from .db import Database
from .extract import URL_POLICY_HINTS, analyze_page, extract_sitemap_urls
from .fetch import FetchError, HardenedFetcher, TooLarge, UnsafeDestination
from .models import CrawlStatus
from .utils import normalize_url, safe_snapshot_path, same_site, sha256_bytes, sha256_text

COMMON_POLICY_PATHS = [
    "/about", "/about-us", "/about/policies", "/policies", "/policy", "/editorial-policy",
    "/editorial-policies", "/ethics", "/ethics-policy", "/standards", "/corrections",
    "/corrections-policy", "/privacy", "/archive", "/archives", "/takedown-policy",
    "/unpublishing-policy", "/content-removal-policy",
]

SKIP_EXTENSIONS = re.compile(
    r"\.(?:jpg|jpeg|png|gif|webp|svg|ico|css|js|woff2?|ttf|eot|mp3|mp4|mov|avi|zip|gz|rar|7z|docx?|xlsx?|pptx?)(?:$|\?)",
    re.I,
)


def create_run(db: Database, settings: Settings, command: str, run_id: str | None = None) -> str:
    run_id = run_id or uuid.uuid4().hex
    db.execute(
        "INSERT OR IGNORE INTO crawl_runs(id,command,config_json,status) VALUES(?,?,?,'running')",
        (run_id, command, settings.model_dump_json()),
    )
    db.conn.commit()
    return run_id


def complete_run(db: Database, run_id: str, status: str = "completed") -> None:
    db.execute(
        "UPDATE crawl_runs SET completed_at=CURRENT_TIMESTAMP,status=? WHERE id=?",
        (status, run_id),
    )
    db.conn.commit()


def _priority(url: str, depth: int) -> float:
    path = urlsplit(url).path.lower()
    score = 20.0 - min(depth, 10) * 2.0
    if URL_POLICY_HINTS.search(path):
        score += 70.0
    if path in {"/", ""}:
        score += 50.0
    if path.endswith(".pdf"):
        score += 10.0
    if re.search(r"/(?:20\d{2})/\d{1,2}/", path):
        score -= 8.0
    return score


def _record_page(
    db: Database,
    publication_id: int,
    run_id: str,
    requested_url: str,
    depth: int,
    status: str,
    **kwargs,
) -> int:
    columns = [
        "final_url", "canonical_url", "http_status", "content_type", "title", "text_length",
        "content_sha256", "meta_robots", "x_robots_tag", "noindex", "nofollow", "policy_score",
        "evidence_tags_json", "headers_json", "snapshot_path", "error",
    ]
    values = [kwargs.get(c) for c in columns]
    db.execute(
        f"""
        INSERT INTO pages(publication_id,run_id,requested_url,depth,status,{','.join(columns)})
        VALUES(?,?,?,?,?,{','.join('?' for _ in columns)})
        ON CONFLICT(publication_id,run_id,requested_url) DO UPDATE SET
          depth=excluded.depth,status=excluded.status,
          {','.join(f'{c}=excluded.{c}' for c in columns)},fetched_at=CURRENT_TIMESTAMP
        """,
        (publication_id, run_id, requested_url, depth, status, *values),
    )
    db.conn.commit()
    row = db.execute(
        "SELECT id FROM pages WHERE publication_id=? AND run_id=? AND requested_url=?",
        (publication_id, run_id, requested_url),
    ).fetchone()
    return int(row["id"])


def _record_error(db: Database, run_id: str, publication_id: int, stage: str, url: str, exc: Exception, retryable: bool) -> None:
    db.execute(
        "INSERT INTO errors(run_id,publication_id,stage,url,error_type,message,retryable) VALUES(?,?,?,?,?,?,?)",
        (run_id, publication_id, stage, url, type(exc).__name__, str(exc), int(retryable)),
    )
    db.conn.commit()


async def _sitemap_candidates(fetcher: HardenedFetcher, homepage: str, settings: Settings) -> list[str]:
    p = urlsplit(homepage)
    origin = f"{p.scheme}://{p.netloc}"
    sitemap_urls = [origin + "/sitemap.xml", origin + "/sitemap_index.xml"]
    pages: list[str] = []
    nested_seen: set[str] = set()
    for sitemap in sitemap_urls:
        try:
            r = await fetcher.fetch(sitemap)
        except Exception:
            continue
        if r.status_code >= 400:
            continue
        urls, nested = extract_sitemap_urls(r.content, settings.sitemap_max_urls)
        pages.extend(urls)
        for nested_url in nested[:30]:
            n = normalize_url(nested_url)
            if not n or n in nested_seen or not same_site(homepage, n):
                continue
            nested_seen.add(n)
            try:
                nr = await fetcher.fetch(n)
            except Exception:
                continue
            if nr.status_code < 400:
                child_urls, _ = extract_sitemap_urls(nr.content, settings.sitemap_max_urls)
                pages.extend(child_urls)
        if pages:
            break
    unique: list[str] = []
    seen: set[str] = set()
    for u in pages:
        n = normalize_url(u)
        if n and n not in seen and same_site(homepage, n) and not SKIP_EXTENSIONS.search(n):
            seen.add(n)
            unique.append(n)
    # Keep policy-looking URLs plus a deterministic early sample for technical noindex detection.
    policy = [u for u in unique if URL_POLICY_HINTS.search(urlsplit(u).path)]
    sample = [u for u in unique if u not in set(policy)][: max(0, settings.crawl_max_pages_per_publication // 2)]
    return (policy[: settings.policy_candidate_limit] + sample)[: settings.crawl_max_pages_per_publication]


async def crawl_publication(
    db: Database,
    settings: Settings,
    fetcher: HardenedFetcher,
    publication,
    run_id: str,
) -> dict[str, int]:
    pub_id = int(publication["id"])
    homepage = normalize_url(publication["homepage_url"])
    if not homepage:
        return {"attempted": 0, "fetched": 0, "evidence": 0, "failed": 1}

    existing = {
        r["requested_url"]
        for r in db.execute(
            "SELECT requested_url FROM pages WHERE publication_id=? AND run_id=? AND status IN ('fetched','skipped_robots','blocked','unsupported','too_large')",
            (pub_id, run_id),
        ).fetchall()
    }
    heap: list[tuple[float, int, str]] = []
    queued: set[str] = set()

    def enqueue(url: str, depth: int, bonus: float = 0.0) -> None:
        n = normalize_url(url)
        if not n or n in queued or n in existing or not same_site(homepage, n):
            return
        if SKIP_EXTENSIONS.search(n):
            return
        if depth > settings.crawl_max_depth:
            return
        queued.add(n)
        heapq.heappush(heap, (-(_priority(n, depth) + bonus), depth, n))

    enqueue(homepage, 0, 100)
    origin = f"{urlsplit(homepage).scheme}://{urlsplit(homepage).netloc}"
    for path in COMMON_POLICY_PATHS:
        enqueue(urljoin(origin, path), 1, 50)
    try:
        for u in await _sitemap_candidates(fetcher, homepage, settings):
            enqueue(u, 1, 30 if URL_POLICY_HINTS.search(urlsplit(u).path) else 0)
    except Exception as exc:
        _record_error(db, run_id, pub_id, "sitemap", homepage, exc, True)

    attempted = fetched = evidence_count = failed = 0
    while heap and attempted < settings.crawl_max_pages_per_publication:
        _, depth, url = heapq.heappop(heap)
        attempted += 1
        try:
            result = await fetcher.fetch(url)
            if not result.robots_allowed:
                _record_page(
                    db, pub_id, run_id, url, depth, CrawlStatus.SKIPPED_ROBOTS.value,
                    final_url=result.final_url, http_status=0, content_type="", title=None,
                    text_length=0, content_sha256=None, meta_robots=None, x_robots_tag=None,
                    noindex=0, nofollow=0, policy_score=0.0, evidence_tags_json="[]",
                    headers_json="{}", snapshot_path=None, error=result.error,
                )
                continue
            if result.status_code >= 400:
                status = CrawlStatus.BLOCKED.value if result.status_code in {401, 403, 429} else CrawlStatus.FAILED.value
                _record_page(
                    db, pub_id, run_id, url, depth, status,
                    final_url=result.final_url, http_status=result.status_code, content_type=result.content_type,
                    title=None, text_length=0, content_sha256=None, meta_robots=None, x_robots_tag=None,
                    noindex=0, nofollow=0, policy_score=0.0, evidence_tags_json="[]",
                    headers_json=json.dumps(result.headers, sort_keys=True), snapshot_path=None,
                    error=f"HTTP {result.status_code}",
                )
                failed += 1
                continue
            supported = (
                "html" in result.content_type
                or result.content_type in {"text/plain", "application/xhtml+xml"}
                or (settings.allow_pdf and (result.content_type == "application/pdf" or url.lower().endswith(".pdf")))
            )
            if not supported:
                _record_page(
                    db, pub_id, run_id, url, depth, CrawlStatus.UNSUPPORTED.value,
                    final_url=result.final_url, http_status=result.status_code, content_type=result.content_type,
                    title=None, text_length=0, content_sha256=sha256_bytes(result.content), meta_robots=None,
                    x_robots_tag=result.headers.get("x-robots-tag"), noindex=0, nofollow=0,
                    policy_score=0.0, evidence_tags_json="[]", headers_json=json.dumps(result.headers, sort_keys=True),
                    snapshot_path=None, error="Unsupported content type",
                )
                continue

            analysis = analyze_page(result.final_url, result.content, result.content_type, result.headers)
            snapshot_path: str | None = None
            if settings.save_html_snapshots:
                suffix = ".pdf" if result.content_type == "application/pdf" else ".html"
                p = safe_snapshot_path(settings.snapshot_dir, pub_id, url, suffix)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(result.content)
                snapshot_path = str(p)

            page_id = _record_page(
                db, pub_id, run_id, url, depth, CrawlStatus.FETCHED.value,
                final_url=result.final_url, canonical_url=analysis.canonical_url,
                http_status=result.status_code, content_type=result.content_type, title=analysis.title,
                text_length=len(analysis.text), content_sha256=sha256_bytes(result.content),
                meta_robots=analysis.meta_robots, x_robots_tag=analysis.x_robots_tag,
                noindex=int(analysis.noindex), nofollow=int(analysis.nofollow),
                policy_score=analysis.policy_score, evidence_tags_json=json.dumps(analysis.evidence_tags),
                headers_json=json.dumps(result.headers, sort_keys=True), snapshot_path=snapshot_path, error=None,
            )
            fetched += 1

            with db.transaction():
                for tag, excerpt in analysis.excerpts:
                    db.conn.execute(
                        """
                        INSERT OR IGNORE INTO evidence(
                          publication_id,page_id,run_id,tag,excerpt,source_url,evidence_type,
                          supports_relief,confidence,excerpt_sha256
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                        """,
                        (pub_id, page_id, run_id, tag, excerpt, result.final_url, "written_policy",
                         None, min(0.98, 0.45 + analysis.policy_score / 200.0), sha256_text(excerpt)),
                    )
                    evidence_count += 1
                if analysis.noindex:
                    excerpt = f"Observed noindex directive. meta={analysis.meta_robots!r}; x-robots-tag={analysis.x_robots_tag!r}"
                    db.conn.execute(
                        """
                        INSERT OR IGNORE INTO evidence(
                          publication_id,page_id,run_id,tag,excerpt,source_url,evidence_type,
                          supports_relief,confidence,excerpt_sha256
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                        """,
                        (pub_id, page_id, run_id, "noindex", excerpt, result.final_url, "technical",
                         None, 1.0, sha256_text(excerpt + result.final_url)),
                    )
                    evidence_count += 1

            for link in analysis.links:
                if same_site(homepage, link):
                    enqueue(link, depth + 1, 25 if URL_POLICY_HINTS.search(urlsplit(link).path) else 0)

        except TooLarge as exc:
            _record_page(
                db, pub_id, run_id, url, depth, CrawlStatus.TOO_LARGE.value,
                final_url=None, http_status=None, content_type=None, title=None, text_length=0,
                content_sha256=None, meta_robots=None, x_robots_tag=None, noindex=0, nofollow=0,
                policy_score=0.0, evidence_tags_json="[]", headers_json="{}", snapshot_path=None, error=str(exc),
            )
            _record_error(db, run_id, pub_id, "crawl", url, exc, False)
            failed += 1
        except UnsafeDestination as exc:
            _record_page(
                db, pub_id, run_id, url, depth, CrawlStatus.BLOCKED.value,
                final_url=None, http_status=None, content_type=None, title=None, text_length=0,
                content_sha256=None, meta_robots=None, x_robots_tag=None, noindex=0, nofollow=0,
                policy_score=0.0, evidence_tags_json="[]", headers_json="{}", snapshot_path=None, error=str(exc),
            )
            _record_error(db, run_id, pub_id, "security", url, exc, False)
            failed += 1
        except FetchError as exc:
            _record_page(
                db, pub_id, run_id, url, depth, CrawlStatus.FAILED.value,
                final_url=None, http_status=None, content_type=None, title=None, text_length=0,
                content_sha256=None, meta_robots=None, x_robots_tag=None, noindex=0, nofollow=0,
                policy_score=0.0, evidence_tags_json="[]", headers_json="{}", snapshot_path=None, error=str(exc),
            )
            _record_error(db, run_id, pub_id, "crawl", url, exc, True)
            failed += 1
        except Exception as exc:
            _record_error(db, run_id, pub_id, "crawl_unexpected", url, exc, True)
            failed += 1

    return {"attempted": attempted, "fetched": fetched, "evidence": evidence_count, "failed": failed}


async def crawl_all(
    db: Database,
    settings: Settings,
    run_id: str,
    publication_limit: int | None = None,
) -> dict[str, int]:
    sql = "SELECT * FROM publications ORDER BY id"
    params: tuple = ()
    if publication_limit:
        sql += " LIMIT ?"
        params = (publication_limit,)
    pubs = db.execute(sql, params).fetchall()
    totals = {"publications": 0, "attempted": 0, "fetched": 0, "evidence": 0, "failed": 0}
    async with HardenedFetcher(settings) as fetcher:
        for pub in pubs:
            stats = await crawl_publication(db, settings, fetcher, pub, run_id)
            totals["publications"] += 1
            for k in ("attempted", "fetched", "evidence", "failed"):
                totals[k] += stats[k]
    return totals

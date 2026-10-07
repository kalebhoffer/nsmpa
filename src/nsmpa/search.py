"""Search providers and the ledgered, cached, budgeted SearchBroker.

Every query (live, cached, failed or skipped for budget) is written to ``search_queries``
with its purpose, entity/institution, provider, cache key, credits and result URLs
(``search_results``). Identical queries are served from ``search_cache`` and never spend
another credit unless ``refresh=True`` is passed explicitly.
"""
from __future__ import annotations

import abc
import asyncio
import hashlib
import json
import os
import random
import time
from datetime import datetime, timedelta, timezone

import httpx

from .config import Settings
from .db import Database
from .models import SearchResult
from .utils import normalize_url, registrableish_domain


class SearchError(RuntimeError):
    retryable = False


class SearchRetryable(SearchError):
    retryable = True


class SearchAuthError(SearchError):
    """Invalid/expired key or exhausted account: fatal for the run."""


class SearchBudgetExceeded(RuntimeError):
    pass


def _redact(text: str) -> str:
    """Strip any configured API key from text before it is stored or displayed."""
    for var in ("SERPER_API_KEY", "BRAVE_SEARCH_API_KEY"):
        key = os.getenv(var)
        if key and len(key) >= 8:
            text = text.replace(key, "***redacted***")
    return text


class SearchProvider(abc.ABC):
    name = "base"
    #: Last call's provider-reported credit usage, if the API reports it.
    last_credits: int | None = None

    @abc.abstractmethod
    async def search(self, query: str, count: int = 8) -> list[SearchResult]:
        raise NotImplementedError

    def estimate_credits(self, count: int) -> int:
        return 1

    async def aclose(self) -> None:
        return None


def _raise_for_provider_status(r: httpx.Response, provider: str) -> None:
    if r.status_code < 400:
        return
    detail = ""
    try:
        detail = str(r.json().get("message", ""))[:200]
    except Exception:
        detail = r.text[:200] if r.text else ""
    msg = _redact(f"{provider} HTTP {r.status_code}: {detail}".strip())
    if r.status_code in (401, 403):
        raise SearchAuthError(msg)
    if r.status_code == 400 and "credit" in detail.lower():
        raise SearchAuthError(msg)
    if r.status_code == 429 or r.status_code >= 500:
        raise SearchRetryable(msg)
    raise SearchError(msg)


class SerperSearchProvider(SearchProvider):
    name = "serper"
    endpoint = "https://google.serper.dev/search"

    def __init__(self, api_key: str, user_agent: str, *, gl: str = "us", hl: str = "en",
                 transport: httpx.AsyncBaseTransport | None = None):
        self._api_key = api_key
        self.user_agent = user_agent
        self.gl, self.hl = gl, hl
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), transport=transport, trust_env=False)

    def __repr__(self) -> str:  # never expose the key in reprs/tracebacks
        return f"SerperSearchProvider(gl={self.gl!r}, hl={self.hl!r})"

    def estimate_credits(self, count: int) -> int:
        # Serper bills 1 credit for up to 10 results and 2 credits for larger pages.
        return 1 if count <= 10 else 2

    async def search(self, query: str, count: int = 8) -> list[SearchResult]:
        headers = {"X-API-KEY": self._api_key, "Content-Type": "application/json", "User-Agent": self.user_agent}
        payload = {"q": query, "num": max(1, min(count, 100)), "gl": self.gl, "hl": self.hl}
        try:
            r = await self._client.post(self.endpoint, headers=headers, json=payload)
        except httpx.TransportError as exc:
            raise SearchRetryable(_redact(f"serper transport error: {type(exc).__name__}: {exc}")) from None
        _raise_for_provider_status(r, "serper")
        try:
            data = r.json()
        except ValueError:
            raise SearchRetryable("serper returned non-JSON response") from None
        return parse_serper_response(data, query, self)

    async def aclose(self) -> None:
        await self._client.aclose()


def parse_serper_response(data: object, query: str, provider: SearchProvider | None = None) -> list[SearchResult]:
    """Defensive parser: tolerates missing/odd fields; drops items without a usable link."""
    if not isinstance(data, dict):
        return []
    if provider is not None:
        credits = data.get("credits")
        provider.last_credits = int(credits) if isinstance(credits, (int, float)) else None
    organic = data.get("organic")
    if not isinstance(organic, list):
        return []
    out: list[SearchResult] = []
    for n, item in enumerate(organic, start=1):
        if not isinstance(item, dict):
            continue
        link = item.get("link")
        if not isinstance(link, str) or not link.strip():
            continue
        rank = item.get("position") if isinstance(item.get("position"), int) else n
        out.append(SearchResult(
            url=link.strip(), title=str(item.get("title") or "")[:500],
            snippet=str(item.get("snippet") or "")[:2000], rank=rank, provider="serper", query=query,
        ))
    return out


class BraveSearchProvider(SearchProvider):
    name = "brave"

    def __init__(self, api_key: str, user_agent: str, transport: httpx.AsyncBaseTransport | None = None):
        self._api_key = api_key
        self.user_agent = user_agent
        self._client = httpx.AsyncClient(timeout=20, transport=transport, trust_env=False)

    def __repr__(self) -> str:
        return "BraveSearchProvider()"

    async def search(self, query: str, count: int = 8) -> list[SearchResult]:
        headers = {"Accept": "application/json", "X-Subscription-Token": self._api_key, "User-Agent": self.user_agent}
        params = {"q": query, "count": min(count, 20), "safesearch": "moderate", "spellcheck": "true"}
        try:
            r = await self._client.get("https://api.search.brave.com/res/v1/web/search", headers=headers, params=params)
        except httpx.TransportError as exc:
            raise SearchRetryable(_redact(f"brave transport error: {type(exc).__name__}")) from None
        _raise_for_provider_status(r, "brave")
        data = r.json()
        results = data.get("web", {}).get("results", []) if isinstance(data, dict) else []
        return [SearchResult(url=i.get("url", ""), title=i.get("title", ""), snippet=i.get("description", ""),
                             rank=n, provider=self.name, query=query)
                for n, i in enumerate(results, start=1) if isinstance(i, dict) and i.get("url")]

    async def aclose(self) -> None:
        await self._client.aclose()


class NullSearchProvider(SearchProvider):
    name = "none"

    async def search(self, query: str, count: int = 8) -> list[SearchResult]:
        return []

    def estimate_credits(self, count: int) -> int:
        return 0


def get_search_provider(name: str, user_agent: str) -> SearchProvider:
    brave = os.getenv("BRAVE_SEARCH_API_KEY")
    serper = os.getenv("SERPER_API_KEY")
    # In auto mode, prefer Serper for Google-backed recall; Brave remains supported as a fallback.
    if name in {"auto", "serper"} and serper:
        return SerperSearchProvider(serper, user_agent)
    if name in {"auto", "brave"} and brave:
        return BraveSearchProvider(brave, user_agent)
    if name not in {"auto", "none"}:
        raise RuntimeError(f"Search provider '{name}' requested but its API key is not configured")
    return NullSearchProvider()


def cache_key(query: str, count: int) -> str:
    normalized = " ".join(query.split()).strip().lower()
    return hashlib.sha256(f"{normalized}|{count}".encode()).hexdigest()


class SearchBroker:
    """Ledgered, cached, resumable search wrapper with hard credit budgets.

    Budgets:
    - ``max_searches`` (CLI ``--max-searches``): live credits this *invocation* may spend.
    - ``settings.search_credit_budget_per_run``: cumulative live credits for the run id,
      including credits spent by earlier invocations that resumed the same run.
    """

    def __init__(self, db: Database, settings: Settings, run_id: str, provider: SearchProvider, *,
                 max_searches: int | None = None, refresh: bool = False):
        self.db = db
        self.settings = settings
        self.run_id = run_id
        self.provider = provider
        self.max_searches = max_searches
        self.refresh = refresh
        self.run_credits_before = int(db.scalar("SELECT credits_estimated FROM research_runs WHERE id=?", (run_id,), 0))
        self.credits_used = 0          # this invocation
        self.live_calls = 0
        self.cached_calls = 0
        self.failed_calls = 0
        self.budget_exhausted = False
        self.budget_reason = ""
        self._inflight: dict[str, asyncio.Future] = {}
        self._refreshed: set[str] = set()

    # Backwards-compatible alias.
    _hash = staticmethod(cache_key)

    @property
    def run_credits_total(self) -> int:
        return self.run_credits_before + self.credits_used

    @property
    def budget_limit(self) -> int | None:
        return self.max_searches

    def credits_remaining(self) -> int:
        caps = [self.settings.search_credit_budget_per_run - self.run_credits_total]
        if self.max_searches is not None:
            caps.append(self.max_searches - self.credits_used)
        return max(0, min(caps))

    def _cache_fresh(self, created_at: str) -> bool:
        if self.settings.search_cache_days == 0:
            return False
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) - dt <= timedelta(days=self.settings.search_cache_days)
        except Exception:
            return False

    def cached_results(self, query: str, count: int | None = None) -> list[SearchResult] | None:
        count = count or self.settings.search_results_per_query
        row = self.db.execute("SELECT * FROM search_cache WHERE provider=? AND query_hash=?",
                              (self.provider.name, cache_key(query, count))).fetchone()
        if not row or not self._cache_fresh(row["created_at"]):
            return None
        return [SearchResult(**x) for x in json.loads(row["response_json"])]

    async def _live(self, query: str, count: int) -> tuple[list[SearchResult], int, int]:
        cost = self.provider.estimate_credits(count)
        if cost > self.credits_remaining():
            self.budget_exhausted = True
            if self.max_searches is not None and self.credits_used + cost > self.max_searches:
                self.budget_reason = f"--max-searches {self.max_searches} reached"
            else:
                self.budget_reason = f"run budget search_credit_budget_per_run={self.settings.search_credit_budget_per_run} reached"
            raise SearchBudgetExceeded(self.budget_reason)
        attempts = self.settings.search_max_retries + 1
        last: Exception | None = None
        t0 = time.monotonic()
        for attempt in range(attempts):
            try:
                results = await self.provider.search(query, count)
                reported = self.provider.last_credits
                charged = int(reported) if reported is not None else cost
                return results, charged, int((time.monotonic() - t0) * 1000)
            except SearchAuthError:
                raise
            except SearchRetryable as exc:
                last = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(min(30.0, self.settings.search_retry_backoff_seconds * (2 ** attempt))
                                        * (0.75 + random.random() * 0.5))
        assert last is not None
        raise last

    async def search(self, query: str, *, purpose: str, count: int | None = None, entity_id: int | None = None,
                     unitid: str | None = None) -> tuple[list[SearchResult], int, bool]:
        count = count or self.settings.search_results_per_query
        qh = cache_key(query, count)
        # Coalesce identical concurrent queries so parallel entity workers never double-spend.
        if qh in self._inflight:
            await asyncio.shield(self._inflight[qh])
        refresh_this = self.refresh and qh not in self._refreshed
        cache = None if refresh_this else self.db.execute(
            "SELECT * FROM search_cache WHERE provider=? AND query_hash=?", (self.provider.name, qh)
        ).fetchone()
        cached = bool(cache and self._cache_fresh(cache["created_at"]))
        results: list[SearchResult] = []
        error = None
        status = "completed"
        credits = 0
        latency = None
        fut: asyncio.Future | None = None
        try:
            if cached:
                results = [SearchResult(**x) for x in json.loads(cache["response_json"])]
                self.cached_calls += 1
                self.db.execute("UPDATE search_cache SET last_used_at=CURRENT_TIMESTAMP,uses=uses+1 WHERE provider=? AND query_hash=?",
                                (self.provider.name, qh))
            else:
                fut = asyncio.get_running_loop().create_future()
                self._inflight[qh] = fut
                try:
                    results, credits, latency = await self._live(query, count)
                except SearchBudgetExceeded:
                    status = "budget_skipped"
                    raise
                except SearchError:
                    # A failed live call may still have been billed; count it conservatively.
                    credits = self.provider.estimate_credits(count)
                    raise
                self._refreshed.add(qh)
                self.live_calls += 1
                payload = json.dumps([{
                    "url": r.url, "title": r.title, "snippet": r.snippet, "rank": r.rank,
                    "provider": r.provider, "query": r.query,
                } for r in results], ensure_ascii=False)
                self.db.execute(
                    """
                    INSERT INTO search_cache(provider,query_hash,query,count_requested,response_json,result_count,created_at)
                    VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)
                    ON CONFLICT(provider,query_hash) DO UPDATE SET query=excluded.query,count_requested=excluded.count_requested,
                      response_json=excluded.response_json,result_count=excluded.result_count,created_at=CURRENT_TIMESTAMP,
                      last_used_at=CURRENT_TIMESTAMP,uses=search_cache.uses+1
                    """,
                    (self.provider.name, qh, query, count, payload, len(results)),
                )
        except SearchBudgetExceeded:
            self._ledger(entity_id, unitid, purpose, query, qh, count, [], False, 0, "budget_skipped", self.budget_reason, None)
            raise
        except Exception as exc:
            status = "failed"
            error = _redact(f"{type(exc).__name__}: {exc}")[:500]
            self.failed_calls += 1
            if credits:
                self.credits_used += credits
            self._ledger(entity_id, unitid, purpose, query, qh, count, [], False, credits, status, error, latency)
            if isinstance(exc, SearchAuthError):
                raise
            return [], 0, False
        finally:
            if fut is not None:
                self._inflight.pop(qh, None)
                if not fut.done():
                    fut.set_result(None)
        self.credits_used += credits
        qid = self._ledger(entity_id, unitid, purpose, query, qh, count, results, cached, credits, status, error, latency)
        return results, qid, cached

    def _ledger(self, entity_id, unitid, purpose, query, qh, count, results, cached, credits, status, error, latency) -> int:
        cur = self.db.execute(
            """
            INSERT INTO search_queries(run_id,entity_id,unitid,provider,purpose,query,query_hash,count_requested,result_count,
              was_cached,credits_estimated,status,error,latency_ms,refresh)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (self.run_id, entity_id, unitid, self.provider.name, purpose, query, qh, count, len(results), int(cached),
             credits, status, error, latency, int(self.refresh)),
        )
        qid = int(cur.lastrowid)
        for r in results:
            url = normalize_url(r.url)
            if not url:
                continue
            self.db.execute(
                "INSERT OR IGNORE INTO search_results(query_id,rank,url,domain,title,snippet) VALUES(?,?,?,?,?,?)",
                (qid, r.rank, r.url, registrableish_domain(url), r.title, r.snippet),
            )
        live = 1 if status == "completed" and not cached else 0
        self.db.execute(
            "UPDATE research_runs SET searches_live=searches_live+?,searches_cached=searches_cached+?,"
            "credits_estimated=credits_estimated+?,last_checkpoint_at=CURRENT_TIMESTAMP WHERE id=?",
            (live, int(cached), credits, self.run_id),
        )
        self.db.conn.commit()
        return qid

    def mark_useful(self, query_id: int | None) -> None:
        if query_id:
            self.db.execute("UPDATE search_queries SET produced_evidence=1 WHERE id=?", (query_id,))

    async def aclose(self) -> None:
        await self.provider.aclose()

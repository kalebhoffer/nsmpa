from __future__ import annotations

import abc
import hashlib
import json
import os
from datetime import datetime, timezone, timedelta

import httpx

from .config import Settings
from .db import Database
from .models import SearchResult
from .utils import normalize_url, registrableish_domain


class SearchProvider(abc.ABC):
    name = "base"

    @abc.abstractmethod
    async def search(self, query: str, count: int = 8) -> list[SearchResult]:
        raise NotImplementedError


class BraveSearchProvider(SearchProvider):
    name = "brave"

    def __init__(self, api_key: str, user_agent: str):
        self.api_key = api_key
        self.user_agent = user_agent

    async def search(self, query: str, count: int = 8) -> list[SearchResult]:
        headers = {"Accept": "application/json", "X-Subscription-Token": self.api_key, "User-Agent": self.user_agent}
        params = {"q": query, "count": min(count, 20), "safesearch": "moderate", "spellcheck": "true"}
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.get("https://api.search.brave.com/res/v1/web/search", headers=headers, params=params)
            r.raise_for_status()
            data = r.json()
        return [SearchResult(url=i.get("url", ""), title=i.get("title", ""), snippet=i.get("description", ""), rank=n, provider=self.name, query=query)
                for n, i in enumerate(data.get("web", {}).get("results", []), start=1)]


class SerperSearchProvider(SearchProvider):
    name = "serper"

    def __init__(self, api_key: str, user_agent: str):
        self.api_key = api_key
        self.user_agent = user_agent

    async def search(self, query: str, count: int = 8) -> list[SearchResult]:
        headers = {"X-API-KEY": self.api_key, "Content-Type": "application/json", "User-Agent": self.user_agent}
        payload = {"q": query, "num": min(count, 20)}
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.post("https://google.serper.dev/search", headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
        return [SearchResult(url=i.get("link", ""), title=i.get("title", ""), snippet=i.get("snippet", ""), rank=n, provider=self.name, query=query)
                for n, i in enumerate(data.get("organic", []), start=1)]


class NullSearchProvider(SearchProvider):
    name = "none"
    async def search(self, query: str, count: int = 8) -> list[SearchResult]:
        return []


def get_search_provider(name: str, user_agent: str) -> SearchProvider:
    brave = os.getenv("BRAVE_SEARCH_API_KEY")
    serper = os.getenv("SERPER_API_KEY")
    # In auto mode, prefer Serper for Google-backed recall; Brave remains supported as an independent fallback.
    if name in {"auto", "serper"} and serper:
        return SerperSearchProvider(serper, user_agent)
    if name in {"auto", "brave"} and brave:
        return BraveSearchProvider(brave, user_agent)
    if name not in {"auto", "none"}:
        raise RuntimeError(f"Search provider '{name}' requested but its API key is not configured")
    return NullSearchProvider()


class SearchBudgetExceeded(RuntimeError):
    pass


class SearchBroker:
    """Ledgered, cached, resumable search wrapper with a hard per-run credit budget."""

    def __init__(self, db: Database, settings: Settings, run_id: str, provider: SearchProvider):
        self.db = db
        self.settings = settings
        self.run_id = run_id
        self.provider = provider
        row = db.execute("SELECT credits_estimated FROM research_runs WHERE id=?", (run_id,)).fetchone()
        self.credits_used = int(row["credits_estimated"] if row else 0)
        self.live_calls = 0
        self.cached_calls = 0

    @staticmethod
    def _hash(query: str, count: int) -> str:
        normalized = " ".join(query.split()).strip().lower()
        return hashlib.sha256(f"{normalized}|{count}".encode()).hexdigest()

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

    async def search(self, query: str, *, purpose: str, count: int | None = None, entity_id: int | None = None, unitid: str | None = None) -> tuple[list[SearchResult], int, bool]:
        count = count or self.settings.search_results_per_query
        qh = self._hash(query, count)
        cache = self.db.execute(
            "SELECT * FROM search_cache WHERE provider=? AND query_hash=?", (self.provider.name, qh)
        ).fetchone()
        cached = bool(cache and self._cache_fresh(cache["created_at"]))
        results: list[SearchResult] = []
        error = None
        status = "completed"
        credits = 0
        try:
            if cached:
                raw = json.loads(cache["response_json"])
                results = [SearchResult(**x) for x in raw]
                self.cached_calls += 1
                self.db.execute("UPDATE search_cache SET last_used_at=CURRENT_TIMESTAMP,uses=uses+1 WHERE provider=? AND query_hash=?", (self.provider.name, qh))
            else:
                if self.credits_used >= self.settings.search_credit_budget_per_run:
                    raise SearchBudgetExceeded(f"Search credit budget {self.settings.search_credit_budget_per_run} reached")
                results = await self.provider.search(query, count)
                credits = 0 if self.provider.name == "none" else 1
                self.credits_used += credits
                self.live_calls += 1
                payload = json.dumps([{
                    "url": r.url, "title": r.title, "snippet": r.snippet, "rank": r.rank,
                    "provider": r.provider, "query": r.query,
                } for r in results], ensure_ascii=False)
                self.db.execute(
                    """
                    INSERT INTO search_cache(provider,query_hash,query,count_requested,response_json,result_count)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(provider,query_hash) DO UPDATE SET query=excluded.query,count_requested=excluded.count_requested,
                      response_json=excluded.response_json,result_count=excluded.result_count,last_used_at=CURRENT_TIMESTAMP,uses=search_cache.uses+1
                    """,
                    (self.provider.name, qh, query, count, payload, len(results)),
                )
        except Exception as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, SearchBudgetExceeded):
                raise
        cur = self.db.execute(
            """
            INSERT INTO search_queries(run_id,entity_id,unitid,provider,purpose,query,query_hash,count_requested,result_count,was_cached,credits_estimated,status,error)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (self.run_id, entity_id, unitid, self.provider.name, purpose, query, qh, count, len(results), int(cached), credits, status, error),
        )
        qid = int(cur.lastrowid)
        for r in results:
            url = normalize_url(r.url)
            if not url:
                continue
            self.db.execute(
                "INSERT OR IGNORE INTO search_results(query_id,rank,url,domain,title,snippet) VALUES(?,?,?,?,?,?)",
                (qid, r.rank, url, registrableish_domain(url), r.title, r.snippet),
            )
        self.db.execute(
            "UPDATE research_runs SET searches_live=searches_live+?,searches_cached=searches_cached+?,credits_estimated=credits_estimated+? WHERE id=?",
            (0 if cached else 1, 1 if cached else 0, credits, self.run_id),
        )
        self.db.conn.commit()
        return results, qid, cached

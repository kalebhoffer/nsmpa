from __future__ import annotations

import asyncio
import email.utils
import time
import urllib.robotparser
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import httpx

from .config import Settings
from .models import FetchResult
from .utils import normalize_url, resolved_host_is_public


class FetchError(RuntimeError):
    pass


class UnsafeDestination(FetchError):
    pass


class TooLarge(FetchError):
    pass


class RobotsCache:
    def __init__(self, fetcher: "HardenedFetcher"):
        self.fetcher = fetcher
        self.cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self.locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def allowed(self, url: str) -> bool:
        if not self.fetcher.settings.respect_robots_txt:
            return True
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        async with self.locks[origin]:
            if origin not in self.cache:
                self.cache[origin] = await self._load(origin)
        rp = self.cache[origin]
        if rp is None:
            return True
        return rp.can_fetch(self.fetcher.settings.user_agent, url)

    async def _load(self, origin: str) -> urllib.robotparser.RobotFileParser | None:
        robots_url = origin + "/robots.txt"
        try:
            result = await self.fetcher._fetch_http(robots_url, check_robots=False, max_bytes=1_000_000)
            if result.status_code >= 400:
                return None
            text = result.content.decode("utf-8", errors="replace")
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(robots_url)
            rp.parse(text.splitlines())
            return rp
        except Exception:
            # Failure to retrieve robots.txt is not treated as a blanket denial.
            return None


class HardenedFetcher:
    def __init__(self, settings: Settings):
        self.settings = settings
        timeout = httpx.Timeout(
            timeout=settings.request_timeout_seconds,
            connect=settings.connect_timeout_seconds,
        )
        limits = httpx.Limits(
            max_connections=settings.max_concurrency,
            max_keepalive_connections=settings.max_concurrency,
        )
        self.client = httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            http2=True,
            follow_redirects=False,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.8,*/*;q=0.2",
                "Accept-Language": "en-US,en;q=0.8",
            },
        )
        self.global_sem = asyncio.Semaphore(settings.max_concurrency)
        self.host_sems: defaultdict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(settings.max_per_host_concurrency)
        )
        self.host_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.last_request: dict[str, float] = {}
        self.robots = RobotsCache(self)

    async def __aenter__(self) -> "HardenedFetcher":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        await self.client.aclose()

    async def _validate_destination(self, url: str) -> None:
        host = urlsplit(url).hostname
        if not host or not await asyncio.to_thread(resolved_host_is_public, host):
            raise UnsafeDestination(f"Destination is not a public Internet host: {host!r}")

    async def _pace(self, host: str) -> None:
        async with self.host_locks[host]:
            now = time.monotonic()
            last = self.last_request.get(host, 0)
            wait = self.settings.per_host_delay_seconds - (now - last)
            if wait > 0:
                await asyncio.sleep(wait)
            self.last_request[host] = time.monotonic()

    @staticmethod
    def _retry_after_seconds(value: str | None) -> float | None:
        if not value:
            return None
        value = value.strip()
        if value.isdigit():
            return min(float(value), 120.0)
        try:
            dt = email.utils.parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, min((dt - datetime.now(timezone.utc)).total_seconds(), 120.0))
        except Exception:
            return None

    async def fetch(self, url: str) -> FetchResult:
        clean = normalize_url(url)
        if not clean:
            raise FetchError(f"Invalid URL: {url}")
        return await self._fetch_http(clean, check_robots=True, max_bytes=self.settings.max_response_bytes)

    async def _single_get(self, url: str, max_bytes: int) -> tuple[int, dict[str, str], bytes]:
        host = urlsplit(url).hostname or ""
        async with self.global_sem, self.host_sems[host]:
            await self._pace(host)
            async with self.client.stream("GET", url) as response:
                status = response.status_code
                headers = {k.lower(): v for k, v in response.headers.items()}
                # Redirects and retryable error responses do not need body buffering.
                if status in {301, 302, 303, 307, 308, 429, 500, 502, 503, 504}:
                    return status, headers, b""
                declared_len = headers.get("content-length")
                if declared_len and declared_len.isdigit() and int(declared_len) > max_bytes:
                    raise TooLarge(f"Content-Length {declared_len} exceeds {max_bytes} bytes: {url}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > max_bytes:
                        raise TooLarge(f"Response exceeds {max_bytes} bytes: {url}")
                    body.extend(chunk)
                return status, headers, bytes(body)

    async def _fetch_http(self, url: str, check_robots: bool, max_bytes: int) -> FetchResult:
        current = url
        redirects = 0
        start = time.monotonic()
        while True:
            await self._validate_destination(current)
            if check_robots and not await self.robots.allowed(current):
                return FetchResult(
                    requested_url=url, final_url=current, status_code=0, headers={}, content=b"",
                    elapsed_ms=int((time.monotonic() - start) * 1000), content_type="",
                    robots_allowed=False, error="robots.txt disallows fetch",
                )

            status: int | None = None
            headers: dict[str, str] = {}
            content = b""
            last_exc: Exception | None = None
            for attempt in range(self.settings.max_retries + 1):
                try:
                    status, headers, content = await self._single_get(current, max_bytes)
                    if status in {429, 500, 502, 503, 504} and attempt < self.settings.max_retries:
                        delay = self._retry_after_seconds(headers.get("retry-after"))
                        await asyncio.sleep(delay if delay is not None else min(2 ** attempt, 8))
                        continue
                    break
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    last_exc = exc
                    if attempt >= self.settings.max_retries:
                        raise FetchError(f"Network failure fetching {current}: {exc}") from exc
                    await asyncio.sleep(min(2 ** attempt, 8))
            if status is None:
                raise FetchError(f"Failed fetching {current}: {last_exc}")

            if status in {301, 302, 303, 307, 308}:
                location = headers.get("location")
                if not location:
                    raise FetchError(f"Redirect without Location from {current}")
                redirects += 1
                if redirects > 10:
                    raise FetchError(f"Too many redirects from {url}")
                nxt = normalize_url(urljoin(current, location))
                if not nxt:
                    raise FetchError(f"Invalid redirect target from {current}")
                current = nxt
                continue

            content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
            return FetchResult(
                requested_url=url,
                final_url=current,
                status_code=status,
                headers=headers,
                content=content,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                content_type=content_type,
                robots_allowed=True,
            )

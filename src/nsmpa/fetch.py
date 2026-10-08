"""Hardened asynchronous HTTP fetcher for hostile, malformed public web content.

Guarantees:
- Only http/https to public unicast addresses on allowed ports; every redirect hop is
  re-validated; the connected peer address is re-checked (defeats DNS rebinding).
- No proxy environment variables are honored (they would bypass destination checks).
- Bounded redirects with loop detection, connect/read timeouts, streaming size limit,
  content-type gate before any body is buffered.
- Retries with exponential backoff + jitter for transport errors, 429 and 5xx; honors
  Retry-After (capped).
- robots.txt awareness (including Crawl-delay), per-host pacing and concurrency limits,
  and a per-host circuit breaker so one broken site cannot stall a national run.
- Fetch failures are *classified* (``access_class``) instead of raised wherever possible,
  so callers can distinguish "inaccessible" from "inspected and empty".
"""
from __future__ import annotations

import asyncio
import email.utils
import ipaddress
import random
import time
import urllib.robotparser
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlsplit

import httpx

from .config import Settings
from .models import FetchResult
from .utils import ip_is_public, prepare_request_url, resolve_public_addresses


class FetchError(RuntimeError):
    access_class = "network_error"


class UnsafeDestination(FetchError):
    access_class = "unsafe_destination"


class TooLarge(FetchError):
    access_class = "too_large"


class RedirectError(FetchError):
    access_class = "redirect_error"


class InvalidURL(FetchError):
    access_class = "invalid_url"


class HostCircuitOpen(FetchError):
    access_class = "host_unavailable"


RETRYABLE_STATUS = {429, 500, 502, 503, 504}
REDIRECT_STATUS = {301, 302, 303, 307, 308}
# Content types whose bodies are never useful for policy research; we stop before reading them.
TEXTUAL_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "application/xml", "text/xml",
                 "application/rss+xml", "application/atom+xml")


def classify_status(status: int) -> str:
    if 200 <= status < 300:
        return "ok"
    if status in (401, 403, 407, 451):
        return "blocked"
    if status == 429:
        return "rate_limited"
    if status == 404:
        return "not_found"
    if status == 410:
        return "gone"
    if 400 <= status < 500:
        return "client_error"
    if status >= 500:
        return "server_error"
    return "unexpected_status"


# Access classes that mean the source could not be inspected (=> never a negative finding).
INACCESSIBLE_CLASSES = {
    "blocked", "rate_limited", "server_error", "robots_disallowed", "network_error", "timeout",
    "too_large", "unsafe_destination", "redirect_error", "invalid_url", "host_unavailable",
    "unsupported_content", "unexpected_status", "client_error",
}


@dataclass
class FetchStats:
    requests: int = 0
    fetched_ok: int = 0
    retries: int = 0
    robots_blocked: int = 0
    access_blocked: int = 0
    malformed_skipped: int = 0
    unsafe_rejected: int = 0
    too_large: int = 0
    network_errors: int = 0
    by_class: dict[str, int] = field(default_factory=lambda: defaultdict(int))


def parse_crawl_delay(text: str, agent_token: str) -> float | None:
    """Crawl-delay for our agent (or '*'), accepting fractional values that robotparser drops."""
    agent_token = agent_token.lower()
    groups: list[tuple[set[str], float | None]] = []
    agents: set[str] = set()
    delay: float | None = None
    in_rules = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (x.strip() for x in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if in_rules:
                groups.append((agents, delay))
                agents, delay, in_rules = set(), None, False
            agents.add(value.lower())
        elif key == "crawl-delay":
            in_rules = True
            try:
                delay = float(value)
            except ValueError:
                pass
        else:
            in_rules = True
    groups.append((agents, delay))
    specific = [d for a, d in groups if any(x != "*" and x in agent_token for x in a) and d is not None]
    wildcard = [d for a, d in groups if "*" in a and d is not None]
    found = (specific or wildcard or [None])[0]
    return None if found is None or found < 0 else found


class RobotsCache:
    def __init__(self, fetcher: "HardenedFetcher"):
        self.fetcher = fetcher
        self.cache: dict[str, tuple[urllib.robotparser.RobotFileParser | None, str]] = {}
        self.delays: dict[str, float | None] = {}
        self.locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @staticmethod
    def origin(url: str) -> str:
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc}"

    async def entry(self, url: str) -> tuple[urllib.robotparser.RobotFileParser | None, str]:
        origin = self.origin(url)
        async with self.locks[origin]:
            if origin not in self.cache:
                self.cache[origin] = await self._load(origin)
        return self.cache[origin]

    async def allowed(self, url: str) -> bool:
        if not self.fetcher.settings.respect_robots_txt:
            return True
        rp, status = await self.entry(url)
        if status == "unavailable_5xx":
            # RFC 9309 §2.3.1.4: a server error on robots.txt means assume complete disallow.
            return False
        if rp is None:
            return True
        return rp.can_fetch(self.fetcher.settings.user_agent, url)

    def cached_crawl_delay(self, url: str) -> float | None:
        """Crawl-delay from an already-loaded robots.txt (never triggers a fetch or takes a lock)."""
        if not self.fetcher.settings.respect_robots_txt:
            return None
        delay = self.delays.get(self.origin(url))
        return min(delay, 30.0) if delay else None

    async def _load(self, origin: str) -> tuple[urllib.robotparser.RobotFileParser | None, str]:
        robots_url = origin + "/robots.txt"
        try:
            result = await self.fetcher._fetch_http(robots_url, check_robots=False, max_bytes=512_000,
                                                    textual_only=False)
        except FetchError:
            # Unreachable robots.txt (DNS/timeouts) is treated as "no restrictions" (RFC 9309 4xx/unreachable).
            return None, "unreachable"
        if result.status_code >= 500:
            return None, "unavailable_5xx"
        if result.status_code >= 400:
            return None, "absent"
        text = result.content.decode("utf-8", errors="replace")
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(robots_url)
        try:
            rp.parse(text.splitlines())
        except Exception:
            return None, "unparseable"
        agent_token = self.fetcher.settings.user_agent.split("/", 1)[0]
        self.delays[origin] = parse_crawl_delay(text, agent_token)
        return rp, "ok"


class HardenedFetcher:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None,
                 resolver: Callable[[str], list[str]] | None = None,
                 on_event: Callable[[str], None] | None = None):
        self.settings = settings
        self.allowed_ports = frozenset(settings.allowed_ports)
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
            http2=False,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.8,*/*;q=0.2",
                "Accept-Language": "en-US,en;q=0.8",
            },
        )
        # A test transport has no real sockets; peer-address verification only applies to real I/O.
        self._verify_peer = transport is None
        self.resolver = resolver or resolve_public_addresses
        self.on_event = on_event
        self.global_sem = asyncio.Semaphore(settings.max_concurrency)
        self.host_sems: defaultdict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(settings.max_per_host_concurrency)
        )
        self.host_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.last_request: dict[str, float] = {}
        self.host_failures: defaultdict[str, int] = defaultdict(int)
        self.robots = RobotsCache(self)
        self.stats = FetchStats()

    async def __aenter__(self) -> "HardenedFetcher":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        await self.client.aclose()

    def _event(self, msg: str) -> None:
        if self.on_event:
            try:
                self.on_event(msg)
            except Exception:
                pass

    # ------------------------------------------------------------------ safety
    async def _validate_destination(self, url: str) -> None:
        host = urlsplit(url).hostname
        if not host:
            raise UnsafeDestination(f"URL has no host: {url!r}")
        try:
            await asyncio.to_thread(self.resolver, host)
        except ValueError as exc:
            self.stats.unsafe_rejected += 1
            raise UnsafeDestination(f"Destination is not a public Internet host: {exc}") from None

    def _check_peer(self, response: httpx.Response, url: str) -> None:
        if not self._verify_peer:
            return
        stream = response.extensions.get("network_stream")
        if stream is None:
            return
        try:
            addr = stream.get_extra_info("server_addr")
        except Exception:
            return
        if not addr:
            return
        try:
            ip = ipaddress.ip_address(str(addr[0]).split("%", 1)[0])
        except ValueError:
            return
        if not ip_is_public(ip):
            self.stats.unsafe_rejected += 1
            raise UnsafeDestination(f"Connected peer {ip} for {url} is not public (possible DNS rebinding)")

    # ------------------------------------------------------------------ pacing
    async def _pace(self, host: str, url: str) -> None:
        delay = self.settings.per_host_delay_seconds
        crawl_delay = self.robots.cached_crawl_delay(url)
        if crawl_delay:
            delay = max(delay, crawl_delay)
        async with self.host_locks[host]:
            now = time.monotonic()
            last = self.last_request.get(host, 0)
            wait = delay - (now - last)
            if wait > 0:
                await asyncio.sleep(wait)
            self.last_request[host] = time.monotonic()

    @staticmethod
    def _retry_after_seconds(value: str | None) -> float | None:
        if not value:
            return None
        value = value.strip()
        if value.isdigit():
            return min(float(value), 60.0)
        try:
            dt = email.utils.parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, min((dt - datetime.now(timezone.utc)).total_seconds(), 60.0))
        except Exception:
            return None

    def _backoff(self, attempt: int) -> float:
        base = self.settings.retry_backoff_seconds * (2 ** attempt)
        return min(base, 30.0) * (0.75 + random.random() * 0.5)

    # ------------------------------------------------------------------ public API
    async def fetch(self, url: str) -> FetchResult:
        """Fetch a URL. Raises FetchError subclasses for unfetchable targets."""
        clean = prepare_request_url(url, allowed_ports=self.allowed_ports)
        if not clean:
            self.stats.malformed_skipped += 1
            raise InvalidURL(f"Invalid or disallowed URL: {url[:200]!r}")
        host = (urlsplit(clean).hostname or "").lower()
        if self.host_failures[host] >= self.settings.host_failure_threshold:
            raise HostCircuitOpen(f"Host {host} skipped after {self.host_failures[host]} consecutive failures")
        try:
            result = await self._fetch_http(clean, check_robots=True, max_bytes=self.settings.max_response_bytes)
        except (UnsafeDestination, InvalidURL, TooLarge):
            raise
        except FetchError:
            self.host_failures[host] += 1
            self.stats.network_errors += 1
            raise
        if result.access_class in {"ok", "not_found", "gone", "client_error", "robots_disallowed"}:
            self.host_failures[host] = 0
        elif result.access_class in {"server_error", "blocked", "rate_limited"}:
            self.host_failures[host] += 1
        self.stats.by_class[result.access_class] += 1
        if result.access_class == "ok":
            self.stats.fetched_ok += 1
        elif result.access_class == "robots_disallowed":
            self.stats.robots_blocked += 1
        elif result.access_class in {"blocked", "rate_limited"}:
            self.stats.access_blocked += 1
        return result

    async def fetch_safe(self, url: str) -> FetchResult:
        """Like fetch() but never raises: errors become a FetchResult with access_class set."""
        try:
            return await self.fetch(url)
        except FetchError as exc:
            if isinstance(exc, TooLarge):
                self.stats.too_large += 1
            return FetchResult(
                requested_url=url, final_url=url, status_code=0, headers={}, content=b"", elapsed_ms=0,
                content_type="", error=f"{type(exc).__name__}: {exc}", access_class=exc.access_class,
            )
        except Exception as exc:  # parser/transport surprises must not kill a run
            self.stats.network_errors += 1
            return FetchResult(
                requested_url=url, final_url=url, status_code=0, headers={}, content=b"", elapsed_ms=0,
                content_type="", error=f"{type(exc).__name__}: {exc}", access_class="network_error",
            )

    # ------------------------------------------------------------------ internals
    async def _single_get(self, url: str, max_bytes: int, textual_only: bool) -> tuple[int, dict[str, str], bytes, bool]:
        host = urlsplit(url).hostname or ""
        async with self.global_sem, self.host_sems[host]:
            await self._pace(host, url)
            self.stats.requests += 1
            async with self.client.stream("GET", url) as response:
                self._check_peer(response, url)
                status = response.status_code
                headers = {k.lower(): v for k, v in response.headers.items()}
                if status in REDIRECT_STATUS or status in RETRYABLE_STATUS or status >= 400:
                    return status, headers, b"", True
                ctype = headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if textual_only and not self._content_type_ok(ctype, url):
                    return status, headers, b"", False
                declared_len = headers.get("content-length")
                if declared_len and declared_len.strip().isdigit() and int(declared_len) > max_bytes:
                    raise TooLarge(f"Content-Length {declared_len} exceeds {max_bytes} bytes: {url}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > max_bytes:
                        raise TooLarge(f"Response exceeds {max_bytes} bytes: {url}")
                    body.extend(chunk)
                return status, headers, bytes(body), True

    def _content_type_ok(self, ctype: str, url: str) -> bool:
        if not ctype:
            return True  # many servers omit it; the parser will cope
        if ctype.startswith(TEXTUAL_TYPES):
            return True
        if self.settings.allow_pdf and (ctype == "application/pdf" or urlsplit(url).path.lower().endswith(".pdf")):
            return True
        if ctype in {"application/gzip", "application/x-gzip", "application/octet-stream"} and urlsplit(url).path.lower().endswith(".xml.gz"):
            return True  # compressed sitemaps
        return False

    async def _fetch_http(self, url: str, check_robots: bool, max_bytes: int, textual_only: bool = True) -> FetchResult:
        current = url
        chain: list[str] = []
        seen: set[str] = set()
        start = time.monotonic()
        while True:
            if current in seen:
                raise RedirectError(f"Redirect loop at {current}")
            seen.add(current)
            await self._validate_destination(current)
            if check_robots and not await self.robots.allowed(current):
                return FetchResult(
                    requested_url=url, final_url=current, status_code=0, headers={}, content=b"",
                    elapsed_ms=int((time.monotonic() - start) * 1000), content_type="",
                    robots_allowed=False, error="robots.txt disallows fetch", redirect_chain=chain,
                    access_class="robots_disallowed",
                )

            status: int | None = None
            headers: dict[str, str] = {}
            content = b""
            body_ok = True
            for attempt in range(self.settings.max_retries + 1):
                try:
                    status, headers, content, body_ok = await self._single_get(current, max_bytes, textual_only)
                except (TooLarge, UnsafeDestination):
                    raise
                except httpx.TransportError as exc:  # timeouts, resets, protocol errors, TLS failures
                    if attempt >= self.settings.max_retries:
                        kind = "Timeout" if isinstance(exc, httpx.TimeoutException) else type(exc).__name__
                        err = FetchError(f"{kind} fetching {current}: {exc}")
                        if isinstance(exc, httpx.TimeoutException):
                            err.access_class = "timeout"
                        raise err from None
                    self.stats.retries += 1
                    self._event(f"retry {attempt + 1} after {type(exc).__name__}: {current}")
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                if status in RETRYABLE_STATUS and attempt < self.settings.max_retries:
                    delay = self._retry_after_seconds(headers.get("retry-after"))
                    self.stats.retries += 1
                    self._event(f"retry {attempt + 1} after HTTP {status}: {current}")
                    await asyncio.sleep(delay if delay is not None else self._backoff(attempt))
                    continue
                break
            assert status is not None

            if status in REDIRECT_STATUS:
                location = headers.get("location")
                if not location:
                    raise RedirectError(f"Redirect without Location from {current}")
                chain.append(current)
                if len(chain) > self.settings.max_redirects:
                    raise RedirectError(f"Too many redirects (>{self.settings.max_redirects}) from {url}")
                nxt = prepare_request_url(location, base=current, allowed_ports=self.allowed_ports)
                if not nxt:
                    self.stats.malformed_skipped += 1
                    raise RedirectError(f"Invalid or disallowed redirect target from {current}: {location[:200]!r}")
                current = nxt
                continue

            content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
            access = classify_status(status)
            if access == "ok" and not body_ok:
                access = "unsupported_content"
            return FetchResult(
                requested_url=url,
                final_url=current,
                status_code=status,
                headers=headers,
                content=content,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                content_type=content_type,
                robots_allowed=True,
                error=None if access == "ok" else f"HTTP {status}" + ("" if body_ok else f" unsupported content-type {content_type}"),
                redirect_chain=chain,
                access_class=access,
            )

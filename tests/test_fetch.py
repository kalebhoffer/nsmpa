from __future__ import annotations

import ipaddress
import time

import httpx
import pytest
from conftest import make_settings, public_resolver

from nsmpa.fetch import (
    HardenedFetcher,
    HostCircuitOpen,
    InvalidURL,
    RedirectError,
    TooLarge,
    UnsafeDestination,
)
from nsmpa.utils import (
    host_is_public,
    ip_is_public,
    near_duplicate_key,
    normalize_url,
    prepare_request_url,
)


def fetcher_for(tmp_path, handler, **overrides) -> HardenedFetcher:
    settings = make_settings(tmp_path, **overrides)
    return HardenedFetcher(settings, transport=httpx.MockTransport(handler), resolver=public_resolver)


def html(body: str = "<html><body>ok</body></html>", status: int = 200, **headers) -> httpx.Response:
    return httpx.Response(status, headers={"content-type": "text/html; charset=utf-8", **headers}, content=body.encode())


ROBOTS_NONE = lambda req: httpx.Response(404)  # noqa: E731


# ----------------------------------------------------------------- URL validation

@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "ftp://example.com/x", "mailto:a@b.com", "http://", "https:///path",
    "http://[::1", "http://[zzzz::1]/", "http://exa mple.com/", "http://example.com:99999/",
    "http://example.com:abc/", "http://user:pw@example.com/", "http://-bad-.com/", "http://a..b.com/",
    "http://example.com\r\nHost: evil/", "", "   ", None, "x" * 9000, "http://exa_mple.com/",
])
def test_malformed_urls_rejected(url):
    assert prepare_request_url(url) is None
    assert normalize_url(url) is None


def test_invalid_and_disallowed_ports():
    assert prepare_request_url("http://example.com:22/") is None
    assert prepare_request_url("http://example.com:8080/a") == "http://example.com:8080/a"
    assert prepare_request_url("https://example.com:443/a") == "https://example.com/a"
    assert prepare_request_url("http://example.com:0/") is None


def test_prepare_request_url_preserves_trailing_slash_and_query():
    assert prepare_request_url("HTTPS://Example.COM/a/b/?q=1&utm_source=x#frag") == "https://example.com/a/b/?q=1&utm_source=x"
    assert prepare_request_url("/policy/", base="https://paper.edu/about/") == "https://paper.edu/policy/"
    assert prepare_request_url("https://例え.jp/パス") == "https://xn--r8jz45g.jp/%E3%83%91%E3%82%B9"


def test_identity_normalization_still_dedupes():
    assert normalize_url("HTTPS://Example.COM/a/?utm_source=x&b=2#frag") == "https://example.com/a?b=2"
    assert normalize_url("https://example.com/a") == normalize_url("https://example.com/a/")


@pytest.mark.parametrize("host", [
    "127.0.0.1", "10.0.0.1", "172.16.5.4", "192.168.1.1", "169.254.169.254", "0.0.0.0", "100.64.0.1",
    "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "2002:7f00:0001::1",
    "localhost", "foo.local", "metadata.internal", "2130706433", "0x7f000001", "017700000001",
    "224.0.0.1", "240.0.0.1", "255.255.255.255", "",
])
def test_ssrf_hosts_rejected(host):
    assert host_is_public(host) is False


def test_public_hosts_allowed():
    assert host_is_public("example.com")
    assert host_is_public("8.8.8.8")
    assert host_is_public("2606:4700:4700::1111")
    assert ip_is_public(ipaddress.ip_address("1.1.1.1"))


def test_near_duplicate_key_tolerates_punctuation_and_case():
    a = "We do not remove articles from our archive, except for legal reasons."
    b = "We do NOT remove articles from our archive — except for legal reasons!"
    assert near_duplicate_key(a) == near_duplicate_key(b)
    assert near_duplicate_key(a) != near_duplicate_key("Editors may anonymize names after charges are dismissed.")


# ----------------------------------------------------------------- redirects

async def test_trailing_slash_redirect_does_not_loop(tmp_path):
    """Regression: v0.2 normalized /a/ back to /a and looped on every WordPress page."""
    def handler(req: httpx.Request):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        if req.url.path == "/policy":
            return httpx.Response(301, headers={"location": "https://paper.edu/policy/"})
        if req.url.path == "/policy/":
            return html("<html><title>Policy</title><body>policy</body></html>")
        return httpx.Response(404)
    async with fetcher_for(tmp_path, handler) as f:
        r = await f.fetch("https://paper.edu/policy")
    assert r.status_code == 200 and r.final_url == "https://paper.edu/policy/"
    assert r.redirect_chain == ["https://paper.edu/policy"]
    assert r.access_class == "ok"


async def test_redirect_loop_detected(tmp_path):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        target = "/b" if req.url.path == "/a" else "/a"
        return httpx.Response(302, headers={"location": target})
    async with fetcher_for(tmp_path, handler) as f:
        with pytest.raises(RedirectError, match="loop"):
            await f.fetch("https://paper.edu/a")


async def test_redirect_chain_bounded(tmp_path):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        n = int(req.url.path.strip("/") or 0)
        return httpx.Response(302, headers={"location": f"/{n + 1}"})
    async with fetcher_for(tmp_path, handler, max_redirects=3) as f:
        with pytest.raises(RedirectError, match="Too many"):
            await f.fetch("https://paper.edu/0")


async def test_redirect_to_private_ip_rejected(tmp_path):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})
    async with fetcher_for(tmp_path, handler) as f:
        with pytest.raises(UnsafeDestination):
            await f.fetch("https://paper.edu/a")


async def test_redirect_to_host_resolving_private_rejected(tmp_path):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "https://evil.internal-test/"})
    async with fetcher_for(tmp_path, handler) as f:
        with pytest.raises(UnsafeDestination):
            await f.fetch("https://paper.edu/a")


async def test_redirect_to_non_http_scheme_rejected(tmp_path):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "file:///etc/passwd"})
    async with fetcher_for(tmp_path, handler) as f:
        with pytest.raises(RedirectError):
            await f.fetch("https://paper.edu/a")


async def test_invalid_url_raises_and_fetch_safe_classifies(tmp_path):
    async with fetcher_for(tmp_path, lambda r: html()) as f:
        with pytest.raises(InvalidURL):
            await f.fetch("http://[::1")
        r = await f.fetch_safe("http://example.com:99999/")
        assert r.access_class == "invalid_url"
        assert f.stats.malformed_skipped == 2


# ----------------------------------------------------------------- retries / limits

async def test_429_retry_after_then_success(tmp_path):
    calls = {"n": 0}
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return html()
    async with fetcher_for(tmp_path, handler) as f:
        r = await f.fetch("https://paper.edu/x")
    assert r.status_code == 200 and calls["n"] == 2 and f.stats.retries == 1


async def test_5xx_exhausts_retries_and_is_classified(tmp_path):
    calls = {"n": 0}
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        calls["n"] += 1
        return httpx.Response(503)
    async with fetcher_for(tmp_path, handler, max_retries=2) as f:
        r = await f.fetch("https://paper.edu/x")
    assert calls["n"] == 3
    assert r.access_class == "server_error"


async def test_transport_errors_retried_then_raised(tmp_path):
    calls = {"n": 0}
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        calls["n"] += 1
        raise httpx.RemoteProtocolError("peer closed connection", request=req)
    async with fetcher_for(tmp_path, handler, max_retries=2) as f:
        r = await f.fetch_safe("https://paper.edu/x")
    assert calls["n"] == 3
    assert r.access_class == "network_error"


async def test_timeout_classified(tmp_path):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        raise httpx.ReadTimeout("slow", request=req)
    async with fetcher_for(tmp_path, handler, max_retries=0) as f:
        r = await f.fetch_safe("https://paper.edu/x")
    assert r.access_class == "timeout"


async def test_streaming_size_limit(tmp_path):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"a" * 200_000)
    async with fetcher_for(tmp_path, handler, max_response_bytes=100_000) as f:
        with pytest.raises(TooLarge):
            await f.fetch("https://paper.edu/big")


async def test_declared_content_length_limit(tmp_path):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "text/html", "content-length": "99999999"}, content=b"x")
    async with fetcher_for(tmp_path, handler, max_response_bytes=100_000) as f:
        r = await f.fetch_safe("https://paper.edu/big")
    assert r.access_class == "too_large"


async def test_binary_content_type_not_buffered(tmp_path):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"\x00" * 5000)
    async with fetcher_for(tmp_path, handler) as f:
        r = await f.fetch("https://paper.edu/movie")
    assert r.access_class == "unsupported_content" and r.content == b""


async def test_403_is_blocked_not_failed(tmp_path):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(403, text="cloudflare")
    async with fetcher_for(tmp_path, handler) as f:
        r = await f.fetch("https://paper.edu/x")
    assert r.access_class == "blocked" and r.error == "HTTP 403"


async def test_host_circuit_breaker(tmp_path):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(503)
    async with fetcher_for(tmp_path, handler, max_retries=0, host_failure_threshold=2) as f:
        await f.fetch("https://down.edu/1")
        await f.fetch("https://down.edu/2")
        with pytest.raises(HostCircuitOpen):
            await f.fetch("https://down.edu/3")
        r = await f.fetch_safe("https://down.edu/4")
        assert r.access_class == "host_unavailable"


# ----------------------------------------------------------------- robots / throttling

async def test_robots_disallow_and_crawl_delay(tmp_path):
    seen = []
    def handler(req):
        seen.append(req.url.path)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\nCrawl-delay: 0.2\n")
        return html()
    async with fetcher_for(tmp_path, handler) as f:
        blocked = await f.fetch("https://paper.edu/private/page")
        assert blocked.access_class == "robots_disallowed" and blocked.robots_allowed is False
        t0 = time.monotonic()
        await f.fetch("https://paper.edu/a")
        await f.fetch("https://paper.edu/b")
        assert time.monotonic() - t0 >= 0.18  # crawl-delay honored between same-host requests
    assert "/private/page" not in seen
    assert f.stats.robots_blocked == 1


async def test_robots_5xx_means_disallow(tmp_path):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(500)
        return html()
    async with fetcher_for(tmp_path, handler, max_retries=0) as f:
        r = await f.fetch("https://paper.edu/a")
    assert r.access_class == "robots_disallowed"


async def test_robots_disabled(tmp_path):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        return html()
    async with fetcher_for(tmp_path, handler, respect_robots_txt=False) as f:
        r = await f.fetch("https://paper.edu/a")
    assert r.access_class == "ok"


async def test_per_host_throttle(tmp_path):
    async with fetcher_for(tmp_path, lambda r: httpx.Response(404) if r.url.path == "/robots.txt" else html(),
                           per_host_delay_seconds=0.15) as f:
        t0 = time.monotonic()
        for i in range(3):
            await f.fetch(f"https://paper.edu/{i}")
        assert time.monotonic() - t0 >= 0.3


async def test_non_utf8_and_garbage_bytes_survive(tmp_path):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "text/html; charset=latin-1"},
                              content="<html><body>Caf\xe9 \x00\xff<<<</body".encode("latin-1"))
    async with fetcher_for(tmp_path, handler) as f:
        r = await f.fetch("https://paper.edu/x")
    from nsmpa.extract import analyze_page
    a = analyze_page(r.final_url, r.content, r.content_type, r.headers)
    assert "Caf" in a.text

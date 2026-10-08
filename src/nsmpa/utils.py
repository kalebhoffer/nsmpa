from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import unicodedata
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

TRACKING_PARAMS = {
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source", "utm_campaign", "utm_content",
    "utm_medium", "utm_source", "utm_term",
}

BLOCKED_HOST_SUFFIXES = (
    "facebook.com", "instagram.com", "linkedin.com", "tiktok.com", "x.com", "twitter.com",
    "youtube.com", "youtu.be", "wikipedia.org", "reddit.com", "amazon.com", "amazonaws.com",
    "pinterest.com", "threads.net", "bsky.app", "issuu.com", "scribd.com", "yumpu.com",
)

# Registered hostnames: labels of letters/digits/hyphens (IDNA-encoded), not starting/ending with '-'.
_HOST_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
DEFAULT_ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def ensure_scheme(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, flags=re.I):
        url = "https://" + url
    return url


def _clean_host(host: str) -> str | None:
    """Return a lowercase ASCII host, or None if it is not a syntactically valid host."""
    host = host.strip().lower().rstrip(".")
    if not host or len(host) > 253:
        return None
    # IP literals (IPv6 arrives without brackets from urlsplit().hostname).
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if ":" in host or "%" in host:
        return None
    try:
        ascii_host = host.encode("idna").decode("ascii") if not host.isascii() else host
    except UnicodeError:
        return None
    labels = ascii_host.split(".")
    if len(labels) < 2 and ascii_host != "localhost":
        return None
    if not all(_HOST_LABEL.match(label) for label in labels):
        return None
    return ascii_host


def _split_checked(url: str, base: str | None, allowed_ports: frozenset[int] | None):
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or len(url) > 8192 or any(c in url for c in "\x00\r\n\t"):
        return None
    if base:
        url = urljoin(base, url)
    parts = urlsplit(url)  # may raise ValueError, e.g. "Invalid IPv6 URL"
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        return None
    if parts.username or parts.password:
        return None  # credentials in URLs are never legitimate crawl targets
    if not parts.hostname:
        return None
    host = _clean_host(parts.hostname)
    if not host:
        return None
    port = parts.port  # raises ValueError for out-of-range/non-numeric ports
    if port is not None and allowed_ports is not None and port not in allowed_ports:
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{netloc}:{port}"
    return scheme, netloc, parts


def normalize_url(url: str, base: str | None = None,
                  allowed_ports: frozenset[int] | None = DEFAULT_ALLOWED_PORTS) -> str | None:
    """Identity/dedupe key for an HTTP(S) URL (lossy: strips tracking params and trailing '/').

    Never use the result as the URL to request: servers commonly redirect ``/a`` to ``/a/``
    and re-normalizing the redirect target would loop. Use :func:`prepare_request_url`.
    """
    try:
        checked = _split_checked(url, base, allowed_ports)
        if not checked:
            return None
        scheme, netloc, parts = checked
        path = re.sub(r"/{2,}", "/", parts.path or "/")
        if path != "/" and path.endswith("/"):
            path = path[:-1]
        query_pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if k.lower() not in TRACKING_PARAMS]
        query = urlencode(query_pairs, doseq=True)
        return urlunsplit((scheme, netloc, path, query, ""))
    except (ValueError, UnicodeError):
        return None


def prepare_request_url(url: str, base: str | None = None,
                        allowed_ports: frozenset[int] | None = DEFAULT_ALLOWED_PORTS) -> str | None:
    """Validate a URL for fetching while preserving its path and query exactly.

    Only the scheme/host case is normalized, the fragment is dropped and unsafe
    characters are percent-encoded. Returns None for malformed or disallowed URLs.
    """
    try:
        checked = _split_checked(url, base, allowed_ports)
        if not checked:
            return None
        scheme, netloc, parts = checked
        path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
        query = quote(parts.query, safe="=&%:@!$'()*+,;/?-._~")
        return urlunsplit((scheme, netloc, path, query, ""))
    except (ValueError, UnicodeError):
        return None


def url_port_allowed(url: str, allowed_ports: frozenset[int] = DEFAULT_ALLOWED_PORTS) -> bool:
    try:
        port = urlsplit(url).port
    except ValueError:
        return False
    return port is None or port in allowed_ports


def registrableish_domain(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def is_blocked_social_or_aggregator(url: str) -> bool:
    host = registrableish_domain(url)
    return any(host == d or host.endswith("." + d) for d in BLOCKED_HOST_SUFFIXES)


def same_site(a: str, b: str) -> bool:
    ah = registrableish_domain(a)
    bh = registrableish_domain(b)
    if not ah or not bh:
        return False
    return ah == bh or ah.endswith("." + bh) or bh.endswith("." + ah)


def slugify(text: str, max_len: int = 80) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", text).strip("-").lower()
    return value[:max_len] or "item"


def safe_snapshot_path(root: Path, publication_id: int, url: str, suffix: str = ".html") -> Path:
    h = sha256_text(url)[:20]
    return root / str(publication_id) / f"{h}{suffix}"


def ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for globally routable unicast addresses.

    Unwraps IPv4-mapped, 6to4 and Teredo IPv6 forms so that e.g. ``::ffff:127.0.0.1``
    cannot smuggle a loopback destination past the check.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        for embedded in (ip.ipv4_mapped, ip.sixtofour):
            if embedded is not None:
                return ip_is_public(embedded)
        if ip.teredo is not None:
            return all(ip_is_public(x) for x in ip.teredo)
    if ip.is_multicast or ip.is_unspecified or ip.is_reserved or ip.is_loopback:
        return False
    if ip.is_private or ip.is_link_local:
        return False
    return bool(ip.is_global)


def host_is_public(host: str) -> bool:
    """Reject loopback/private/link-local/reserved destinations to limit SSRF risk."""
    host = (host or "").lower().strip().strip("[]").rstrip(".")
    if not host:
        return False
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".localhost", ".internal", ".lan", ".home.arpa")):
        return False
    try:
        return ip_is_public(ipaddress.ip_address(host))
    except ValueError:
        pass
    # Purely numeric/hex hostnames (e.g. "2130706433", "0x7f000001") are resolver-dependent
    # encodings of IP addresses; never treat them as ordinary names.
    if re.fullmatch(r"(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+)){0,3}", host):
        return False
    return _clean_host(host) is not None


def resolve_public_addresses(host: str) -> list[str]:
    """Resolve a host and return its addresses only if every address is public.

    Raises ValueError when the host is not public or does not resolve.
    """
    if not host_is_public(host):
        raise ValueError(f"host is not public: {host!r}")
    try:
        ipaddress.ip_address(host.strip("[]"))
        return [host.strip("[]")]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise ValueError(f"DNS resolution failed for {host!r}: {exc}") from exc
    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        raise ValueError(f"no addresses for {host!r}")
    for addr in addresses:
        try:
            if not ip_is_public(ipaddress.ip_address(addr.split("%", 1)[0])):
                raise ValueError(f"{host!r} resolves to non-public address {addr}")
        except ValueError as exc:
            raise ValueError(str(exc)) from None
    return addresses


def resolved_host_is_public(host: str) -> bool:
    try:
        resolve_public_addresses(host)
        return True
    except ValueError:
        return False


def compact_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_for_hash(text: str) -> str:
    """Canonical text form used for excerpt identity: NFKC, lowercase, punctuation-insensitive."""
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def near_duplicate_key(text: str, shingle: int = 5) -> str:
    """Order-insensitive fingerprint of the most informative word shingles.

    Two excerpts that share their lexically smallest shingle hashes are near-duplicates
    (syndicated or lightly edited boilerplate). Cheap, deterministic and indexable.
    """
    words = normalize_for_hash(text).split()
    if len(words) <= shingle:
        return sha256_text(" ".join(words))[:24]
    hashes = sorted({sha256_text(" ".join(words[i:i + shingle]))[:16] for i in range(len(words) - shingle + 1)})
    return sha256_text("|".join(hashes[:6]))[:24]


def json_meta(row, column: str = "metadata_json") -> dict:
    """A row's JSON metadata column as a dict; empty, missing or malformed metadata reads as ``{}``."""
    try:
        value = json.loads(row[column] or "{}")
    except (KeyError, IndexError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}

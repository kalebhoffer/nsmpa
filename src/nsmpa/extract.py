from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from io import BytesIO
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .models import PageAnalysis
from .utils import compact_ws, normalize_url

POLICY_TERMS: dict[str, tuple[re.Pattern[str], float]] = {
    "deindex": (re.compile(r"\bde[- ]?index(?:ing|ed)?\b", re.I), 8.0),
    "noindex": (re.compile(r"\bnoindex\b", re.I), 8.0),
    "anonymize": (re.compile(r"\banonym(?:ize|ise|ized|ised|ization|isation|ity)\b", re.I), 7.0),
    "unpublish": (re.compile(r"\bunpublish(?:ed|ing)?\b", re.I), 8.0),
    "takedown": (re.compile(r"\btake[- ]?down\b", re.I), 7.0),
    "remove": (re.compile(r"\bremov(?:e|al|ed|ing)\b", re.I), 3.0),
    "changed_circumstances": (re.compile(r"\b(chang(?:e|ed|ing) circumstances|circumstances have changed)\b", re.I), 8.0),
    "dismissed_charges": (re.compile(r"\b(charges? (?:were |was )?(?:dismissed|dropped)|case (?:was )?dismissed)\b", re.I), 9.0),
    "expunged": (re.compile(r"\bexpung(?:e|ed|ement)\b", re.I), 9.0),
    "sealed": (re.compile(r"\b(record|records|case).{0,30}\bseal(?:ed|ing)?\b", re.I), 8.0),
    "acquitted": (re.compile(r"\bacquitt(?:ed|al)\b", re.I), 9.0),
    "rehabilitation": (re.compile(r"\brehabilitat(?:e|ed|ion)\b", re.I), 6.0),
    "privacy": (re.compile(r"\bprivacy\b", re.I), 3.0),
    "safety": (re.compile(r"\b(safety|physical harm|credible threat)\b", re.I), 4.0),
    "reputational_harm": (re.compile(r"\b(reputational? harm|damage to (?:a |their |his |her )?reputation)\b", re.I), 7.0),
    "minimize_harm": (re.compile(r"\bminimi[sz](?:e|ing) harm\b", re.I), 5.0),
    "public_interest": (re.compile(r"\bpublic interest\b", re.I), 3.0),
    "case_by_case": (re.compile(r"\bcase[- ]by[- ]case\b", re.I), 7.0),
    "update": (re.compile(r"\b(update|updated|updating|editor'?s note|addendum|follow[- ]up)\b", re.I), 2.0),
    "correction": (re.compile(r"\b(correction|corrected|corrections policy)\b", re.I), 2.0),
    "archive_integrity": (re.compile(r"\b(archive|historical record|record of publication|integrity of (?:the )?archive)\b", re.I), 2.0),
    "search_engine": (re.compile(r"\b(search engine|google|bing|search results?)\b", re.I), 2.0),
    "robots": (re.compile(r"\brobots\.txt\b", re.I), 4.0),
}

URL_POLICY_HINTS = re.compile(
    r"(?:about|policy|policies|ethics|standards|correction|archive|privacy|remov|unpublish|takedown|deindex|editorial)",
    re.I,
)

NEGATIVE_ARCHIVE_PHRASES = re.compile(
    r"(?:do not (?:remove|delete|unpublish)|will not (?:remove|delete|unpublish)|"
    r"never (?:remove|delete|unpublish)|archive.{0,50}(?:permanent|historical record)|"
    r"content.{0,30}(?:will|shall) remain)",
    re.I,
)


def _extract_pdf_text(content: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(BytesIO(content))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:100])
    except Exception:
        return ""


def _directives(meta: str | None, x_header: str | None) -> tuple[bool, bool]:
    joined = " ".join(x for x in [meta, x_header] if x).lower()
    tokens = {t.strip() for t in re.split(r"[,;\s]+", joined) if t.strip()}
    return "noindex" in tokens or "none" in tokens, "nofollow" in tokens or "none" in tokens


def extract_sitemap_urls(content: bytes, max_urls: int) -> tuple[list[str], list[str]]:
    """Return (page URLs, nested sitemap URLs). Handles standard XML namespaces."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return [], []
    tag = root.tag.lower()
    locs = []
    for elem in root.iter():
        if elem.tag.lower().endswith("loc") and elem.text:
            locs.append(html.unescape(elem.text.strip()))
            if len(locs) >= max_urls:
                break
    if tag.endswith("sitemapindex"):
        return [], locs
    return locs, []


def policy_score_for(url: str, title: str, text: str) -> tuple[float, list[str]]:
    haystack = f"{title}\n{text[:500_000]}"
    tags: list[str] = []
    score = 0.0
    for tag, (pattern, weight) in POLICY_TERMS.items():
        if pattern.search(haystack):
            tags.append(tag)
            score += weight
    if URL_POLICY_HINTS.search(url):
        score += 6.0
    if re.search(r"\b(editorial|newsroom|publication) (?:policy|policies|standards|guidelines)\b", haystack, re.I):
        score += 6.0
    if NEGATIVE_ARCHIVE_PHRASES.search(haystack):
        score += 7.0
        if "archive_integrity" not in tags:
            tags.append("archive_integrity")
    return min(score, 100.0), sorted(set(tags))


def _excerpt_windows(text: str, tags: list[str], radius: int = 360) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for tag in tags:
        pattern = POLICY_TERMS[tag][0]
        for m in list(pattern.finditer(text))[:4]:
            start = max(0, m.start() - radius)
            end = min(len(text), m.end() + radius)
            excerpt = compact_ws(text[start:end])
            key = (tag, excerpt.lower())
            if len(excerpt) >= 50 and key not in seen:
                seen.add(key)
                out.append((tag, excerpt))
    return out[:30]


def analyze_page(url: str, content: bytes, content_type: str, headers: dict[str, str]) -> PageAnalysis:
    title = ""
    canonical = None
    meta_robots = None
    links: list[str] = []

    if content_type == "application/pdf" or url.lower().endswith(".pdf"):
        text = _extract_pdf_text(content)
    else:
        decoded = content.decode("utf-8", errors="replace")
        soup = BeautifulSoup(decoded, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "template"]):
            tag.decompose()
        if soup.title:
            title = compact_ws(soup.title.get_text(" ", strip=True))
        can = soup.find("link", attrs={"rel": lambda v: v and "canonical" in str(v).lower()})
        if can and can.get("href"):
            canonical = normalize_url(can.get("href"), url)
        robots_meta = soup.find("meta", attrs={"name": lambda v: v and str(v).lower() in {"robots", "googlebot", "bingbot"}})
        if robots_meta and robots_meta.get("content"):
            meta_robots = compact_ws(str(robots_meta.get("content")))
        for a in soup.find_all("a", href=True):
            normalized = normalize_url(str(a.get("href")), url)
            if normalized:
                links.append(normalized)
        text = compact_ws(soup.get_text(" ", strip=True))

    x_robots = headers.get("x-robots-tag")
    noindex, nofollow = _directives(meta_robots, x_robots)
    score, tags = policy_score_for(url, title, text)
    excerpts = _excerpt_windows(text, tags)
    return PageAnalysis(
        url=url, title=title, text=text, canonical_url=canonical, meta_robots=meta_robots,
        x_robots_tag=x_robots, noindex=noindex, nofollow=nofollow,
        links=list(dict.fromkeys(links)), policy_score=score, evidence_tags=tags, excerpts=excerpts,
    )

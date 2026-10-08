from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from io import BytesIO

from dataclasses import dataclass, field

from bs4 import BeautifulSoup, UnicodeDammit

from .models import PageAnalysis
from .utils import compact_ws, normalize_url, prepare_request_url

try:  # lxml is faster and more tolerant; html.parser is the always-available fallback
    import lxml  # noqa: F401
    _PARSER = "lxml"
except ImportError:  # pragma: no cover
    _PARSER = "html.parser"

BOILERPLATE_TAGS = ["script", "style", "noscript", "svg", "template", "nav", "header", "footer", "aside",
                    "form", "iframe", "button", "select", "dialog", "menu"]
BOILERPLATE_ATTR = re.compile(
    r"(?:^|[-_ ])(?:nav|navbar|menu|footer|header|sidebar|side-bar|widget|cookie|consent|gdpr|banner|"
    r"subscribe|newsletter|share|sharing|social|breadcrumb|comments?|related|recommend|promo|advert|"
    r"ads?|sponsor|popup|modal|masthead|skip|search-form|pagination|tags?-list|byline-social)(?:$|[-_ ])",
    re.I,
)
LISTING_URL = re.compile(r"/(?:tag|tags|category|categories|topics?|author|search|page/\d+)(?:/|$)|[?&](?:s|q|query|search)=", re.I)


@dataclass
class MainText:
    url: str
    title: str
    main_text: str
    full_text: str
    canonical_url: str | None
    meta_robots: str | None
    x_robots_tag: str | None
    noindex: bool
    nofollow: bool
    links: list[tuple[str, str]] = field(default_factory=list)
    is_listing: bool = False
    is_pdf: bool = False
    parse_error: str | None = None
    author: str | None = None
    published: str | None = None


def _meta(soup, *names: str) -> str | None:
    for n in names:
        tag = soup.find("meta", attrs={"name": n}) or soup.find("meta", attrs={"property": n})
        if tag and tag.get("content"):
            return compact_ws(str(tag.get("content")))[:200]
    return None


def _author(soup) -> str | None:
    a = _meta(soup, "author", "article:author", "parsely-author", "sailthru.author", "dc.creator")
    if a and not a.startswith("http"):
        return a
    by = soup.find(attrs={"class": re.compile(r"\b(?:byline|author-name|author)\b", re.I)}) or soup.find(attrs={"rel": "author"})
    if by:
        txt = re.sub(r"^\s*by\s+", "", compact_ws(by.get_text(" ", strip=True)), flags=re.I)
        if 3 <= len(txt) <= 80:
            return txt
    return None


def decode_html(content: bytes, content_type_header: str | None = None) -> str:
    declared = None
    if content_type_header and "charset=" in content_type_header.lower():
        declared = content_type_header.lower().split("charset=", 1)[1].split(";")[0].strip(" \"'")
    try:
        dammit = UnicodeDammit(content, [declared] if declared else [], is_html=True)
        if dammit.unicode_markup is not None:
            return dammit.unicode_markup
    except Exception:
        pass
    return content.decode("utf-8", errors="replace")


def _strip_boilerplate(soup: BeautifulSoup) -> None:
    for tag in soup(BOILERPLATE_TAGS):
        tag.decompose()
    for tag in soup.find_all(True):
        if tag.decomposed if hasattr(tag, "decomposed") else False:
            continue
        attrs = getattr(tag, "attrs", None) or {}
        ident = " ".join([str(attrs.get("id", ""))] + [str(c) for c in (attrs.get("class") or [])])
        role = str(attrs.get("role", "")).lower()
        if role in {"navigation", "banner", "contentinfo", "complementary", "search", "dialog"} or (
                ident.strip() and BOILERPLATE_ATTR.search(ident) and tag.name not in {"body", "html", "main", "article"}):
            tag.decompose()


def extract_main_text(content: bytes, content_type: str, url: str, headers: dict[str, str]) -> MainText:
    """Parse a page into main-content text plus technical indexing signals. Never raises."""
    x_robots = headers.get("x-robots-tag")
    if content_type == "application/pdf" or url.lower().split("?", 1)[0].endswith(".pdf"):
        text = compact_ws(_extract_pdf_text(content))
        noindex, nofollow = _directives(None, x_robots)
        return MainText(url, "", text, text, None, None, x_robots, noindex, nofollow, [], False, True,
                        None if text else "pdf_text_unavailable")
    try:
        decoded = decode_html(content, headers.get("content-type"))
        soup = BeautifulSoup(decoded, _PARSER)
        title = compact_ws(soup.title.get_text(" ", strip=True)) if soup.title else ""
        canonical = None
        can = soup.find("link", attrs={"rel": lambda v: v and "canonical" in str(v).lower()})
        if can and can.get("href"):
            canonical = normalize_url(str(can.get("href")), url)
        meta_robots = None
        robots_meta = soup.find_all("meta", attrs={"name": lambda v: v and str(v).lower() in {"robots", "googlebot", "bingbot"}})
        if robots_meta:
            meta_robots = "; ".join(compact_ws(str(m.get("content", ""))) for m in robots_meta if m.get("content")) or None
        author = _author(soup)
        published = _meta(soup, "article:published_time", "datePublished", "date", "pubdate", "dc.date")
        if not published:
            t = soup.find("time", attrs={"datetime": True})
            published = str(t.get("datetime"))[:40] if t else None
        links: list[tuple[str, str]] = []
        for a in soup.find_all("a", href=True):
            href = prepare_request_url(str(a.get("href")), base=url)
            if href:
                links.append((compact_ws(a.get_text(" ", strip=True))[:200], href))
        for t in soup(["script", "style", "noscript", "template"]):
            t.decompose()
        full_text = compact_ws(soup.get_text(" ", strip=True))
        _strip_boilerplate(soup)
        root = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.find("article") or soup.body or soup
        main_text = compact_ws(root.get_text(" ", strip=True)) if root else ""
        if len(main_text) < 200 and root is not soup.body and soup.body is not None:
            main_text = compact_ws(soup.body.get_text(" ", strip=True))
        noindex, nofollow = _directives(meta_robots, x_robots)
        return MainText(url, title, main_text, full_text, canonical, meta_robots, x_robots, noindex, nofollow,
                        list(dict.fromkeys(links)), bool(LISTING_URL.search(url)), author=author, published=published)
    except Exception as exc:  # malformed markup must never terminate a run
        noindex, nofollow = _directives(None, x_robots)
        return MainText(url, "", "", "", None, None, x_robots, noindex, nofollow, [], False, False,
                        f"{type(exc).__name__}: {exc}"[:300])

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
    """v0.1-compatible page analysis used by the deep student-site crawler."""
    page = extract_main_text(content, content_type, url, headers)
    text = page.main_text or page.full_text
    title, canonical, meta_robots, x_robots = page.title, page.canonical_url, page.meta_robots, page.x_robots_tag
    noindex, nofollow = page.noindex, page.nofollow
    links = [u for _, u in page.links]
    score, tags = policy_score_for(url, title, text)
    excerpts = _excerpt_windows(text, tags)
    return PageAnalysis(
        url=url, title=title, text=text, canonical_url=canonical, meta_robots=meta_robots,
        x_robots_tag=x_robots, noindex=noindex, nofollow=nofollow,
        links=list(dict.fromkeys(links)), policy_score=score, evidence_tags=tags, excerpts=excerpts,
    )

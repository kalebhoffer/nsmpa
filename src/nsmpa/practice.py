"""Deep practice digging: what a newsroom *did*, read from its own archive.

Written policy is often absent. Practice can still be observed directly:
- editor's notes appended to old crime stories when charges were dismissed/dropped, or acquittals;
- notes saying a name was removed or withheld;
- ``noindex`` directives on archived crime/arrest articles (technical de-indexing).

Sampling is transparent and reproducible: crime/arrest article URLs are taken from the site's own
sitemaps by a published keyword rule (``CRIME_SLUG``), sorted oldest-first by the year in the URL
(or lastmod), and sampled at even intervals so old and new coverage are both represented. A small
baseline of ordinary articles is fetched from the same site so a *site-wide* ``noindex`` is never
mistaken for targeted de-indexing. Absence from Google is never used as evidence.
"""
from __future__ import annotations

import gzip
import re
from urllib.parse import urlsplit

from .extract import extract_sitemap_urls
from .fetch import HardenedFetcher
from .utils import prepare_request_url, same_site

CRIME_SLUG = re.compile(
    r"(?:^|[-_/])(?:arrest(?:ed|s)?|charged|charges|police|sheriff|crime|criminal|accused|assault|dui|dwi|theft|"
    r"burglary|robbery|drugs?|sentenced|plea|pleads|guilty|acquitted|dismissed|indicted|jail|booked|cited|"
    r"blotter|crime-?log|misdemeanor|felony|investigation|suspect|stabbing|shooting|vandalism|trespass(?:ing)?)(?:$|[-_/.])",
    re.I,
)
LISTING = re.compile(r"/(?:tag|tags|category|categories|topics?|author|search|page/\d+|feed|amp)(?:/|$)", re.I)
ARTICLE_LIKE = re.compile(r"/(?:19|20)\d{2}/|/\d{4}-\d{2}-\d{2}|/article|/story|/news/|/[a-z0-9]+(?:-[a-z0-9]+){4,}/?$", re.I)
YEAR = re.compile(r"/((?:19|20)\d{2})/")
SITEMAP_LINE = re.compile(r"^\s*sitemap:\s*(\S+)", re.I | re.M)


def _year(url: str) -> int:
    m = YEAR.search(url)
    return int(m.group(1)) if m else 9999


def evenly(items: list[str], k: int) -> list[str]:
    if k <= 0 or not items:
        return []
    if len(items) <= k:
        return list(items)
    step = (len(items) - 1) / (k - 1) if k > 1 else 0
    return [items[round(i * step)] for i in range(k)]


async def site_article_urls(fetcher: HardenedFetcher, home: str, *, max_sitemaps: int = 16, max_urls: int = 25_000) -> list[str]:
    origin = f"{urlsplit(home).scheme}://{urlsplit(home).netloc}"
    maps: list[str] = []
    robots = await fetcher.fetch_safe(origin + "/robots.txt")
    if robots.access_class == "ok":
        maps += [m.strip() for m in SITEMAP_LINE.findall(robots.content.decode("utf-8", "replace"))]
    maps += [origin + p for p in ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml", "/news-sitemap.xml", "/post-sitemap.xml")]
    seen_maps: set[str] = set()
    urls: list[str] = []
    queue = [m for m in maps if prepare_request_url(m)]
    while queue and len(seen_maps) < max_sitemaps and len(urls) < max_urls:
        sm = queue.pop(0)
        if sm in seen_maps:
            continue
        seen_maps.add(sm)
        r = await fetcher.fetch_safe(sm)
        if r.access_class != "ok" or not r.content:
            continue
        body = r.content
        if body[:2] == b"\x1f\x8b":
            try:
                body = gzip.decompress(body)[:20_000_000]
            except OSError:
                continue
        pages, nested = extract_sitemap_urls(body, max_urls)
        # Older post sitemaps first: archives are where historical crime coverage lives.
        nested_sorted = sorted(nested, key=lambda u: (0 if re.search(r"post|article|news|story|20\d\d|19\d\d", u, re.I) else 1, u))
        queue.extend(n for n in nested_sorted if n not in seen_maps and same_site(home, n))
        urls.extend(p for p in pages if same_site(home, p))
    return list(dict.fromkeys(urls))[:max_urls]


def choose_samples(urls: list[str], crime_k: int, baseline_k: int) -> tuple[list[str], list[str]]:
    articles = [u for u in urls if not LISTING.search(urlsplit(u).path) and ARTICLE_LIKE.search(urlsplit(u).path)]
    crime = sorted((u for u in articles if CRIME_SLUG.search(urlsplit(u).path)), key=lambda u: (_year(u), u))
    other = sorted((u for u in articles if not CRIME_SLUG.search(urlsplit(u).path)), key=lambda u: (_year(u), u))
    return evenly(crime, crime_k), evenly(other, baseline_k)

"""Research a publication whose live website blocks automated access, without bypassing the block.

The live site refused NSMPA (HTTP 401/403, a bot challenge, or robots.txt). Disguising the crawler would undercut the
evidence, so the fallback only uses copies and channels that are open to it, in order of cost:

1. **Archived copies** (free). The Internet Archive's most recent capture of the homepage is identity-checked, then
   archived copies of the site's policy/about pages (found from the homepage links, from search results, and from a
   CDX listing of top-level policy-like paths) are read and classified like live pages. Each page and excerpt is
   labelled ``acquisition='archive'`` with the capture timestamp and archive URL.
2. **Search snippets** (the usual tier-1 ``site:`` searches). Google has indexed the site even though NSMPA cannot fetch
   it. Snippets are stored as ``acquisition='snippet'`` evidence: *leads* that never drive a stance on their own. Their
   URLs feed step 1.
3. **AI search** (Gemini with Google Search grounding, or Claude's web search tool). The model is asked where the
   policy is and what it says, verbatim. Its answer is an ``ai_leads`` row, never evidence: each quote must be found
   word-for-word in text NSMPA itself holds (a live or archived page, a snippet, or a researcher capture). A claimed
   URL is read from the archive; failing that, one exact-phrase search locates the real page.
4. **Published elsewhere** (professional and support organizations): one search for the outlet's standards handbook
   or ethics policy on other sites, read live with the usual third-party rules.

What still cannot be confirmed stays UNDETERMINED with the reason recorded, and the researcher can add the text with
``nsmpa capture`` or the GUI's Capture tab (see :mod:`nsmpa.capture`).
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

from .evidence import action_positions, extract_evidence
from .extract import extract_main_text
from .identity import check_identity
from .models import FetchResult
from .utils import normalize_for_hash, registrableish_domain

CDX_POLICY = ("https://web.archive.org/cdx/search/cdx?url={domain}&matchType=domain&output=json&fl=original,timestamp"
              "&filter=statuscode:200&filter=original:{regex}&collapse=urlkey&limit=200")
RAW = "https://web.archive.org/web/{ts}id_/{url}"
VIEW = "https://web.archive.org/web/{ts}/{url}"
# Top-level (or /page/, /about/) paths only: articles that merely mention "policy" are not the policy.
POLICY_PATH = (r"^https?://(www\.)?[^/]+(:\d+)?/((page|pages|about|info|staff|policies)/)?"
               r"[a-z0-9-]*(polic|ethic|standard|correction|unpublish|takedown|removal|archive|about|mission|principle)"
               r"[a-z0-9-]*/?$")

FIND_POLICY_PROMPT_VERSION = "find_policy_v1"
FIND_POLICY_SYSTEM = (
    "You locate a news organization's published policy on post-publication relief: unpublishing or removing articles, "
    "removing or anonymizing names, de-indexing from search engines, or a refusal to do any of these (including "
    "'update only' or 'we never unpublish' policies). Use web search. Report only text you actually found, quoted "
    "verbatim (one or two key sentences per quote, at most 40 words each) with the exact URL of the page it appears on. Prefer the organization's own website; "
    "a standards handbook published elsewhere by the organization also counts. If you find nothing, return empty lists. "
    "Never paraphrase inside a quote and never guess a URL.")
FIND_POLICY_SCHEMA = {
    "type": "object",
    "properties": {
        "policy_urls": {"type": "array", "items": {"type": "string"}},
        "quotes": {"type": "array", "items": {
            "type": "object",
            "properties": {"quote": {"type": "string"}, "url": {"type": "string"}},
            "required": ["quote", "url"]}},
        "summary": {"type": "string"},
    },
    "required": ["policy_urls", "quotes", "summary"],
}


def _rows(content: bytes) -> list[list[str]]:
    try:
        data = json.loads((content or b"[]").decode("utf-8", "replace") or "[]")
    except ValueError:
        return []
    return data[1:] if data and isinstance(data[0], list) and data[0] and data[0][0] in {"timestamp", "original"} else data


def _bare(url: str) -> str:
    return re.sub(r"^https?://", "", url)


class BlockedSiteFallback:
    """Runs inside :class:`nsmpa.research.EntityResearcher` for one blocked entity."""

    def __init__(self, researcher, entity, budget: int):
        self.r = researcher
        self.db, self.settings, self.fetcher = researcher.db, researcher.settings, researcher.fetcher
        self.entity = entity
        self.budget = budget
        self.used = 0
        self.archived: set[str] = set()
        self.notes: list[str] = []

    # ------------------------------------------------------------------ archive
    async def read_archived(self, url: str, *, topic: str, ts: str | None = None, forced_kind: str | None = None):
        """Fetch the latest (or given) capture of ``url`` and record it as an archived first-party page."""
        key = normalize_for_hash(_bare(url)).rstrip("/")
        if key in self.archived:
            return None, None
        self.archived.add(key)
        # Asking for a capture "as of now" redirects to the nearest one in a second or two; a CDX "latest" query
        # scans the whole history and times out on large sites.
        snap = await self.fetcher.fetch_safe(RAW.format(ts=ts or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"), url=url))
        if snap.access_class != "ok" or not snap.content:
            return None, None
        m = re.search(r"/web/(\d{14})id_/", snap.final_url or "")
        ts = m.group(1) if m else ts
        if not ts:
            return None, None
        raw_url = RAW.format(ts=ts, url=url)
        fr = FetchResult(requested_url=raw_url, final_url=url, status_code=200, headers=dict(snap.headers),
                         content=snap.content, elapsed_ms=snap.elapsed_ms, content_type=snap.content_type,
                         access_class="ok")
        _, page, page_id = await self.r._record_page(self.entity, fr, target_id=None, query_id=None, topic=topic,
                                                     forced_kind=forced_kind)
        archive_url = VIEW.format(ts=ts, url=url)
        self.db.execute("UPDATE research_pages SET acquisition='archive', archive_ts=?, archive_url=? WHERE id=?",
                        (ts, archive_url, page_id))
        self.db.execute("UPDATE evidence_items SET acquisition='archive', archive_ts=?, archive_url=? WHERE page_id=?",
                        (ts, archive_url, page_id))
        self.db.conn.commit()
        return page, page_id

    async def read_first_party(self, url: str, *, topic: str):
        """Own-site page: live when its host is open (a sibling domain such as ap.org), else the archived copy."""
        from .utils import prepare_request_url
        req = prepare_request_url(url)
        if not req:
            return None, None
        blocked_host = (urlsplit(self.entity["homepage_url"] or "").hostname or "").lower()
        host = (urlsplit(req).hostname or "").lower()
        if host and host != blocked_host and self.fetcher.host_failures.get(host, 0) < self.settings.host_failure_threshold:
            key = normalize_for_hash(_bare(req)).rstrip("/")
            if key not in self.archived:
                self.archived.add(key)
                r = await self.fetcher.fetch_safe(req)
                if r.access_class == "ok":
                    _, page, page_id = await self.r._record_page(self.entity, r, target_id=None, query_id=None, topic=topic)
                    return page, page_id
                self.archived.discard(key)
        return await self.read_archived(req, topic=topic)

    async def policy_paths(self) -> list[tuple[str, str]]:
        domain = self.entity["domain"] or registrableish_domain(self.entity["homepage_url"] or "")
        if not domain:
            return []
        cdx = await self.fetcher.fetch_safe(CDX_POLICY.format(domain=quote(domain, safe=""), regex=quote(POLICY_PATH, safe="")))
        if cdx.access_class != "ok":
            return []
        seen, out = set(), []
        for row in _rows(cdx.content):
            if len(row) < 2:
                continue
            original, ts = row[0], row[1]
            path = urlsplit(original).path.rstrip("/").lower()
            if path in seen:
                continue
            seen.add(path)
            out.append((original, ts))
        # Policy words beat "about"; shorter paths first.
        out.sort(key=lambda x: (not re.search(r"polic|ethic|standard|correction|unpublish|takedown|removal", x[0], re.I),
                                len(x[0])))
        return out

    # ------------------------------------------------------------------ snippets
    def store_snippets(self) -> int:
        """Tier-1 search snippets from the blocked site, kept as leads (acquisition='snippet')."""
        from .research import authority_for, entity_terms, evidence_class_for, is_first_party
        rows = self.db.execute(
            "SELECT * FROM research_targets WHERE run_id=? AND entity_id=? AND snippet IS NOT NULL AND snippet!=''",
            (self.r.run_id, self.entity["id"])).fetchall()
        n = 0
        for t in rows:
            if not is_first_party(self.entity, t["url"]):
                continue
            from .research import ABOUT_PAGE_RE, POLICY_PAGE_RE
            path = urlsplit(t["url"]).path
            first_person = re.search(r"\b(?:we|our|us)\b", t["snippet"], re.I)
            dated_story = re.search(r"/(?:19|20)\d\d/|-(?:19|20)\d\d-\d\d-\d\d\b|/article/", path)
            if dated_story and not first_person:
                continue  # a news story on the site is not the site's policy
            if not (POLICY_PAGE_RE.search(f"{t['title']} {path}") or ABOUT_PAGE_RE.search(path) or first_person):
                continue
            text = re.sub(r"^[A-Z][a-z]{2} \d{1,2}, \d{4}\s*[—–-]\s*", "", t["snippet"])  # "Jan 5, 2020 — "
            for it in extract_evidence(text, entity_terms=entity_terms(self.entity), max_items=5):
                st = it.statement
                if st.statement_type == "mention":
                    continue

                class _Page:
                    pass
                pg = _Page()
                pg.url, pg.title = t["url"], t["title"] or ""
                ev_class = evidence_class_for(self.entity["cohort"], True, False, "snippet", st.statement_type)
                new_id = self.r._insert_item(
                    self.entity, page=pg, page_id=None, query_id=t["query_id"], topic="search_snippet", first=True,
                    about=True, ev_class=ev_class, statement_type=st.statement_type, direction=st.direction,
                    excerpt=it.excerpt, context=f"Search-result snippet (site blocks automated access): {t['snippet'][:600]}",
                    excerpt_sha=it.excerpt_sha256, near_dup=it.near_dup_key, tags=st.tags + ["search_snippet"],
                    actions=action_positions(it.excerpt), authority=authority_for(ev_class, "snippet", t["url"]) * 0.5,
                    confidence=st.confidence * 0.6, cues="; ".join(st.cues + ["acquisition:snippet"]), raw_sha=None, text_sha=None)
                if new_id:
                    self.db.execute("UPDATE evidence_items SET acquisition='snippet' WHERE id=?", (new_id,))
                    n += 1
        self.db.conn.commit()
        return n

    # ------------------------------------------------------------------ text we hold
    def held_texts(self) -> list[tuple[str, str, str]]:
        """(acquisition, url, normalized text) for everything NSMPA holds about this entity in this run."""
        from .ai_review import _page_text
        out = []
        for p in self.db.execute("SELECT final_url, text_sha256, acquisition FROM research_pages WHERE run_id=? AND entity_id=? "
                                 "AND text_sha256 IS NOT NULL", (self.r.run_id, self.entity["id"])):
            txt = _page_text(self.db, p["text_sha256"])
            if txt:
                out.append((p["acquisition"] or "live", p["final_url"], normalize_for_hash(txt)))
        for t in self.db.execute("SELECT url, snippet FROM research_targets WHERE run_id=? AND entity_id=? AND snippet!=''",
                                 (self.r.run_id, self.entity["id"])):
            out.append(("snippet", t["url"], normalize_for_hash(t["snippet"])))
        for c in self.db.execute("SELECT url, text FROM captures WHERE entity_id=?", (self.entity["id"],)):
            out.append(("capture", c["url"], normalize_for_hash(c["text"])))
        return out

    def find_quote(self, quote_text: str) -> tuple[str, str] | None:
        """Where NSMPA itself holds this quote. Full-text sources must contain all of it; a snippet confirms only a
        12+ word stretch (snippets are truncated), and is reported as such."""
        q = normalize_for_hash(quote_text)
        if len(q) < 25:
            return None
        held = self.held_texts()
        for acq, url, text in held:
            if acq != "snippet" and q in text:
                return acq, url
        words = q.split()
        for i in range(0, max(1, len(words) - 12), 6):
            chunk = " ".join(words[i:i + 12])
            for acq, url, text in held:
                if acq == "snippet" and len(chunk) >= 40 and chunk in text:
                    return "snippet", url
        return None

    # ------------------------------------------------------------------ AI search
    async def ai_search(self) -> int:
        from . import ai_review
        from .ai_review import AIUnavailable, make_client
        from .utils import sha256_text
        from .research import is_first_party
        if self.r.ai_fallback_calls >= self.settings.blocked_fallback_ai_max_calls:
            self.notes.append("AI search skipped: per-run cap reached")
            return 0
        try:
            client = make_client(self.settings)
        except AIUnavailable as exc:
            self.notes.append(f"AI search unavailable: {exc}")
            return 0
        e = self.entity
        where = f" ({e['parent_name']})" if e["parent_name"] else ""
        user = (f"Organization: {e['name']}{where}\nWebsite: {e['homepage_url']}\nState: {e['state'] or 'unknown'}\n\n"
                "Find this organization's published policy or stated practice on unpublishing, removing, anonymizing or "
                "de-indexing previously published articles (or its refusal to). Quote it verbatim with the exact URL.")
        # Cache lookups/writes stay on this thread (SQLite connections are thread-bound); only the API call is offloaded.
        key = sha256_text(f"{self.settings.ai_provider}|{self.settings.ai_model}|{FIND_POLICY_PROMPT_VERSION}|{e['id']}|{e['homepage_url']}")
        row = self.db.execute("SELECT response_json FROM ai_cache WHERE cache_key=?", (key,)).fetchone()
        try:
            if row:
                data, was_cached = json.loads(row["response_json"]), True
            else:
                self.r.ai_fallback_calls += 1
                try:
                    data, meta = await asyncio.to_thread(ai_review.call_json, client, self.settings, user, FIND_POLICY_SYSTEM,
                                                         FIND_POLICY_SCHEMA, grounded=True)
                except AIUnavailable as exc:
                    if "RECITATION" not in str(exc):
                        raise
                    # Gemini will not reproduce long passages: ask for locations and a paraphrase instead.
                    self.r.ai_fallback_calls += 1
                    data, meta = await asyncio.to_thread(
                        ai_review.call_json, client, self.settings, user + "\n\nDo not quote. Give the exact URLs of "
                        "the policy pages and a one-sentence paraphrase; leave quotes empty.", FIND_POLICY_SYSTEM,
                        FIND_POLICY_SCHEMA, grounded=True)
                    data = {**data, "quotes": []}
                data = {**data, "_sources": meta.get("sources", []), "_search_queries": meta.get("search_queries", [])}
                self.db.execute("INSERT OR REPLACE INTO ai_cache(cache_key,model,prompt_version,response_json,input_tokens,"
                                "output_tokens,stop_reason) VALUES(?,?,?,?,?,?,?)",
                                (key, meta.get("model"), FIND_POLICY_PROMPT_VERSION, json.dumps(data), meta.get("input_tokens"),
                                 meta.get("output_tokens"), meta.get("stop_reason")))
                self.db.conn.commit()
                was_cached = False
        except Exception as exc:  # AIUnavailable, API errors: the fallback continues without AI
            self.notes.append(f"AI search failed: {type(exc).__name__}: {str(exc)[:120]}")
            self.r.dash.log(f"  AI search failed for {e['name']}: {exc}")
            return 0
        self.r.dash.update(phase="checking AI-reported quotes")
        # Claimed policy URLs on the organization's own site: read their archived copies (free).
        for url in list(dict.fromkeys([q.get("url", "") for q in data.get("quotes", [])] + data.get("policy_urls", []))):
            if url and is_first_party(e, url):
                await self.read_first_party(url, topic="ai_claimed_url")
        confirmed = 0
        phrase_left = self.settings.blocked_fallback_phrase_searches
        for q in data.get("quotes", [])[:6]:
            text, claimed = (q.get("quote") or "").strip(), (q.get("url") or "").strip()
            if not text:
                continue
            found = self.find_quote(text)
            if found and found[0] == "snippet":
                # A snippet only confirms part of the quote: try for the full page (archive drops the query string).
                bare = found[1].split("?", 1)[0]
                await self.read_first_party(bare, topic="ai_quote_page")
                found = self.find_quote(text) or found
            if not found and phrase_left > 0 and self.used < self.budget:
                phrase_left -= 1
                found = await self._phrase_search(text)
            status = "confirmed" if found else "unconfirmed"
            ev_id = self._evidence_for(text, found[1]) if found else None
            self.db.execute(
                "INSERT INTO ai_leads(run_id,entity_id,model,quote,claimed_url,summary,status,confirmed_via,confirmed_url,"
                "evidence_id,grounding_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (self.r.run_id, e["id"], self.settings.ai_model, text[:4000], claimed[:1000], (data.get("summary") or "")[:2000],
                 status, found[0] if found else None, found[1] if found else None, ev_id,
                 json.dumps({"sources": data.get("_sources", []), "queries": data.get("_search_queries", []),
                             "cached": was_cached})[:20000]))
            if found:
                confirmed += 1
                self.r.dash.add_recent(f"✓ AI lead confirmed ({found[0]})  {e['name']}: {text[:50]}")
        if not data.get("quotes"):
            self.db.execute("INSERT INTO ai_leads(run_id,entity_id,model,summary,status,grounding_json) VALUES(?,?,?,?,?,?)",
                            (self.r.run_id, e["id"], self.settings.ai_model, (data.get("summary") or "")[:2000], "no_policy_found",
                             json.dumps({"sources": data.get("_sources", []), "queries": data.get("_search_queries", [])})))
        self.db.conn.commit()
        return confirmed

    async def _phrase_search(self, text: str) -> tuple[str, str] | None:
        """One exact-phrase search on the organization's site to find the page an AI quote came from."""
        from .research import entity_site, is_first_party
        words = re.findall(r"[A-Za-z0-9’']+", text)
        if len(words) < 8:
            return None
        start = max(0, len(words) // 2 - 6)
        phrase = " ".join(words[start:start + 12])
        query = f'"{phrase}" site:{entity_site(self.entity)}'
        self.r.dash.update(phase="locating AI-reported quote")
        results, qid, _ = await self.r.broker.search(query, purpose="research:fallback:ai_quote", entity_id=int(self.entity["id"]))
        self.used += 1
        self.r._sync_search_counters()
        for res in results:
            if not is_first_party(self.entity, res.url):
                continue
            self.db.execute(
                "INSERT OR IGNORE INTO research_targets(run_id,entity_id,query_id,purpose,topic,url,domain,title,snippet,score,"
                "score_reasons_json,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.r.run_id, self.entity["id"], qid or None, "fallback", "ai_quote", res.url, registrableish_domain(res.url),
                 (res.title or "")[:500], (res.snippet or "")[:2000], 0.9, json.dumps(["ai_quote_phrase"]), "archive_fallback"))
            await self.read_first_party(res.url, topic="ai_quote_page")
        self.db.conn.commit()
        return self.find_quote(text)

    def _evidence_for(self, text: str, url: str) -> int | None:
        q = normalize_for_hash(text)[:80]
        for row in self.db.execute("SELECT id, excerpt FROM evidence_items WHERE run_id=? AND entity_id=? AND source_url=?",
                                   (self.r.run_id, self.entity["id"], url)):
            ex = normalize_for_hash(row["excerpt"])
            if ex and (ex[:60] in normalize_for_hash(text) or q[:60] in ex):
                return int(row["id"])
        return None

    # ------------------------------------------------------------------ driver
    async def run(self, ident) -> dict:
        from .research import GUIDANCE_COHORTS, QuerySpec, is_first_party, plan_queries
        e = self.entity
        dash = self.r.dash
        dash.update(phase="site blocks robots: reading archived copies")
        # 1. Identity from the archived homepage.
        home_page, _ = await self.read_archived(e["homepage_url"], topic="homepage", forced_kind="homepage")
        if home_page is None:
            self.notes.append("no archived copy of the homepage")
        else:
            arch_ident = check_identity(e, json.loads(e["metadata_json"] or "{}"), "ok", home_page)
            if arch_ident.status == "mismatch":
                return {"identity": "mismatch", "reason": f"archived homepage: {arch_ident.reason}"}
            for text, href in home_page.links:
                if len(self.archived) > self.settings.blocked_fallback_archive_pages:
                    break
                from .research import ABOUT_PAGE_RE, LEGAL_BOILERPLATE_RE, POLICY_PAGE_RE
                path = urlsplit(href).path
                if is_first_party(e, href) and not LEGAL_BOILERPLATE_RE.search(f"{text} {path}") and (
                        POLICY_PAGE_RE.search(f"{text} {path}") or ABOUT_PAGE_RE.search(path)):
                    await self.read_archived(href, topic="first_party_link")
        # 2. Search snippets (tier 1) -> leads + archive their pages.
        searching = self.r.broker.provider.name != "none"
        if searching and self.settings.blocked_fallback_snippets and self.used < self.budget:
            u, _ = await self.r._search_tier(e, plan_queries(self.settings, e, 1), self.budget - self.used)
            self.used += u
            self.store_snippets()
            targets = self.db.execute(
                "SELECT id, url FROM research_targets WHERE run_id=? AND entity_id=? AND status='candidate' ORDER BY score DESC, id",
                (self.r.run_id, e["id"])).fetchall()
            for t in targets:
                if not is_first_party(e, t["url"]):
                    continue
                if len(self.archived) <= self.settings.blocked_fallback_archive_pages:
                    await self.read_first_party(t["url"], topic="search_result")
                self.db.execute("UPDATE research_targets SET status='archive_fallback' WHERE id=?", (t["id"],))
        # Archived listing of top-level policy-like pages the searches may have missed.
        for url, ts in (await self.policy_paths())[: max(0, self.settings.blocked_fallback_archive_pages - len(self.archived) + 1)]:
            await self.read_archived(url, topic="archive_policy_path", ts=ts)
        # 3. AI search, every quote confirmed or left as a lead.
        if self.settings.blocked_fallback_ai and self.settings.ai_provider:
            await self.ai_search()
        # 4. Standards published elsewhere (third-party pages, read live under the usual mention rules).
        if (searching and self.settings.blocked_fallback_other_sources and e["cohort"] not in {"student_media"}
                and self.used < self.budget):
            spec = QuerySpec(2, "policy", "published_elsewhere",
                             '"{name}" ("standards and values" OR "ethics policy" OR handbook OR unpublishing OR '
                             '"corrections policy" OR "trust principles") -site:{site}', third_party=True)
            u, _ = await self.r._search_tier(e, [spec], self.budget - self.used)
            self.used += u
            await self.r._fetch_targets(e)
        self.db.conn.commit()
        held = self.db.scalar("SELECT COUNT(*) FROM research_pages WHERE run_id=? AND entity_id=? AND acquisition='archive' "
                              "AND access_class='ok'", (self.r.run_id, e["id"]))
        return {"identity": "archive" if home_page is not None else "unverified", "archived_pages": held,
                "searches": self.used, "notes": self.notes, "guidance": e["cohort"] in GUIDANCE_COHORTS}

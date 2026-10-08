"""AI second opinion on fetched pages. Off unless you run `nsmpa ai-review`.

Providers: Google Gemini (default, ``ai_model: gemini-3.8-flash``, via ``google-genai`` ``generate_content``, which
is stateless: unlike the Interactions API it does not store each request server-side by default) or Anthropic
Claude (``ai_provider: anthropic``).

Guardrails:
- The rules-based classifier stays the baseline. AI output lives in ``ai_reviews`` / ``ai_findings``
  and never changes a stance; it only adds review-queue items and comparison columns.
- Every quote the model returns is checked verbatim (normalized whitespace/punctuation/case) against the
  saved page text. Quotes that are not found are stored with ``quote_verified=0`` and never used.
- The prompt is neutral: it asks what the page says, in both directions. It never mentions the
  requester's goal or case.
- Responses are cached by (model, prompt version, page text hash), so reruns are free and reproducible;
  model id and prompt version are recorded on every review.
- Hard per-run call cap (``ai_max_calls_per_run`` / ``--max-calls``).
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .progress import RunDashboard
from .runs import StopController, create_or_resume_run, done_keys, finish_run, mark_item, register_items
from .search import _redact
from .utils import normalize_for_hash, sha256_text

PROMPT_VERSION = "ai-review-v1"

SYSTEM = """You analyze web pages for a research database about how news organizations handle requests to remove,
de-index, anonymize, or update previously published articles (for example, old crime or arrest stories).

Report what the page actually says, with equal care for statements that permit such changes and statements that
refuse them. Do not infer positions the text does not state. Quote exactly, character for character, from the page
text provided; never paraphrase inside "quote". If the page contains nothing relevant, return an empty findings list.

Definitions:
- kind: "policy" (the organization's own rule or standard), "practice" (something a newsroom actually did),
  "guidance" (advice to journalists or newsrooms), "opinion" (a named person's view), "other".
- action: "unpublish" (delete/take down the article), "deindex" (keep it but hide it from search engines),
  "anonymize" (remove or replace a person's name), "update" (editor's note, correction, follow-up, outcome added),
  "none".
- position: "permitted", "conditional" (only in some circumstances), "rejected", "practiced" (it was done),
  "denied" (a request was refused), "mentioned" (discussed without a position).
- direction: "supportive" if it favors making the change, "adverse" if it opposes it, "neutral" otherwise."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "quote": {"type": "string"},
                    "kind": {"type": "string", "enum": ["policy", "practice", "guidance", "opinion", "other"]},
                    "action": {"type": "string", "enum": ["unpublish", "deindex", "anonymize", "update", "none"]},
                    "position": {"type": "string", "enum": ["permitted", "conditional", "rejected", "practiced", "denied", "mentioned"]},
                    "direction": {"type": "string", "enum": ["supportive", "adverse", "neutral"]},
                    "conditions": {"type": "string"},
                    "speaker": {"type": "string"},
                    "speaker_role": {"type": "string"},
                },
                "required": ["quote", "kind", "action", "position", "direction", "conditions", "speaker", "speaker_role"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["relevant", "summary", "findings"],
    "additionalProperties": False,
}


class AIUnavailable(RuntimeError):
    pass


GEMINI_BLOCK_REASONS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION", "IMAGE_SAFETY", "OTHER", "LANGUAGE"}
RETRYABLE_CODES = {429, 500, 502, 503, 504}


def make_client(settings: Settings):
    if settings.ai_provider == "gemini":
        if not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
            raise AIUnavailable("Set GEMINI_API_KEY (or GOOGLE_API_KEY) in your environment or ./.env to use Gemini")
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover
            raise AIUnavailable("The 'google-genai' package is not installed (pip install -e '.[ai]')") from exc
        return genai.Client()  # reads GEMINI_API_KEY / GOOGLE_API_KEY
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise AIUnavailable("The 'anthropic' package is not installed (pip install anthropic)") from exc
    return anthropic.Anthropic()  # resolves ANTHROPIC_API_KEY / ant auth profile


def _user_text(page_text: str, entity_name: str) -> str:
    return f"Organization: {entity_name}\n\nPage text:\n<page>\n{page_text}\n</page>"


def _call_gemini(client, settings: Settings, user_text: str, system: str, schema: dict, *,
                 grounded: bool = False) -> tuple[dict, dict]:
    from google.genai import types
    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_json_schema=schema,
        max_output_tokens=16000,
        tools=[types.Tool(google_search=types.GoogleSearch())] if grounded else None,
    )
    last: Exception | None = None
    for attempt in range(settings.ai_max_retries + 1):
        try:
            response = client.models.generate_content(model=settings.ai_model, contents=user_text, config=config)
            break
        except Exception as exc:  # google.genai.errors.APIError carries .code
            last = exc
            if getattr(exc, "code", None) in RETRYABLE_CODES and attempt < settings.ai_max_retries:
                time.sleep(min(30.0, 2.0 * (2 ** attempt)) * (0.75 + random.random() * 0.5))
                continue
            raise
    else:  # pragma: no cover
        raise last  # type: ignore[misc]
    fb = getattr(response, "prompt_feedback", None)
    if fb is not None and getattr(fb, "block_reason", None):
        raise AIUnavailable(f"Gemini blocked this page ({getattr(fb.block_reason, 'name', fb.block_reason)})")
    cands = getattr(response, "candidates", None) or []
    finish = getattr(getattr(cands[0], "finish_reason", None), "name", None) if cands else None
    usage = getattr(response, "usage_metadata", None)
    meta = {"stop_reason": finish, "model": settings.ai_model,
            "input_tokens": getattr(usage, "prompt_token_count", None),
            "output_tokens": getattr(usage, "candidates_token_count", None)}
    if finish == "MAX_TOKENS":
        raise AIUnavailable("response truncated at max_output_tokens")
    if finish in GEMINI_BLOCK_REASONS:
        raise AIUnavailable(f"Gemini declined this page (finish_reason={finish})")
    text = response.text or ""
    if not text.strip():
        raise AIUnavailable("Gemini returned an empty response")
    if grounded:
        gm = getattr(cands[0], "grounding_metadata", None) if cands else None
        meta["sources"] = [{"title": getattr(c.web, "title", None), "uri": getattr(c.web, "uri", None)}
                           for c in (getattr(gm, "grounding_chunks", None) or []) if getattr(c, "web", None)]
        meta["search_queries"] = list(getattr(gm, "web_search_queries", None) or [])
    return json.loads(text), meta


def _call_anthropic(client, settings: Settings, user_text: str, system: str, schema: dict, *,
                    grounded: bool = False) -> tuple[dict, dict]:
    kwargs: dict[str, Any] = dict(
        model=settings.ai_model,
        max_tokens=16000,
        system=system,
        output_config={"effort": settings.ai_effort, "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": user_text}],
    )
    if grounded:
        kwargs["tools"] = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}]
    if settings.ai_refusal_fallback:
        response = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    else:
        response = client.messages.create(**kwargs)
    meta = {"stop_reason": response.stop_reason, "model": getattr(response, "model", settings.ai_model),
            "input_tokens": getattr(response.usage, "input_tokens", None),
            "output_tokens": getattr(response.usage, "output_tokens", None)}
    if response.stop_reason == "refusal":
        raise AIUnavailable("model declined this page (refusal)")
    if response.stop_reason == "max_tokens":
        raise AIUnavailable("response truncated at max_tokens")
    if grounded:
        meta["sources"] = [{"title": getattr(r, "title", None), "uri": getattr(r, "url", None)}
                           for b in response.content if getattr(b, "type", "") == "web_search_tool_result"
                           for r in (getattr(b, "content", None) or []) if hasattr(r, "url")]
    texts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
    return json.loads(texts[-1] if texts else ""), meta


def call_json(client, settings: Settings, user_text: str, system: str, schema: dict, *,
              grounded: bool = False) -> tuple[dict, dict]:
    """Provider-neutral structured-output request used by every AI feature.

    ``grounded=True`` lets the model search the web (Gemini: Google Search grounding; Claude: the web search tool).
    Grounded answers are leads only: callers must confirm every quote against text NSMPA itself holds.
    """
    if settings.ai_provider == "gemini":
        return _call_gemini(client, settings, user_text, system, schema, grounded=grounded)
    return _call_anthropic(client, settings, user_text, system, schema, grounded=grounded)


def call_model(client, settings: Settings, page_text: str, entity_name: str) -> tuple[dict, dict]:
    """Page review request. Returns (parsed JSON, usage/meta). Raises AIUnavailable on blocks/refusals."""
    return call_json(client, settings, _user_text(page_text, entity_name), SYSTEM, SCHEMA)


def ai_cache_key(settings: Settings, prompt_version: str, payload_key: str) -> str:
    return sha256_text(f"{settings.ai_provider}|{settings.ai_model}|{prompt_version}|{payload_key}")


def ai_cache_get(db: Database, key: str) -> dict | None:
    row = db.execute("SELECT response_json FROM ai_cache WHERE cache_key=?", (key,)).fetchone()
    return json.loads(row["response_json"]) if row else None


def ai_cache_put(db: Database, key: str, prompt_version: str, data: dict, meta: dict) -> None:
    db.execute("INSERT OR REPLACE INTO ai_cache(cache_key,model,prompt_version,response_json,input_tokens,output_tokens,stop_reason) "
               "VALUES(?,?,?,?,?,?,?)", (key, meta.get("model") or "unknown", prompt_version, json.dumps(data), meta.get("input_tokens"),
                                         meta.get("output_tokens"), meta.get("stop_reason")))
    db.conn.commit()


def with_grounding(data: dict, meta: dict) -> dict:
    """Keep a grounded answer's search sources and queries alongside it (for the audit trail)."""
    return {**data, "_sources": meta.get("sources", []), "_search_queries": meta.get("search_queries", [])}


def cached_call(db: Database, client, settings: Settings, prompt_version: str, payload_key: str, user_text: str,
                system: str, schema: dict, *, grounded: bool = False) -> tuple[dict, bool]:
    """call_json with the shared ai_cache (keyed by provider, model, prompt version and payload)."""
    key = ai_cache_key(settings, prompt_version, payload_key)
    if (hit := ai_cache_get(db, key)) is not None:
        return hit, True
    data, meta = call_json(client, settings, user_text, system, schema, grounded=grounded)
    if grounded:
        data = with_grounding(data, meta)
    ai_cache_put(db, key, prompt_version, data, meta)
    return data, False


def verify_quote(quote: str, page_text_norm: str) -> bool:
    q = normalize_for_hash(quote)
    return len(q) >= 12 and q in page_text_norm


def _match_evidence(db: Database, run_id: str, page_id: int, quote: str):
    qn = normalize_for_hash(quote)
    best = None
    for e in db.execute("SELECT id, excerpt, actions_json, direction FROM evidence_items WHERE run_id=? AND page_id=?", (run_id, page_id)):
        en = normalize_for_hash(e["excerpt"])
        if qn and (qn in en or en in qn):
            best = e
            break
    return best


def _agreement(finding: dict, ev) -> str:
    if ev is None:
        return "ai_only"
    try:
        acts = json.loads(ev["actions_json"] or "{}")
    except ValueError:
        acts = {}
    a, pos = finding["action"], finding["position"]
    if a != "none" and a in acts:
        rule = acts[a]
        same = (rule == pos) or ({rule, pos} <= {"permitted", "practiced"}) or ({rule, pos} <= {"conditional", "permitted"})
        return "agree" if same else "disagree"
    return "agree" if ev["direction"] == finding["direction"] else "disagree"


def candidate_pages(db: Database, run_id: str | None, cohort: str | None, limit: int | None) -> list:
    """Pages worth a second opinion: fetched pages with substantive rule-based evidence, policy/about pages,
    and archived crime articles (where practice notes hide)."""
    sql = """SELECT p.*, re.name AS entity_name, re.cohort FROM research_pages p JOIN research_entities re ON re.id=p.entity_id
             WHERE p.access_class='ok' AND p.text_sha256 IS NOT NULL
               AND p.run_id NOT IN (SELECT id FROM v_excluded_runs)
               AND (p.page_kind IN ('policy','about','crime_article')
                    OR EXISTS (SELECT 1 FROM evidence_items e WHERE e.page_id=p.id AND e.statement_type NOT IN ('mention')))"""
    params: list = []
    if run_id:
        sql += " AND p.run_id=?"
        params.append(run_id)
    if cohort:
        sql += " AND re.cohort=?"
        params.append(cohort)
    sql += " ORDER BY p.id"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return db.execute(sql, params).fetchall()


def _page_text(db: Database, text_sha: str) -> str | None:
    path = db.scalar("SELECT path FROM snapshots WHERE sha256=? AND kind='text'", (text_sha,), None)
    if not path or not Path(path).exists():
        return None
    return Path(path).read_text(encoding="utf-8", errors="replace")


async def run_ai_review(db: Database, settings: Settings, *, run_id: str | None = None, cohort: str | None = None,
                        max_calls: int | None = None, limit: int | None = None, quiet: bool = False, verbose: bool = False,
                        client=None, review_run_id: str | None = None, stop: StopController | None = None,
                        command: str | None = None) -> dict:
    pages = candidate_pages(db, run_id, cohort, limit)
    rid, _ = create_or_resume_run(db, settings, "ai_review", review_run_id,
                                  params={"scope_run": run_id, "cohort": cohort, "provider": settings.ai_provider, "model": settings.ai_model,
                                          "prompt_version": PROMPT_VERSION}, command=command)
    register_items(db, rid, "page", [str(p["id"]) for p in pages])
    done = done_keys(db, rid, "page")
    todo = [p for p in pages if str(p["id"]) not in done]
    cap = min(max_calls if max_calls is not None else settings.ai_max_calls_per_run, settings.ai_max_calls_per_run)
    stop = stop or StopController()
    stats = {"pages": len(pages), "reviewed": 0, "cached": 0, "live_calls": 0, "failed": 0,
             "findings": 0, "verified": 0, "unverified_discarded": 0, "disagreements": 0, "ai_only": 0}
    status, reason = "completed", None
    dash = RunDashboard("NSMPA AI second opinion", len(pages), quiet=quiet, verbose=verbose,
                        universe=f"{settings.ai_provider}:{settings.ai_model} · {PROMPT_VERSION}", db=db, run_id=rid,
                        persist_seconds=settings.heartbeat_seconds)
    if client is None and todo:
        client = make_client(settings)
    sem = asyncio.Semaphore(settings.ai_concurrency)
    with dash:
        dash.update(completed=len(done), skipped_done=len(done))
        stop.on_stop(dash.notice)
        uninstall = stop.install()

        async def one(p) -> None:
            nonlocal status, reason
            if stop.stop_requested:
                return
            key = str(p["id"])
            text = _page_text(db, p["text_sha256"])
            if not text:
                mark_item(db, rid, "page", key, "skipped", error="text snapshot missing")
                dash.increment(completed=1)
                return
            text = text[: settings.ai_max_page_chars]
            cache_key = sha256_text(f"{settings.ai_provider}|{settings.ai_model}|{PROMPT_VERSION}|{p['text_sha256']}|{settings.ai_max_page_chars}")
            cached = db.execute("SELECT * FROM ai_cache WHERE cache_key=?", (cache_key,)).fetchone()
            mark_item(db, rid, "page", key, "running")
            dash.update(current=f"{p['entity_name']}: {(p['title'] or p['final_url'] or '')[:60]}", phase="AI review")
            try:
                if cached:
                    data = json.loads(cached["response_json"])
                    stats["cached"] += 1
                else:
                    if stats["live_calls"] >= cap:
                        status, reason = "budget_exhausted", f"AI call cap {cap} reached"
                        stop.stop_requested = True
                        mark_item(db, rid, "page", key, "pending", error=reason)
                        return
                    stats["live_calls"] += 1
                    async with sem:
                        data, meta = await asyncio.to_thread(call_model, client, settings, text, p["entity_name"])
                    db.execute("INSERT OR REPLACE INTO ai_cache(cache_key,model,prompt_version,response_json,input_tokens,output_tokens,stop_reason) "
                               "VALUES(?,?,?,?,?,?,?)", (cache_key, meta["model"], PROMPT_VERSION, json.dumps(data),
                                                         meta["input_tokens"], meta["output_tokens"], meta["stop_reason"]))
                norm = normalize_for_hash(text)
                db.execute(
                    """INSERT INTO ai_reviews(run_id,entity_id,page_id,model,prompt_version,cache_key,was_cached,status,summary)
                       VALUES(?,?,?,?,?,?,?,'ok',?)
                       ON CONFLICT(run_id,page_id,prompt_version,model) DO UPDATE SET status='ok',summary=excluded.summary,
                         was_cached=excluded.was_cached""",
                    (rid, p["entity_id"], p["id"], settings.ai_model, PROMPT_VERSION, cache_key, int(bool(cached)), data.get("summary", "")[:2000]))
                review_id = db.scalar("SELECT id FROM ai_reviews WHERE run_id=? AND page_id=? AND prompt_version=? AND model=?",
                                      (rid, p["id"], PROMPT_VERSION, settings.ai_model))
                db.execute("DELETE FROM ai_findings WHERE review_id=?", (review_id,))
                total = verified = 0
                for f in data.get("findings", []):
                    total += 1
                    ok = verify_quote(f.get("quote", ""), norm)
                    ev = _match_evidence(db, p["run_id"], p["id"], f["quote"]) if ok else None
                    agreement = _agreement(f, ev) if ok else "unverified_quote"
                    verified += int(ok)
                    stats["unverified_discarded"] += int(not ok)
                    stats["disagreements"] += int(agreement == "disagree")
                    stats["ai_only"] += int(agreement == "ai_only" and f.get("direction") != "neutral")
                    db.execute(
                        """INSERT INTO ai_findings(review_id,entity_id,page_id,quote,quote_verified,kind,action,position,direction,
                             conditions,speaker,speaker_role,matched_evidence_id,agreement) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (review_id, p["entity_id"], p["id"], f["quote"][:2000], int(ok), f.get("kind"), f.get("action"),
                         f.get("position"), f.get("direction"), f.get("conditions") or None, f.get("speaker") or None,
                         f.get("speaker_role") or None, ev["id"] if ev else None, agreement))
                db.execute("UPDATE ai_reviews SET findings_total=?, findings_verified=? WHERE id=?", (total, verified, review_id))
                stats["voices"] = stats.get("voices", 0) + _voices_from_findings(db, review_id, p, settings)
                stats["findings"] += total
                stats["verified"] += verified
                stats["reviewed"] += 1
                _flag_for_review(db, rid, p)
                mark_item(db, rid, "page", key, "done")
                db.conn.commit()
            except Exception as exc:  # one bad page never stops the run; auth errors stop it
                stats["failed"] += 1
                err = _redact(f"{type(exc).__name__}: {exc}")[:500]
                mark_item(db, rid, "page", key, "failed", error=err)
                db.execute("""INSERT INTO ai_reviews(run_id,entity_id,page_id,model,prompt_version,cache_key,status,error)
                              VALUES(?,?,?,?,?,?,'failed',?) ON CONFLICT(run_id,page_id,prompt_version,model)
                              DO UPDATE SET status='failed',error=excluded.error""",
                           (rid, p["entity_id"], p["id"], settings.ai_model, PROMPT_VERSION, cache_key, err))
                db.conn.commit()
                dash.increment(errors=1)
                if type(exc).__name__ in {"AuthenticationError", "PermissionDeniedError"} or getattr(exc, "code", None) in (401, 403):
                    status, reason = "failed", f"AI auth error: {err}"
                    stop.stop_requested = True
            finally:
                dash.increment(completed=1)
                dash.update(evidence_unique=stats["verified"], searches_live=stats["live_calls"], searches_cached=stats["cached"])
                dash.checkpoint()

        try:
            for i in range(0, len(todo), settings.ai_concurrency):
                if stop.stop_requested:
                    break
                await asyncio.gather(*(one(p) for p in todo[i:i + settings.ai_concurrency]))
        finally:
            uninstall()
    if stop.stop_requested and status == "completed":
        status, reason = "interrupted", stop.reason
    finish_run(db, rid, status, reason)
    stats.update(run_id=rid, status=status, stop_reason=reason, provider=settings.ai_provider, model=settings.ai_model,
                 prompt_version=PROMPT_VERSION,
                 remaining=len(pages) - len(done_keys(db, rid, "page")))
    return stats


def _voices_from_findings(db: Database, review_id: int, page, settings: Settings) -> int:
    """Verified AI findings with a named speaker become Expert Voices (attribution_method='ai:<model>').

    Only quotes found verbatim in the page qualify. If the same quote was already captured by the pattern
    extractor, the existing voice is kept and only a missing role is filled in.
    """
    from .evidence import classify_statement, person_key
    n = 0
    for f in db.execute("SELECT * FROM ai_findings WHERE review_id=? AND quote_verified=1 AND speaker IS NOT NULL AND speaker!=''",
                        (review_id,)):
        name = f["speaker"].strip()
        if len(name.split()) < 2 or len(name) > 80:
            continue  # need a full name to attribute responsibly
        key = person_key(name)
        expert = db.execute("SELECT id, role, affiliation FROM experts WHERE person_key=?", (key,)).fetchone()
        st = classify_statement(f["quote"])
        actions = {f["action"]: f["position"]} if f["action"] and f["action"] != "none" else {}
        qsha = sha256_text(normalize_for_hash(f["quote"]))
        cur = db.execute(
            """INSERT OR IGNORE INTO voices(person_key,person_name,role,affiliation,expert_id,quote,quote_sha256,context,source_url,
                 source_title,source_domain,page_id,run_id,entity_id,statement_type,direction,actions_json,attribution_method,
                 attribution_confidence,case_match_score)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            (key, name, f["speaker_role"] or (expert["role"] if expert else None), expert["affiliation"] if expert else None,
             expert["id"] if expert else None, f["quote"], qsha, f["conditions"], page["final_url"] or page["requested_url"],
             page["title"], None, page["id"], page["run_id"], page["entity_id"], st.statement_type, f["direction"],
             json.dumps(actions), f"ai:{settings.ai_model}", 0.75 + (0.1 if expert else 0.0)))
        if cur.rowcount:
            n += 1
        elif f["speaker_role"]:
            db.execute("UPDATE voices SET role=COALESCE(role, ?) WHERE person_key=? AND quote_sha256=?", (f["speaker_role"], key, qsha))
    return n


def _flag_for_review(db: Database, ai_run_id: str, page) -> None:
    """Disagreements and AI-only directional findings send the entity's current stance to human review."""
    n = db.scalar("""SELECT COUNT(*) FROM ai_findings f JOIN ai_reviews r ON r.id=f.review_id
                     WHERE r.page_id=? AND r.run_id=? AND f.quote_verified=1
                       AND (f.agreement='disagree' OR (f.agreement='ai_only' AND f.direction!='neutral'))""", (page["id"], ai_run_id))
    if not n:
        return
    stance = db.execute("SELECT id FROM entity_stances WHERE entity_id=? AND run_id=?", (page["entity_id"], page["run_id"])).fetchone()
    if not stance:
        return
    row = db.execute("SELECT id, reasons_json, priority FROM review_queue WHERE run_id=? AND item_type='entity_stance' AND item_id=?",
                     (page["run_id"], stance["id"])).fetchone()
    if row:
        reasons = sorted(set(json.loads(row["reasons_json"] or "[]")) | {"ai_rule_disagreement_or_additional_finding"})
        db.execute("UPDATE review_queue SET reasons_json=?, priority=priority+CASE WHEN reasons_json LIKE '%ai_rule%' THEN 0 ELSE 15 END WHERE id=?",
                   (json.dumps(reasons), row["id"]))
    else:
        db.execute("INSERT INTO review_queue(run_id,item_type,item_id,entity_id,cohort,priority,reasons_json) VALUES(?,?,?,?,?,?,?)",
                   (page["run_id"], "entity_stance", stance["id"], page["entity_id"], page["cohort"], 15.0,
                    json.dumps(["ai_rule_disagreement_or_additional_finding"])))

"""AI second opinion on fetched pages (Claude API). Off unless you run `nsmpa ai-review`.

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
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .progress import RunDashboard
from .runs import StopController, create_or_resume_run, done_keys, finish_run, mark_item, register_items
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


def make_client():
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise AIUnavailable("The 'anthropic' package is not installed (pip install anthropic)") from exc
    return anthropic.Anthropic()  # resolves ANTHROPIC_API_KEY / ant auth profile


def call_model(client, settings: Settings, page_text: str, entity_name: str) -> tuple[dict, dict]:
    """One structured-output request. Returns (parsed JSON, usage/meta). Raises on refusal or API error."""
    kwargs: dict[str, Any] = dict(
        model=settings.ai_model,
        max_tokens=16000,
        system=SYSTEM,
        output_config={"effort": settings.ai_effort, "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": f"Organization: {entity_name}\n\nPage text:\n<page>\n{page_text}\n</page>"}],
    )
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
    text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), "")
    return json.loads(text), meta


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
               AND p.run_id NOT IN (SELECT id FROM research_runs WHERE status='excluded')
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
                                  params={"scope_run": run_id, "cohort": cohort, "model": settings.ai_model,
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
                        universe=f"{settings.ai_model} · {PROMPT_VERSION}")
    if client is None and todo:
        client = make_client()
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
            cache_key = sha256_text(f"{settings.ai_model}|{PROMPT_VERSION}|{p['text_sha256']}|{settings.ai_max_page_chars}")
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
                stats["findings"] += total
                stats["verified"] += verified
                stats["reviewed"] += 1
                _flag_for_review(db, rid, p)
                mark_item(db, rid, "page", key, "done")
                db.conn.commit()
            except Exception as exc:  # one bad page never stops the run; auth errors stop it
                stats["failed"] += 1
                mark_item(db, rid, "page", key, "failed", error=f"{type(exc).__name__}: {exc}"[:500])
                db.execute("""INSERT INTO ai_reviews(run_id,entity_id,page_id,model,prompt_version,cache_key,status,error)
                              VALUES(?,?,?,?,?,?,'failed',?) ON CONFLICT(run_id,page_id,prompt_version,model)
                              DO UPDATE SET status='failed',error=excluded.error""",
                           (rid, p["entity_id"], p["id"], settings.ai_model, PROMPT_VERSION, cache_key, f"{type(exc).__name__}: {exc}"[:500]))
                db.conn.commit()
                dash.increment(errors=1)
                if type(exc).__name__ in {"AuthenticationError", "PermissionDeniedError"}:
                    status, reason = "failed", f"AI auth error: {exc}"
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
    stats.update(run_id=rid, status=status, stop_reason=reason, model=settings.ai_model, prompt_version=PROMPT_VERSION,
                 remaining=len(pages) - len(done_keys(db, rid, "page")))
    return stats


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

"""AI veto on the excerpts that decide an organization's position.

Rule-based extraction is good at *finding* candidate sentences and poor at the long tail of what a sentence is about:
a statue "taken down", a police dog's "takedown", a copyright notice, a ballot "removal", a letters-to-the-editor
rule. Before a determinate stance is accepted, the excerpts that drive it are shown to an AI model with one question:

    Is this the organization's own policy, practice or professional guidance about removing, unpublishing,
    de-indexing, anonymizing or updating its previously published journalism (or about refusing to)?

The AI can only **veto**: a rejected excerpt stops counting toward the stance (it is kept, labelled
``ai_gate='rejected'`` with the model's reason), and the stance is recomputed. The AI never adds evidence, never
changes a direction and never upgrades a stance. Verdicts are cached and recorded in ``ai_gate_checks``.
"""
from __future__ import annotations

import asyncio

from .config import Settings
from .db import Database

PROMPT_VERSION = "ai_gate_v1"
DECISIVE_TYPES = ("relief_permitted", "changed_circumstance_relief", "case_by_case", "relief_rejected",
                  "relief_narrow_exceptions", "archive_principle", "update_remedy", "changed_circumstance_update",
                  "practice_relief_granted", "practice_relief_denied")
SYSTEM = (
    "You verify evidence for a research database about how U.S. news organizations handle requests to unpublish, "
    "remove, de-index or anonymize their previously published journalism, or to update it instead.\n"
    "Accept the excerpt ONLY if it states or documents the named organization's OWN policy, practice or (for "
    "journalism standards organizations) professional guidance about removing, unpublishing, de-indexing, anonymizing "
    "or updating previously published news content (stories, articles, names in stories, archives, search visibility), "
    "or about refusing to do so.\n"
    "Reject it if it is about anything else, including: removing people from jobs or memberships; physical objects "
    "(statues, signs); documents, records or data held by governments or courts; copyright or legal takedown notices; "
    "reader comments, letters-to-the-editor or submission moderation; correcting errors with no bearing on removal; a "
    "news report about some other person's or organization's removal decision; navigation text, captions, bylines, "
    "headlines or lists; or text too fragmentary to show a position.\n"
    "Judge only the excerpt and its context. Do not use outside knowledge about the organization.")
SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["accept", "reject"]},
        "direction": {"type": "string", "enum": ["supportive", "adverse", "neutral"],
                      "description": "supportive = relief possible; adverse = relief refused; neutral = neither"},
        "reason": {"type": "string", "description": "One short sentence."},
    },
    "required": ["verdict", "direction", "reason"],
}


def _user_text(entity, row) -> str:
    return (f"Organization: {entity['name']} ({entity['cohort'].replace('_', ' ')})\n"
            f"Page: {row['page_title'] or ''} <{row['source_url']}>\n"
            f"Excerpt: {row['excerpt']}\n"
            f"Context: {(row['context'] or '')[:1200]}")


def _candidates(db: Database, run_id: str, entity_id: int, limit: int) -> list:
    """Unchecked decisive excerpts, the ones that weigh most first (same ordering the stance uses for 'strongest')."""
    marks = ",".join("?" * len(DECISIVE_TYPES))
    return db.execute(
        f"""SELECT e.*, p.title AS page_title FROM evidence_items e LEFT JOIN research_pages p ON p.id=e.page_id
            WHERE e.run_id=? AND e.entity_id=? AND e.about_entity=1 AND e.duplicate_of IS NULL
              AND e.ai_gate IS NULL AND COALESCE(e.acquisition,'live')!='snippet' AND e.evidence_class!='technical'
              AND e.statement_type IN ({marks})
            ORDER BY COALESCE(e.relevance_score,0)*COALESCE(e.authority_score,0) DESC, e.id LIMIT ?""",
        (run_id, entity_id, *DECISIVE_TYPES, limit)).fetchall()


class Gate:
    """Holds the AI client, the call budget and the cache; ``check_entity`` runs the veto loop for one organization."""

    def __init__(self, db: Database, settings: Settings, *, client=None, max_calls: int | None = None):
        self.db, self.settings = db, settings
        self.client = client
        self.calls = 0
        self.max_calls = max_calls if max_calls is not None else settings.ai_gate_max_calls_per_run
        self.unavailable: str | None = None

    def _client(self):
        if self.client is None and self.unavailable is None:
            from .ai_review import AIUnavailable, make_client
            try:
                self.client = make_client(self.settings)
            except AIUnavailable as exc:
                self.unavailable = str(exc)
        return self.client

    async def _verdict(self, entity, row) -> dict | None:
        from . import ai_review
        key = ai_review.ai_cache_key(self.settings, PROMPT_VERSION, f"{entity['id']}|{row['excerpt_sha256']}|{row['context']}")
        hit = ai_review.ai_cache_get(self.db, key)
        if hit is not None:
            return {**hit, "_cached": True}
        if self.calls >= self.max_calls or self._client() is None:
            return None
        self.calls += 1
        try:
            data, meta = await asyncio.to_thread(ai_review.call_json, self.client, self.settings, _user_text(entity, row),
                                                 SYSTEM, SCHEMA)
        except Exception as exc:  # an unavailable or refusing model leaves the excerpt unchecked, never rejected
            self.db.execute("INSERT INTO ai_gate_checks(evidence_id,run_id,entity_id,model,verdict,reason) VALUES(?,?,?,?,?,?)",
                            (row["id"], row["run_id"], entity["id"], self.settings.ai_model, "error",
                             f"{type(exc).__name__}: {str(exc)[:200]}"))
            return None
        ai_review.ai_cache_put(self.db, key, PROMPT_VERSION, data, meta)
        return {**data, "_cached": False}

    async def check_entity(self, run_id: str, entity) -> dict:
        """Veto loop: check the decisive excerpts, re-decide, repeat while new excerpts become decisive."""
        from .stance import DETERMINATE, classify_entity
        out = {"checked": 0, "rejected": 0, "rounds": 0}
        result = classify_entity(self.db, self.settings, run_id, entity)
        for _ in range(self.settings.ai_gate_max_rounds):
            if result.stance not in DETERMINATE:
                break
            rows = _candidates(self.db, run_id, int(entity["id"]), self.settings.ai_gate_items_per_round)
            if not rows:
                break
            out["rounds"] += 1
            rejected_now = 0
            for row in rows:
                v = await self._verdict(entity, row)
                if v is None:
                    continue
                verdict = "rejected" if v.get("verdict") == "reject" else "accepted"
                self.db.execute("UPDATE evidence_items SET ai_gate=?, ai_gate_reason=? WHERE id=?",
                                (verdict, (v.get("reason") or "")[:500], row["id"]))
                self.db.execute(
                    "INSERT INTO ai_gate_checks(evidence_id,run_id,entity_id,model,verdict,ai_direction,reason,cached) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (row["id"], run_id, entity["id"], self.settings.ai_model, verdict, v.get("direction"),
                     (v.get("reason") or "")[:500], int(bool(v.get("_cached")))))
                out["checked"] += 1
                if verdict == "rejected":
                    out["rejected"] += 1
                    rejected_now += 1
            self.db.conn.commit()
            if not rejected_now:
                break
            result = classify_entity(self.db, self.settings, run_id, entity)
        out["stance"] = result.stance
        out["result"] = result
        return out


async def gate_run(db: Database, settings: Settings, run_id: str, *, max_calls: int | None = None, client=None,
                   entity_ids: list[int] | None = None) -> dict:
    """Apply the veto to every organization with a determinate stance in ``run_id``; store the recomputed stances."""
    from .review import enqueue_entity_review
    from .stance import DETERMINATE, store_stance
    gate = Gate(db, settings, client=client, max_calls=max_calls)
    rows = db.execute("SELECT entity_id, stance FROM entity_stances WHERE run_id=?", (run_id,)).fetchall()
    summary = {"run_id": run_id, "organizations": 0, "checked": 0, "rejected": 0, "changed": [], "ai_calls": 0}
    for r in rows:
        if r["stance"] not in DETERMINATE or (entity_ids and r["entity_id"] not in entity_ids):
            continue
        entity = db.execute("SELECT * FROM research_entities WHERE id=?", (r["entity_id"],)).fetchone()
        res = await gate.check_entity(run_id, entity)
        summary["organizations"] += 1
        summary["checked"] += res["checked"]
        summary["rejected"] += res["rejected"]
        if res["rejected"]:
            store_stance(db, run_id, int(entity["id"]), res["result"])
            enqueue_entity_review(db, run_id, entity, res["result"])
        if res["stance"] != r["stance"]:
            summary["changed"].append({"entity": entity["name"], "from": r["stance"], "to": res["stance"]})
        db.conn.commit()
        if gate.unavailable:
            summary["unavailable"] = gate.unavailable
            break
    summary["ai_calls"] = gate.calls
    return summary


def gate_report(db: Database, run_id: str) -> list[dict]:
    return [dict(r) for r in db.execute(
        """SELECT re.name AS entity, e.statement_type, e.direction, e.ai_gate, e.ai_gate_reason, substr(e.excerpt,1,300) AS excerpt,
                  e.source_url FROM evidence_items e JOIN research_entities re ON re.id=e.entity_id
           WHERE e.run_id=? AND e.ai_gate IS NOT NULL ORDER BY re.name, e.ai_gate""", (run_id,))]


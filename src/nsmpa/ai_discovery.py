"""AI help identifying each institution's student newspaper (`nsmpa ai-discovery`).

Only institutions whose discovery is uncertain are sent: ambiguous between two domains, or best candidate
between (threshold - 0.15) and the early-stop threshold. The model sees the numbered candidates the
tool already found (URL, title, snippet, verification signals) and answers with a candidate *number* or 0
for "none of these" — it cannot introduce a URL. Answers outside the list are rejected.

The pick is stored on the candidate (``ai_pick_json``). It changes nothing by itself; ``nsmpa promote --use-ai``
lets a confident pick break ties, and every AI-assisted promotion is queued for human review.
"""
from __future__ import annotations

import json

from .ai_review import AIUnavailable, cached_call, make_client
from .config import Settings
from .db import Database
from .utils import sha256_text

PROMPT_VERSION = "ai-discovery-v1"
SYSTEM = """You help identify the primary student-run news publication (student newspaper or student news site) of a U.S.
college or university. You are given numbered candidate web pages found by search and by crawling the institution's site.
Choose the candidate that is the publication itself (its homepage or main site), not a university press office,
marketing/news release page, alumni magazine, directory listing, or a page that merely mentions the paper.
If none of the candidates is the student publication itself, answer 0. Be conservative."""
SCHEMA = {
    "type": "object",
    "properties": {
        "choice": {"type": "integer"},
        "confidence": {"type": "number"},
        "publication_name": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["choice", "confidence", "publication_name", "reason"],
    "additionalProperties": False,
}


def uncertain_institutions(db: Database, settings: Settings, limit: int | None = None, unitids: list[str] | None = None) -> list:
    th, early = settings.publication_confidence_threshold, settings.publication_early_stop_threshold
    sql = """SELECT i.unitid, i.name, i.website, MAX(COALESCE(c.verified_score, c.score)) best,
                    (SELECT COUNT(*) FROM publications p WHERE p.unitid=i.unitid AND p.ambiguous=1 AND p.is_primary=1) amb
             FROM institutions i JOIN publication_candidates c ON c.unitid=i.unitid
             WHERE i.included=1 AND c.status!='rejected'
               AND i.unitid NOT IN (SELECT unitid FROM publications WHERE verification_status IN ('manual','human_verified'))"""
    params: list = []
    if unitids:
        sql += f" AND i.unitid IN ({','.join('?' * len(unitids))})"
        params += unitids
    sql += " GROUP BY i.unitid HAVING amb>0 OR (best>=? AND best<?) ORDER BY i.unitid"
    params += [th - 0.15, early]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return db.execute(sql, params).fetchall()


def candidates_for(db: Database, unitid: str, k: int = 6) -> list:
    rows = db.execute("""SELECT * FROM publication_candidates WHERE unitid=? AND status!='rejected'
                         ORDER BY COALESCE(verified_score, score) DESC, id""", (unitid,)).fetchall()
    seen: set[str] = set()
    out = []
    for r in rows:
        if r["domain"] in seen:
            continue
        seen.add(r["domain"])
        out.append(r)
        if len(out) >= k:
            break
    return out


def prompt_for(inst, cands: list) -> str:
    lines = [f"Institution: {inst['name']}", f"Institution website: {inst['website'] or 'unknown'}", "", "Candidates:"]
    for i, c in enumerate(cands, start=1):
        ver = json.loads(c["verification_json"] or "{}")
        lines.append(f"{i}. URL: {c['url']}\n   Title: {c['title'] or ''}\n   Snippet: {(c['snippet'] or '')[:300]}\n"
                     f"   Found via: {c['source']}\n   Verification signals: {', '.join(ver.get('signals', [])) or 'none'}")
    return "\n".join(lines)


def run_ai_discovery(db: Database, settings: Settings, *, client=None, max_calls: int = 100, limit: int | None = None,
                     unitids: list[str] | None = None) -> dict:
    insts = uncertain_institutions(db, settings, limit, unitids)
    stats = {"institutions": len(insts), "picked": 0, "none": 0, "invalid": 0, "cached": 0, "live_calls": 0, "failed": 0}
    if insts and client is None:
        client = make_client(settings)
    for inst in insts:
        cands = candidates_for(db, inst["unitid"])
        if not cands:
            continue
        payload = prompt_for(inst, cands)
        if stats["live_calls"] >= max_calls:
            stats["stopped"] = f"--max-calls {max_calls} reached"
            break
        try:
            data, cached = cached_call(db, client, settings, PROMPT_VERSION, sha256_text(payload), payload, SYSTEM, SCHEMA)
        except AIUnavailable as exc:
            stats["failed"] += 1
            stats.setdefault("errors", []).append(f"{inst['unitid']}: {exc}")
            continue
        stats["cached" if cached else "live_calls"] += 1
        choice = data.get("choice")
        db.execute("UPDATE publication_candidates SET ai_pick_json=NULL WHERE unitid=?", (inst["unitid"],))
        if not isinstance(choice, int) or choice < 0 or choice > len(cands):
            stats["invalid"] += 1
            continue
        record = {"model": settings.ai_model, "provider": settings.ai_provider, "prompt_version": PROMPT_VERSION,
                  "confidence": float(data.get("confidence") or 0), "reason": str(data.get("reason", ""))[:500],
                  "publication_name": str(data.get("publication_name", ""))[:200], "candidates_shown": len(cands)}
        if choice == 0:
            stats["none"] += 1
            db.execute("UPDATE publication_candidates SET ai_pick_json=? WHERE id=?",
                       (json.dumps({**record, "choice": "none"}), cands[0]["id"]))
        else:
            stats["picked"] += 1
            db.execute("UPDATE publication_candidates SET ai_pick_json=? WHERE id=?",
                       (json.dumps({**record, "choice": "this"}), cands[choice - 1]["id"]))
        db.conn.commit()
    return stats

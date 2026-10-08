"""Researcher captures: policy text a person copied from a site that blocks automated access.

A human reading a website is not what a robots block is aimed at, so the researcher can open the page in a browser,
copy the policy text, and record it here (``nsmpa capture`` or the GUI's Capture tab). The capture is stored verbatim
with its SHA-256 fingerprint, who captured it and when; it is then classified exactly like a fetched page and attached
to the organization's latest research run, whose stance is recomputed. Every excerpt it produces is labelled
``acquisition='capture'`` in the database, the GUI and the evidence packet, so readers can see it came from a person's
copy rather than NSMPA's own fetch — and can re-check it against the live page or an archive.

Pending AI-search leads for the organization are re-checked against the new text (a lead whose quote appears in the
capture becomes confirmed).
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from .config import Settings
from .db import Database
from .utils import normalize_for_hash, prepare_request_url, registrableish_domain, sha256_text

MIN_CHARS, MAX_CHARS = 40, 2_000_000


class CaptureError(ValueError):
    pass


class _Page:
    """Minimal stand-in for extract.MainText built from pasted plain text."""

    def __init__(self, url: str, title: str, text: str):
        self.url, self.title = url, title
        self.main_text = self.full_text = text
        self.links: list = []
        self.author = self.published = None
        self.is_pdf = self.is_listing = False


def _target_run(db: Database, settings: Settings, entity_id: int) -> str:
    row = db.execute(
        "SELECT s.run_id FROM entity_stances s JOIN research_runs r ON r.id=s.run_id WHERE s.entity_id=? "
        "AND COALESCE(r.status,'')!='excluded' ORDER BY s.created_at DESC, s.id DESC LIMIT 1", (entity_id,)).fetchone()
    if row:
        return row["run_id"]
    from .runs import create_or_resume_run
    rid, _ = create_or_resume_run(db, settings, "capture", f"capture-{datetime.now(UTC):%Y%m%d}",
                                  params={"kind": "researcher_capture"}, command="nsmpa capture")
    return rid


def add_capture(db: Database, settings: Settings, *, entity_id: int, url: str, text: str, captured_by: str,
                title: str = "", note: str = "") -> dict:
    """Store and classify one capture. Returns a summary dict. Raises CaptureError on bad input."""
    from .research import EntityResearcher, is_first_party, page_kind
    from .review import enqueue_entity_review
    from .snapshots import store_text
    from .stance import classify_entity, store_stance

    entity = db.execute("SELECT * FROM research_entities WHERE id=?", (entity_id,)).fetchone()
    if entity is None:
        raise CaptureError(f"no organization with id {entity_id}")
    req = prepare_request_url((url or "").strip())
    if not req:
        raise CaptureError("a valid http(s) URL of the page you copied from is required")
    text = re.sub(r"\r\n?", "\n", text or "").strip()
    if not MIN_CHARS <= len(text) <= MAX_CHARS:
        raise CaptureError(f"paste between {MIN_CHARS} and {MAX_CHARS:,} characters of page text")
    captured_by = (captured_by or "").strip()[:80]
    if not captured_by:
        raise CaptureError("say who captured it (your name or initials)")
    digest = sha256_text(normalize_for_hash(text))
    dup = db.execute("SELECT * FROM captures WHERE entity_id=? AND text_sha256=?", (entity_id, digest)).fetchone()
    if dup:
        return {"capture_id": dup["id"], "duplicate": True, "evidence": dup["evidence_count"], "run_id": dup["run_id"],
                "stance": db.scalar("SELECT stance FROM entity_stances WHERE run_id=? AND entity_id=?", (dup["run_id"], entity_id))}

    run_id = _target_run(db, settings, entity_id)
    first = is_first_party(entity, req)
    cur = db.execute("INSERT INTO captures(entity_id,run_id,url,title,text,text_sha256,captured_by,note,first_party) "
                     "VALUES(?,?,?,?,?,?,?,?,?)", (entity_id, run_id, req, (title or "")[:500], text, digest, captured_by,
                                                   (note or "")[:2000], int(first)))
    cap_id = int(cur.lastrowid)
    kind = page_kind(req, title or "", False, False)
    text_sha = store_text(db, settings.research_snapshot_dir, text, req)
    requested = f"{req}#nsmpa-capture-{cap_id}"
    db.execute(
        "INSERT INTO research_pages(run_id,entity_id,requested_url,final_url,http_status,status,content_type,title,text_length,"
        "page_kind,text_sha256,first_party,access_class,acquisition,headers_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, entity_id, requested, req, None, "captured", "text/plain", (title or "")[:500], len(text), kind, text_sha,
         int(first), "ok", "capture", json.dumps({"captured_by": captured_by, "capture_id": cap_id})))
    page_id = int(db.scalar("SELECT id FROM research_pages WHERE run_id=? AND entity_id=? AND requested_url=?",
                            (run_id, entity_id, requested)))
    researcher = EntityResearcher.offline(db, settings, run_id)
    page = _Page(req, title or "", text)
    useful = researcher._store_evidence(entity, page, page_id, None, "researcher_capture", first, kind, None, text_sha)
    useful += researcher._store_voices(entity, page, page_id)
    db.execute("UPDATE evidence_items SET acquisition='capture' WHERE page_id=?", (page_id,))
    n_ev = db.scalar("SELECT COUNT(*) FROM evidence_items WHERE page_id=?", (page_id,))
    db.execute("UPDATE captures SET page_id=?, evidence_count=? WHERE id=?", (page_id, n_ev, cap_id))

    # Pending AI leads whose quote is in this capture are now confirmed.
    norm = normalize_for_hash(text)
    confirmed = 0
    for lead in db.execute("SELECT id, quote FROM ai_leads WHERE entity_id=? AND status='unconfirmed'", (entity_id,)).fetchall():
        q = normalize_for_hash(lead["quote"] or "")
        if len(q) >= 25 and q in norm:
            db.execute("UPDATE ai_leads SET status='confirmed', confirmed_via='capture', confirmed_url=? WHERE id=?", (req, lead["id"]))
            confirmed += 1

    result = classify_entity(db, settings, run_id, entity)
    result.review_reasons.append("includes_researcher_capture")
    store_stance(db, run_id, entity_id, result)
    enqueue_entity_review(db, run_id, entity, result)
    db.conn.commit()
    return {"capture_id": cap_id, "duplicate": False, "run_id": run_id, "first_party": first, "page_kind": kind,
            "evidence": n_ev, "substantive": useful, "ai_leads_confirmed": confirmed, "stance": result.stance,
            "relief_mode": result.relief_mode, "fingerprint": digest, "domain": registrableish_domain(req)}


def list_captures(db: Database, limit: int = 100) -> list[dict]:
    rows = db.execute(
        "SELECT c.id, c.entity_id, re.name AS entity, c.url, c.title, c.captured_by, c.captured_at, c.evidence_count, "
        "c.first_party, c.text_sha256, c.note, c.run_id FROM captures c JOIN research_entities re ON re.id=c.entity_id "
        "ORDER BY c.id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def capture_queue(db: Database, limit: int = 200) -> list[dict]:
    """Organizations that most need a capture: blocked sites with no confirmed policy, with AI leads to check first."""
    rows = db.execute(
        """SELECT re.id, re.name, re.cohort, re.homepage_url, s.stance, s.run_id,
                  (SELECT COUNT(*) FROM ai_leads l WHERE l.entity_id=re.id AND l.status='unconfirmed') AS leads,
                  (SELECT l.claimed_url FROM ai_leads l WHERE l.entity_id=re.id AND l.status='unconfirmed'
                     AND l.claimed_url!='' ORDER BY l.id DESC LIMIT 1) AS lead_url,
                  (SELECT l.quote FROM ai_leads l WHERE l.entity_id=re.id AND l.status='unconfirmed' ORDER BY l.id DESC LIMIT 1) AS lead_quote,
                  (SELECT COUNT(*) FROM captures c WHERE c.entity_id=re.id) AS captures
           FROM entity_stances s JOIN research_entities re ON re.id=s.entity_id
           JOIN research_runs r ON r.id=s.run_id AND COALESCE(r.status,'')!='excluded'
           WHERE s.id = (SELECT s2.id FROM entity_stances s2 JOIN research_runs r2 ON r2.id=s2.run_id
                         WHERE s2.entity_id=re.id AND COALESCE(r2.status,'')!='excluded' ORDER BY s2.created_at DESC, s2.id DESC LIMIT 1)
             AND (s.coverage_json LIKE '%blocks_automated_access%' OR s.rationale LIKE '%blocks automated access%')
           ORDER BY s.stance!='UNDETERMINED', leads DESC, re.name LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]

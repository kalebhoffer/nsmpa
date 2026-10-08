"""Direct outreach: ask newsrooms for their policy, and store their answers as first-party evidence.

The tool never sends email. It:
1. harvests contact addresses already published on pages it fetched (``mailto:`` links and visible addresses),
   preferring editorial roles (editor, standards, managing editor, adviser);
2. writes neutral, honestly-identified survey drafts as ``.eml`` files plus a mail-merge CSV for you to review and send
   from your own account;
3. records when you sent them and stores replies (``.eml``/``.txt``/``.pdf``/``.html``) as ``direct_response`` evidence,
   which counts as the organization's own statement of policy and refreshes its stance.

Contacts can be marked do-not-contact; they are never drafted again.
"""
from __future__ import annotations

import csv
import email
import email.policy
import json
import re
from email.message import EmailMessage
from pathlib import Path

from .config import Settings
from .db import Database
from .utils import registrableish_domain, sha256_bytes, slugify

EMAIL_RE = re.compile(r"(?<![\w.+-])([A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24})(?![\w-])")
ROLE_RANK = [("standards", "standards editor"), ("editor-in-chief", "editor-in-chief"), ("eic", "editor-in-chief"),
             ("editor", "editor"), ("managing", "managing editor"), ("adviser", "adviser"), ("advisor", "adviser"),
             ("publisher", "publisher"), ("news", "newsroom"), ("letters", "letters"), ("tips", "tips"),
             ("contact", "general contact"), ("info", "general contact")]
SKIP_LOCAL = re.compile(r"^(?:no-?reply|donotreply|privacy|abuse|postmaster|webmaster|ads?|advertis\w*|sales|billing|"
                        r"subscriptions?|circulation|careers|jobs|hr|classifieds?|marketing)$", re.I)


def guess_role(local: str) -> tuple[str, int]:
    low = local.lower()
    for i, (needle, role) in enumerate(ROLE_RANK):
        if needle in low:
            return role, i
    return "contact", len(ROLE_RANK)


def emails_in(html: str) -> set[str]:
    found = set(m.lower() for m in re.findall(r"mailto:([^\"'?>\s]+)", html, re.I))
    found |= {m.lower() for m in EMAIL_RE.findall(html)}
    return {e.strip(".;,") for e in found if "@" in e and not e.endswith((".png", ".jpg", ".gif", ".svg", ".webp"))}


def harvest_contacts(db: Database) -> dict:
    """Scan stored first-party snapshots for published addresses on the entity's own domain."""
    rows = db.execute(
        """SELECT p.entity_id, p.final_url, s.path, re.domain FROM research_pages p
           JOIN snapshots s ON s.sha256=p.content_sha256 AND s.kind='raw'
           JOIN research_entities re ON re.id=p.entity_id
           WHERE p.first_party=1 AND p.access_class='ok' AND re.cohort NOT IN ('expert','precedent_case')""").fetchall()
    added = 0
    for r in rows:
        try:
            html = Path(r["path"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for addr in emails_in(html):
            local, _, dom = addr.partition("@")
            if SKIP_LOCAL.match(local):
                continue
            ent_dom = (r["domain"] or "").lower()
            if not (dom == ent_dom or dom.endswith("." + ent_dom) or ent_dom.endswith("." + dom)):
                continue  # only addresses on the organization's own domain
            role, _ = guess_role(local)
            cur = db.execute("INSERT OR IGNORE INTO contacts(entity_id,email,role,source_url,source) VALUES(?,?,?,?, 'harvested')",
                             (r["entity_id"], addr, role, r["final_url"]))
            added += cur.rowcount
    db.conn.commit()
    return {"pages_scanned": len(rows), "contacts_added": added,
            "entities_with_contacts": db.scalar("SELECT COUNT(DISTINCT entity_id) FROM contacts WHERE do_not_contact=0")}


def add_contact(db: Database, entity_id: int, email_addr: str, name: str = "", role: str = "") -> None:
    db.execute("INSERT INTO contacts(entity_id,email,name,role,source) VALUES(?,?,?,?, 'manual') "
               "ON CONFLICT(entity_id,email) DO UPDATE SET name=excluded.name,role=excluded.role",
               (entity_id, email_addr.lower(), name or None, role or None))
    db.conn.commit()


def do_not_contact(db: Database, email_addr: str) -> int:
    n = db.execute("UPDATE contacts SET do_not_contact=1 WHERE email=?", (email_addr.lower(),)).rowcount
    db.conn.commit()
    return n


SURVEY = """Hello{greeting},

My name is {sender_name}{affiliation_clause}. I am researching how U.S. news organizations, including student
publications, handle requests to update, de-index, anonymize, or remove archived articles, particularly older crime or
arrest coverage where charges were later dismissed or records were sealed or expunged.

I found {publication}'s website while compiling publicly available policies, and I would be grateful for a few minutes of
your time. Short answers are welcome, and "no written policy" is a useful answer too.

1. Does {publication} have a written policy on requests to unpublish, de-index, anonymize, or update archived stories?
   If so, where can I find it?
2. In practice, how do you handle such requests? Which options do you use (removal, de-indexing from search engines,
   removing or replacing a name, adding an update or editor's note, or declining)?
3. Does it matter if charges were later dismissed, the person was acquitted, or the record was sealed or expunged?
4. Roughly how often do you receive these requests, and is there an example you are comfortable describing?
5. May I quote your response with attribution to {publication}? If you prefer, I will use it without attribution or
   only as background; please tell me which.

Participation is entirely voluntary. I am collecting responses on all sides of this question, including from
organizations that do not remove or alter archived work.

Thank you,
{sender_name}
{sender_email}
"""


def _survey_body(settings: Settings, publication: str, contact_name: str | None) -> str:
    aff = settings.outreach_affiliation.strip()
    return SURVEY.format(greeting=f" {contact_name}" if contact_name else "", sender_name=settings.outreach_sender_name,
                         affiliation_clause=f", {aff}" if aff else "", publication=publication,
                         sender_email=settings.outreach_sender_email)


def draft_campaign(db: Database, settings: Settings, campaign: str, out_dir: Path, *, cohort: str | None = None,
                   limit: int | None = None) -> dict:
    if not settings.outreach_sender_name or "@" not in settings.outreach_sender_email:
        raise ValueError("Set outreach_sender_name and outreach_sender_email in config.yml before drafting (honest identification).")
    sql = """SELECT re.*, c.id AS contact_id, c.email, c.name AS contact_name, c.role FROM research_entities re
             JOIN contacts c ON c.entity_id=re.id AND c.do_not_contact=0
             WHERE re.active=1 AND re.cohort NOT IN ('expert','precedent_case')"""
    params: list = []
    if cohort:
        sql += " AND re.cohort=?"
        params.append(cohort)
    rows = db.execute(sql + " ORDER BY re.id", params).fetchall()
    best: dict[int, object] = {}
    for r in rows:  # one contact per organization: most editorial role first
        if r["id"] not in best or guess_role(r["email"].split("@")[0])[1] < guess_role(best[r["id"]]["email"].split("@")[0])[1]:
            best[r["id"]] = r
    chosen = list(best.values())[:limit] if limit else list(best.values())
    folder = out_dir / slugify(campaign)
    folder.mkdir(parents=True, exist_ok=True)
    drafted = skipped = 0
    merge_rows = []
    for r in chosen:
        if db.scalar("SELECT COUNT(*) FROM outreach_messages WHERE campaign=? AND entity_id=?", (campaign, r["id"])):
            skipped += 1
            continue
        pub = r["name"]
        subject = f"Research question: how {pub} handles requests to update or remove archived articles"
        msg = EmailMessage()
        msg["From"] = f"{settings.outreach_sender_name} <{settings.outreach_sender_email}>"
        msg["To"] = r["email"]
        msg["Subject"] = subject
        msg["X-Unsent"] = "1"  # opens as an editable draft in Apple Mail / Outlook
        body = _survey_body(settings, pub, r["contact_name"])
        msg.set_content(body)
        path = folder / f"{r['id']:05d}-{slugify(pub, 50)}.eml"
        path.write_bytes(bytes(msg))
        db.execute("INSERT INTO outreach_messages(campaign,entity_id,contact_id,status,draft_path,subject) VALUES(?,?,?, 'drafted',?,?)",
                   (campaign, r["id"], r["contact_id"], str(path), subject))
        merge_rows.append({"entity_id": r["id"], "organization": pub, "email": r["email"], "role": r["role"],
                           "subject": subject, "draft_file": path.name})
        drafted += 1
    if merge_rows:
        with open(folder / "mail_merge.csv", "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(merge_rows[0]))
            if f.tell() == 0:
                w.writeheader()
            w.writerows(merge_rows)
    db.conn.commit()
    return {"campaign": campaign, "drafted": drafted, "already_drafted": skipped, "folder": str(folder),
            "organizations_without_contacts": db.scalar(
                "SELECT COUNT(*) FROM research_entities re WHERE re.active=1 AND re.cohort NOT IN ('expert','precedent_case') "
                + ("AND re.cohort=? " if cohort else "") + "AND NOT EXISTS (SELECT 1 FROM contacts c WHERE c.entity_id=re.id)",
                [cohort] if cohort else [])}


def mark_sent(db: Database, campaign: str, entity_id: int | None = None) -> int:
    sql = "UPDATE outreach_messages SET status='sent', sent_at=CURRENT_TIMESTAMP WHERE campaign=? AND status='drafted'"
    params: list = [campaign]
    if entity_id is not None:
        sql += " AND entity_id=?"
        params.append(entity_id)
    n = db.execute(sql, params).rowcount
    db.conn.commit()
    return n


def _response_text(path: Path) -> str:
    raw = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix == ".eml":
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        part = msg.get_body(preferencelist=("plain", "html"))
        text = part.get_content() if part else ""
        if part is not None and part.get_content_type() == "text/html":
            from .extract import extract_main_text
            text = extract_main_text(text.encode(), "text/html", "response:", {}).full_text
        # Drop quoted copies of our own survey so only their words are analysed.
        return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))
    if suffix == ".pdf":
        from .extract import _extract_pdf_text
        return _extract_pdf_text(raw)
    if suffix in {".html", ".htm"}:
        from .extract import extract_main_text
        return extract_main_text(raw, "text/html", "response:", {}).full_text
    return raw.decode("utf-8", errors="replace")


def record_response(db: Database, settings: Settings, entity_id: int, path: Path, *, campaign: str = "default") -> dict:
    """Store a reply as first-party `direct_response` evidence and refresh the organization's stance."""
    from .evidence import action_positions, extract_evidence
    from .review import enqueue_entity_review
    from .runs import create_or_resume_run
    from .similarity import score_case_match, score_similarity
    from .snapshots import store_raw, store_text
    from .stance import classify_entity, store_stance

    entity = db.execute("SELECT * FROM research_entities WHERE id=?", (entity_id,)).fetchone()
    if not entity:
        raise ValueError(f"unknown entity {entity_id}")
    text = _response_text(path)
    raw = path.read_bytes()
    run_id = db.scalar("SELECT run_id FROM entity_stances WHERE entity_id=? AND stance_version='0.3' ORDER BY id DESC LIMIT 1",
                       (entity_id,), None)
    if not run_id:
        run_id, _ = create_or_resume_run(db, settings, "outreach_responses", "outreach-responses")
    raw_sha, snap = store_raw(db, settings.research_snapshot_dir, raw, "text/plain", f"outreach:{campaign}:{entity_id}")
    text_sha = store_text(db, settings.research_snapshot_dir, text, f"outreach:{campaign}:{entity_id}")
    url = f"outreach://{slugify(campaign)}/{entity_id}/{raw_sha[:12]}"
    db.execute("""INSERT OR REPLACE INTO research_pages(run_id,entity_id,requested_url,final_url,status,content_type,title,text_length,
                    content_sha256,snapshot_path,access_class,page_kind,text_sha256,first_party)
                  VALUES(?,?,?,?, 'fetched','text/plain',?,?,?,?, 'ok','direct_response',?,1)""",
               (run_id, entity_id, url, url, f"Response from {entity['name']} ({campaign})", len(text), raw_sha, snap, text_sha))
    page_id = db.scalar("SELECT id FROM research_pages WHERE run_id=? AND entity_id=? AND requested_url=?", (run_id, entity_id, url))
    n = 0
    for it in extract_evidence(text, max_items=80):
        st = it.statement
        if st.statement_type == "mention":
            continue
        sim = score_similarity(settings.case_profile, it.excerpt, it.context, cohort=entity["cohort"])
        cm = score_case_match(settings.case_profile, settings.my_case, it.excerpt, it.context, cohort=entity["cohort"])
        cur = db.execute(
            """INSERT OR IGNORE INTO evidence_items(run_id,entity_id,page_id,cohort,source_url,source_title,source_domain,page_sha256,
                 text_sha256,excerpt,context,excerpt_sha256,near_dup_key,first_party,about_entity,evidence_class,statement_type,direction,
                 topic,authority_score,relevance_score,similarity_score,similarity_factors_json,extraction_confidence,rationale,
                 actions_json,case_match_score,case_match_factors_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,1,'direct_response',?,?,'outreach_response',1.0,?,?,?,?,?,?,?,?)""",
            (run_id, entity_id, page_id, entity["cohort"], url, f"Direct response ({campaign})", registrableish_domain(entity["homepage_url"] or ""),
             raw_sha, text_sha, it.excerpt, it.context, it.excerpt_sha256, it.near_dup_key, st.statement_type, st.direction,
             0.95, sim.score, json.dumps(sim.factors), st.confidence, "; ".join(st.cues)[:500],
             json.dumps(action_positions(it.excerpt)), cm.score, json.dumps(cm.factors)))
        if cur.rowcount and cur.lastrowid:
            db.conn.executemany("INSERT OR IGNORE INTO evidence_item_tags(evidence_id,tag) VALUES(?,?)", [(cur.lastrowid, t) for t in st.tags])
            n += 1
    db.execute("""UPDATE outreach_messages SET status='responded', responded_at=CURRENT_TIMESTAMP, response_path=?, response_sha256=?
                  WHERE campaign=? AND entity_id=?""", (str(path), sha256_bytes(raw), campaign, entity_id))
    result = classify_entity(db, settings, run_id, entity)
    store_stance(db, run_id, entity_id, result)
    enqueue_entity_review(db, run_id, entity, result)
    db.conn.commit()
    return {"entity": entity["name"], "evidence_items": n, "stance": result.stance, "run_id": run_id}


def status(db: Database) -> list[dict]:
    return [dict(r) for r in db.execute(
        "SELECT campaign, status, COUNT(*) n FROM outreach_messages GROUP BY campaign, status ORDER BY campaign, status")]

"""Import a researcher's hand-built evidence index (organizations + source links) without biasing the statistics.

An evidence index lists organizations *because* they have relief policies or practices. Counting them in a cohort's
percentages would inflate the result, so:

- Every row becomes a **precedent seed** (``idx_<slug>_<id>``, prior ``user_supplied``) whose source NSMPA fetches and
  classifies itself (``nsmpa verify-precedents --only-index --no-search``). A row is never evidence until NSMPA has read
  its source; secondary reports count only where they name the organization.
- U.S. newsrooms, student publications and standards organizations become **research entities** in their cohort
  (broadcasters in ``broadcast_newsroom``). Organizations that are new to the database are flagged
  ``selected_from_evidence_index`` and ``excluded_from_rates``: researched, catalogued and shown in the packet, but kept
  out of every percentage and denominator. Organizations already in a cohort's sourced universe stay counted as before.
- Shared-standards groups (e.g. NBCUniversal Local stations) and repeated outlets (OregonLive/The Oregonian) are
  recorded with their ``group`` so reports can count a policy framework once.
- Non-U.S. organizations, high-school media, reporting outlets and research centres are kept as precedents only.
"""
from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path

from .db import Database
from .utils import json_meta, prepare_request_url, registrableish_domain

REQUIRED = {"index_id", "organization", "tier", "evidence_focus", "source_url", "kind", "homepage", "country", "source_kind"}
KIND_TO_COHORT = {
    "newspaper": "professional_newsroom", "digital": "professional_newsroom", "wire": "professional_newsroom",
    "broadcast": "broadcast_newsroom", "student_college": "student_media", "standards_org": "support_org",
}
SOURCE = "user_evidence_index"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40] or "index"


def _find_entity(db: Database, cohort: str, domain: str):
    return db.execute(
        "SELECT * FROM research_entities WHERE cohort=? AND active=1 AND merged_into IS NULL AND lower(domain)=? "
        "ORDER BY id LIMIT 1", (cohort, domain)).fetchone()


def import_evidence_index(db: Database, path: Path, *, name: str) -> dict:
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise ValueError(f"{path} has no rows")
    missing = REQUIRED - set(rows[0])
    if missing:
        raise ValueError(f"{path} is missing columns: {', '.join(sorted(missing))}")
    slug = _slug(name)
    out: Counter = Counter()
    with db.transaction():
        for r in rows:
            iid = r["index_id"].strip()
            key = f"idx_{slug}_{iid}"
            url = prepare_request_url(r["source_url"].strip())
            if not url:
                out["skipped_bad_url"] += 1
                continue
            claim = (f"{r['evidence_focus'].strip()}. Listed in '{name}' (tier {r['tier'].strip()}; source type: "
                     f"{r['source_kind'].strip()}). To verify: NSMPA must read the source.")
            db.conn.execute(
                """INSERT INTO precedent_seeds(seed_key,organization,title,claim,actions,verification_query,primary_url,
                     prior_confidence,index_id,tier,kind,source_kind,group_name,country,source_list)
                   VALUES(?,?,?,?,?,?,?,'user_supplied',?,?,?,?,?,?,?)
                   ON CONFLICT(seed_key) DO UPDATE SET organization=excluded.organization,title=excluded.title,
                     claim=excluded.claim,primary_url=excluded.primary_url,tier=excluded.tier,kind=excluded.kind,
                     source_kind=excluded.source_kind,group_name=excluded.group_name,country=excluded.country,
                     source_list=excluded.source_list""",
                (key, r["organization"].strip(), r["evidence_focus"].strip(), claim, "", None, url, iid, r["tier"].strip(),
                 r["kind"].strip(), r["source_kind"].strip(), (r.get("group") or "").strip() or None, r["country"].strip(), name))
            out["precedent_seeds"] += 1
            cohort = KIND_TO_COHORT.get(r["kind"].strip())
            home = prepare_request_url((r.get("homepage") or "").strip())
            if not cohort or r["country"].strip() != "US" or not home:
                out["precedent_only"] += 1
                continue
            domain = registrableish_domain(home)
            ent = _find_entity(db, cohort, domain)
            entry = {"index_id": iid, "list": name, "tier": r["tier"].strip(), "source_url": url,
                     "source_kind": r["source_kind"].strip()}
            if ent is None:
                meta = {"selected_from_evidence_index": True, "excluded_from_rates": True, "evidence_index": [entry],
                        "group": (r.get("group") or "").strip() or None, "kind": r["kind"].strip()}
                db.conn.execute(
                    """INSERT INTO research_entities(cohort,source_key,name,homepage_url,domain,state,source,verification_status,
                         metadata_json,entity_type) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (cohort, f"{SOURCE}:{domain}", r["organization"].strip(), home, domain, (r.get("state") or "").strip() or None,
                     SOURCE, "seeded", json.dumps(meta), r["kind"].strip()))
                ent = _find_entity(db, cohort, domain)
                out[f"new_{cohort}"] += 1
            else:
                meta = json_meta(ent)
                listed = meta.setdefault("evidence_index", [])
                if not any(x.get("index_id") == iid and x.get("list") == name for x in listed):
                    listed.append(entry)
                    if ent["name"].strip().lower() != r["organization"].strip().lower():
                        meta.setdefault("also_listed_as", [])
                        if r["organization"].strip() not in meta["also_listed_as"]:
                            meta["also_listed_as"].append(r["organization"].strip())
                db.conn.execute("UPDATE research_entities SET metadata_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                (json.dumps(meta), ent["id"]))
                out[f"matched_existing_{cohort}" if not meta.get("selected_from_evidence_index") else f"merged_duplicate_{cohort}"] += 1
            db.conn.execute(
                "INSERT INTO entity_sources(entity_id,source,source_key,source_url,membership_label,raw_json) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(entity_id,source,source_key) DO UPDATE SET source_url=excluded.source_url,raw_json=excluded.raw_json",
                (ent["id"], SOURCE, key, url, f"{name} tier {r['tier'].strip()}", json.dumps(r)))
            db.conn.execute("UPDATE precedent_seeds SET status_note=COALESCE(status_note, ?) WHERE seed_key=?",
                            (f"organization tracked as research entity #{ent['id']} ({cohort})", key))
    return dict(out)


def index_report(db: Database, name: str | None = None) -> list[dict]:
    """One row per index entry: what the list says (tier) vs what NSMPA found when it read the source."""
    sql = """SELECT p.index_id, p.organization, p.tier, p.kind, p.source_kind, p.group_name, p.country, p.primary_url,
                    p.status, p.status_note, p.source_list, p.entity_id
             FROM precedent_seeds p WHERE p.index_id IS NOT NULL"""
    params: list = []
    if name:
        sql += " AND p.source_list=?"
        params.append(name)
    out = []
    for p in db.execute(sql + " ORDER BY p.source_list, p.index_id", params).fetchall():
        ev = db.execute(
            """SELECT direction, COUNT(*) n FROM evidence_items WHERE entity_id=? AND statement_type!='mention'
               AND run_id NOT IN (SELECT id FROM v_excluded_runs) GROUP BY direction""",
            (p["entity_id"],)).fetchall() if p["entity_id"] else []
        counts = {r["direction"]: r["n"] for r in ev}
        best = db.execute(
            """SELECT excerpt, acquisition FROM evidence_items WHERE entity_id=? AND direction='supportive'
               AND statement_type!='mention' AND run_id NOT IN (SELECT id FROM v_excluded_runs)
               ORDER BY authority_score DESC, extraction_confidence DESC LIMIT 1""", (p["entity_id"],)).fetchone() \
            if p["entity_id"] else None
        out.append({**dict(p), "supportive": counts.get("supportive", 0), "adverse": counts.get("adverse", 0),
                    "neutral": counts.get("neutral", 0), "best_supportive": best["excerpt"] if best else "",
                    "best_supportive_acquisition": best["acquisition"] if best else ""})
    return out

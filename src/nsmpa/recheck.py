"""Scheduled re-checks: notice when a newsroom changes a policy page we already captured.

`nsmpa recheck` builds a watch list from first-party policy/about pages (and any page that yielded a substantive
first-party statement), re-fetches each, and compares the normalized main text with the last captured version.
A change records similarity plus how many relief-relevant statements were added or removed, snapshots the new text,
and queues the organization for review (re-run `nsmpa research --entity-id ID` to refresh its stance).

`nsmpa schedule` writes a macOS launchd job that runs `nsmpa recheck --quiet` weekly. Fetch failures are recorded as
access results, never as changes.
"""
from __future__ import annotations

import difflib
import json
import os
import plistlib
import sys
from pathlib import Path

from .config import Settings
from .db import Database
from .evidence import extract_evidence
from .extract import extract_main_text
from .fetch import HardenedFetcher
from .progress import RunDashboard
from .snapshots import store_text
from .utils import normalize_for_hash, sha256_text

LABEL = "com.nsmpa.recheck"


def build_watchlist(db: Database) -> int:
    cur = db.execute(
        """INSERT OR IGNORE INTO policy_watch(entity_id,url,baseline_text_sha256,last_text_sha256,last_checked_at,last_status)
           SELECT p.entity_id, COALESCE(p.final_url, p.requested_url), p.text_sha256, p.text_sha256, p.fetched_at, 'baseline'
           FROM research_pages p
           WHERE p.first_party=1 AND p.access_class='ok' AND p.text_sha256 IS NOT NULL
             AND p.run_id NOT IN (SELECT id FROM v_excluded_runs)
             AND p.final_url NOT LIKE 'outreach://%'
             AND (p.page_kind IN ('policy','about')
                  OR EXISTS (SELECT 1 FROM evidence_items e WHERE e.page_id=p.id AND e.first_party=1
                             AND e.statement_type NOT IN ('mention','harm_consideration')))""")
    db.conn.commit()
    return cur.rowcount


def _text_for(db: Database, sha: str | None) -> str:
    if not sha:
        return ""
    path = db.scalar("SELECT path FROM snapshots WHERE sha256=? AND kind='text'", (sha,), None)
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace") if path else ""
    except OSError:
        return ""


def _relief_statements(text: str) -> set[str]:
    return {e.excerpt_sha256 for e in extract_evidence(text, max_items=200)
            if e.statement.statement_type not in {"mention", "harm_consideration"}}


def compare_texts(old: str, new: str) -> dict:
    sim = round(difflib.SequenceMatcher(None, normalize_for_hash(old)[:30000], normalize_for_hash(new)[:30000], autojunk=False).ratio(), 3)
    o, n = _relief_statements(old), _relief_statements(new)
    return {"similarity": sim, "added": len(n - o), "removed": len(o - n)}


async def run_recheck(db: Database, settings: Settings, *, limit: int | None = None, quiet: bool = False, verbose: bool = False,
                      fetcher: HardenedFetcher | None = None) -> dict:
    added = build_watchlist(db)
    sql = "SELECT * FROM policy_watch ORDER BY COALESCE(last_checked_at,'') ASC, id"
    rows = db.execute(sql + (" LIMIT ?" if limit else ""), ([limit] if limit else [])).fetchall()
    stats = {"watching": db.scalar("SELECT COUNT(*) FROM policy_watch"), "new_watches": added, "checked": 0, "changed": 0,
             "unchanged": 0, "inaccessible": 0}
    dash = RunDashboard("NSMPA Policy re-check", len(rows), quiet=quiet, verbose=verbose, universe="Known policy pages",
                        db=db, run_id="recheck:latest", persist_seconds=settings.heartbeat_seconds)
    own = fetcher is None
    fetcher = fetcher or HardenedFetcher(settings, on_event=dash.log)
    try:
        with dash:
            for w in rows:
                dash.update(current=w["url"][:80])
                r = await fetcher.fetch_safe(w["url"])
                stats["checked"] += 1
                if r.access_class != "ok" or not r.content:
                    stats["inaccessible"] += 1
                    db.execute("UPDATE policy_watch SET last_checked_at=CURRENT_TIMESTAMP, last_status=? WHERE id=?",
                               (r.access_class, w["id"]))
                else:
                    page = extract_main_text(r.content, r.content_type, r.final_url, r.headers)
                    new_sha = sha256_text(page.main_text)
                    if new_sha == w["last_text_sha256"]:
                        stats["unchanged"] += 1
                        db.execute("UPDATE policy_watch SET last_checked_at=CURRENT_TIMESTAMP, last_status='unchanged' WHERE id=?", (w["id"],))
                    else:
                        old_text = _text_for(db, w["last_text_sha256"])
                        store_text(db, settings.research_snapshot_dir, page.main_text, r.final_url)
                        cmp = compare_texts(old_text, page.main_text)
                        summary = (f"text similarity {cmp['similarity']}; {cmp['added']} relief-relevant statement(s) added, "
                                   f"{cmp['removed']} removed")
                        db.execute("""INSERT INTO policy_changes(watch_id,old_text_sha256,new_text_sha256,similarity,
                                        added_relief_statements,removed_relief_statements,summary) VALUES(?,?,?,?,?,?,?)""",
                                   (w["id"], w["last_text_sha256"], new_sha, cmp["similarity"], cmp["added"], cmp["removed"], summary))
                        db.execute("""UPDATE policy_watch SET last_text_sha256=?, last_checked_at=CURRENT_TIMESTAMP, last_status='changed',
                                        changes=changes+1 WHERE id=?""", (new_sha, w["id"]))
                        change_id = db.scalar("SELECT MAX(id) FROM policy_changes WHERE watch_id=?", (w["id"],))
                        cohort = db.scalar("SELECT cohort FROM research_entities WHERE id=?", (w["entity_id"],), None)
                        db.execute("""INSERT OR IGNORE INTO review_queue(run_id,item_type,item_id,entity_id,cohort,priority,reasons_json)
                                      VALUES('recheck','policy_change',?,?,?,?,?)""",
                                   (change_id, w["entity_id"], cohort, 25.0 + 5 * (cmp["added"] + cmp["removed"]),
                                    json.dumps(["policy_page_changed", "rerun_research_for_this_entity"])))
                        stats["changed"] += 1
                        dash.add_recent(f"! Policy page changed  {w['url'][:60]} ({summary})")
                db.conn.commit()
                dash.increment(completed=1)
    finally:
        if own:
            await fetcher.close()
    return stats


def launchd_plist(repo: Path, config: Path | None, *, weekday: int = 1, hour: int = 3) -> dict:
    exe = Path(sys.executable).with_name("nsmpa")
    args = [str(exe), "recheck", "--quiet"] + (["--config", str(config.resolve())] if config else [])
    logs = repo / "output" / "logs"
    return {
        "Label": LABEL,
        "ProgramArguments": args,
        "WorkingDirectory": str(repo),
        "StartCalendarInterval": {"Weekday": weekday, "Hour": hour, "Minute": 0},
        "StandardOutPath": str(logs / "recheck.out.log"),
        "StandardErrorPath": str(logs / "recheck.err.log"),
        "RunAtLoad": False,
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    }


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def write_plist(plist: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Path(plist["StandardOutPath"]).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        plistlib.dump(plist, f)
    os.chmod(path, 0o644)
    return path

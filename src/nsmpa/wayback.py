"""Wayback Machine comparison: did a newsroom quietly change or remove an article?

For each sampled archived crime/arrest article (and each first-party article URL that now returns
404/410), the Internet Archive's CDX index is queried. When more than one distinct capture exists,
the earliest capture is fetched (raw ``id_`` form) and compared with the current page:

- ``names_removed``  person names present in the archived article no longer appear in the current one
- ``noindex_added``  the archived copy was indexable; the current copy carries noindex
- ``content_altered`` substantial text change (similarity below ``wayback_change_threshold``)
- ``title_changed``  headline changed
- ``unpublished``     archived as live (HTTP 200) but the current URL returns 404/410

Privacy rule: names of people are NEVER stored or displayed. Only a count and unsalted SHA-256
prefixes (for reproducibility of the comparison) are kept. Recording "who was anonymized" would undo
the very relief being documented. Titles are not stored either (they often contain names).

Name detection is heuristic; every observation is labelled for human verification.
"""
from __future__ import annotations

import difflib
import json
import re
from urllib.parse import quote, urlsplit

from .extract import extract_main_text
from .utils import normalize_for_hash, sha256_text

CDX = "https://web.archive.org/cdx/search/cdx?url={url}&output=json&fl=timestamp,statuscode,digest&filter=statuscode:200&collapse=digest&limit=60"
RAW = "https://web.archive.org/web/{ts}id_/{url}"
VIEW = "https://web.archive.org/web/{ts}/{url}"

_NAME = re.compile(r"\b([A-Z][a-z]{1,20}(?:\s+[A-Z]\.)?\s+(?:Mc|Mac|O')?[A-Z][a-z]{1,25}(?:-[A-Z][a-z]{1,20})?)\b")
_NOT_NAME_WORD = {
    "The", "This", "That", "University", "College", "Police", "Department", "County", "City", "State", "Court",
    "District", "Street", "Avenue", "Hall", "Center", "Campus", "Student", "Students", "News", "Daily", "Times",
    "Office", "Public", "Safety", "North", "South", "East", "West", "New", "Monday", "Tuesday", "Wednesday",
    "Thursday", "Friday", "Saturday", "Sunday", "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December", "Editor", "Editors", "Staff", "Sheriff", "Superior",
    "Associated", "Press", "United", "States", "Read", "More", "Related", "Share", "Comments", "Contact", "Advertise",
    "Prosecuting", "Attorney", "Judge", "Officer", "Sergeant", "Detective", "Chief", "President", "Dean", "Professor",
}


def person_names(text: str) -> set[str]:
    out = set()
    for m in _NAME.finditer(text or ""):
        cand = m.group(1)
        words = re.split(r"\s+", cand)
        if any(w.rstrip(".") in _NOT_NAME_WORD for w in words):
            continue
        out.add(cand)
    return out


def _ratio(a: str, b: str) -> float:
    a, b = normalize_for_hash(a)[:20000], normalize_for_hash(b)[:20000]
    if not a and not b:
        return 1.0
    return round(difflib.SequenceMatcher(None, a, b, autojunk=False).ratio(), 3)


def compare_versions(old_html: bytes, new_html: bytes | None, *, url: str, current_status: int | None,
                     threshold: float) -> list[dict]:
    """Pure comparison (unit-testable). Returns observation dicts with no names or titles."""
    old = extract_main_text(old_html, "text/html", url, {})
    obs: list[dict] = []
    if new_html is None:
        if current_status in (404, 410):
            obs.append({"type": "unpublished", "current_status": current_status})
        return obs
    new = extract_main_text(new_html, "text/html", url, {})
    sim = _ratio(old.main_text, new.main_text)
    old_names, new_text = person_names(old.main_text), new.full_text or new.main_text
    removed = sorted(n for n in old_names if n not in new_text)
    if removed and sim >= 0.4:  # still recognisably the same article
        obs.append({"type": "names_removed", "count": len(removed),
                    "name_hashes": [sha256_text(normalize_for_hash(n))[:12] for n in removed],
                    "update_note_present": bool(re.search(r"editor'?s'? note|has been updated|updated to remove|name has been", new_text, re.I))})
    if new.noindex and not old.noindex:
        obs.append({"type": "noindex_added", "directive": new.meta_robots or new.x_robots_tag})
    if old.title and new.title and normalize_for_hash(old.title) != normalize_for_hash(new.title):
        obs.append({"type": "title_changed", "title_similarity": _ratio(old.title, new.title)})
    if sim < threshold:
        obs.append({"type": "content_altered", "text_similarity": sim})
    return obs


OBS_TO_EVIDENCE = {
    "names_removed": ("wayback_names_removed", "supportive", {"anonymize": "practiced"}, 0.7),
    "noindex_added": ("wayback_noindex_added", "supportive", {"deindex": "practiced"}, 0.85),
    "unpublished": ("wayback_unpublished", "supportive", {"unpublish": "practiced"}, 0.5),
    "content_altered": ("wayback_content_altered", "neutral", {}, 0.6),
    "title_changed": ("wayback_title_changed", "neutral", {}, 0.6),
}


def describe(o: dict, earliest: str, compared: str) -> str:
    d = f"{earliest[:4]}-{earliest[4:6]}-{earliest[6:8]}"
    if o["type"] == "names_removed":
        return (f"Wayback Machine comparison: the archived copy of this crime/arrest article captured {d} named "
                f"{o['count']} person(s) who are no longer named in the current version"
                f"{' (the current version carries an update/editor note)' if o.get('update_note_present') else ''}. "
                "Names withheld by NSMPA.")
    if o["type"] == "noindex_added":
        return (f"Wayback Machine comparison: the copy captured {d} had no noindex directive; the current version "
                f"carries one ({o.get('directive')}).")
    if o["type"] == "unpublished":
        return (f"Wayback Machine comparison: this article was archived as live on {d}, but the URL now returns HTTP "
                f"{o['current_status']}. May reflect unpublishing or a site migration; verify.")
    if o["type"] == "content_altered":
        return f"Wayback Machine comparison: the text changed substantially since the {d} capture (similarity {o['text_similarity']})."
    if o["type"] == "title_changed":
        return f"Wayback Machine comparison: the headline changed since the {d} capture."
    return json.dumps(o)


async def check_url(researcher, entity, page_row, url: str) -> dict:
    """Query CDX, fetch the earliest capture when needed, compare, store evidence. Returns the check record."""
    db, fetcher, settings, run_id = researcher.db, researcher.fetcher, researcher.settings, researcher.run_id
    bare = re.sub(r"^https?://", "", url)
    cdx = await fetcher.fetch_safe(CDX.format(url=quote(bare, safe="")))
    rec = {"status": "no_archive", "snapshots": 0, "earliest": None, "compared": None, "observations": [], "error": None}
    if cdx.access_class != "ok":
        rec.update(status="archive_unavailable", error=cdx.error or cdx.access_class)
    else:
        try:
            rows = json.loads(cdx.content.decode("utf-8", "replace") or "[]")[1:]
        except ValueError:
            rows = []
        rec["snapshots"] = len(rows)
        if rows:
            earliest = rows[0][0]
            rec["earliest"] = earliest
            current_status = page_row["http_status"]
            gone = page_row["access_class"] in {"not_found", "gone"}
            if len(rows) > 1 or gone:
                snap = await fetcher.fetch_safe(RAW.format(ts=earliest, url=url))
                if snap.access_class == "ok" and snap.content:
                    current = None
                    if not gone:
                        cur = await fetcher.fetch_safe(url)
                        current = cur.content if cur.access_class == "ok" else None
                    rec["compared"] = earliest
                    rec["observations"] = compare_versions(snap.content, current, url=url, current_status=current_status,
                                                           threshold=settings.wayback_change_threshold)
                    rec["status"] = "changed" if rec["observations"] else "unchanged"
                else:
                    rec.update(status="archive_unavailable", error=snap.error or snap.access_class)
            else:
                rec["status"] = "single_capture"
    db.execute(
        """INSERT INTO wayback_checks(run_id,entity_id,page_id,url,status,snapshots,earliest_ts,compared_ts,archive_url,observations_json,error)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(run_id,entity_id,url) DO UPDATE SET status=excluded.status,snapshots=excluded.snapshots,
             earliest_ts=excluded.earliest_ts,compared_ts=excluded.compared_ts,archive_url=excluded.archive_url,
             observations_json=excluded.observations_json,error=excluded.error""",
        (run_id, entity["id"], page_row["id"], url, rec["status"], rec["snapshots"], rec["earliest"], rec["compared"],
         VIEW.format(ts=rec["earliest"], url=url) if rec["earliest"] else None, json.dumps(rec["observations"]), rec["error"]))
    for o in rec["observations"]:
        stype, direction, actions, conf = OBS_TO_EVIDENCE[o["type"]]
        text = describe(o, rec["earliest"], rec["compared"] or rec["earliest"])

        class _Page:  # minimal page facade for _insert_item; deliberately no title (may contain names)
            pass
        pg = _Page()
        pg.url, pg.title = url, ""
        researcher._insert_item(
            entity, page=pg, page_id=page_row["id"], query_id=None, topic="wayback", first=True, about=True,
            ev_class="technical", statement_type=stype, direction=direction, excerpt=text,
            context=f"Archive capture {rec['earliest']}; snapshots={rec['snapshots']}",
            excerpt_sha=sha256_text(normalize_for_hash(text + url)), near_dup=sha256_text("wb" + url + o["type"])[:24],
            tags=["wayback"], actions=actions, authority=0.85, confidence=conf,
            cues=f"wayback:{o['type']}", raw_sha=None, text_sha=None)
        if direction == "supportive":
            researcher.dash.add_recent(f"✓ Archive change ({o['type'].replace('_', ' ')})  {entity['name']}")
    db.conn.commit()
    return rec


async def wayback_for_entity(researcher, entity) -> int:
    """Check sampled crime articles and first-party article URLs that now 404/410."""
    s = researcher.settings
    if not s.wayback_enabled or s.wayback_max_checks_per_entity <= 0:
        return 0
    rows = researcher.db.execute(
        """SELECT * FROM research_pages WHERE run_id=? AND entity_id=? AND first_party=1 AND (
             page_kind='crime_article' OR (access_class IN ('not_found','gone') AND page_kind IN ('unknown','article','other')))
           ORDER BY CASE WHEN access_class IN ('not_found','gone') THEN 0 ELSE 1 END, id LIMIT ?""",
        (researcher.run_id, entity["id"], s.wayback_max_checks_per_entity)).fetchall()
    n = 0
    for row in rows:
        if researcher.stop.force:
            break
        url = row["final_url"] or row["requested_url"]
        if not urlsplit(url).netloc:
            continue
        researcher.dash.update(phase="archive: Wayback comparison")
        await check_url(researcher, entity, row, url)
        n += 1
    return n


async def run_wayback(db, settings, run_id: str, *, quiet: bool = False, verbose: bool = False, fetcher=None,
                      entity_ids: list[int] | None = None) -> dict:
    """Run Wayback comparisons over an existing research run, then refresh affected stances."""
    from .fetch import HardenedFetcher
    from .progress import RunDashboard
    from .research import EntityResearcher
    from .review import enqueue_entity_review
    from .runs import StopController
    from .search import NullSearchProvider, SearchBroker
    from .stance import classify_entity, store_stance

    sql = ("SELECT DISTINCT re.* FROM research_entities re JOIN research_pages p ON p.entity_id=re.id "
           "WHERE p.run_id=? AND p.first_party=1")
    params: list = [run_id]
    if entity_ids:
        sql += f" AND re.id IN ({','.join('?' * len(entity_ids))})"
        params += entity_ids
    entities = db.execute(sql, params).fetchall()
    stop = StopController()
    dash = RunDashboard("NSMPA Wayback comparison", len(entities), quiet=quiet, verbose=verbose, universe=f"run {run_id}",
                        db=db, run_id=f"wayback:{run_id}", persist_seconds=settings.heartbeat_seconds)
    own = fetcher is None
    fetcher = fetcher or HardenedFetcher(settings, on_event=dash.log)
    broker = SearchBroker(db, settings, run_id, NullSearchProvider())
    checks = 0
    with dash:
        stop.on_stop(dash.notice)
        uninstall = stop.install()
        researcher = EntityResearcher(db, settings, broker, fetcher, run_id, dash, stop)
        try:
            for e in entities:
                if stop.stop_requested:
                    break
                dash.update(current=e["name"])
                checks += await wayback_for_entity(researcher, e)
                if db.scalar("SELECT COUNT(*) FROM entity_stances WHERE run_id=? AND entity_id=?", (run_id, e["id"])):
                    r = classify_entity(db, settings, run_id, e)
                    store_stance(db, run_id, int(e["id"]), r)
                    enqueue_entity_review(db, run_id, e, r)
                db.conn.commit()
                dash.increment(completed=1)
        finally:
            uninstall()
            if own:
                await fetcher.close()
    changed = db.scalar("SELECT COUNT(*) FROM wayback_checks WHERE run_id=? AND status='changed'", (run_id,))
    return {"run_id": run_id, "entities": len(entities), "checks": checks, "changed": changed,
            "interrupted": stop.stop_requested}

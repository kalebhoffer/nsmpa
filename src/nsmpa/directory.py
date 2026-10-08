"""Import / export of the user's U.S. newspaper directory CSV (student + professional).

Import rules (documented in docs/directory_import.md):
- Professional rows sharing a website domain are editions/titles of one site (e.g. community titles under statesman.com)
  and share its policy, so they are merged into one organization per domain; every title is kept as a provenance row.
- Student rows are matched to IPEDS institutions:
    1. ``name``      institution named in the row, matched within the state (exact, then fuzzy >= 0.88)
    2. ``edu_domain`` publication host is the institution's web domain or a subdomain of it (longest match wins)
    3. ``heuristic``  independent domain contains the institution's acronym or a distinctive name token (unique match only;
                      queued for review)
  Matched rows become publication candidates (then promoted) so they flow through the normal student pipeline.
  Unmatched rows are still researched as student media but excluded from IPEDS coverage statistics.
- Platform-hosted URLs (issuu, blogspot, wordpress.com, weebly, sites.google.com) are flagged for a better URL.
- The directory's research priority (1 = highest) orders research.
"""
from __future__ import annotations

import csv
import difflib
import json
import re
from collections import defaultdict
from pathlib import Path

from .db import Database
from .utils import normalize_url, prepare_request_url, registrableish_domain

PLATFORM_HOSTS = ("issuu.com", "blogspot.com", "wordpress.com", "weebly.com", "sites.google.com", "wixsite.com", "squarespace.com")
GENERIC = {"university", "college", "the", "of", "at", "and", "state", "community", "institute", "technology", "school",
           "campus", "main", "center", "saint", "st", "for", "in", "a", "an", "sciences", "arts", "polytechnic", "system"}


def _norm_name(s: str) -> str:
    s = s.lower().replace("&", " and ").replace("-", " ").replace("/", " ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\bsaint\b", "st", s)
    s = re.sub(r"^the\s+", "", s)
    s = re.sub(r"\ba\s+and\s+m\b", "a and m", s)
    return re.sub(r"\s+", " ", s).strip()


def _host(url: str) -> str:
    return registrableish_domain(url)


def _acronym(name: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z]+", name) if w.lower() not in {"of", "the", "at", "and", "for", "in"}]
    return "".join(w[0] for w in words).lower()


# Words that identify a region or kind of school rather than one institution: useless as matching tokens.
GENERIC |= {"mountain", "pacific", "atlantic", "valley", "central", "northern", "southern", "eastern", "western", "north", "south",
            "east", "west", "christian", "baptist", "methodist", "lutheran", "catholic", "national", "international", "american",
            "technical", "metropolitan", "medical", "health", "online", "learning", "global", "liberty", "regional", "county",
            "city", "coast", "great", "lakes", "river", "plains", "southwest", "northwest", "southeast", "northeast", "midwest",
            "colorado", "california", "texas", "florida", "washington", "oregon", "carolina", "virginia", "georgia", "kentucky",
            "tennessee", "missouri", "minnesota", "michigan", "indiana", "illinois", "pennsylvania", "jersey", "york", "arizona",
            "alabama", "louisiana", "oklahoma", "kansas", "nebraska", "dakota", "montana", "idaho", "nevada", "mexico", "alaska",
            "hawaii", "maine", "vermont", "hampshire", "massachusetts", "connecticut", "island", "delaware", "maryland", "ohio",
            "wisconsin", "iowa", "arkansas", "mississippi", "wyoming", "utah"}
MASTHEAD = ("news", "daily", "times", "post", "voice", "sentinel", "chronicle", "bulletin", "echo", "review", "spinnaker", "herald",
            "observer", "tribune", "gazette", "journal", "press", "record", "crescent", "scroll", "beacon", "lantern", "signal",
            "spectator", "oracle", "independent", "collegian", "courier", "vista", "front", "pioneer", "ledger", "star", "sun",
            "mirror", "monitor", "argonaut", "flyer", "torch", "ram", "eagle", "hawk", "tiger", "lion", "online", "today", "weekly",
            "report", "reporter", "standard", "leader", "letter", "newsletter", "dispatch", "mast", "quad", "current", "wire")
NON_FLAGSHIP = re.compile(r"\b(?:global|online|distance|extended|adult|college of online|virtual)\b", re.I)


def _tokens(name: str) -> set[str]:
    return {w for w in _norm_name(name).split() if len(w) >= 5 and w not in GENERIC}


def priority_of(value: str) -> int | None:
    m = re.match(r"\s*(\d+)", value or "")
    return int(m.group(1)) if m else None


class InstitutionIndex:
    def __init__(self, db: Database):
        self.by_state: dict[str, list[dict]] = defaultdict(list)
        for r in db.execute("""SELECT unitid, name, state, website, json_extract(raw_json,'$.IALIAS') alias,
                                      CAST(json_extract(raw_json,'$.INSTSIZE') AS INTEGER) size
                               FROM institutions WHERE included=1"""):
            d = dict(r)
            d["norm"] = _norm_name(d["name"])
            d["aliases"] = {_norm_name(a) for a in (d["alias"] or "").split("|") if a.strip()}
            self.by_state[(d["state"] or "").upper()].append(d)
        # Institutions outside the included universe (two-year, for-profit...): used only to label out-of-scope papers.
        self.excluded_domains: dict[str, str] = {}
        for r in db.execute("SELECT name, website FROM institutions WHERE included=0 AND website IS NOT NULL AND website!=''"):
            h = registrableish_domain(r["website"])
            if h:
                self.excluded_domains[h] = r["name"]

    @staticmethod
    def _flagship(cands: list[dict]) -> dict | None:
        """Among campuses sharing a name, prefer the main campus: explicit 'main campus' wording, else the largest
        enrollment, never an online/global division. Returns None when it is still a tie."""
        pool = [c for c in cands if not NON_FLAGSHIP.search(c["name"])] or cands
        main = [c for c in pool if re.search(r"main campus|campus immersion|twin cities|pittsburgh campus|college station|"
                                             r"new brunswick|columbia$|fort collins|ann arbor|urbana champaign", c["norm"])]
        if len(main) == 1:
            return main[0]
        pool = main or pool
        pool = sorted(pool, key=lambda c: -(c["size"] or 0))
        if len(pool) == 1 or (pool[0]["size"] or 0) > (pool[1]["size"] or 0):
            return pool[0]
        return None

    def match(self, state: str, institution: str, url: str, publication: str) -> tuple[str | None, str, float, str]:
        """Return (unitid, method, confidence, note). Methods: name, name_campus, edu_domain, heuristic (suggestion only),
        ambiguous, unmatched."""
        cands = self.by_state.get((state or "").upper(), [])
        if not cands:
            return None, "unmatched", 0.0, "no IPEDS institutions in state"
        if institution.strip():
            target = _norm_name(institution)
            exact = [c for c in cands if c["norm"] == target or target in c["aliases"]]
            if len(exact) == 1:
                return exact[0]["unitid"], "name", 0.85, ""
            prefixed = exact or [c for c in cands if c["norm"].startswith(target + " ") or c["norm"].startswith(target + "-")]
            if prefixed:
                f = self._flagship(prefixed)
                if f:
                    return f["unitid"], "name_campus", 0.8, f"named institution resolved to main campus: {f['name']}"
                return None, "ambiguous", 0.0, "several campuses: " + "; ".join(c["name"] for c in prefixed[:4])
            scored = sorted(((difflib.SequenceMatcher(None, target, c["norm"]).ratio(), c) for c in cands), key=lambda x: -x[0])
            if scored and scored[0][0] >= 0.88 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.03):
                return scored[0][1]["unitid"], "name_fuzzy", 0.78, f"name similarity {scored[0][0]:.2f}"
            # A named institution we cannot find is reported, never guessed from the domain.
        host = _host(url)
        hits = []
        for c in cands:
            ih = _host(c["website"] or "")
            if ih and (host == ih or host.endswith("." + ih)):
                hits.append((len(ih), c))
        if hits:
            hits.sort(key=lambda x: -x[0])
            top = [c for n, c in hits if n == hits[0][0]]
            if len(top) == 1:
                return top[0]["unitid"], "edu_domain", 0.8, ""
            f = self._flagship(top)
            if f:
                return f["unitid"], "edu_domain", 0.75, f"shared domain resolved to main campus: {f['name']}"
            return None, "ambiguous", 0.0, "several institutions share this domain: " + "; ".join(c["name"] for c in top[:4])
        for dom, name in self.excluded_domains.items():
            if host == dom or host.endswith("." + dom):
                return None, "out_of_scope", 0.0, f"paper belongs to {name}, which is outside the included IPEDS universe (e.g. two-year)"
        if institution.strip():
            return None, "unmatched", 0.0, f"named institution '{institution}' not found in IPEDS list for {state}"
        label = host.split(".")[0] if host else ""
        label = label[3:] if label.startswith("the") else label
        joined = re.sub(r"[^a-z]", "", host.rsplit(".", 1)[0]) + " " + _norm_name(publication).replace(" ", "")
        scored = []
        for c in cands:
            if NON_FLAGSHIP.search(c["name"]):
                continue
            score = 0.0
            ac = _acronym(c["name"])
            rest = label[len(ac):] if len(ac) >= 3 and label.startswith(ac) else None
            if rest is not None and (rest == "" or rest.startswith(MASTHEAD)):
                score += 0.6
            toks = _tokens(c["name"])
            if toks and any(t in joined for t in toks):
                score += 0.7
            if score:
                scored.append((score, c))
        scored.sort(key=lambda x: -x[0])
        if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            return scored[0][1]["unitid"], "heuristic", 0.55, f"suggested: {scored[0][1]['name']} (domain/name contains its acronym or a distinctive word)"
        if len(scored) > 1:
            return None, "ambiguous", 0.0, "possible: " + "; ".join(x[1]["name"] for x in scored[:4])
        return None, "unmatched", 0.0, "no name, domain or token match"


def read_directory(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        return [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]


def _source_label(row: dict) -> str:
    name = row.get("Source name") or "directory"
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:60]


def import_directory(db: Database, path: Path, *, promote: bool = True, threshold: float = 0.6,
                     report_dir: Path | None = None) -> dict:
    from .discovery import promote_candidates
    from .research import sync_student_entities
    rows = read_directory(path)
    required = {"Publication ID", "Publication", "Category", "State", "Website (historical or supplied)"}
    missing = required - set(rows[0]) if rows else required
    if missing:
        raise ValueError(f"Not a recognised directory file; missing columns: {sorted(missing)}")
    idx = InstitutionIndex(db)
    stats = defaultdict(int)
    match_log: list[dict] = []
    pro_groups: dict[str, list[dict]] = defaultdict(list)
    student_entities: list[tuple[dict, str | None, str, float, str]] = []

    for r in rows:
        url = prepare_request_url(r["Website (historical or supplied)"])
        if not url:
            stats["invalid_url"] += 1
            match_log.append({"id": r["Publication ID"], "publication": r["Publication"], "result": "invalid_url"})
            continue
        r["_url"] = url
        r["_platform"] = any(_host(url) == p or _host(url).endswith("." + p) for p in PLATFORM_HOSTS)
        if r["Category"].lower().startswith("student"):
            unitid, method, conf, note = idx.match(r["State"], r.get("Institution / Affiliation", ""), url, r["Publication"])
            student_entities.append((r, unitid, method, conf, note))
            stats[f"student_{method}"] += 1
            stats["student_platform_hosted"] += int(r["_platform"])
        else:
            pro_groups[_host(url)].append(r)

    with db.transaction():
        # ---------------- professional: one organization per website domain
        for dom, group in pro_groups.items():
            root = next((g for g in group if (normalize_url(g["_url"]) or "").rstrip("/").count("/") <= 2), None)
            lead = root or group[0]
            name = lead["Publication"] if (root or len(group) == 1) else f"{dom} ({len(group)} titles)"
            prio = min((p for p in (priority_of(g.get("Research priority", "")) for g in group) if p is not None), default=None)
            meta = {"research_priority": prio, "titles": [g["Publication"] for g in group], "city": lead.get("City") or None,
                    "directory_source": lead.get("Source name"), "source_year": lead.get("Source year"),
                    "platform_group_size": len(group)}
            key = f"directory:{dom}"
            home = f"https://{dom}/" if len(group) > 1 else lead["_url"]
            existing = db.conn.execute(
                "SELECT id, metadata_json FROM research_entities WHERE cohort='professional_newsroom' AND domain=? AND active=1 "
                "AND merged_into IS NULL AND source_key!=? ORDER BY id LIMIT 1", (dom, key)).fetchone()
            if existing:  # already in the universe (e.g. benchmark panel): attach the directory titles to it
                eid = existing[0]
                old = json.loads(existing[1] or "{}")
                old.update({k: v for k, v in meta.items() if v is not None and k != "research_priority"})
                if prio is not None:
                    old["research_priority"] = min(prio, old.get("research_priority") or prio)
                db.conn.execute("UPDATE research_entities SET metadata_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                (json.dumps(old), eid))
                stats["professional_attached_to_existing"] += 1
            else:
                db.conn.execute(
                    """INSERT INTO research_entities(cohort,source_key,name,homepage_url,domain,state,source,verification_status,metadata_json)
                       VALUES('professional_newsroom',?,?,?,?,?,?, 'directory',?)
                       ON CONFLICT(cohort,source_key) DO UPDATE SET name=excluded.name,homepage_url=excluded.homepage_url,
                         state=excluded.state,metadata_json=excluded.metadata_json,updated_at=CURRENT_TIMESTAMP""",
                    (key, name, home, dom, lead["State"], _source_label(lead), json.dumps(meta)))
                eid = db.conn.execute("SELECT id FROM research_entities WHERE cohort='professional_newsroom' AND source_key=?", (key,)).fetchone()[0]
            for g in group:
                db.conn.execute(
                    "INSERT INTO entity_sources(entity_id,source,source_key,source_url,membership_label,raw_json) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(entity_id,source,source_key) DO UPDATE SET raw_json=excluded.raw_json",
                    (eid, _source_label(g), g["Publication ID"], g["_url"], g["Publication"], json.dumps(g, default=str)))
            stats["professional_rows"] += len(group)
            stats["professional_organizations"] += 1
            stats["professional_merged_titles"] += len(group) - 1

        # ---------------- student: candidates for matched rows; direct entities for the rest
        for r, unitid, method, conf, note in student_entities:
            prio = priority_of(r.get("Research priority", ""))
            match_log.append({"id": r["Publication ID"], "publication": r["Publication"], "state": r["State"],
                              "institution_in_file": r.get("Institution / Affiliation", ""), "url": r["_url"],
                              "unitid": unitid or "", "method": method, "confidence": conf, "note": note,
                              "platform_hosted": r["_platform"]})
            if unitid and method != "heuristic":
                db.conn.execute(
                    """INSERT INTO publication_candidates(unitid,url,domain,title,snippet,source,query,score,score_reasons_json,
                         verification_json,verified_score)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(unitid,url) DO UPDATE SET title=excluded.title,source=excluded.source,score=MAX(score,excluded.score),
                         verified_score=MAX(COALESCE(verified_score,0),excluded.verified_score),score_reasons_json=excluded.score_reasons_json""",
                    (unitid, r["_url"], _host(r["_url"]), r["Publication"], f"Directory: {r.get('Source name')}",
                     f"directory:{_source_label(r)}", r["Publication ID"], conf,
                     json.dumps([f"directory_match:{method}", note] + (["platform_hosted"] if r["_platform"] else [])),
                     json.dumps({"directory_id": r["Publication ID"], "research_priority": prio, "match_method": method}), conf))
            else:
                key = f"directory:{r['Publication ID']}"
                db.conn.execute(
                    """INSERT INTO research_entities(cohort,source_key,name,homepage_url,domain,state,source,verification_status,metadata_json)
                       VALUES('student_media',?,?,?,?,?,?, 'directory_unmatched',?)
                       ON CONFLICT(cohort,source_key) DO UPDATE SET name=excluded.name,homepage_url=excluded.homepage_url,
                         metadata_json=excluded.metadata_json,updated_at=CURRENT_TIMESTAMP""",
                    (key, r["Publication"], r["_url"], _host(r["_url"]), r["State"], _source_label(r),
                     json.dumps({"research_priority": prio, "ipeds_match": method, "match_note": note, "directory_id": r["Publication ID"],
                                 "out_of_scope": method == "out_of_scope",
                                 "suggested_unitid": unitid if method == "heuristic" else None,
                                 "platform_hosted": r["_platform"], "excluded_from_ipeds_coverage": True})))
                eid = db.conn.execute("SELECT id FROM research_entities WHERE cohort='student_media' AND source_key=?", (key,)).fetchone()[0]
                db.conn.execute("INSERT OR IGNORE INTO entity_sources(entity_id,source,source_key,source_url,raw_json) VALUES(?,?,?,?,?)",
                                (eid, _source_label(r), r["Publication ID"], r["_url"], json.dumps(r, default=str)))

    result = {k: v for k, v in sorted(stats.items())}
    result["rows"] = len(rows)
    if promote:
        result["promotion"] = promote_candidates(db, threshold)
        result["student_entities_synced"] = sync_student_entities(db)["synced"]
        _propagate_student_metadata(db)
        result["secondary_student_publications"] = _secondary_entities(db)
    out = Path(report_dir or Path(path).parent) / (Path(path).stem + "_ipeds_matches.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    names = {r["unitid"]: r["name"] for r in db.execute("SELECT unitid, name FROM institutions")}
    with open(out, "w", newline="", encoding="utf-8") as f:
        cols = ["id", "publication", "state", "institution_in_file", "url", "method", "unitid", "matched_institution", "confidence",
                "note", "platform_hosted", "confirm (y/n)", "correct UNITID (if wrong or missing)"]
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        order = {"heuristic": 0, "ambiguous": 1, "unmatched": 2, "out_of_scope": 3, "name_fuzzy": 4, "name_campus": 5, "edu_domain": 6, "name": 7}
        for m_ in sorted((m for m in match_log if "method" in m), key=lambda m: (order.get(m["method"], 9), m["state"], m["publication"])):
            w.writerow({**m_, "matched_institution": names.get(m_["unitid"], "")})
    result["match_report"] = str(out)
    return result


def _secondary_entities(db: Database) -> int:
    """Matched directory papers that lost the primary slot to another paper at the same institution stay researchable."""
    rows = db.execute("""SELECT c.*, i.name AS institution FROM publication_candidates c JOIN institutions i ON i.unitid=c.unitid
                         WHERE c.source LIKE 'directory:%' AND c.status='candidate'
                           AND EXISTS (SELECT 1 FROM publications p WHERE p.unitid=c.unitid AND p.is_primary=1)""").fetchall()
    n = 0
    for c in rows:
        v = json.loads(c["verification_json"] or "{}")
        key = f"directory:{v.get('directory_id') or c['query']}"
        db.execute(
            """INSERT INTO research_entities(cohort,source_key,name,homepage_url,domain,parent_name,source,verification_status,metadata_json)
               VALUES('student_media',?,?,?,?,?,'directory','directory_secondary',?)
               ON CONFLICT(cohort,source_key) DO UPDATE SET metadata_json=excluded.metadata_json,updated_at=CURRENT_TIMESTAMP""",
            (key, c["title"], c["url"], c["domain"], c["institution"],
             json.dumps({"unitid": c["unitid"], "research_priority": v.get("research_priority"), "directory_id": v.get("directory_id"),
                         "ipeds_match": v.get("match_method"), "secondary_publication": True, "excluded_from_ipeds_coverage": True})))
        n += 1
    db.conn.commit()
    return n


def _propagate_student_metadata(db: Database) -> None:
    """Carry directory priority / ID / match method onto synced student research entities."""
    rows = db.execute(
        """SELECT re.id, c.verification_json FROM research_entities re
           JOIN publications p ON re.source_key='student_publication:' || p.id
           JOIN publication_candidates c ON c.id=p.candidate_id
           WHERE re.cohort='student_media' AND (c.source LIKE 'directory:%' OR c.source='directory_review')""").fetchall()
    for r in rows:
        v = json.loads(r["verification_json"] or "{}")
        db.execute("UPDATE research_entities SET metadata_json=json_set(metadata_json,'$.research_priority',?,'$.directory_id',?,"
                   "'$.ipeds_match',?) WHERE id=?", (v.get("research_priority"), v.get("directory_id"), v.get("match_method"), r["id"]))
    db.conn.commit()


def apply_match_review(db: Database, review_csv: Path, directory_csv: Path) -> dict:
    """Apply the reviewed match report: rows with confirm=y adopt the suggested UNITID; rows with a correct UNITID use it.

    Confirmed rows become publication candidates (source 'directory_review', confidence 0.9) and are promoted; the
    unmatched placeholder entity is retired into the promoted publication's entity.
    """
    from .discovery import promote_candidates
    from .research import sync_student_entities
    by_id = {r["Publication ID"]: r for r in read_directory(directory_csv)}
    valid = {r[0] for r in db.execute("SELECT unitid FROM institutions WHERE included=1")}
    applied = rejected = invalid = 0
    with open(review_csv, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            confirm = (row.get("confirm (y/n)") or "").strip().lower()
            correct = (row.get("correct UNITID (if wrong or missing)") or "").strip()
            unitid = correct or (row.get("unitid") if confirm.startswith("y") else "")
            if not unitid:
                rejected += int(confirm.startswith("n"))
                continue
            if unitid not in valid or row["id"] not in by_id:
                invalid += 1
                continue
            src = by_id[row["id"]]
            url = prepare_request_url(src["Website (historical or supplied)"])
            db.execute(
                """INSERT INTO publication_candidates(unitid,url,domain,title,snippet,source,query,score,score_reasons_json,verification_json,verified_score)
                   VALUES(?,?,?,?,?,'directory_review',?,0.9,?,?,0.9)
                   ON CONFLICT(unitid,url) DO UPDATE SET source='directory_review',score=0.9,verified_score=0.9""",
                (unitid, url, _host(url), src["Publication"], "Human-confirmed directory match", row["id"],
                 json.dumps(["human_confirmed_match"]),
                 json.dumps({"directory_id": row["id"], "research_priority": priority_of(src.get("Research priority", "")),
                             "match_method": "human_review"})))
            db.execute("UPDATE research_entities SET active=0 WHERE source_key=? AND cohort='student_media'", (f"directory:{row['id']}",))
            applied += 1
    db.conn.commit()
    promo = promote_candidates(db, 0.6)
    db.execute("UPDATE publications SET verification_status='human_verified' WHERE candidate_id IN "
               "(SELECT id FROM publication_candidates WHERE source='directory_review')")
    db.conn.commit()
    sync_student_entities(db)
    _propagate_student_metadata(db)
    return {"applied": applied, "rejected": rejected, "invalid_unitid_or_row": invalid, "promotion": promo}


# =========================================================================== homepage-based matching (free; no search credits)

_PHRASE = (r"(?:student[- ]run|student|independent|official|campus|college)\s+(?:news(?:paper)?|media|publication|voice|press|"
           r"news\s?site|magazine)\b[^.|]{0,90}?\b(?:of|at|for|serving|from)\s+(?:the\s+)?")


def _mentions(text_norm: str, inst: dict) -> tuple[int, bool]:
    names = {inst["norm"]} | {a for a in inst["aliases"] if len(a) >= 6 and " " in a}
    count, phrase = 0, False
    for n in names:
        if not n:
            continue
        count += len(re.findall(rf"\b{re.escape(n)}\b", text_norm))
        if re.search(_PHRASE + re.escape(n) + r"\b", text_norm) or re.search(rf"\b{re.escape(n)}(?: s)?\s+(?:student|independent)\s+news", text_norm):
            phrase = True
    return count, phrase


def classify_homepage(text: str, cands: list[dict]) -> tuple[dict | None, str, float, str]:
    norm = _norm_name(text)
    scored = []
    for c in cands:
        n, phrase = _mentions(norm, c)
        if n or phrase:
            scored.append((phrase, n, c))
    phrased = [x for x in scored if x[0]]
    if len(phrased) == 1:
        return phrased[0][2], "homepage_phrase", 0.85, f"homepage calls itself the student publication of {phrased[0][2]['name']}"
    scored.sort(key=lambda x: -x[1])
    if scored and scored[0][1] >= 2 and (len(scored) == 1 or scored[0][1] >= 2 * scored[1][1]):
        return scored[0][2], "homepage_mention", 0.6, f"homepage mentions {scored[0][2]['name']} {scored[0][1]}x (suggestion)"
    return None, "unmatched", 0.0, "homepage does not identify a single institution"


async def match_from_homepages(db: Database, settings, *, limit: int | None = None, fetcher=None, quiet: bool = False) -> dict:
    """Resolve unmatched / ambiguous / heuristic student directory papers by reading their own homepages."""
    from .fetch import HardenedFetcher
    from .progress import RunDashboard
    idx = InstitutionIndex(db)
    rows = db.execute("""SELECT * FROM research_entities WHERE cohort='student_media' AND active=1 AND source_key LIKE 'directory:%'
                         AND json_extract(metadata_json,'$.ipeds_match') IN ('unmatched','ambiguous','heuristic')
                         ORDER BY COALESCE(CAST(json_extract(metadata_json,'$.research_priority') AS INTEGER), 9), id""").fetchall()
    if limit:
        rows = rows[:limit]
    stats = defaultdict(int)
    own = fetcher is None
    fetcher = fetcher or HardenedFetcher(settings)
    sem = __import__("asyncio").Semaphore(max(1, settings.discovery_concurrency * 2))
    results: list[tuple] = []

    async def one(e):
        async with sem:
            r = await fetcher.fetch_safe(e["homepage_url"])
        meta = json.loads(e["metadata_json"] or "{}")
        meta["homepage_access"] = r.access_class
        if r.access_class != "ok" or not r.content:
            results.append((e, meta, None, "unreachable", 0.0, f"homepage {r.access_class}"))
            return
        from .extract import extract_main_text
        page = extract_main_text(r.content, r.content_type, r.final_url, r.headers)
        inst, method, conf, note = classify_homepage(f"{page.title} {page.full_text[:60000]}", idx.by_state.get((e["state"] or "").upper(), []))
        results.append((e, meta, inst, method, conf, note))

    with RunDashboard("NSMPA Directory homepage matching", len(rows), quiet=quiet) as dash:
        import asyncio as _asyncio
        for i in range(0, len(rows), 25):
            await _asyncio.gather(*(one(e) for e in rows[i:i + 25]))
            dash.update(completed=min(len(rows), i + 25))
    if own:
        await fetcher.close()
    with db.transaction():
        for e, meta, inst, method, conf, note in results:
            stats[method] += 1
            meta["homepage_match"] = {"method": method, "note": note, "unitid": inst["unitid"] if inst else None}
            if inst and method == "homepage_phrase":
                db.conn.execute(
                    """INSERT INTO publication_candidates(unitid,url,domain,title,snippet,source,query,score,score_reasons_json,verification_json,verified_score)
                       VALUES(?,?,?,?,?,'directory:homepage',?,?,?,?,?)
                       ON CONFLICT(unitid,url) DO UPDATE SET score=MAX(score,excluded.score),verified_score=MAX(COALESCE(verified_score,0),excluded.verified_score)""",
                    (inst["unitid"], e["homepage_url"], e["domain"], e["name"], note, meta.get("directory_id"), conf,
                     json.dumps(["directory_match:homepage_phrase"]),
                     json.dumps({"directory_id": meta.get("directory_id"), "research_priority": meta.get("research_priority"),
                                 "match_method": "homepage_phrase"}), conf))
                db.conn.execute("UPDATE research_entities SET active=0, metadata_json=? WHERE id=?", (json.dumps(meta), e["id"]))
            else:
                if inst:
                    meta["suggested_unitid"] = inst["unitid"]
                db.conn.execute("UPDATE research_entities SET metadata_json=? WHERE id=?", (json.dumps(meta), e["id"]))
    from .discovery import promote_candidates
    from .research import sync_student_entities
    promo = promote_candidates(db, settings.publication_confidence_threshold)
    sync_student_entities(db)
    _propagate_student_metadata(db)
    stats_out = dict(stats)
    stats_out.update(checked=len(rows), promotion=promo, secondary=_secondary_entities(db))
    return stats_out


# =========================================================================== export back into the user's template

STANCE_TEXT = {
    "SUPPORTS_RELIEF": "Permits relief (removal / de-indexing / anonymization)",
    "SUPPORTS_CHANGED_CIRCUMSTANCES": "Permits relief when circumstances change (e.g. dismissed charges)",
    "CASE_BY_CASE": "Case-by-case review of requests",
    "UPDATE_ONLY": "Updates / editor's notes only; no removal",
    "STRICT_ARCHIVE": "No removal (strict archive)",
    "MIXED": "Mixed / conflicting statements",
    "NO_RELEVANT_GUIDANCE": "No relevant written policy found (site inspected)",
    "UNDETERMINED": "Undetermined (not adequately inspected)",
}
ACCESS_TEXT = {"ok": "Active - website reachable", "not_found": "Website not found (404)", "gone": "Website removed (410)",
               "blocked": "Blocks automated access", "robots_disallowed": "Robots.txt disallows access",
               "network_error": "Unreachable (network error)", "timeout": "Unreachable (timeout)", "server_error": "Server error"}


def _entity_for_row(db: Database, row: dict):
    r = db.execute("""SELECT re.* FROM entity_sources es JOIN research_entities re ON re.id=es.entity_id
                      WHERE es.source_key=? ORDER BY re.active DESC LIMIT 1""", (row["Publication ID"],)).fetchone()
    if r:
        return r
    c = db.execute("SELECT * FROM publication_candidates WHERE query=? AND source LIKE 'directory:%' LIMIT 1", (row["Publication ID"],)).fetchone()
    if c:
        p = db.execute("SELECT id FROM publications WHERE unitid=? AND domain=? AND is_primary=1", (c["unitid"], c["domain"])).fetchone()
        if p:
            return db.execute("SELECT * FROM research_entities WHERE source_key=?", (f"student_publication:{p['id']}",)).fetchone()
    return None


def export_directory(db: Database, template: Path, out: Path) -> dict:
    rows = read_directory(template)
    filled = 0
    extra = ["NSMPA entity ID", "IPEDS UNITID", "IPEDS match method", "Relief mode", "Stance confidence", "Evidence count"]
    for row in rows:
        ent = _entity_for_row(db, row)
        if row["Category"].lower().startswith("student"):
            c = db.execute("SELECT unitid, verification_json FROM publication_candidates WHERE query=? LIMIT 1",
                           (row["Publication ID"],)).fetchone()
            row["IPEDS UNITID"] = c["unitid"] if c else ""
            row["IPEDS match method"] = json.loads(c["verification_json"] or "{}").get("match_method", "") if c else \
                (json.loads(ent["metadata_json"] or "{}").get("ipeds_match", "unmatched") if ent else "")
        if not ent:
            continue
        row["NSMPA entity ID"] = ent["id"]
        st = db.execute("""SELECT s.* FROM entity_stances s WHERE s.entity_id=? AND s.stance_version='0.3'
                           AND s.run_id NOT IN (SELECT id FROM research_runs WHERE status='excluded') ORDER BY s.id DESC LIMIT 1""",
                        (ent["id"],)).fetchone()
        home = db.execute("""SELECT access_class, fetched_at FROM research_pages WHERE entity_id=? AND page_kind='homepage'
                             ORDER BY id DESC LIMIT 1""", (ent["id"],)).fetchone()
        if home:
            row["Operating status"] = ACCESS_TEXT.get(home["access_class"], home["access_class"])
            row["Website verification"] = f"Automated check: {home['access_class']}"
            row["Verified on"] = (home["fetched_at"] or "")[:10]
        if not st:
            continue
        filled += 1
        pol = db.execute("""SELECT p.final_url FROM research_pages p WHERE p.entity_id=? AND p.run_id=? AND p.first_party=1
                            AND p.page_kind='policy' AND p.access_class='ok' ORDER BY p.id LIMIT 1""", (ent["id"], st["run_id"])).fetchone()
        rel = db.execute("""SELECT e.source_url FROM evidence_items e WHERE e.entity_id=? AND e.run_id=? AND e.first_party=1
                            AND e.statement_type IN ('relief_permitted','changed_circumstance_relief','case_by_case','relief_rejected',
                              'relief_narrow_exceptions','update_remedy','changed_circumstance_update')
                            ORDER BY e.authority_score DESC LIMIT 1""", (ent["id"], st["run_id"])).fetchone()
        tech = db.execute("""SELECT SUM(statement_type='technical_noindex') ni, SUM(statement_type LIKE 'wayback_%' AND direction='supportive') wb,
                                    COUNT(*) n FROM evidence_items WHERE entity_id=? AND run_id=? AND evidence_class='technical'""",
                          (ent["id"], st["run_id"])).fetchone()
        sampled = db.scalar("SELECT COUNT(*) FROM research_pages WHERE entity_id=? AND run_id=? AND page_kind='crime_article' AND access_class='ok'",
                            (ent["id"], st["run_id"]))
        row["Editorial policy URL"] = pol["final_url"] if pol else row.get("Editorial policy URL", "")
        row["Removal / deindexing policy URL"] = rel["source_url"] if rel else row.get("Removal / deindexing policy URL", "")
        row["Removal / deindexing stance"] = STANCE_TEXT.get(st["stance"], st["stance"])
        row["Policy review status"] = ("Human reviewed: " + st["review_status"]) if st["review_status"] != "unreviewed" else "Automated - not yet human reviewed"
        row["Article noindex evidence"] = (f"Targeted noindex on {tech['ni'] or 0} archived crime article(s); "
                                           f"{tech['wb'] or 0} archive change(s)") if tech and tech["n"] else "None observed"
        row["Article audit status"] = f"{sampled} crime/arrest article(s) sampled" if sampled else "Not sampled"
        row["Last policy review"] = (st["created_at"] or "")[:10]
        row["Notes"] = (st["rationale"] or "")[:500] + " [NSMPA automated; verify before citing]"
        row["Relief mode"] = st["relief_mode"] or ""
        row["Stance confidence"] = f"{st['confidence']:.2f}"
        row["Evidence count"] = st["evidence_count"]
    cols = list(read_directory(template)[0].keys()) + extra if rows else extra
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: v for k, v in row.items() if not k.startswith("_")})
    return {"rows": len(rows), "with_findings": filled, "out": str(out)}

from __future__ import annotations

import csv
import json

from conftest import make_settings
from nsmpa.directory import export_directory, import_directory
from nsmpa.research import select_entities
import test_pipeline as tp

COLS = ["Publication ID", "Publication", "Category", "State", "City", "Institution / Affiliation", "Website (historical or supplied)",
        "Domain", "Ownership", "Operating status", "Website verification", "Verified on", "Source year", "Source name", "Source URL",
        "Source detail", "Research priority", "Editorial policy URL", "Removal / deindexing policy URL", "Removal / deindexing stance",
        "Policy review status", "Article noindex evidence", "Article audit status", "Last policy review", "Notes"]

ROWS = [
    ("NP-1", "The Easterner Online", "Student", "WA", "Eastern Washington University", "https://easterneronline.com", "1 | Easterner focus"),
    ("NP-2", "Daily Evergreen", "Student", "WA", "", "https://dailyevergreen.wsu.edu", "2 | WA student peer"),
    ("NP-3", "The Observer", "Student", "WA", "", "https://cwuobserver.com", "2 | WA student peer"),
    ("NP-4", "The Spectator Online", "Student", "WA", "", "https://seattlespectator.com", "2 | WA student peer"),
    ("NP-5", "Mystery Paper", "Student", "WA", "", "https://mysterypaper.com", "3 | Student press"),
    ("NP-6", "Concord", "Student", "WA", "", "https://theconcordonline.wordpress.com", "3 | Student press"),
    ("NP-7", "Austin American Statesman", "Professional", "TX", "", "https://statesman.com", "5 | Professional"),
    ("NP-8", "Lake Travis View", "Professional", "TX", "", "https://statesman.com/news/local/lake-travis-view", "5 | Professional"),
    ("NP-9", "Allen American", "Professional", "TX", "", "https://starlocalmedia.com/allenamerican", "5 | Professional"),
    ("NP-10", "Coppell Gazette", "Professional", "TX", "", "https://starlocalmedia.com/coppellgazette", "5 | Professional"),
    ("NP-11", "Strict Times", "Professional", "WA", "", "https://strict.example/", "4 | WA professional"),
    ("NP-12", "Relief Daily", "Professional", "WA", "", "https://relief.example/", "4 | WA professional"),
    ("NP-13", "The Pitt News", "Student", "WA", "", "https://pittnews.com", "3 | Student press"),
    ("NP-14", "Rocky Mountain Collegian", "Student", "WA", "Colorado State University", "https://collegian.com", "3 | Student press"),
    ("NP-16", "Campus Press", "Student", "WA", "", "https://camdencc.edu/studentlife/campuspress", "3 | Student press"),
    ("NP-15", "Huskies Daily", "Student", "WA", "Washington State University", "https://huskiesdaily.com", "3 | Student press"),
]
INSTITUTIONS = [("235097", "Eastern Washington University", "https://www.ewu.edu/"),
                ("236939", "Washington State University", "https://wsu.edu/"),
                ("234827", "Central Washington University", "https://www.cwu.edu/"),
                ("236595", "Seattle University", "https://www.seattleu.edu/"),
                ("236577", "Seattle Pacific University", "https://spu.edu/"),
                ("999001", "Pennsylvania Institute of Technology", "https://pit.edu/"),
                ("999002", "Colorado Mountain College", "https://coloradomtn.edu/"),
                ("999003", "Washington State University Global Campus", "https://online.wsu.edu/")]


def write_dir(path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for pid, pub, cat, st, inst, url, prio in ROWS:
            w.writerow({"Publication ID": pid, "Publication": pub, "Category": cat, "State": st, "Institution / Affiliation": inst,
                        "Website (historical or supplied)": url, "Domain": url.split("/")[2], "Source year": "2018",
                        "Source name": "Leon Yin Local News Dataset (2018)", "Research priority": prio,
                        "Removal / deindexing stance": "Unknown - not investigated"})


def seed(db):
    for unitid, name, web in INSTITUTIONS:
        db.execute("INSERT INTO institutions(unitid,name,state,website,control,level,included) VALUES(?,?,?,?,1,1,1)", (unitid, name, "WA", web))
    db.execute("INSERT INTO institutions(unitid,name,state,website,control,level,included) VALUES('999009','Camden County College','WA','https://camdencc.edu/',1,2,0)")
    db.execute("INSERT INTO institutions(unitid,name,state,website,control,level,included) VALUES('999010','Camden University','WA','https://camdenu.edu/',1,1,1)")
    db.conn.commit()


def test_import_matches_merges_and_flags(tmp_path, db):
    seed(db)
    p = tmp_path / "dir.csv"
    write_dir(p)
    res = import_directory(db, p)
    m = {r["id"]: r for r in csv.DictReader(open(res["match_report"], encoding="utf-8"))}
    assert (m["NP-1"]["method"], m["NP-1"]["unitid"]) == ("name", "235097")
    assert (m["NP-2"]["method"], m["NP-2"]["unitid"]) == ("edu_domain", "236939")
    assert (m["NP-3"]["method"], m["NP-3"]["unitid"]) == ("heuristic", "234827")
    assert not db.execute("SELECT 1 FROM publications WHERE unitid='234827'").fetchone()        # suggestion only, not promoted
    assert m["NP-13"]["unitid"] != "999001"                                                    # "pit"+"tnews" is not a masthead match
    assert m["NP-14"]["method"] == "unmatched" and m["NP-14"]["unitid"] == ""                  # named school absent: never guessed
    assert (m["NP-15"]["method"], m["NP-15"]["unitid"]) == ("name", "236939")
    assert m["NP-16"]["method"] == "out_of_scope" and "Camden County College" in m["NP-16"]["note"]                  # flagship, not the Global Campus
    assert m["NP-4"]["method"] == "ambiguous" and m["NP-4"]["unitid"] == ""      # Seattle U vs Seattle Pacific: not guessed
    assert m["NP-5"]["method"] == "unmatched" and m["NP-6"]["platform_hosted"] == "True"
    pubs = {r["unitid"]: r for r in db.execute("SELECT * FROM publications WHERE is_primary=1")}
    assert pubs["235097"]["domain"] == "easterneronline.com" and "directory" in pubs["235097"]["discovery_method"]
    # unmatched student papers are researchable but excluded from IPEDS coverage
    um = db.execute("SELECT * FROM research_entities WHERE source_key='directory:NP-5'").fetchone()
    assert json.loads(um["metadata_json"])["excluded_from_ipeds_coverage"] is True
    # professional: editions on one website merged into one organization, every title kept as provenance
    pro = {r["domain"]: r for r in db.execute("SELECT * FROM research_entities WHERE cohort='professional_newsroom' AND active=1")}
    assert pro["statesman.com"]["name"] == "Austin American Statesman"
    assert pro["starlocalmedia.com"]["name"] == "starlocalmedia.com (2 titles)"
    eid = pro["starlocalmedia.com"]["id"]
    assert db.scalar("SELECT COUNT(*) FROM entity_sources WHERE entity_id=?", (eid,)) == 2
    assert res["professional_merged_titles"] == 2


def test_research_order_follows_directory_priority(tmp_path, db):
    seed(db)
    p = tmp_path / "dir.csv"
    write_dir(p)
    import_directory(db, p)
    order = [r["name"] for r in select_entities(db, None, None)]
    assert order[0] == "The Easterner Online"
    assert set(order[1:4]) == {"Daily Evergreen", "The Observer", "The Spectator Online"}
    ev = db.execute("SELECT * FROM research_entities WHERE name='Daily Evergreen'").fetchone()
    assert json.loads(ev["metadata_json"])["secondary_publication"] is True   # second WSU paper kept, not dropped
    top = select_entities(db, None, None, max_priority=2)
    assert {r["name"] for r in top} == {"The Easterner Online", "Daily Evergreen", "The Observer", "The Spectator Online"}


def test_reimport_is_idempotent(tmp_path, db):
    seed(db)
    p = tmp_path / "dir.csv"
    write_dir(p)
    import_directory(db, p)
    n1 = db.scalar("SELECT COUNT(*) FROM research_entities")
    import_directory(db, p)
    assert db.scalar("SELECT COUNT(*) FROM research_entities") == n1


async def test_export_fills_template_columns(tmp_path, db):
    seed(db)
    p = tmp_path / "dir.csv"
    write_dir(p)
    import_directory(db, p)
    s = make_settings(tmp_path)
    rid, _ = await tp.run_research(db, s, tp.FakeSearch(tp.SEARCH_ROUTES))
    res = export_directory(db, p, tmp_path / "out.csv")
    rows = {r["Publication ID"]: r for r in csv.DictReader(open(tmp_path / "out.csv", encoding="utf-8"))}
    assert res["with_findings"] >= 2
    assert rows["NP-11"]["Removal / deindexing stance"].startswith("Updates / editor's notes only")
    assert rows["NP-12"]["Removal / deindexing stance"].startswith("Permits relief when circumstances change")
    assert rows["NP-11"]["Editorial policy URL"].startswith("https://strict.example/")
    assert rows["NP-11"]["Operating status"] == "Active - website reachable" and "verify" in rows["NP-11"]["Notes"]
    assert rows["NP-1"]["IPEDS UNITID"] == "235097" and list(rows["NP-1"].keys())[:25] == COLS   # original columns preserved, in order



def test_review_loop_applies_confirmations(tmp_path, db):
    from nsmpa.directory import apply_match_review
    seed(db)
    p = tmp_path / "dir.csv"
    write_dir(p)
    res = import_directory(db, p, report_dir=tmp_path)
    rows = list(csv.DictReader(open(res["match_report"], encoding="utf-8")))
    for r in rows:
        if r["id"] == "NP-3":
            r["confirm (y/n)"] = "y"                                   # accept the CWU suggestion
        if r["id"] == "NP-4":
            r["correct UNITID (if wrong or missing)"] = "236595"        # resolve the Seattle ambiguity by hand
        if r["id"] == "NP-5":
            r["correct UNITID (if wrong or missing)"] = "000000"        # invalid id is reported, not applied
    edited = tmp_path / "edited.csv"
    with open(edited, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    out = apply_match_review(db, edited, p)
    assert out["applied"] == 2 and out["invalid_unitid_or_row"] == 1
    pubs = {r["unitid"]: r for r in db.execute("SELECT * FROM publications WHERE is_primary=1")}
    assert pubs["234827"]["verification_status"] == "human_verified" and pubs["236595"]["domain"] == "seattlespectator.com"
    assert db.scalar("SELECT active FROM research_entities WHERE source_key='directory:NP-3'") == 0



def test_classify_homepage_phrase_and_mentions():
    from nsmpa.directory import InstitutionIndex, classify_homepage

    class FakeDB:
        def execute(self, sql, *a):
            return [{"unitid": "1", "name": "The Ohio State University", "state": "OH", "website": "https://osu.edu", "alias": "OSU", "size": 5},
                    {"unitid": "2", "name": "University of Michigan-Ann Arbor", "state": "OH", "website": "https://umich.edu", "alias": "", "size": 5}]
    idx = InstitutionIndex.__new__(InstitutionIndex)
    InstitutionIndex.__init__(idx, FakeDB())
    cands = idx.by_state["OH"]
    inst, method, conf, _ = classify_homepage("The Lantern | The student voice of The Ohio State University since 1881. "
                                              "Football: Ohio State beats University of Michigan-Ann Arbor.", cands)
    assert (inst["unitid"], method) == ("1", "homepage_phrase")
    inst, method, *_ = classify_homepage("Sports roundup: University of Michigan-Ann Arbor and The Ohio State University tie.", cands)
    assert inst is None and method == "unmatched"   # rival-school sports mentions are not evidence


async def test_match_from_homepages_resolves_unmatched(tmp_path, db):
    import httpx
    from conftest import public_resolver
    from nsmpa.directory import match_from_homepages
    from nsmpa.fetch import HardenedFetcher
    seed(db)
    p = tmp_path / "dir.csv"
    write_dir(p)
    import_directory(db, p, report_dir=tmp_path)
    pages = {"https://mysterypaper.com/": "<html><title>Mystery Paper</title><body><footer>The independent student newspaper of "
                                         "Seattle Pacific University.</footer></body></html>"}

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        body = pages.get(str(req.url))
        return httpx.Response(200 if body else 404, headers={"content-type": "text/html"}, content=(body or "nf").encode())

    s = make_settings(tmp_path)
    res = await match_from_homepages(db, s, fetcher=HardenedFetcher(s, transport=httpx.MockTransport(handler), resolver=public_resolver),
                                     quiet=True)
    assert res["homepage_phrase"] == 1 and res.get("unreachable", 0) >= 1
    pub = db.execute("SELECT * FROM publications WHERE unitid='236577'").fetchone()
    assert pub and pub["domain"] == "mysterypaper.com"

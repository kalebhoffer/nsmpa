from __future__ import annotations

import plistlib

import httpx

from conftest import make_settings, public_resolver
from nsmpa.fetch import HardenedFetcher
from nsmpa.recheck import compare_texts, launchd_plist, run_recheck, write_plist
import test_pipeline as tp


async def test_recheck_detects_policy_change(tmp_path, db, monkeypatch):
    s = make_settings(tmp_path)
    tp.seed_newsrooms(tmp_path, db)
    await tp.run_research(db, s, tp.FakeSearch(tp.SEARCH_ROUTES))
    f = HardenedFetcher(s, transport=httpx.MockTransport(tp.handler), resolver=public_resolver)
    first = await run_recheck(db, s, quiet=True, fetcher=f)
    assert first["watching"] >= 2 and first["changed"] == 0
    monkeypatch.setitem(tp.WEB, "https://strict.example/ethics-policy/", (200, tp.PAGE.format(title="Ethics Policy", body=(
        "<p>We do not remove stories from our archive. In rare cases editors may de-index an article when charges were "
        "later dismissed.</p>"))))
    f2 = HardenedFetcher(s, transport=httpx.MockTransport(tp.handler), resolver=public_resolver)
    second = await run_recheck(db, s, quiet=True, fetcher=f2)
    assert second["changed"] == 1
    ch = db.execute("SELECT * FROM policy_changes").fetchone()
    assert ch["added_relief_statements"] >= 1 and ch["similarity"] < 1
    assert db.scalar("SELECT COUNT(*) FROM review_queue WHERE item_type='policy_change'") == 1


async def test_recheck_inaccessible_is_not_a_change(tmp_path, db):
    s = make_settings(tmp_path)
    tp.seed_newsrooms(tmp_path, db)
    await tp.run_research(db, s, tp.FakeSearch(tp.SEARCH_ROUTES))
    down = HardenedFetcher(s, transport=httpx.MockTransport(lambda r: httpx.Response(503)), resolver=public_resolver)
    res = await run_recheck(db, s, quiet=True, fetcher=down)
    assert res["changed"] == 0 and res["inaccessible"] == res["checked"] > 0


def test_compare_texts_counts_statement_changes():
    old = "We do not remove stories from our archive."
    new = "We do not remove stories from our archive. Editors may remove a name from an article when charges were dismissed."
    c = compare_texts(old, new)
    assert c["added"] == 1 and c["removed"] == 0 and c["similarity"] < 1


def test_launchd_plist(tmp_path):
    p = launchd_plist(tmp_path, tmp_path / "config.yml", weekday=2, hour=4)
    assert p["ProgramArguments"][1:3] == ["recheck", "--quiet"] and p["StartCalendarInterval"] == {"Weekday": 2, "Hour": 4, "Minute": 0}
    path = write_plist(p, tmp_path / "x.plist")
    assert plistlib.loads(path.read_bytes())["Label"] == "com.nsmpa.recheck"

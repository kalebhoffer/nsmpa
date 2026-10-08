from __future__ import annotations

import json

import httpx

from conftest import make_settings, public_resolver
from nsmpa.estimate import estimate_all
from nsmpa.fetch import HardenedFetcher
from nsmpa.pilot import run_pilot
import test_pipeline as tp
from test_v05 import FakeAI


def test_estimate_counts_cached_queries_as_free(tmp_path, db):
    s = make_settings(tmp_path)
    tp.seed_newsrooms(tmp_path, db)
    before = estimate_all(db, s, cohort="professional_newsroom")
    assert before["research"]["credits_low"] == 4 * (3 + 4)  # tier 1 + practice queries (dig=always), none cached
    from nsmpa.research import TIER1, render_query
    from nsmpa.search import cache_key
    e = db.execute("SELECT * FROM research_entities WHERE name='Strict Times'").fetchone()
    for q in TIER1:
        db.execute("INSERT INTO search_cache(provider,query_hash,query,count_requested,response_json) VALUES('serper',?,?,8,'[]')",
                   (cache_key(render_query(q, e), 8), render_query(q, e)))
    db.conn.commit()
    after = estimate_all(db, s, cohort="professional_newsroom")
    assert after["research"]["credits_low"] == before["research"]["credits_low"] - 3
    assert after["research"]["queries_already_cached"] == 3
    assert after["research"]["credits_high"] >= after["research"]["credits_low"]
    assert "usd_high" not in after["ai_review"]  # no invented prices


def test_estimate_dollars_only_when_prices_configured(tmp_path, db):
    s = make_settings(tmp_path, serper_usd_per_credit=0.001, ai_usd_per_million_input=0.3, ai_usd_per_million_output=2.5)
    tp.seed_newsrooms(tmp_path, db)
    res = estimate_all(db, s, cohort="professional_newsroom")
    assert "serper_usd_high" in res and "usd_high" in res["ai_review"]


async def test_pilot_end_to_end_offline(tmp_path, db):
    s = make_settings(tmp_path)
    tp.seed_newsrooms(tmp_path, db)
    db.execute("INSERT INTO institutions(unitid,name,city,state,website,control,level,included,raw_json) VALUES(?,?,?,?,?,?,?,1,?)",
               ("900001", "State University", "Capital", "WA", "https://www.state.edu/", 1, 1, json.dumps({"C21BASIC": "16"})))
    db.conn.commit()
    ff = lambda settings: HardenedFetcher(settings, transport=httpx.MockTransport(tp.handler), resolver=public_resolver)  # noqa: E731
    rep = await run_pilot(db, s, budget=90, ai_calls=5, support=0, newsrooms=4, schools=1, unitids=["900001"], quiet=True,
                          provider=tp.FakeSearch(tp.SEARCH_ROUTES), fetcher_factory=ff, ai_client=FakeAI())
    status = {x["step"]: x["status"] for x in rep["steps"]}
    assert status["discovery"] == "ok" and status["research"] == "ok" and status["outputs"] == "ok"
    assert status["ai_review"] in {"ok", "warning"} and status["accuracy_sample"] == "ok"
    assert rep["credits_spent"] <= 90
    names = {x["name"] for x in rep["stances"]}
    assert "The State Lantern" in names and "Strict Times" in names
    md = open(f"{rep['out_dir']}/pilot_report.md").read()
    assert "Is it working?" in md and "Strongest opposing excerpts" in md



async def test_pilot_budget_exhaustion_is_explained(tmp_path, db):
    s = make_settings(tmp_path)
    tp.seed_newsrooms(tmp_path, db)
    ff = lambda settings: HardenedFetcher(settings, transport=httpx.MockTransport(tp.handler), resolver=public_resolver)  # noqa: E731
    rep = await run_pilot(db, s, budget=8, ai_calls=0, support=0, newsrooms=4, schools=0, quiet=True,
                          provider=tp.FakeSearch(tp.SEARCH_ROUTES), fetcher_factory=ff)
    research = next(x for x in rep["steps"] if x["step"] == "research")
    assert research["status"] == "warning" and "search budget" in research["detail"] and rep["credits_spent"] <= 8

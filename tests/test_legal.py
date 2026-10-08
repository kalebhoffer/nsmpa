from __future__ import annotations

import httpx
import pytest
from conftest import make_settings, public_resolver
from test_pipeline import PAGE, FakeSearch

from nsmpa.fetch import HardenedFetcher
from nsmpa.legal import best_excerpt, run_legal_research, seed_legal, set_legal_status

GDPR = PAGE.format(title="Regulation (EU) 2016/679", body=(
    "<p>Article 17. Right to erasure ('right to be forgotten'). The data subject shall have the right to obtain from the "
    "controller the erasure of personal data concerning him or her without undue delay.</p>"))
WA = PAGE.format(title="RCW 9.94A.640", body=(
    "<p>Every offender who has been discharged may apply to the sentencing court for a vacation of the offender's record "
    "of conviction, and the conviction shall be vacated and released from all penalties.</p>"))


def handler(req):
    if req.url.path == "/robots.txt":
        return httpx.Response(404)
    body = {"https://eur-lex.europa.eu/eli/reg/2016/679/oj": GDPR, "https://app.leg.wa.gov/rcw/9.94A.640": WA}.get(str(req.url))
    return httpx.Response(200 if body else 404, headers={"content-type": "text/html"}, content=(body or "nf").encode())


def test_seed_has_core_and_all_states(db):
    n = seed_legal(db)["legal_leads"]
    assert n == 7 + 51
    assert db.scalar("SELECT citation FROM legal_context WHERE key='us_martin_v_hearst_2015'") == "777 F.3d 546 (2d Cir. 2015)"
    assert db.scalar("SELECT COUNT(*) FROM legal_context WHERE key LIKE 'state_%' AND citation IS NOT NULL") == 0  # no invented citations


async def test_legal_research_finds_sources_never_self_verifies(tmp_path, db):
    s = make_settings(tmp_path)
    f = HardenedFetcher(s, transport=httpx.MockTransport(handler), resolver=public_resolver)
    routes = {"RCW 9.94A.640": [("https://app.leg.wa.gov/rcw/9.94A.640", "RCW 9.94A.640", "vacation")]}
    res = await run_legal_research(db, s, keys=["eu_gdpr_art17", "wa_vacation_statutes", "us_clean_slate_laws"], quiet=True,
                                   provider=FakeSearch(routes), fetcher=f)
    st = {r["key"]: r for r in db.execute("SELECT * FROM legal_context")}
    assert st["eu_gdpr_art17"]["status"] == "sources_found" and "erasure" in st["eu_gdpr_art17"]["best_excerpt"]
    assert st["wa_vacation_statutes"]["status"] == "sources_found" and "authoritative" in st["wa_vacation_statutes"]["status_note"]
    assert st["us_clean_slate_laws"]["status"] == "not_found"
    assert "human_verified" not in {r["status"] for r in st.values()} and res["with_sources"] == 2
    set_legal_status(db, "eu_gdpr_art17", "human_verified", "read EUR-Lex text")
    with pytest.raises(ValueError):
        set_legal_status(db, "nope", "human_verified")


def test_best_excerpt_prefers_legal_sentences():
    assert "expunged" in best_excerpt("Welcome to the site. Records of dismissed charges may be expunged upon petition to the court. Contact us.")
    assert best_excerpt("Nothing relevant here at all, just a homepage.") is None

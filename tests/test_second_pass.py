"""Second pass over undetermined organizations: unlinked policy pages, transient failures, and selective --fresh."""
from __future__ import annotations

import httpx
from conftest import make_settings, public_resolver
from test_pipeline import PAGE, FakeSearch, stances

from nsmpa.fetch import HardenedFetcher
from nsmpa.research import import_entities_csv, research_all, start_research
from nsmpa.runs import item_counts

POLICY = ("<p>We will consider removing a name from an old arrest story when the charges were later dismissed "
          "or the record was expunged.</p>")
WEB = {
    # Policy page exists but nothing links to it and search finds nothing.
    "https://hidden.example/": (200, PAGE.format(title="Hidden Herald", body="<p>Local news, sports and weather.</p>")),
    "https://hidden.example/ethics": (200, PAGE.format(title="Ethics | Hidden Herald", body=POLICY)),
    # Policy page listed in the sitemap under an unguessable path.
    "https://mapped.example/": (200, PAGE.format(title="Mapped Mercury", body="<p>Local news, sports and weather.</p>")),
    "https://mapped.example/sitemap.xml": (200, "<urlset><url><loc>https://mapped.example/our-standards-and-practices/</loc></url>"
                                                "<url><loc>https://mapped.example/2024/01/02/council-meets/</loc></url></urlset>"),
    "https://mapped.example/our-standards-and-practices/": (200, PAGE.format(title="Standards | Mapped Mercury", body=POLICY)),
    # Policy page that is down on the first pass.
    "https://flaky.example/": (200, PAGE.format(title="Flaky Ledger", body="<a href='/policies'>Our policies</a>")),
    "https://flaky.example/policies": (200, PAGE.format(title="Policies | Flaky Ledger", body=POLICY)),
}
DOWN: set[str] = set()


def handler(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/robots.txt":
        return httpx.Response(404)
    key = str(req.url)
    if key in DOWN:
        return httpx.Response(503, content=b"unavailable")
    if key in WEB:
        status, body = WEB[key]
        ctype = "application/xml" if key.endswith(".xml") else "text/html; charset=utf-8"
        return httpx.Response(status, headers={"content-type": ctype}, content=body.encode())
    return httpx.Response(404, headers={"content-type": "text/html"}, content=b"not found")


def seed(tmp_path, db):
    p = tmp_path / "n.csv"
    p.write_text("name,url\nHidden Herald,https://hidden.example/\nMapped Mercury,https://mapped.example/\n"
                 "Flaky Ledger,https://flaky.example/\n")
    import_entities_csv(db, p, "professional_newsroom", "t")


async def run(db, settings, run_id=None, **kw):
    rid, _ = start_research(db, settings, cohort="professional_newsroom", limit=None, run_id=run_id, max_searches=None,
                            command="test")
    fetcher = HardenedFetcher(settings, transport=httpx.MockTransport(handler), resolver=public_resolver)
    stats = await research_all(db, settings, rid, cohort="professional_newsroom", quiet=True, provider=FakeSearch(),
                               fetcher=fetcher, **kw)
    return rid, stats


async def test_unlinked_policy_pages_need_the_probe(tmp_path, db):
    seed(tmp_path, db)
    DOWN.clear()
    rid, _ = await run(db, make_settings(tmp_path, research_probe_policy_paths=False, ai_gate_enabled=False))
    before = stances(db, rid)
    assert before["Hidden Herald"] in {"UNDETERMINED", "NO_RELEVANT_GUIDANCE"}
    assert before["Mapped Mercury"] in {"UNDETERMINED", "NO_RELEVANT_GUIDANCE"}
    rid2, _ = await run(db, make_settings(tmp_path, research_probe_policy_paths=True, ai_gate_enabled=False))
    after = stances(db, rid2)
    assert after["Hidden Herald"] == "SUPPORTS_CHANGED_CIRCUMSTANCES"      # found at a common path
    assert after["Mapped Mercury"] == "SUPPORTS_CHANGED_CIRCUMSTANCES"     # found in the sitemap
    probed = {r["url"] for r in db.execute("SELECT url FROM research_targets WHERE run_id=? AND topic='probe_path'", (rid2,))}
    assert not any("mapped.example" in u for u in probed)                 # sitemap hit: no guessing


async def test_retry_refetches_transient_failures_and_fresh_touches_only_selected(tmp_path, db):
    seed(tmp_path, db)
    DOWN.clear()
    DOWN.add("https://flaky.example/policies")
    s = make_settings(tmp_path, ai_gate_enabled=False)
    rid, _ = await run(db, s)
    assert stances(db, rid)["Flaky Ledger"] == "UNDETERMINED"
    DOWN.clear()
    flaky = db.scalar("SELECT id FROM research_entities WHERE name='Flaky Ledger'")
    # Without retry the failed page is remembered and not read again.
    await run(db, s, run_id=rid, fresh=True, entity_ids=[flaky])
    assert stances(db, rid)["Flaky Ledger"] == "UNDETERMINED"
    rid_again, stats = await run(db, make_settings(tmp_path, ai_gate_enabled=False, research_retry_failed=True),
                                 run_id=rid, fresh=True, entity_ids=[flaky])
    assert rid_again == rid and stats["status"] == "completed" and stats["entities"] == 1
    assert stances(db, rid)["Flaky Ledger"] == "SUPPORTS_CHANGED_CIRCUMSTANCES"
    assert item_counts(db, rid, "entity").get("pending", 0) == 0          # the other entities stayed done

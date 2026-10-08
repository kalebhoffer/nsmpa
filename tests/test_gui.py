from __future__ import annotations

import json
import re
import threading
import urllib.request
from urllib.error import HTTPError

import pytest
import test_pipeline as tp
from conftest import make_settings

from nsmpa.gui import make_server, write_dashboard


@pytest.fixture
async def gui(tmp_path, db):
    s = make_settings(tmp_path)
    tp.seed_newsrooms(tmp_path, db)
    rid, _ = await tp.run_research(db, s, tp.FakeSearch(tp.SEARCH_ROUTES))
    httpd, token = make_server(s, port=0)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield s, port, token, db, rid
    httpd.shutdown()
    httpd.server_close()


def req(port, path, *, method="GET", body=None, headers=None, host=None):
    h = {"Host": host or f"127.0.0.1:{port}", **(headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    if data:
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read().decode(), dict(resp.headers)
    except HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


async def test_gui_api_endpoints(gui):
    _s, port, token, db, rid = gui
    st, html, hdr = req(port, "/")
    assert st == 200 and "NSMPA Monitor" in html and token in html
    assert "frame-ancestors 'none'" in hdr["Content-Security-Policy"]
    st, body, _ = req(port, "/api/overview")
    o = json.loads(body)
    assert o["counts"]["researched"] == 4 and any(h["run_id"] == rid for h in o["heartbeats"])
    res = json.loads(req(port, "/api/results")[1])
    assert any(c["cohort"] == "professional_newsroom" and c["researched"] == 4 for c in res["cohorts"])
    ev = json.loads(req(port, "/api/evidence?direction=adverse")[1])
    assert ev["total"] > 0 and all(r["direction"] == "adverse" for r in ev["rows"])
    for path in ("/api/review", "/api/ops", "/api/insights"):
        assert req(port, path)[0] == 200
    eid = db.scalar("SELECT id FROM research_entities WHERE name='Strict Times'")
    ent = json.loads(req(port, f"/api/entity/{eid}")[1])
    assert ent["stance"]["stance"] == "UPDATE_ONLY"


async def test_gui_security_guards(gui):
    _s, port, token, db, _rid = gui
    assert req(port, "/api/overview", host="evil.example")[0] == 403          # DNS-rebinding guard
    item = json.loads(req(port, "/api/review")[1])["items"][0]
    assert req(port, f"/api/review/{item['id']}", method="POST", body={"decision": "accept"})[0] == 403   # no token
    assert req(port, f"/api/review/{item['id']}", method="POST", body={"decision": "accept"},
               headers={"X-NSMPA-Token": "wrong"})[0] == 403
    st, body, _ = req(port, f"/api/review/{item['id']}", method="POST", body={"decision": "accept", "note": "ok"},
                      headers={"X-NSMPA-Token": token})
    assert st == 200 and json.loads(body)["ok"]
    assert db.scalar("SELECT status FROM review_queue WHERE id=?", (item["id"],)) == "accepted"


async def test_offline_dashboard_file(gui, tmp_path):
    s, _port, _token, db, _rid = gui
    out = write_dashboard(db, s, tmp_path / "dash.html")
    html = out.read_text()
    assert '"static": true' in html and "Strict Times" in html
    assert not re.search(r"<(?:script|link)[^>]+(?:src|href)=[\"']https?:", html)   # no external loads
    assert "</script><script" not in html.split("const DATA =")[1][:200]


async def test_verify_queue_snapshot_and_decision(gui):
    _s, port, token, db, _rid = gui
    q = json.loads(req(port, "/api/verify?kind=evidence")[1])
    assert q["items"] and q["counts"].get("unverified", 0) >= len(q["items"])
    first = q["items"][0]
    assert first["drives_stance"] == 1  # stance-driving excerpts come first
    st, html, hdr = req(port, f"/snapshot/evidence/{first['id']}")
    assert st == 200 and "<mark id=hit>" in html and "<script" not in html.lower()
    assert hdr["X-Frame-Options"] == "SAMEORIGIN" and "default-src 'none'" in hdr["Content-Security-Policy"]
    assert req(port, f"/api/verify/evidence/{first['id']}", method="POST", body={"status": "verified"})[0] == 403  # token required
    assert req(port, f"/api/verify/evidence/{first['id']}", method="POST", body={"status": "approved"},
               headers={"X-NSMPA-Token": token})[0] == 400  # only verified|rejected|disputed
    st, _body, _ = req(port, f"/api/verify/evidence/{first['id']}", method="POST", body={"status": "verified", "note": "matches live page"},
                      headers={"X-NSMPA-Token": token})
    assert st == 200 and db.scalar("SELECT verification_status FROM evidence_items WHERE id=?", (first["id"],)) == "verified"
    assert req(port, "/snapshot/evidence/notanumber")[0] == 404
    assert req(port, "/snapshot/../etc/passwd")[0] == 404


async def test_snapshot_escapes_hostile_page_text(gui):
    _s, port, _token, db, _rid = gui
    eid = db.scalar("SELECT id FROM evidence_items WHERE page_id IS NOT NULL LIMIT 1")
    db.execute("UPDATE evidence_items SET excerpt='<script>alert(1)</script>' WHERE id=?", (eid,))
    db.conn.commit()
    html = req(port, f"/snapshot/evidence/{eid}")[1]
    assert "<script>alert(1)</script>" not in html



async def test_technical_snapshot_explains_instead_of_suggesting_dispute(gui):
    _s, port, _token, db, _rid = gui
    eid = db.scalar("SELECT id FROM evidence_items WHERE page_id IS NOT NULL LIMIT 1")
    db.execute("UPDATE evidence_items SET evidence_class='technical' WHERE id=?", (eid,))
    db.conn.commit()
    html = req(port, f"/snapshot/evidence/{eid}")[1]
    assert "Technical observation, not a quote" in html and "mark as disputed" not in html

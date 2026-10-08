from __future__ import annotations

import email
import email.policy

import pytest

from conftest import make_settings
from nsmpa.outreach import add_contact, do_not_contact, draft_campaign, emails_in, harvest_contacts, mark_sent, record_response
import test_pipeline as tp


async def _setup(tmp_path, db):
    s = make_settings(tmp_path, outreach_sender_name="Test Researcher", outreach_sender_email="researcher@example.org")
    tp.seed_newsrooms(tmp_path, db)
    rid, _ = await tp.run_research(db, s, tp.FakeSearch(tp.SEARCH_ROUTES))
    return s, rid


def test_emails_in_and_filters():
    html = '<a href="mailto:editor@quiet.example?subject=x">Email</a> or write to news@quiet.example. logo@2x.png'
    assert emails_in(html) == {"editor@quiet.example", "news@quiet.example"}


async def test_harvest_draft_never_send_and_dnc(tmp_path, db, monkeypatch):
    s, rid = await _setup(tmp_path, db)
    monkeypatch.setitem(tp.WEB, "https://quiet.example/about", (200, tp.PAGE.format(title="About us", body=(
        "<p>The Quiet Gazette is a weekly newspaper owned by local residents since 1950.</p>"
        "<a href='mailto:editor@quiet.example'>editor</a> <a href='mailto:ads@quiet.example'>ads</a> "
        "<a href='mailto:someone@gmail.com'>x</a>"))))
    rid2, _ = await tp.run_research(db, s, tp.FakeSearch(tp.SEARCH_ROUTES), fresh=True)
    res = harvest_contacts(db)
    assert res["contacts_added"] >= 1 and res["entities_with_contacts"] >= 1
    emails = {r["email"] for r in db.execute("SELECT email FROM contacts")}
    assert "editor@quiet.example" in emails and "ads@quiet.example" not in emails and "someone@gmail.com" not in emails
    out = draft_campaign(db, s, "pilot", s.output_dir / "outreach")
    assert out["drafted"] >= 1
    path = db.scalar("SELECT draft_path FROM outreach_messages WHERE campaign='pilot'")
    msg = email.message_from_bytes(open(path, "rb").read(), policy=email.policy.default)
    body = msg.get_body().get_content()
    assert msg["X-Unsent"] == "1" and "Test Researcher" in body and "voluntary" in body
    assert "do not remove or alter" in body  # explicitly invites responses on all sides
    assert db.scalar("SELECT status FROM outreach_messages WHERE campaign='pilot'") == "drafted"  # never auto-sent
    assert draft_campaign(db, s, "pilot", s.output_dir / "outreach")["drafted"] == 0  # idempotent
    do_not_contact(db, "editor@quiet.example")
    assert draft_campaign(db, s, "second", s.output_dir / "outreach")["drafted"] == 0
    assert mark_sent(db, "pilot") >= 1


async def test_draft_requires_honest_identification(tmp_path, db):
    s = make_settings(tmp_path)
    with pytest.raises(ValueError, match="outreach_sender_name"):
        draft_campaign(db, s, "x", tmp_path / "o")


async def test_response_becomes_first_party_evidence_and_updates_stance(tmp_path, db):
    s, rid = await _setup(tmp_path, db)
    eid = db.scalar("SELECT id FROM research_entities WHERE name='Quiet Gazette'")
    assert tp.stances(db, rid)["Quiet Gazette"] == "NO_RELEVANT_GUIDANCE"
    add_contact(db, eid, "editor@quiet.example", "Pat", "editor")
    reply = tmp_path / "reply.eml"
    m = email.message.EmailMessage()
    m["From"], m["To"], m["Subject"] = "editor@quiet.example", "researcher@example.org", "Re: Research question"
    m.set_content("Thanks for asking. In rare cases, editors may remove a name from an article when charges were later dismissed.\n"
                  "> 1. Does the Quiet Gazette have a written policy on requests to unpublish?")
    reply.write_bytes(bytes(m))
    res = record_response(db, s, eid, reply, campaign="pilot")
    assert res["stance"] == "SUPPORTS_CHANGED_CIRCUMSTANCES" and res["evidence_items"] >= 1
    ev = db.execute("SELECT * FROM evidence_items WHERE evidence_class='direct_response'").fetchall()
    assert all(e["authority_score"] == 1.0 for e in ev)
    assert not any("written policy on requests" in e["excerpt"] for e in ev)  # quoted survey text ignored

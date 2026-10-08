"""Regression tests for the problems found by the second live pilot (2026-10-08)."""
from __future__ import annotations

import pytest

from nsmpa.evidence import classify_statement
from nsmpa.extract import extract_main_text
from nsmpa.identity import check_identity, institution_base_name
from nsmpa.wayback import compare_versions, person_names

STUDENT = {"name": "The Easterner Online", "cohort": "student_media", "parent_name": "Eastern Washington University"}


def _page(title, body):
    html = f"<html><head><title>{title}</title></head><body>{body}</body></html>".encode()
    return extract_main_text(html, "text/html", "https://x.example/", {})


def test_scattered_institution_words_do_not_identify_a_repurposed_site():
    page = _page("Home - Easterner Online", "<p>" + "Travel guides for every season. " * 5 + "Best hikes in eastern Oregon. "
                 + "Visiting Washington, D.C. on a budget. " * 3 + "</p>")
    assert check_identity(STUDENT, {}, "ok", page).status == "mismatch"
    real = _page("The Easterner", "<p>The Easterner is the student-run newspaper of Eastern Washington University. "
                 "Editor-in-chief: A. Writer. News, sports and opinion from Cheney.</p>")
    assert check_identity(STUDENT, {}, "ok", real).status == "ok"


def test_campus_suffix_is_stripped_for_names_and_queries():
    assert institution_base_name("University of Washington-Seattle Campus") == "University of Washington"
    assert institution_base_name("Pennsylvania State University-Main Campus") == "Pennsylvania State University"


@pytest.mark.parametrize("sentence", [
    "Political views aired in a quote — there's no shortage of content people wish they could hide from the all-seeing eyes of Google.",
    "Student journalists who wrote a piece that doesn't meet their current standards sometimes ask the publications to take down these pieces.",
    "But just because you can easily remove an electronically archived article, should you?",
    "First, the subject may demand removal of an entire article or column.",
    "In the cases of the more than 7,100 campus newspapers stolen this past year, free newspapers were removed from stands in overt acts of theft.",
    "A takedown policy, created to guide these decisions when the law does not provide a definitive answer, can be helpful in keeping takedowns consistent.",
    "As long as your publication accurately quoted the police report, it could not be liable for libel if your state recognizes a fair report privilege.",
    "She is also concerned the law may require we use their legal name because the yearbook functions as a historical record.",
    "Contact For any general inquiries, contact: Jordan Avery, Editor-in-Chief [email protected] To submit corrections, contact the editor.",
])
def test_pilot2_false_positives_are_mentions(sentence):
    st = classify_statement(sentence)
    assert st.direction == "neutral", (st.statement_type, st.cues)


@pytest.mark.parametrize("sentence,direction", [
    ("The Daily does not remove articles from its archive.", "adverse"),
    ("Editor's note: This story has been updated to remove the name of the student after charges were dropped.", "supportive"),
    ("We will consider requests to unpublish articles about minor crimes after five years.", "supportive"),
    ("Editors may remove an article when the subject was never charged.", "supportive"),
])
def test_real_positions_still_detected(sentence, direction):
    assert classify_statement(sentence).direction == direction


ARTICLE = ("<p>Police arrested a man near campus on Tuesday after a report of an assault outside the library.</p>"
           "<p>The investigation is continuing and officers asked witnesses to contact the department.</p>"
           "<p>The university said counseling services are available to students affected by the incident.</p>")


def test_redesign_is_not_reported_as_content_change():
    old = f"<html><head><title>Arrest near campus - The Spectator</title></head><body><main>{ARTICLE}</main></body></html>"
    # New theme: no <main>, the page wrapper carries a boilerplate-sounding class, title suffix uses an en dash.
    new = (f"<html><head><title>Arrest near campus – The Spectator</title></head><body><div class='site-header-wrap'>"
           f"<nav>News Sports Opinion</nav><div class='entry'>By Riley Morgan Apr 13th {ARTICLE}</div></div></body></html>")
    assert compare_versions(old.encode(), new.encode(), url="https://x.example/a", current_status=200, threshold=0.85) == []


def test_byline_reflow_fragments_are_not_names():
    names = person_names("News > Seattle Safe Drug Sites By Riley Morgan Apr 13th, 2016. On Dec. 30, 2017 work began.")
    assert "By Riley" not in names and "Morgan Apr" not in names and "On De" not in names


@pytest.mark.parametrize("sentence", [
    "Certain parts of the memos were redacted, yet they still relay a detailed look into private conversations.",
    "A parking sign was removed on the Northeast corner of Reese Court building and was found outside on the grass.",
    "The Easterner reserves the right to edit or delete hate speech, inflammatory or off-topic comments.",
    "Professor Removed Following Public Criticism of Administration.",
])
def test_pilot3_false_positives_are_mentions(sentence):
    st = classify_statement(sentence)
    assert st.direction == "neutral", (st.statement_type, st.cues)


def test_passive_removal_of_journalism_still_counts():
    assert classify_statement("The article was removed from our website after the charges were dismissed.").direction == "supportive"


def test_densest_block_beats_menus_when_wrapper_is_stripped():
    body = "".join(f"<p>Paragraph {i} of the article reports what happened at the council meeting on Tuesday night.</p>" for i in range(6))
    html = (f"<html><head><title>Story</title></head><body><div class='site-header-wrap'><nav>News Sports Opinion Comments</nav>"
            f"<div class='entry'>{body}</div><div class='widget'>Leave a Comment. Comments will be removed if off-topic.</div>"
            f"</div></body></html>").encode()
    page = extract_main_text(html, "text/html", "https://x.example/story", {})
    assert "council meeting" in page.main_text and "Leave a Comment" not in page.main_text


@pytest.mark.parametrize("sentence,stype", [
    ("The short story category is for unpublished writers and is open to a writer of any genre.", "mention"),
    ("Reese Court Sign Removed (10/18/21) A parking sign was removed on the Northeast corner of Reese Court building. "
     "About the Contributor Sam Ortiz, Managing Editor", "mention"),
    ("Editor's note: This article has been updated to remove information that can not be verified at this time.", "practice_update"),
    ("Even if no police report was involved, handling requests to remove content is simpler because publications will not "
     "typically be held liable for minor inaccuracies if these errors do not significantly alter the gist.", "mention"),
])
def test_pilot4_cases(sentence, stype):
    assert classify_statement(sentence).statement_type == stype


def test_run_on_comment_policy_is_judged_by_its_archive_tail():
    s = ("Prohibited comments include: Comments with directed profanity, bullying, spam, false or misleading statements "
         "Comments that are off-topic Comments by students working for The Office of Student Media, unless authorized "
         "Story changes and removal Once content is published by The Daily Evergreen, either in print or online, it is "
         "considered historical record and shall not be altered, unless with a correction or update.")
    assert classify_statement(s).direction == "adverse"


def test_real_findings_survive():
    assert classify_statement("The Daily Evergreen will not remove any content unless the information is later proven "
                              "irreparably false or puts someone in danger.").direction == "adverse"
    assert classify_statement("In the interest of minimizing harm to those involved with the case, we have removed the "
                              "document containing that information.").direction == "supportive"


@pytest.mark.parametrize("sentence,direction", [
    ("In those cases, the AP would also request that Google “deindex” the stories, removing them from its search results.", "supportive"),
    ("REMOVAL OF MINOR CRIME STORIES At its discretion, The Associated Press considers requests to block old stories, photos "
     "and video about minor crimes from APNews.com and other AP platforms.", "supportive"),
    ("However, the use of these methods must not conceal, obscure, remove or otherwise alter the content of the audio.", "neutral"),
    ("Voice reports by AP correspondents may be edited to remove pauses or stumbles.", "neutral"),
    ("The background of a photograph, for example, may be removed to leave the headshot of the newsmaker.", "neutral"),
])
def test_ap_standards_cases(sentence, direction):
    assert classify_statement(sentence).direction == direction


@pytest.mark.parametrize("sentence,direction", [
    # From the user's hand-built evidence index (October 2026), used as ground truth.
    ("Our main remedy is to deindex an article so it remains online but is not findable via search engines.", "supportive"),
    ("The main remedy we will offer is deindexing.", "supportive"),
    ("Deindexing does not impact an article’s accessibility in databases like Nexis Lexis or other archives over which "
     "The Inquirer has no control.", "neutral"),
    ("Not one of the 110 editors surveyed would remove content because of source remorse.", "adverse"),
    ("You can begin the deletion process by finding information and the required forms at the following address: "
     "https://www.courts.state.co.us.", "neutral"),
    ("A newspaper may choose to see if Google will assist these people, or may choose to cooperate with Google if Google "
     "decides to help these people.", "supportive"),
    ("If the subject of a story provides proof charges were dropped, they were found not guilty or their conviction was "
     "expunged, we will consider removing the story.", "supportive"),
    ("Crime stories may be de-indexed in certain circumstances, meaning they would not show up in search results.", "supportive"),
])
def test_user_evidence_index_cases(sentence, direction):
    assert classify_statement(sentence).direction == direction


def test_embedded_page_data_is_read():
    from nsmpa.extract import extract_main_text
    para = ("Our main remedy is to deindex an article so it remains online but is not findable via search engines. "
            "Links will still work, and the article is still findable on our own site search. ") * 3
    html = ('<html><head><title>Up for Review</title></head><body><div id="app"></div><script>Fusion.globalContent='
            + __import__("json").dumps({"content_elements": [{"type": "text", "content": f"<p>{para}</p>"}]})
            + ';</script></body></html>').encode()
    page = extract_main_text(html, "text/html", "https://news.example/up-for-review", {})
    assert "main remedy is to deindex" in page.main_text

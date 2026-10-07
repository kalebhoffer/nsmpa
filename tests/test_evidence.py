from __future__ import annotations

import pytest

from nsmpa.evidence import classify_statement, extract_evidence, split_sentences, tags_for
from nsmpa.extract import extract_main_text

# (sentence, expected statement_type). Includes the exact junk sentences that the v0.2
# keyword classifier mis-scored as adverse/supportive evidence in the pilot run.
STATEMENT_CASES = [
    # v0.2 false positives -> must be non-evidence
    ("Please do not remove this page Social media and connective journalism: the formation of counterpublics.", "mention"),
    ("Subscribe with your email to receive Family Style Newsletter (read archive here), our monthly newsletter including chapter updates.", "mention"),
    ("Madden M, Lenhart A, Cortesi S (2013) Teens, Social Media and Privacy. Pew Research Center on Internet.", "mention"),
    ("Thanks to our Partners Contact Due to the coronavirus pandemic, please reach out to us by email for privacy questions.", "mention"),
    # adverse
    ("The Daily does not unpublish articles except in cases of legal requirement or a credible threat to someone's safety.", "relief_narrow_exceptions"),
    ("We do not remove stories from our archive.", "relief_rejected"),
    ("We will never unpublish content because our archive is a historical record.", "relief_rejected"),
    ("We do not honor requests to unpublish stories simply because the subject is embarrassed.", "relief_rejected"),
    ("Our archive serves as the historical record of the campus and its community.", "archive_principle"),
    ("The editor declined the request to remove the article, saying it was accurate when published.", "practice_relief_denied"),
    # supportive
    ("In rare cases, editors may remove a name from an article when charges were later dismissed.", "changed_circumstance_relief"),
    ("We generally do not unpublish, but we may consider removing a name when a case was expunged.", "changed_circumstance_relief"),
    ("Editors may consider requests to anonymize a story about a minor.", "relief_permitted"),
    ("Names of people arrested for misdemeanors will be removed from the police blotter after two years.", "relief_permitted"),
    ("After the charges were dropped, the newspaper removed the student's name from the 2019 article.", "practice_relief_granted"),
    ("The Cleveland Plain Dealer began a program to remove names from old stories about minor crimes.", "practice_relief_granted"),
    # case by case / process
    ("Requests to de-index an article will be considered on a case-by-case basis.", "case_by_case"),
    ("Removal requests are reviewed by the editor in chief and the adviser.", "case_by_case"),
    # update-only family
    ("If charges are dismissed or the person is acquitted, we will update the story to reflect the outcome of the case.", "changed_circumstance_update"),
    ("Corrections will be appended to the article with an editor's note explaining the change.", "update_remedy"),
    # harm context
    ("A story about an arrest can follow someone forever because search engines surface old articles.", "harm_consideration"),
]


@pytest.mark.parametrize("sentence,expected", STATEMENT_CASES)
def test_statement_classification(sentence, expected):
    st = classify_statement(sentence)
    assert st.statement_type == expected, (st.statement_type, st.cues)


def test_directions_follow_statement_types():
    assert classify_statement("We do not remove stories from our archive.").direction == "adverse"
    assert classify_statement("Requests to de-index an article will be considered on a case-by-case basis.").direction == "supportive"
    assert classify_statement("Corrections will be appended to the article with an editor's note explaining the change.").direction == "neutral"


def test_split_sentences_handles_abbreviations_and_runons():
    text = "Dr. Smith said the U.S. policy changed. We may remove names. " + "Word " * 400
    sents = split_sentences(text)
    assert sents[0].startswith("Dr. Smith said the U.S. policy changed.")
    assert sents[1] == "We may remove names."
    assert all(len(s) <= 700 for s in sents)


def test_one_sentence_many_tags_is_one_item():
    text = ("Editors may de-index or anonymize an article about an arrest when charges were dismissed, "
            "the record was expunged, and search engines continue to surface it. "
            "Editors may de-index or anonymize an article about an arrest when charges were dismissed, "
            "the record was expunged, and search engines continue to surface it.")
    items = extract_evidence(text)
    assert len(items) == 1
    assert {"deindex", "anonymize", "dismissed_charges", "expunged_sealed", "search_engine", "arrest_record"} <= set(items[0].statement.tags)


def test_third_party_requires_entity_mention():
    text = ("The Example Daily removed the name of a student after the charges were dismissed. "
            "Another newspaper in Ohio removed names from old crime stories after a review.")
    items = extract_evidence(text, entity_terms=["Example Daily"], require_entity_mention=True)
    assert len(items) == 1 and "Example Daily" in items[0].excerpt


def test_tags_cover_changed_outcomes():
    assert "vacated" in tags_for("The conviction was vacated in 2021.")
    assert "plea_withdrawn" in tags_for("He withdrew his guilty plea.")
    assert "expunged_sealed" in tags_for("Her record was sealed by the court.")
    assert "exonerated" in tags_for("He was exonerated after DNA testing.")
    assert "right_to_be_forgotten" in tags_for("Europe's right to be forgotten does not apply here.")


def test_main_text_strips_navigation_and_footer():
    html = b"""<html><head><title>Unpublishing Policy | The Example Daily</title>
    <meta name="robots" content="noindex"></head><body>
    <nav><a href="/">Home</a> Please do not remove this page. Subscribe to our newsletter archive.</nav>
    <header>The Example Daily - Independent student newspaper</header>
    <main><h1>Unpublishing policy</h1><p>We do not remove stories from our archive.
    In rare cases, editors may remove a name from an article when charges were later dismissed.</p></main>
    <footer>Copyright 2026. All rights reserved. Privacy policy. Do not remove.</footer>
    <div class="cookie-banner">This site uses cookies and we never remove them.</div>
    </body></html>"""
    page = extract_main_text(html, "text/html", "https://daily.example.edu/unpublishing-policy", {})
    assert "We do not remove stories" in page.main_text
    assert "Please do not remove this page" not in page.main_text
    assert "cookies" not in page.main_text
    assert page.noindex is True
    items = extract_evidence(page.main_text)
    types = {i.statement.statement_type for i in items}
    assert types == {"relief_rejected", "changed_circumstance_relief"}

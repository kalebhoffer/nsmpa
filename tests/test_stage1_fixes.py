"""Regression cases from the Stage 1 national run on standards/support organizations (Oct 2026)."""
from __future__ import annotations

import pytest

from nsmpa.evidence import classify_statement

NOT_RELIEF = [
    # people removed from roles
    "In the event that an Editor is removed, the Editorial Selection Committee will select an Interim Editor to complete the term.",
    "Editors serve without term limits and may be removed from the publication for cause by the executive director.",
    "If we find out later that a member has concealed information, including political affiliation, the case will be reviewed "
    "as a Code of Conduct violation and the member could be removed.",
    # "unpublished" = never published (reporters' materials)
    "However, ICE may attempt to obtain unpublished information from student journalists, such as notes, photos or confidential "
    "sources, through a subpoena.",
    "If a reporter is a party to a case and refuses to reveal a confidential source or unpublished information, the court may act.",
    # case-by-case about something else
    "The department chair stated that administration did not get involved in its content operations, considering issues on a "
    "case-by-case basis.",
    "Publications based at universities with a different IRS status may have different requirements and are considered on a "
    "case-by-case basis.",
    "When we inquired about the fees, we were told they were decided on a case-by-case basis.",
    "Some style usage may change on a case-by-case basis, depending on the material or subject matter.",
    # historical record / records outside journalism
    "But the effort to sanitize the historical record, our shared understanding of what America is, was, and hopes to become, "
    "extends far beyond a single museum.",
    "The President will also retain the program currently in place for electronic records: emails and documents cannot be deleted.",
    # codes of conduct, abuse reports
    "Reports intended to silence legitimate criticism may be deleted without response.",
    "To be clear, the following behavior is strictly prohibited: passing off someone's ideas or words as your own, even if anonymised.",
    # the topic, not a position
    "Working together, I believe we can better address some of the most pressing challenges related to unpublishing.",
    "But a good crediting plan can replace anonymization for contributors to open-source newsroom projects.",
    "Removal of the criteria of single issue coverage for a national audience: LION has been listening to publishers.",
    "Other suggestions to consider redacting: URLs, portfolio content and descriptions, skills that are too specific.",
    "In the case that it is deemed necessary to remove an Editor, the Division Head will reconstitute the Editorial Selection "
    "Committee.",
    "Known as the Criminal Record Expungement Act, the legislation would have removed all public access to certain law "
    "enforcement records.",
    "Unpublishing is not an issue that any one of us can address alone.",
    # someone else demanding removal
    "The right to be forgotten de-listing regime essentially amounts to a notice-and-takedown system where private parties can "
    "demand the removal of links.",
]
STILL_SUPPORTIVE = [
    "Quinn: With many stories, we are able to remove the names and identifying information while leaving the story published.",
    "If you have something such as a crime database that includes only people convicted of crimes, the item should be removed "
    "from the database.",
    "We will consider requests to unpublish articles about minor crimes after five years.",
    "We consider article removal requests on a case-by-case basis.",
    "Requests to de-index an article will be considered on a case-by-case basis.",
]
STILL_ADVERSE = [
    "The Daily does not remove articles from its archive.",
    "Our archive is a historical record, and we do not unpublish stories.",
]


@pytest.mark.parametrize("sentence", NOT_RELIEF)
def test_stage1_false_positives_are_neutral(sentence):
    st = classify_statement(sentence)
    assert st.direction == "neutral", (st.statement_type, st.cues)


@pytest.mark.parametrize("sentence", STILL_SUPPORTIVE)
def test_real_relief_still_supportive(sentence):
    assert classify_statement(sentence).direction == "supportive"


@pytest.mark.parametrize("sentence", STILL_ADVERSE)
def test_real_refusals_still_adverse(sentence):
    assert classify_statement(sentence).direction == "adverse"


@pytest.mark.parametrize("sentence,context", [
    ("We handle these requests on a case-by-case basis.", "Readers sometimes ask us to remove old articles about them."),
    ("Each request is considered on a case-by-case basis and does not set precedent for future decisions.",
     "Requests to unpublish content are reviewed by the managing editor."),
    ("We're considering these on a case-by-case basis but we think the value of giving someone a fresh start often outweighs "
     "the historic value of keeping a story widely accessible.", ""),
    ("Take-down requests are weighed on a case-by-case basis with senior editors, and some situations may require legal "
     "guidance.", ""),
])
def test_case_by_case_relief_kept_with_context(sentence, context):
    assert classify_statement(sentence, context).direction == "supportive"


def test_story_removed_from_post_gazette_is_practice_not_a_person():
    st = classify_statement("The original story has been removed from the Post-Gazette website.")
    assert st.direction == "supportive", st.cues


@pytest.mark.parametrize("sentence,direction", [
    ("Unanimous agreement of the Editorial Selection Committee is required to submit a proposal for removal to the Division Head.",
     "neutral"),
    ("Known as the Criminal Record Expungement Act, the legislation would have removed all public access to certain law "
     "enforcement records.", "neutral"),
    ("We will not remove editorials from our archive.", "adverse"),
    ("Editors may remove an editorial when the subject was never charged.", "supportive"),
])
def test_editorial_as_adjective_and_records_order(sentence, direction):
    assert classify_statement(sentence).direction == direction


def test_rarely_granted_is_a_narrow_exception():
    st = classify_statement("As a matter of editorial policy, takedown requests will be rarely granted.")
    assert st.statement_type == "relief_narrow_exceptions"


@pytest.mark.parametrize("sentence,direction", [
    ("When deciding whether to classify a court record as private, protected, or sealed, or to redact information from the "
     "record, the court may consider any relevant factor.", "neutral"),
    ("A judge may order an arrest record sealed when charges are dismissed.", "neutral"),
    ("When a court seals the record, we will consider removing the defendant's name from our story.", "supportive"),
])
def test_courts_acting_on_records_are_not_newsrooms(sentence, direction):
    assert classify_statement(sentence).direction == direction

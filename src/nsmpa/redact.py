"""Name redaction for anything shared outside your machine (packet, dashboard).

Crime coverage names private individuals, including people a newsroom later anonymized. Shared files should not
republish them. ``Redactor`` replaces likely person names with "[name withheld]" while keeping names that the research
is *about* or that belong to public speakers: organizations, institutions, parent institutions, seeded/added experts and
attributed speakers (their own quotes), precedent organizations.

Heuristic and conservative toward privacy: it may over-redact (e.g. some place names) and can miss unusual names, so
shared files say "automated redaction". The private database keeps original text, URLs and hashes for verification.
"""
from __future__ import annotations

import re
import sqlite3

from .db import Database

PLACEHOLDER = "[name withheld]"
_NAME = re.compile(r"\b([A-Z][a-z]{1,20}(?:\s+[A-Z]\.)?(?:\s+(?:Mc|Mac|O'|D')?[A-Z][a-z]{1,25}(?:-[A-Z][a-z]{1,20})?){1,2})(?:'s|’s)?\b")
# Words that make a capitalized sequence an organization, place, title or ordinary phrase rather than a person.
NOT_PERSON = {
    "The", "This", "That", "These", "Those", "A", "An", "In", "On", "At", "For", "From", "With", "After", "Before", "When",
    "University", "College", "School", "Institute", "Academy", "Police", "Department", "County", "City", "State", "Court",
    "District", "Street", "Avenue", "Road", "Hall", "Center", "Centre", "Campus", "Student", "Students", "News", "Daily",
    "Times", "Post", "Herald", "Tribune", "Gazette", "Journal", "Review", "Press", "Chronicle", "Observer", "Sentinel",
    "Office", "Public", "Safety", "North", "South", "East", "West", "Northern", "Southern", "Eastern", "Western", "New",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "January", "February", "March", "April",
    "May", "June", "July", "August", "September", "October", "November", "December", "Editor", "Editors", "Staff",
    "Sheriff", "Superior", "Associated", "United", "States", "America", "American", "Federal", "Supreme", "Circuit",
    "Prosecuting", "Attorney", "Judge", "Officer", "Sergeant", "Detective", "Chief", "President", "Dean", "Professor",
    "Board", "Council", "Committee", "Association", "Society", "Foundation", "Media", "Washington", "Oregon",
    "California", "Texas", "Ohio", "Florida", "Google", "Facebook", "Twitter", "Bing", "Wayback", "Machine", "Internet",
    "Archive", "Code", "Ethics", "Policy", "Standards", "Guidelines", "Read", "More", "Related", "Share", "Comments",
    "Contact", "Advertise", "Subscribe", "Update", "Editor's", "Note", "Fresh", "Start", "Clean", "Slate", "Act", "Law",
    "Regulation", "Article", "Section", "Chapter", "Part", "First", "Amendment", "Right", "Forgotten", "Company", "Inc",
    "Corp", "Group", "Network", "Project", "Program", "Initiative", "Lab", "Labs", "Week", "Day", "Year", "Spring",
    "Summer", "Fall", "Winter", "Black", "Hispanic", "Asian", "Indigenous", "National", "International", "Global",
}
# Newspaper masthead words and place-name words (two-capitalized-word places and papers are common in this corpus).
NOT_PERSON |= {
    "Star", "Globe", "Bee", "Courier", "Dispatch", "Register", "Inquirer", "Democrat", "Republican", "Examiner", "Bulletin",
    "Telegraph", "Record", "Ledger", "Mirror", "Monitor", "Standard", "Union", "Leader", "Independent", "Argonaut",
    "Collegian", "Lantern", "Beacon", "Spectator", "Oracle", "Signal", "Echo", "Dealer", "Plain", "Tribune", "Enquirer",
    "Statesman", "Advocate", "Citizen", "Gazette", "Courant", "Constitution", "Picayune", "Item", "Pilot", "Free",
    "Los", "Las", "San", "Santa", "Fort", "Saint", "St", "Lake", "Valley", "Bay", "Beach", "Springs", "Park", "Port",
    "Mount", "Falls", "Island", "Heights", "Rapids", "Grand", "Little", "Rock", "Salt", "Palm", "Baton", "Des", "El",
    "Toronto", "London", "Chicago", "Boston", "Philadelphia", "Seattle", "Spokane", "Portland", "Denver", "Phoenix",
    "Atlanta", "Miami", "Houston", "Dallas", "Austin", "Detroit", "Cleveland", "Baltimore", "Pittsburgh", "Minneapolis",
}
NOT_PERSON |= {w for state in ["Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada", "Hampshire", "Jersey", "Mexico", "York", "Carolina", "Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode", "Island", "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington", "Wisconsin", "Wyoming", "Columbia"] for w in [state]}
URL_SAFE_PLACEHOLDER = "[URL withheld: contains a redacted name; available on request with evidence ID]"


def _norm(name: str) -> str:
    return re.sub(r"[^a-z ]", "", name.lower().replace("’", "'")).strip()


class Redactor:
    def __init__(self, keep_names: set[str] | None = None):
        self.keep = {_norm(n) for n in (keep_names or set()) if n}
        # Keep every multi-word fragment of allowed names (e.g. "Boston Globe" from "The Boston Globe").
        frags = set()
        for n in self.keep:
            words = n.split()
            for i in range(len(words)):
                for j in range(i + 2, len(words) + 1):
                    frags.add(" ".join(words[i:j]))
        self.keep |= frags
        self.redacted_tokens: set[str] = set()   # tokens redacted from the current row (reset per row)
        self.count = 0

    @classmethod
    def from_db(cls, db: Database) -> Redactor:
        names: set[str] = set()
        for sql in ("SELECT name FROM research_entities", "SELECT parent_name FROM research_entities WHERE parent_name IS NOT NULL",
                    "SELECT name FROM experts", "SELECT person_name FROM voices", "SELECT organization FROM precedent_seeds",
                    "SELECT name FROM institutions WHERE included=1", "SELECT jurisdiction FROM legal_context"):
            try:
                names |= {r[0] for r in db.execute(sql) if r[0]}
            except sqlite3.OperationalError:  # table may not exist in very old databases; redaction must still work
                continue
        return cls(names)

    def _is_person(self, cand: str) -> bool:
        words = cand.split()
        if len({w.lower() for w in words}) == 1:
            return False  # "Walla Walla", "Sing Sing": repeated-word place names
        if any(w.rstrip(".") in NOT_PERSON for w in words):
            return False
        return _norm(cand) not in self.keep

    def text(self, value, *, speaker: str | None = None):
        """Redact person names in a string. ``speaker`` (an attributed quote's own author) is kept."""
        if not isinstance(value, str) or not value:
            return value
        keep_extra = {_norm(speaker)} if speaker else set()
        surnames: set[str] = set()

        def repl(m: re.Match) -> str:
            cand = m.group(1)
            if not self._is_person(cand) or _norm(cand) in keep_extra:
                return m.group(0)
            parts = cand.split()
            surnames.add(parts[-1])
            self.redacted_tokens.update(p.lower().rstrip(".") for p in parts if len(p) > 2)
            self.count += 1
            return PLACEHOLDER

        out = _NAME.sub(repl, value)
        # Later bare-surname references to a redacted person ("Smithers was charged").
        for sn in surnames:
            if _norm(sn) not in self.keep and sn not in NOT_PERSON:
                out = re.sub(rf"\b{re.escape(sn)}(?:'s|’s)?\b", PLACEHOLDER, out)
        return out

    def url(self, value):
        """Withhold URLs whose path contains a token from a redacted name (slugs often carry names)."""
        if not isinstance(value, str) or not value.startswith(("http://", "https://")):
            return value
        path = re.sub(r"^https?://[^/]+", "", value).lower()
        slug_tokens = set(re.split(r"[^a-z]+", path))
        if self.redacted_tokens & slug_tokens:
            return URL_SAFE_PLACEHOLDER
        return value


TEXT_FIELDS = {"excerpt", "quote", "source_title", "best_excerpt", "strongest_supportive", "strongest_adverse", "title",
               "context", "rationale", "practice_summary", "claim", "status_note", "summary", "conditions", "sentence", "note"}
URL_FIELDS = {"source_url", "strongest_supportive_url", "strongest_adverse_url", "best_source", "final_url", "url",
              "requested_url", "homepage_url", "confirmed_url", "claimed_url"}
DROP_FIELDS = {"archive_url", "snapshot_path"}


def redact_rows(rows: list[dict], r: Redactor) -> list[dict]:
    out = []
    for row in rows:
        r.redacted_tokens = set()
        new = dict(row)
        speaker = new.get("person_name")
        for k in list(new):
            if k in TEXT_FIELDS:
                new[k] = r.text(new[k], speaker=speaker)
        for k in list(new):  # URLs after text, so tokens redacted from this row's text are known
            if k in URL_FIELDS:
                new[k] = r.url(new[k])
            elif k in DROP_FIELDS and new[k]:
                new[k] = "[withheld in shared copy]"
        out.append(new)
    return out


def redact_packet_data(data: dict, r: Redactor) -> dict:
    """Apply redaction to every row list in a packet/dashboard payload (in place) and return it."""
    for key, val in list(data.items()):
        if isinstance(val, list) and val and isinstance(val[0], dict):
            data[key] = redact_rows(val, r)
    for summ in (data.get("ai_summaries") or {}).values():
        for snt in summ.get("sentences", []):
            r.redacted_tokens = set()
            snt["text"] = r.text(snt["text"])
    data["redacted"] = True
    data["redaction_count"] = r.count
    return data

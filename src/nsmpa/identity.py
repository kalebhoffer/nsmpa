"""Is this website still the organization we think it is?

Directory URLs age: papers close, move, or let domains lapse, and lapsed domains are often bought and refilled with
unrelated content that keeps the old title (the 2026 pilot found `easterneronline.com` serving a travel guide). Research
must not attribute that content to the publication. ``check_identity`` runs on the homepage *before any search credits
are spent*:

- ``ok``         the site identifies as the organization
- ``weak``       partial signals; research continues, the entity is flagged for review
- ``mismatch``   parked/for-sale domain, or none of the required signals: research stops, nothing is attributed
- ``unreachable`` / ``blocked``  the homepage cannot be read: research stops (searching would only waste credits)

Student publications need a student-media signal ("student newspaper", "student-run", "editor-in-chief"...) or the
institution's name; the publication's own name is not enough, because lapsed domains keep it. Professional newsrooms
need their name plus news-site structure (section/date links or newsroom vocabulary).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .discovery import DATE_LINK, SECTION_LINK, STRONG_STUDENT_TERMS, VERIFY_STUDENT, _name_tokens
from .utils import normalize_for_hash

PARKED = re.compile(r"\b(?:domain (?:name )?(?:is |may be )?for sale|buy this domain|this domain (?:may be|is) for sale|"
                    r"parked (?:free|domain|by)|domain parking|hugedomains|sedo(?:parking)?|dan\.com|afternic|"
                    r"this site can'?t be reached|account suspended|default web site page|index of /)\b", re.I)
NEWSROOM_WORDS = re.compile(r"\b(?:obituar(?:y|ies)|e-?edition|letters? to the editor|breaking news|local news|sports|opinion|"
                            r"classifieds|subscribe|subscription|newsletter|editorial|reporters?|newsroom|police|"
                            r"city council|school board|weather)\b", re.I)
BLOCKED = {"blocked", "robots_disallowed", "rate_limited"}


@dataclass
class Identity:
    status: str
    reason: str
    signals: dict

    @property
    def proceed(self) -> bool:
        return self.status in {"ok", "weak"}


CAMPUS_SUFFIX = re.compile(r"\s*[-–,]\s*(?:main campus|[A-Z][\w .'&]*campus)\s*$", re.I)


def institution_base_name(name: str) -> str:
    """'University of Washington-Seattle Campus' -> 'University of Washington' (how papers and people write it)."""
    return CAMPUS_SUFFIX.sub("", name or "").strip()


def _names(entity, metadata: dict) -> list[str]:
    out = [entity["name"] or ""]
    out += [t for t in (metadata.get("titles") or []) if t]
    cleaned = []
    for n in out:
        n = re.sub(r"\(\d+ titles\)$", "", n).strip()
        n = re.sub(r"\b(?:online|the)\b", "", n, flags=re.I).strip()
        if len(n) >= 4 and "." not in n:
            cleaned.append(normalize_for_hash(n))
    return [c for c in dict.fromkeys(cleaned) if c]


def _acronym(inst: str) -> str | None:
    """'Brigham Young University' -> 'BYU'; only for 3+ capitalised words, never single words."""
    words = [w for w in re.findall(r"[A-Za-z]+", inst) if w[0].isupper() and w.lower() not in {"of", "the", "and", "at"}]
    return "".join(w[0] for w in words).upper() if len(words) >= 3 else None


def _news_signals(text: str, page) -> tuple[int, int]:
    links = getattr(page, "links", []) or []
    structure = sum(1 for _, h in links if SECTION_LINK.search(h) or DATE_LINK.search(h))
    return structure, len(set(m.lower() for m in NEWSROOM_WORDS.findall(text)))


def _newsy(text: str, page) -> bool:
    structure, vocab = _news_signals(text, page)
    return structure >= 3 or vocab >= 3


def check_identity(entity, metadata: dict, access_class: str, page=None) -> Identity:
    if access_class in BLOCKED:
        return Identity("blocked", "the website blocks automated access", {"access": access_class})
    if access_class != "ok" or page is None:
        return Identity("unreachable", f"homepage could not be fetched ({access_class})", {"access": access_class})
    text = f"{page.title} {page.full_text[:30000]}"
    norm = normalize_for_hash(text)
    if PARKED.search(text) or len(norm) < 60:
        return Identity("mismatch", "parked, for-sale, empty or placeholder domain", {"chars": len(norm)})
    name_hit = any(re.search(rf"\b{re.escape(n)}\b", norm) for n in _names(entity, metadata))
    cohort = entity["cohort"]
    if cohort == "student_media":
        student = bool(VERIFY_STUDENT.search(text) or STRONG_STUDENT_TERMS.search(text))
        inst = institution_base_name(entity["parent_name"] or "")
        inst_norm = normalize_for_hash(inst)
        toks = _name_tokens(inst)
        # Whole-word phrase matches only: "eastern" must not match inside "Easterner", and scattered words ("Eastern" in
        # one travel article, "Washington" in another) are not the institution's name.
        inst_hit = bool(inst_norm and re.search(rf"\b{re.escape(inst_norm)}\b", norm)) or (
            len(toks) >= 2 and re.search(r"\b" + r"\W+(?:\w+\W+)?".join(map(re.escape, toks)) + r"\b", norm) is not None)
        acro = _acronym(inst)
        if not inst_hit and acro and re.search(rf"\b{acro}\b", text):  # "BYU", "UCLA": case-sensitive, whole word
            inst_hit = True
        sig = {"student_signal": student, "institution_named": inst_hit, "name_on_page": name_hit}
        if student and (inst_hit or name_hit):
            return Identity("ok", "site identifies as a student publication of the institution", sig)
        if student or inst_hit:
            return Identity("weak", "partial student-publication signals; verify", sig)
        if not inst and name_hit and _newsy(text, page):
            # No linked institution to look for (unmatched directory paper): own name plus a working news site.
            return Identity("weak", "publication name and news-site structure, but no institution on record; verify", sig)
        return Identity("mismatch", "no student-media signal: the domain no longer appears to be this student "
                                    "publication (possibly lapsed and repurposed)", sig)
    structure, vocab = _news_signals(text, page)
    newsy = structure >= 3 or vocab >= 3
    sig = {"name_on_page": name_hit, "news_structure_links": structure, "newsroom_vocabulary": vocab}
    if cohort in {"professional_newsroom", "broadcast_newsroom"}:
        if name_hit and newsy:
            return Identity("ok", "site carries the publication's name and news-site structure", sig)
        if name_hit or newsy:
            return Identity("weak", "partial newsroom signals; verify", sig)
        return Identity("mismatch", "neither the publication's name nor news-site structure: the domain no longer appears "
                                    "to be this newsroom", sig)
    # Guidance organizations and others: name on page is sufficient; absence is only a review flag.
    return Identity("ok" if name_hit else "weak", "name found on site" if name_hit else "name not found on homepage; verify", sig)

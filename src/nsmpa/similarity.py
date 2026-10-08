"""Transparent fact-pattern similarity scoring.

Score = 100 * (sum of weights of matched factors) / (sum of all factor weights).
Weights come from ``Settings.case_profile`` so they are visible in config.yml and recorded
in every run's ``config_json``. Each scored excerpt stores the matched factors and the exact
text that triggered them (``similarity_factors_json``). The score ranks review priority only;
it is not evidence of authority or truth.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import CaseProfile
from .evidence import TAGS

FACTORS: list[tuple[str, str, re.Pattern[str] | None]] = [
    # (factor name, CaseProfile weight attribute, pattern; None = computed specially)
    ("student_or_university_setting", "student_context_weight",
     re.compile(r"\b(?:student|campus|universit(?:y|ies)|college|collegiate|school newspaper)\b", re.I)),
    ("criminal_allegation_or_arrest", "criminal_allegation_weight",
     re.compile(TAGS["arrest_record"].pattern + "|" + TAGS["criminal_allegation"].pattern + r"|\bcriminal\b|\bcharg(?:e|ed|es)\b", re.I)),
    ("charges_dismissed_or_dropped", "dismissed_charges_weight", TAGS["dismissed_charges"]),
    ("acquittal_or_exoneration", "acquittal_exoneration_weight",
     re.compile(TAGS["acquitted"].pattern + "|" + TAGS["exonerated"].pattern, re.I)),
    ("conviction_vacated_or_plea_withdrawn", "vacated_conviction_weight",
     re.compile(TAGS["vacated"].pattern + "|" + TAGS["plea_withdrawn"].pattern, re.I)),
    ("record_expunged_or_sealed", "sealed_or_expunged_weight", TAGS["expunged_sealed"]),
    ("substantial_time_passed", "long_time_passed_weight", TAGS["time_passage"]),
    ("private_individual_no_public_role", "private_individual_weight",
     re.compile(r"\bprivate (?:individual|citizen|person|figure)s?\b|\bnot a public (?:figure|official)\b|"
                r"\bno (?:longer )?(?:ongoing )?public (?:role|interest)\b|\bno longer newsworthy\b", re.I)),
    ("search_engine_prominence", "search_prominence_weight", TAGS["search_engine"]),
    ("reputational_consequences", "reputational_harm_weight", TAGS["reputational_harm"]),
    ("preserve_article_suppress_name_search", "name_search_suppression_weight", None),
    ("deindexing", "deindex_relief_weight", re.compile(TAGS["deindex"].pattern + "|" + TAGS["noindex"].pattern, re.I)),
    ("anonymization_or_name_removal", "anonymization_weight", TAGS["anonymize"]),
    ("updated_disposition", "update_context_weight",
     re.compile(r"\b(?:update[sd]?|editor'?s'? note|follow[- ]?up|addend\w+|append\w*)\b.{0,120}\b(?:outcome|disposition|dismiss\w*|acquitt\w*|"
                r"dropped|expung\w*|vacat\w*|cleared|not guilty)\b|\b(?:outcome|disposition)\b.{0,60}\b(?:added|noted|updated)\b", re.I)),
]

_PRESERVE = re.compile(r"\b(?:keep|kept|remain|remains|preserv\w*|stay(?:s)? (?:online|up|published)|without (?:removing|deleting|unpublishing))\b", re.I)
_SUPPRESS = re.compile(TAGS["deindex"].pattern + "|" + TAGS["noindex"].pattern + "|" + TAGS["anonymize"].pattern, re.I)


@dataclass
class SimilarityResult:
    score: float
    factors: list[dict]

    @property
    def factor_names(self) -> list[str]:
        return [f["factor"] for f in self.factors]


def score_similarity(profile: CaseProfile, excerpt: str, context: str = "", *, cohort: str | None = None) -> SimilarityResult:
    """Score how closely an excerpt matches the research fact pattern.

    Matching uses the excerpt plus its immediate context (neighbouring sentences), because a
    precedent is frequently described across two sentences. Cohort membership counts toward the
    student-setting factor only when the excerpt itself is about student media (recorded as basis).
    """
    text = f"{excerpt} {context}".strip()
    total = 0.0
    matched = 0.0
    factors: list[dict] = []
    for name, attr, pattern in FACTORS:
        weight = float(getattr(profile, attr, 0.0) or 0.0)
        if weight <= 0:
            continue
        total += weight
        hit: str | None = None
        basis = "text"
        if pattern is None:  # preserve article while suppressing name search
            s, p = _SUPPRESS.search(text), _PRESERVE.search(text)
            if s and p:
                hit = f"{p.group(0)} … {s.group(0)}"
        else:
            m = pattern.search(text)
            if m:
                hit = m.group(0)
            elif name == "student_or_university_setting" and cohort == "student_media":
                hit, basis = "cohort=student_media", "cohort"
        if hit:
            matched += weight
            factors.append({"factor": name, "weight": weight, "matched": hit[:80], "basis": basis})
    score = round(100.0 * matched / total, 2) if total else 0.0
    return SimilarityResult(score, factors)


def score_case_match(profile: CaseProfile, my_case, excerpt: str, context: str = "", *, cohort: str | None = None) -> SimilarityResult:
    """Similarity restricted to the factors that are true of the requester's own case (my_case.yml).

    Denominator = weights of *your* factors only, so a precedent that matches every element of your
    situation scores 100 even if it says nothing about, e.g., vacated convictions you never had.
    """
    active = my_case.active_factors()
    full = score_similarity(profile, excerpt, context, cohort=cohort)
    total = sum(float(getattr(profile, attr, 0) or 0) for name, attr, _ in FACTORS if name in active)
    matched = [f for f in full.factors if f["factor"] in active]
    got = sum(f["weight"] for f in matched)
    return SimilarityResult(round(100.0 * got / total, 2) if total else 0.0, matched)

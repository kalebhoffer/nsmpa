"""Sentence-level evidence extraction and transparent statement classification.

Design principles (see docs/classification_rubric.md):

1. The unit of evidence is one *sentence* from the page's main content (navigation,
   headers, footers, sidebars and cookie banners are stripped first). One sentence is
   stored once per entity per run, however many tags it matches; tags live in a join table.
2. Keyword presence alone is never a finding. A sentence becomes a *policy statement* only
   when it combines a journalism object (article, story, archive, name, photo...) with a
   relevant action (remove, unpublish, de-index, anonymize, update...) and, for direction,
   a modality (may/consider vs. do not/never). Everything else is a ``mention`` and is
   excluded from stance classification.
3. Every decision is explainable: ``classify_statement`` returns the matched cues.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .utils import compact_ws, near_duplicate_key, normalize_for_hash, sha256_text

# --------------------------------------------------------------------------- topic tags
TAGS: dict[str, re.Pattern[str]] = {k: re.compile(v, re.I) for k, v in {
    "unpublish": r"\bunpublish(?:ed|es|ing)?\b",
    "deindex": r"\bde-?\s?index(?:ed|es|ing)?\b|\bremov\w* from search(?: engines?| results?)?\b",
    "noindex": r"\bno-?index\b|\bnoarchive\b",
    "anonymize": r"\banonymi[sz](?:e|ed|es|ing|ation)\b|\bremov\w* (?:the |a |their |his |her )?names?\b|\breplac\w* (?:the |a )?names? with\b|\binitials? only\b",
    "remove": r"\b(?:remov(?:e|ed|es|al|ing)|delet(?:e|ed|es|ion|ing)|take(?:n)? down|takedowns?|withdraw(?:n|al)?)\b",
    "redact": r"\bredact(?:ed|ion|ing)?\b",
    "changed_circumstances": r"\bchang(?:e|ed|ing) (?:in )?circumstances?\b|\bcircumstances (?:have |had )?changed\b|\bnew information\b",
    "dismissed_charges": r"\bcharges?\b(?:\s+(?!not\b)[\w.'-]+){0,5}?\s+(?:were |was |are |is |have been |had been |being )?(?:later |ultimately |eventually )?(?:dismissed|dropped|withdrawn|reduced)\b|\bcase (?:was |is |were )?(?:later )?(?:dismissed|dropped)\b|\bdismiss(?:ed|al of) (?:the |all )?charges?\b|\bnolle pros",
    "acquitted": r"\bacquitt(?:ed|al|als)\b|\bfound not guilty\b|\bnot guilty verdict\b",
    "exonerated": r"\bexonerat(?:ed|ion)\b|\bwrongful(?:ly)? convict",
    "vacated": r"\bvacat(?:e|ed|ing)\b(?:.{0,40}\bconvictions?\b)?|\bconvictions? (?:was |were )?(?:overturned|set aside|reversed)\b|\bset aside\b",
    "expunged_sealed": r"\bexpung(?:e|ed|ement|ements)\b|\b(?:records?|case|file)s?\b.{0,30}\bseal(?:ed|ing)\b|\bsealed records?\b",
    "plea_withdrawn": r"\bplea (?:was )?withdrawn\b|\bwithdr[ae]w (?:a |the |his |her |their )?(?:guilty )?plea\b",
    "arrest_record": r"\barrest(?:ed|s)?\b|\bmug ?shots?\b|\bbooking photos?\b|\bpolice (?:blotter|log|report)s?\b|\bcrime log\b",
    "criminal_allegation": r"\b(?:charged with|criminal charges?|accused of|indicted|allegations?|alleged(?:ly)?|suspects?|defendants?)\b",
    "juvenile": r"\b(?:juveniles?|minors?)\b",
    "rehabilitation": r"\brehabilitat(?:e|ed|ion)\b|\bsecond chance\b|\bmoved on\b|\bserved (?:their|his|her) sentence\b",
    "time_passage": r"\b(?:\d{1,2}|several|many|ten|five|seven|twenty) (?:or more )?years? (?:ago|later|old|after|have passed|has passed)\b|\bpassage of time\b|\bdecades? (?:ago|later|old)\b|\byears (?:later|after)\b",
    "reputational_harm": r"\breputation(?:al)?\b|\bemployment prospects\b|\bjob (?:search|prospects|applications?)\b|\bstigma\b|\bharm(?:s|ed|ful)? to (?:a |the )?(?:person|individual|subject)\b",
    "search_engine": r"\bsearch engines?\b|\bgoogle\b|\bsearch results?\b|\bbing\b|\bwhen (?:someone|people|employers) search\b",
    "digital_permanence": r"\bpermanence of (?:publication|the (?:internet|web|archive))\b|\bextended reach\b|\bdigital (?:permanence|footprint|age)\b|\bonline forever\b|\bpermanent(?:ly)? (?:online|available|searchable)\b|\blives? (?:on )?(?:online )?forever\b",
    "right_to_be_forgotten": r"\bright to be forgotten\b|\bright to erasure\b",
    "privacy": r"\bprivacy\b|\bprivate (?:individual|citizen|person|figure)s?\b",
    "safety": r"\b(?:physical )?safety\b|\bthreat(?:s|ened)? of (?:violence|harm)\b|\bdanger\b|\bstalk(?:ing|er)\b|\bdomestic violence\b",
    "minimize_harm": r"\bminimi[sz](?:e|ing) harm\b|\bharm(?:s)? (?:to|caused)\b|\bcompassion\b",
    "public_interest": r"\bpublic interest\b|\bnewsworth(?:y|iness)\b|\bpublic (?:figure|official)s?\b",
    "case_by_case": r"\bcase[- ]by[- ]case\b|\bon an individual basis\b|\beach (?:request|case) (?:is|will be) (?:reviewed|considered|evaluated)\b",
    "discretion": r"\b(?:sole |editorial |editor'?s? |editors'? )discretion\b|\bat the discretion\b",
    "update_note": r"\beditor'?s'? notes?\b|\b(?:update|updated|updating|append(?:ed)?|addend(?:um|a))\b|\bfollow[- ]?up (?:story|coverage|article)\b|\boutcome of the case\b|\bdisposition\b",
    "correction": r"\bcorrect(?:ion|ions|ed|ing)\b|\bclarification\b|\berrors?\b|\binaccura(?:te|cy|cies)\b",
    "archive_integrity": r"\bhistorical record\b|\brecord of (?:history|publication|what happened)\b|\bintegrity of (?:the |our )?(?:archives?|record)\b|\bpermanent (?:archive|record)\b|\bfirst (?:rough )?draft of history\b|\bwe (?:are|serve as) (?:a |the )?record\b",
    "removal_request": r"\brequests? (?:to|for) (?:remove|removal|unpublish|delete|take down|anonymi[sz]e|de-?index)\b|\b(?:removal|takedown|unpublishing) requests?\b|\basked (?:us )?to (?:remove|unpublish|take down|delete)\b",
    "legal_order": r"\bcourt orders?\b|\blegal(?:ly)? (?:required|obligation|reasons?|grounds)\b|\bdefamat(?:ion|ory)\b|\blibel(?:ous)?\b|\bsubpoena\b",
    "canonical_or_sitemap": r"\bcanonical\b|\bsitemaps?\b|\brobots\.txt\b",
}.items()}

# --------------------------------------------------------------------------- statement cues
CONTENT_OBJECT = re.compile(
    r"\b(?:articles?|stor(?:y|ies)|content|archives?|archived|coverage|reports?|reporting|posts?|"
    r"names?|photos?|photographs?|images?|mug ?shots?|pieces?|headlines?|urls?|links?|items?|"
    r"published (?:work|material|information)|material|blotter|crime logs?|police logs?|columns?|"
    r"editorials?|news (?:items?|reports?|stories)|our (?:site|website|pages)|information about|publications?|information)\b",
    re.I,
)
RELIEF_ACTION = re.compile(
    r"\b(?:unpublish\w*|remov(?:e|ed|es|al|ing)|delet(?:e|ed|es|ion|ing)|take[sn]? down|taking down|"
    r"de-?\s?index\w*|no-?index\w*|anonymi[sz]\w*|redact\w*|withdraw\w*|suppress\w*|"
    r"replac\w* (?:the |a )?names?|block\w* (?:from )?search|hide|hidden from search)\b",
    re.I,
)
UPDATE_ACTION = re.compile(
    r"\b(?:update[sd]?|updating|append\w*|add(?:ed|ing)? (?:an? )?(?:editor'?s'? note|note|update|addendum)|"
    r"editor'?s'? notes?|correct(?:ion|ions|ed|ing)?|clarif\w+|follow[- ]?up|addend\w+|"
    r"note the (?:outcome|disposition)|report (?:on )?the outcome|outcome of the case|disposition)\b",
    re.I,
)
_RELIEF_OR_UPDATE_VERB = (r"(?:remov|unpublish|delet|take[ns]? down|taken down|de-?index|no-?index|anonymi[sz]|redact|withdr[ae]w|"
                          r"replac|suppress|hid|updat|add|append|chang|alter|edit|omit)\w*")
# Explicit permission: a non-negated modal governing a relief/update verb, or limited-case language.
PERMISSIVE = re.compile(
    r"\b(?:may|might|can|could|will|would|shall|should|must|ought to)\s+(?!not\b|never\b)(?:\w+\s+){0,3}?" + _RELIEF_OR_UPDATE_VERB + r"|"
    r"\b(?:is|are) (?:willing|able|permitted|allowed) to\b|\b(?:is|are) (?:possible|permitted|allowed)\b|"
    r"\bin (?:some|rare|limited|certain|exceptional|extraordinary|special|unusual) (?:cases|circumstances|instances|situations)\b|"
    r"\bunder (?:some|certain|limited|rare|exceptional) circumstances\b|\brare(?:ly)?\b|\bexceptional(?:ly)?\b|"
    r"\bon occasion\b|\boccasionally\b|\bsometimes\b|\b(?:where|when|if) (?:appropriate|warranted|justified)\b|"
    r"\bopen to\b",
    re.I,
)
# Codes of ethics are written as imperatives ("Consider ...", "Provide updated information ...").
IMPERATIVE = re.compile(
    r"^\s*(?:consider|provide|update|remove|avoid|weigh|explain|recognize|show|seek|balance|ensure|correct|add|use|treat|"
    r"acknowledge|minimi[sz]e|be (?:cautious|careful|transparent|open)|think about|evaluate)\b", re.I)
# Review-process language: requests are considered/evaluated, without a promise either way.
PROCESS = re.compile(
    r"\b(?:review(?:ed|s|ing)?|evaluat\w+|consider(?:ed|s|ing|ation)?|assess\w*|weigh(?:ed|s|ing)?|decid\w+|"
    r"handled by|go(?:es)? (?:to|through)|submit\w*|forward\w*)\b",
    re.I,
)
PROHIBITIVE = re.compile(
    r"\b(?:do(?:es)? not|don'?t|doesn'?t|will not|won'?t|never|cannot|can'?t|can not|"
    r"(?:is|are) not (?:able|permitted|allowed|in the business)|no longer|refuse\w*|declin\w*|"
    r"not (?:remove|unpublish|delete|take down|de-?index|anonymi[sz]e|alter|change)|"
    r"policy is (?:not to|to not)|(?:strongly )?resist\w*|against (?:our|the) policy|"
    r"under no circumstances|as a rule,? we do not|generally (?:do not|does not|will not))\b",
    re.I,
)
NARROW_EXCEPTION = re.compile(
    r"\b(?:only|except|unless|other than|save for|limited to)\b.{0,90}\b(?:legal(?:ly)?|court orders?|law|"
    r"libel|defamat\w+|errors?|inaccura\w+|factual(?:ly)?|safety|danger|threat\w*|copyright|"
    r"plagiari\w+|fabricat\w+|privacy laws?)\b",
    re.I,
)
CHANGED_OUTCOME = re.compile(
    "|".join(TAGS[k].pattern for k in ("dismissed_charges", "acquitted", "exonerated", "vacated",
                                       "expunged_sealed", "plea_withdrawn", "changed_circumstances",
                                       "rehabilitation"))
    + r"|\bnever (?:convicted|charged)\b|\bnot (?:convicted|charged)\b|\bcleared\b",
    re.I,
)
CASE_BY_CASE_CUE = re.compile(TAGS["case_by_case"].pattern + "|" + TAGS["discretion"].pattern, re.I)
ARCHIVE_CUE = TAGS["archive_integrity"]
REQUEST_CUE = re.compile(r"\brequests?\b|\basks?\b|\basked\b|\bpetition\w*\b|\bcontact(?:s|ed)? (?:us|the editor)\b", re.I)
# Past-tense descriptions of what a newsroom actually did (documented practice / precedent).
PRACTICE_SUPPORT = re.compile(
    r"\b(?:began|begun|started|launched|created|adopted|introduced|implemented|announced)\s+(?:a |an |its |our |the )?"
    r"(?:new\s+)?(?:\w+\s+){0,2}?(?:program|policy|process|initiative|committee|panel|practice)\s+(?:to|for|of)\s+"
    r"(?:review\w*\s+(?:requests?\s+)?(?:to\s+)?)?(?:remov|unpublish|anonymi|de-?index|delet|updat|review)\w*|"
    r"\b(?:we|the (?:paper|newspaper|newsroom|editors?|publication|daily|staff)|editors|it)\s+(?:have\s+|had\s+|has\s+)?"
    r"(?:recently\s+|ultimately\s+|eventually\s+|then\s+)?"
    r"(?:removed|unpublished|deleted|took down|de-?indexed|anonymi[sz]ed|redacted|agreed to (?:remove|unpublish|anonymi[sz]e|de-?index|update)|"
    r"changed the (?:headline|name)|replaced (?:the )?names?|added (?:a |an )?noindex)\b|"
    r"\b(?:was|were|has been|have been|had been)\s+(?:quietly\s+)?(?:removed|unpublished|deleted|taken down|de-?indexed|anonymi[sz]ed|redacted)\b",
    re.I,
)
# Self-describing update notes on articles ("This story has been updated to remove the name...").
PRACTICE_NOTE = re.compile(
    r"\b(?:has|have|had|was|were) been (?:updated|edited|changed|revised|amended) to (?:remove|omit|withhold|anonymi[sz]e|no longer (?:name|identify|include))|"
    r"\bnames? (?:has|have|had) been (?:removed|withheld|omitted|redacted)\b|"
    r"\bno longer (?:names?|identif(?:y|ies)|includes? the names?)\b|"
    r"\b(?:at the request of|upon request)\b.{0,80}\b(?:removed|withheld|updated|anonymi[sz]ed)\b",
    re.I,
)
# Named-newsroom subject ("The Relief Daily removed ..."); deliberately case-sensitive on the name.
PRACTICE_NAMED = re.compile(
    r"\b(?:The\s+)?(?:[A-Z][\w'&.-]+\s+){1,6}(?:has\s+|had\s+|have\s+)?(?:recently\s+|quietly\s+|ultimately\s+|later\s+)?"
    r"(?:removed|unpublished|deleted|took down|de-?indexed|anonymi[sz]ed|redacted|agreed to (?:remove|unpublish|anonymi[sz]e|de-?index))\b"
)
PRACTICE_ADVERSE = re.compile(
    r"\b(?:declined|refused|denied|rejected)\s+(?:a |the |their |his |her |several |many |)?(?:request|requests|to (?:remove|unpublish|delete|take down|de-?index|anonymi[sz]e))\b|"
    r"\b(?:stood by|kept)\s+(?:the |its |our )?(?:story|article|archive|reporting)\b",
    re.I,
)
PRACTICE_UPDATE = re.compile(
    r"\b(?:we|the (?:paper|newspaper|newsroom|editors?|publication)|editors)\s+(?:have\s+|had\s+|has\s+)?"
    r"(?:added|appended|updated|published)\s+(?:an? )?(?:editor'?s'? note|update|correction|follow[- ]?up|note)\b|"
    r"\b(?:editor'?s'? note|update)\s*:\s*",
    re.I,
)
# A sentence that is self-referential web chrome rather than editorial policy.
CHROME = re.compile(
    r"\b(?:this page|this site uses cookies|cookie|subscribe|newsletter|sign up|log ?in|password|"
    r"read more|click here|share this|follow us|all rights reserved|copyright ©|powered by|"
    r"skip to (?:main )?content|privacy policy|terms of (?:use|service)|javascript)\b",
    re.I,
)
SENTENCE_END = re.compile(r"(?<=[.!?])[\"'”’)\]]*\s+(?=[\"'“‘(\[]?[A-Z0-9])")
ABBREV = re.compile(r"\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|Inc|Co|Corp|Ltd|vs|No|Gov|Sen|Rep|Gen|Lt|Col|Sgt|Capt|U\.S|U\.K|a\.m|p\.m|e\.g|i\.e|etc|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.$", re.I)

STATEMENT_DIRECTION = {
    "relief_permitted": "supportive",
    "changed_circumstance_relief": "supportive",
    "case_by_case": "supportive",
    "relief_rejected": "adverse",
    "relief_narrow_exceptions": "adverse",
    "archive_principle": "adverse",
    "update_remedy": "neutral",
    "changed_circumstance_update": "neutral",
    "harm_consideration": "neutral",
    "practice_relief_granted": "supportive",
    "practice_relief_denied": "adverse",
    "practice_update": "neutral",
    "technical_noindex": "supportive",
    "technical_sitewide_noindex": "neutral",
    "wayback_names_removed": "supportive",
    "wayback_noindex_added": "supportive",
    "wayback_unpublished": "supportive",
    "wayback_content_altered": "neutral",
    "wayback_title_changed": "neutral",
    "mention": "neutral",
}
STATEMENT_RELEVANCE = {
    "changed_circumstance_relief": 0.95, "changed_circumstance_update": 0.85, "relief_permitted": 0.9,
    "relief_rejected": 0.9, "relief_narrow_exceptions": 0.9, "practice_relief_granted": 0.9,
    "practice_relief_denied": 0.9, "case_by_case": 0.75, "archive_principle": 0.65, "update_remedy": 0.6,
    "practice_update": 0.6, "harm_consideration": 0.4, "mention": 0.1,
    "technical_noindex": 0.85, "technical_sitewide_noindex": 0.2,
    "wayback_names_removed": 0.9, "wayback_noindex_added": 0.9, "wayback_unpublished": 0.6,
    "wayback_content_altered": 0.4, "wayback_title_changed": 0.4,
}


@dataclass
class Statement:
    sentence: str
    tags: list[str]
    statement_type: str
    direction: str
    cues: list[str] = field(default_factory=list)
    confidence: float = 0.0

    @property
    def is_substantive(self) -> bool:
        return self.statement_type != "mention"


def split_sentences(text: str, max_len: int = 700) -> list[str]:
    """Split normalized text into sentences; tolerant of abbreviations and run-on text."""
    text = compact_ws(text)
    if not text:
        return []
    parts = SENTENCE_END.split(text)
    merged: list[str] = []
    for p in parts:
        if merged and ABBREV.search(merged[-1]):
            merged[-1] = merged[-1] + " " + p
        else:
            merged.append(p)
    out: list[str] = []
    for s in merged:
        s = s.strip()
        if len(s) <= max_len:
            out.append(s)
            continue
        # Run-on blocks (lists, tables, nav soup): split on semicolons/bullets, then hard-wrap.
        for chunk in re.split(r"\s*[;•·|]\s+|\s{2,}", s):
            while len(chunk) > max_len:
                cut = chunk.rfind(" ", 0, max_len)
                cut = cut if cut > max_len // 2 else max_len
                out.append(chunk[:cut].strip())
                chunk = chunk[cut:].strip()
            if chunk:
                out.append(chunk)
    return [s for s in out if s]


def tags_for(text: str) -> list[str]:
    return sorted(tag for tag, pat in TAGS.items() if pat.search(text))


_FUNCTION_WORDS = {"the", "a", "an", "of", "to", "and", "or", "is", "are", "we", "be", "in", "for", "that", "if", "will", "may",
                   "not", "with", "our", "on", "by", "from", "should", "would", "can", "could", "it", "its", "this", "their",
                   "them", "they", "as", "at", "any", "all", "when", "who", "which", "has", "have", "was", "were", "must", "do",
                   "does", "than", "but", "such", "because", "about", "after", "before", "no", "never", "only", "us", "you"}


def looks_like_chrome(sentence: str) -> bool:
    s = sentence.strip()
    if len(s) < 40:
        return True
    words = s.split()
    if len(words) < 7:
        return True
    # Menus and link soup: very low ratio of lowercase function words.
    function = sum(1 for w in words if w.lower().strip(",.;:") in _FUNCTION_WORDS)
    if len(words) >= 12 and function / len(words) < 0.06:
        return True
    return bool(CHROME.search(s)) and not RELIEF_ACTION.search(s.replace("this page", ""))


def classify_statement(sentence: str, context: str = "") -> Statement:
    """Classify one sentence. ``context`` (neighbouring sentences) only informs changed-outcome cues."""
    tags = tags_for(sentence)
    cues: list[str] = []
    has_object = bool(CONTENT_OBJECT.search(sentence))
    relief = RELIEF_ACTION.search(sentence)
    update = UPDATE_ACTION.search(sentence)
    permissive = PERMISSIVE.search(sentence) or (IMPERATIVE.match(sentence) if not PROHIBITIVE.search(sentence) else None)
    process = PROCESS.search(sentence)
    prohibitive = PROHIBITIVE.search(sentence)
    narrow = NARROW_EXCEPTION.search(sentence)
    changed = CHANGED_OUTCOME.search(sentence)
    changed_ctx = changed or (CHANGED_OUTCOME.search(context) if context else None)
    case = CASE_BY_CASE_CUE.search(sentence)
    archive = ARCHIVE_CUE.search(sentence)
    request = REQUEST_CUE.search(sentence)
    for name, m in (("object", has_object and CONTENT_OBJECT.search(sentence)), ("relief", relief), ("update", update),
                    ("permissive", permissive), ("process", process), ("prohibitive", prohibitive), ("narrow_exception", narrow),
                    ("changed_outcome", changed), ("case_by_case", case), ("archive", archive), ("request", request)):
        if m:
            cues.append(f"{name}:{m.group(0)[:40].lower()}")
    if changed_ctx and not changed:
        cues.append("changed_outcome_in_context")

    def done(stype: str, conf: float) -> Statement:
        return Statement(sentence, tags, stype, STATEMENT_DIRECTION[stype], cues, round(conf, 3))

    if looks_like_chrome(sentence):
        return done("mention", 0.1)

    # Documented practice (past tense, what actually happened) takes precedence.
    if PRACTICE_ADVERSE.search(sentence) and (has_object or request):
        cues.append("practice:denied")
        return done("practice_relief_denied", 0.75)
    if PRACTICE_NOTE.search(sentence):
        cues.append("practice:note")
        return done("practice_relief_granted", 0.8)
    if (PRACTICE_SUPPORT.search(sentence) or PRACTICE_NAMED.search(sentence)) and (has_object or relief) and not prohibitive:
        cues.append("practice:granted")
        return done("practice_relief_granted", 0.75 if changed_ctx else 0.65)
    if PRACTICE_UPDATE.search(sentence) and (has_object or changed_ctx):
        cues.append("practice:update")
        return done("practice_update", 0.6)

    subject_ok = has_object or (request and relief)
    if relief and subject_ok:
        if prohibitive and narrow:
            return done("relief_narrow_exceptions", 0.85)
        if prohibitive and not permissive:
            return done("relief_rejected", 0.85)
        if prohibitive and permissive:
            # e.g. "We generally do not unpublish, but may consider it when charges are dismissed."
            if changed_ctx:
                return done("changed_circumstance_relief", 0.7)
            if case:
                return done("case_by_case", 0.65)
            return done("relief_narrow_exceptions" if narrow else "case_by_case", 0.55)
        if permissive:
            if changed_ctx:
                return done("changed_circumstance_relief", 0.85)
            if case:
                return done("case_by_case", 0.8)
            return done("relief_permitted", 0.8)
        if case or (process and (request or has_object)):
            if changed_ctx:
                return done("changed_circumstance_relief", 0.7)
            return done("case_by_case", 0.7 if case else 0.6)
        if changed and request:
            return done("changed_circumstance_relief", 0.6)
    if case and (has_object or request):
        return done("case_by_case", 0.7)
    if update and (has_object or changed_ctx) and (changed_ctx or permissive or request or re.search(r"\b(?:we|our|editors?)\b", sentence, re.I)):
        if changed_ctx:
            return done("changed_circumstance_update", 0.75)
        if relief is None and (prohibitive is None):
            return done("update_remedy", 0.6)
    if archive and (has_object or re.search(r"\b(?:we|our)\b", sentence, re.I)):
        return done("archive_principle", 0.6)
    if (tags and set(tags) & {"reputational_harm", "search_engine", "digital_permanence", "right_to_be_forgotten", "minimize_harm"}
            and (has_object or request)):
        return done("harm_consideration", 0.5)
    pos = action_positions(sentence)
    relief_pos = {a: p for a, p in pos.items() if a != "update"}
    if any(p == "rejected" for p in relief_pos.values()) and not any(p in {"permitted", "practiced"} for p in relief_pos.values()):
        if pos.get("update") in {"permitted", "practiced"}:
            cues.append("clause:update_instead_of_removal")
            return done("update_remedy", 0.7)
        cues.append("clause:relief_rejected")
        return done("relief_rejected", 0.7)
    if pos.get("update") in {"permitted", "practiced"} and (has_object or re.search(r"\bthem\b|\bit\b", sentence)):
        cues.append("clause:update")
        return done("changed_circumstance_update" if changed_ctx else "update_remedy", 0.65)
    return done("mention", 0.2)


ANAPHOR = re.compile(r"^(?:it|its|they|their|the (?:paper|newspaper|newsroom|publication|daily|outlet|site|editors?|staff)|editors?|"
                     r"the student (?:paper|newspaper|newsroom)|that (?:paper|newspaper|policy)|this (?:policy|practice))\b", re.I)


@dataclass
class ExtractedEvidence:
    excerpt: str
    context: str
    excerpt_sha256: str
    near_dup_key: str
    statement: Statement


def extract_evidence(text: str, *, entity_terms: list[str] | None = None, max_items: int = 60,
                     require_entity_mention: bool = False) -> list[ExtractedEvidence]:
    """Extract unique relevant sentences from main-content text.

    A sentence is kept if it matches any topic tag (or a relief/update action with a journalism
    object). When ``require_entity_mention`` is set (third-party pages), the sentence or its
    immediate neighbours must mention one of ``entity_terms``.
    """
    sentences = split_sentences(text)
    terms = [t.lower() for t in (entity_terms or []) if t and len(t) >= 4]
    out: list[ExtractedEvidence] = []
    seen: set[str] = set()
    for i, sent in enumerate(sentences):
        tags = tags_for(sent)
        if not tags and not (RELIEF_ACTION.search(sent) and CONTENT_OBJECT.search(sent)):
            continue
        context = " ".join(sentences[max(0, i - 1):i + 2])
        if require_entity_mention and terms:
            # The sentence itself must name the entity, or open with an anaphor ("It", "The paper",
            # "Editors") that refers back to a sentence that names it.
            here = sent.lower()
            prev = sentences[i - 1].lower() if i else ""
            if not (any(t in here for t in terms) or (ANAPHOR.match(sent) and any(t in prev for t in terms))):
                continue
        norm = normalize_for_hash(sent)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        stmt = classify_statement(sent, " ".join(sentences[max(0, i - 2):i] + sentences[i + 1:i + 2]))
        if stmt.statement_type == "mention" and len(tags) < 2:
            continue  # single incidental keyword hits are noise, not evidence
        out.append(ExtractedEvidence(sent, context[:1500], sha256_text(norm), near_duplicate_key(sent), stmt))
    # Most substantive first, so caps never drop policy statements in favour of mentions.
    out.sort(key=lambda e: (-STATEMENT_RELEVANCE[e.statement.statement_type], -e.statement.confidence))
    return out[:max_items]



# --------------------------------------------------------------------------- per-action positions

ACTIONS = ("unpublish", "deindex", "anonymize", "update")
_ACTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "anonymize": re.compile(r"\banonymi[sz]\w*|\bredact\w*|\binitials?\b|\bwithh[oe]ld\w* (?:the |a |their )?names?|"
                            r"\b(?:remov|omit|replac|delet|chang|withh[oe]ld)\w* (?:[\w'’]+\s+){0,2}?(?:full\s+)?names?\b|"
                            r"\bnames? (?:has|have|had|were|was|are|is|will be|may be|can be) (?:been )?(?:removed|withheld|omitted|redacted|changed)", re.I),
    "deindex": re.compile(r"\bde-?\s?index\w*|\bno-?index\w*|\b(?:remov|hid|block|exclud|suppress)\w* (?:it |them |the (?:article|story) |(?:a |the |their |his |her )?names? )?from "
                          r"(?:search|google|search engines?|search results?)|\bsearch engines? (?:will )?(?:not|no longer) (?:find|index|surface)|"
                          r"\bnot (?:be )?(?:indexed|searchable)\b|\bright to be forgotten\b", re.I),
    "unpublish": re.compile(r"\bunpublish\w*|\btake[sn]? down|\btaken down|\btaking down|\btakedowns?\b|"
                            r"\b(?:remov|delet|withdr[ae]w)\w* (?:the |an? |our |old |that |this |such )?(?:\w+ )?(?:articles?|stor(?:y|ies)|content|posts?|pieces?|coverage|items?)\b|"
                            r"\b(?:articles?|stor(?:y|ies)|content|posts?) (?:\w+ ){0,3}(?:removed|deleted|taken down|unpublished)\b", re.I),
    "update": re.compile(r"\beditor'?s'? notes?\b|\bupdat\w*|\bappend\w*|\baddend\w*|\bfollow[- ]?up\b|\bcorrect(?:ion|ed|ing|ions)?\b|"
                         r"\bclarif\w+|\b(?:outcome|disposition)\b", re.I),
}
_AFFIRM = re.compile(r"^\s*(?:we|editors?|the (?:paper|newspaper|newsroom|editors?|publication|staff)|our (?:policy|practice) is to)\s+"
                     r"(?:will\s+|generally\s+|typically\s+|usually\s+|always\s+|instead\s+|also\s+)?" + _RELIEF_OR_UPDATE_VERB, re.I)
_CLAUSE_SPLIT = re.compile(r",?\s+\bbut\b\s+|;\s*|\s+\bhowever\b,?\s+|,\s+\b(?:although|though|while)\b\s+|\s+\binstead\b,?\s+|"
                           r"\s+\b(?:rather than)\b\s+|\.\s+", re.I)


def action_positions(sentence: str) -> dict[str, str]:
    """Position on each relief action, judged clause by clause.

    Returns {action: "permitted" | "rejected" | "conditional" | "practiced" | "mentioned"}.
    "We do not unpublish stories, but we may remove a name from search results" ->
    {"unpublish": "rejected", "deindex": "permitted"}.
    "rather than" introduces the rejected alternative ("we update rather than unpublish").
    """
    out: dict[str, str] = {}
    rank = {"mentioned": 0, "conditional": 1, "permitted": 2, "practiced": 2, "rejected": 3}
    parts = _CLAUSE_SPLIT.split(sentence)
    rather = re.search(r"\b(?:rather than|instead of)\b\s+(.*)$", sentence, re.I)
    practice = bool(PRACTICE_SUPPORT.search(sentence) or PRACTICE_NAMED.search(sentence) or PRACTICE_NOTE.search(sentence)
                    or PRACTICE_UPDATE.search(sentence))
    for clause in parts:
        if not clause or not clause.strip():
            continue
        found = [a for a, pat in _ACTION_PATTERNS.items() if pat.search(clause)]
        if "anonymize" in found and "unpublish" in found:
            # "remove the name from the story": the object is the name, not the story.
            if not re.search(r"\b(?:remov|delet|take|unpublish)\w*\s+(?:the |an? |our |old |that |this )?(?:\w+ )?(?:articles?|stor(?:y|ies)|content|posts?)\b", clause, re.I):
                found.remove("unpublish")
        if "deindex" in found and "anonymize" in found and re.search(r"\bnames?\s+from\s+(?:search|google)", clause, re.I):
            found.remove("anonymize")  # "remove a name from search results" is de-indexing, not anonymizing the text
        if not found:
            continue
        neg = PROHIBITIVE.search(clause)
        perm = PERMISSIVE.search(clause)
        proc = PROCESS.search(clause) or CASE_BY_CASE_CUE.search(clause)
        narrow = NARROW_EXCEPTION.search(clause)
        if neg and not perm:
            pos = "rejected"
        elif neg and perm:
            pos = "conditional"
        elif practice and not neg:
            pos = "practiced"
        elif perm:
            pos = "permitted" if not narrow else "conditional"
        elif proc:
            pos = "conditional"
        elif _AFFIRM.search(clause):
            pos = "permitted"
        else:
            pos = "mentioned"
        for a in found:
            if rank[pos] >= rank.get(out.get(a, "mentioned"), 0) or a not in out:
                out[a] = pos
    if rather:
        for a, pat in _ACTION_PATTERNS.items():
            if pat.search(rather.group(1)):
                out[a] = "rejected"
    return out


# --------------------------------------------------------------------------- attributed voices

_NAME = r"(?:[A-Z][a-zA-Z'’.-]+(?:\s+(?:[A-Z]\.|[A-Z][a-zA-Z'’-]+)){1,3})"
_SAY = r"(?:said|says|wrote|writes|argued|argues|explained|explains|noted|notes|added|adds|told|cautioned|warned|believes|contends)"
_QUOTE = re.compile(r"[\"“]([^\"“”]{40,700})[\"”]")
_ATTR_PATTERNS = [
    re.compile(rf"^\s*,?\s*{_SAY}\s+(?P<name>{_NAME})(?:\s*,\s*(?P<role>[^,.;]{{3,120}}))?"),          # "...," said Jane Doe, editor of X
    re.compile(rf"^\s*,?\s*(?P<name>{_NAME})\s+{_SAY}"),                                                # "...," Jane Doe said
    re.compile(rf"^\s*,?\s*(?:according to)\s+(?P<name>{_NAME})(?:\s*,\s*(?P<role>[^,.;]{{3,120}}))?"),
]
_PRE_ATTR = re.compile(rf"(?P<name>{_NAME})(?:\s*,\s*(?P<role>[^,.;\"“]{{3,120}})\s*,)?\s+{_SAY}(?:\s+that)?\s*[:,]?\s*$")
_NOT_PEOPLE = re.compile(r"^(?:The|This|That|These|Those|In|On|At|For|Editor|Editors|Staff|Associated Press|New York|Los Angeles|United States)\b")


@dataclass
class Voice:
    person_name: str
    role: str | None
    quote: str
    context: str
    method: str
    confidence: float
    statement: Statement
    actions: dict[str, str]


def person_key(name: str) -> str:
    return re.sub(r"[^a-z]+", "-", name.lower().replace(".", "")).strip("-")


def extract_voices(text: str, *, author: str | None = None, author_role: str | None = None,
                   relevant_only: bool = True) -> list[Voice]:
    """Attributed opinions in a page: quoted statements with a named speaker, plus first-person
    statements by the page's byline author. Heuristic; every voice needs human verification."""
    out: list[Voice] = []
    seen: set[str] = set()

    def keep(quote: str) -> bool:
        if not relevant_only:
            return True
        tags = set(tags_for(quote))
        return bool(tags & {"unpublish", "deindex", "noindex", "anonymize", "remove", "changed_circumstances", "dismissed_charges",
                            "acquitted", "expunged_sealed", "archive_integrity", "right_to_be_forgotten", "digital_permanence",
                            "search_engine", "removal_request", "reputational_harm", "minimize_harm"}) or bool(RELIEF_ACTION.search(quote))

    for m in _QUOTE.finditer(text):
        quote = compact_ws(m.group(1)).strip(" ,")
        if not keep(quote):
            continue
        after = text[m.end():m.end() + 200]
        before = text[max(0, m.start() - 200):m.start()]
        name = role = None
        method = ""
        for pat in _ATTR_PATTERNS:
            am = pat.match(after)
            if am:
                name, role, method = am.group("name"), am.groupdict().get("role"), "quote_attribution_after"
                break
        if not name:
            bm = _PRE_ATTR.search(before)
            if bm:
                name, role, method = bm.group("name"), bm.group("role"), "quote_attribution_before"
        if not name or _NOT_PEOPLE.match(name):
            continue
        key = normalize_for_hash(quote)
        if key in seen:
            continue
        seen.add(key)
        st = classify_statement(quote)
        out.append(Voice(name.strip(), (role or "").strip() or None, quote, compact_ws(before[-150:] + " " + quote + " " + after[:150]),
                         method, 0.7 if role else 0.6, st, action_positions(quote)))
    if author and not _NOT_PEOPLE.match(author):
        for ev in extract_evidence(text, max_items=30):
            if ev.statement.statement_type == "mention" or ev.statement.statement_type.startswith("practice_"):
                continue
            key = normalize_for_hash(ev.excerpt)
            if key in seen or not re.search(r"\b(?:I|we|my|our|newsrooms?|editors?|journalists?)\b", ev.excerpt):
                continue
            seen.add(key)
            out.append(Voice(author, author_role, ev.excerpt, ev.context, "byline_author", 0.5, ev.statement,
                             action_positions(ev.excerpt)))
    return out

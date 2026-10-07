# Classification rubric (v0.3)

All rules are deterministic and implemented in `evidence.py`, `research.py`, `stance.py`, `similarity.py` and `review.py`. Each stored excerpt records the cues that fired (`evidence_items.rationale`, exported as `classifier_cues`).

## 1. Unit of evidence

One sentence of main-content text (navigation, header, footer, aside, forms, cookie/subscribe/share widgets removed first). A sentence is considered if it matches a topic tag, or contains a relief/update action together with a journalism object. Sentences that look like web chrome (under 40 characters or 7 words, menu-like word patterns, or "this page / cookie / subscribe / read more / all rights reserved…" language without a real relief action) are classified `mention`. A `mention` with fewer than two tags is discarded.

Topic tags (concepts, many per sentence): `unpublish`, `deindex`, `noindex`, `anonymize`, `remove`, `redact`, `changed_circumstances`, `dismissed_charges`, `acquitted`, `exonerated`, `vacated`, `expunged_sealed`, `plea_withdrawn`, `arrest_record`, `criminal_allegation`, `juvenile`, `rehabilitation`, `time_passage`, `reputational_harm`, `search_engine`, `digital_permanence`, `right_to_be_forgotten`, `privacy`, `safety`, `minimize_harm`, `public_interest`, `case_by_case`, `discretion`, `update_note`, `correction`, `archive_integrity`, `removal_request`, `legal_order`, `canonical_or_sitemap`.

## 2. The object + action + modality rule

Keywords alone never make a finding. A **policy statement** requires:

- **Journalism object** — article(s), story/stories, content, archive(s), coverage, report(s)/reporting, post(s), name(s), photo(s)/images, mug shots, headlines, URLs, blotter/crime logs, columns, editorials, news items, … (or a removal *request* plus a relief action);
- **Action** — relief (unpublish, remove, delete, take down, de-index, noindex, anonymize, redact, withdraw, suppress, replace names, hide from search) or update (update, append, editor's note, correction, clarification, follow-up, note the outcome/disposition);
- **Modality** (for direction):
  - *permissive* — a non-negated modal governing a relief/update verb ("may remove", "will be removed"), "in rare/certain/exceptional cases", "under certain circumstances", "where appropriate", "willing to", …
  - *process* — requests are reviewed/considered/evaluated/handled by …
  - *prohibitive* — do not, will not, never, cannot, refuse, decline, "under no circumstances", "generally do not", …
  - *narrow exception* — only/except/unless … legal, court order, libel, error/inaccuracy, safety, copyright, plagiarism, fabrication.

Changed-outcome cues (dismissed/dropped charges, acquittal, exoneration, vacatur, expungement/sealing, withdrawn plea, changed circumstances, rehabilitation, never convicted/charged, cleared) are read from the sentence and, for some types, its neighbouring sentences.

## 3. Statement types

Evaluated in this order (first match wins):

| Statement type | Rule (summary) | Direction | Relevance |
|---|---|---|---|
| `mention` | web chrome | neutral | 0.10 |
| `practice_relief_denied` | past-tense declined/refused/denied a request, or "stood by / kept the story" | adverse | 0.90 |
| `practice_relief_granted` | past-tense removed/unpublished/anonymized/de-indexed by "we/the paper/editors/it" or a named newsroom, or "began/launched a program to remove…" (no prohibitive cue) | supportive | 0.90 |
| `practice_update` | past-tense added an editor's note/update/correction | neutral | 0.60 |
| `relief_narrow_exceptions` | relief + object + prohibitive + narrow exception | adverse | 0.90 |
| `relief_rejected` | relief + object + prohibitive, no permissive | adverse | 0.90 |
| `changed_circumstance_relief` | relief + object + permissive (or prohibitive+permissive, or process) with a changed-outcome cue | supportive | 0.95 |
| `case_by_case` | relief + case-by-case/discretion, or relief + process language, or case-by-case + object/request | supportive | 0.75 |
| `relief_permitted` | relief + object + permissive, no changed-outcome cue | supportive | 0.90 |
| `changed_circumstance_update` | update action + changed-outcome cue | neutral | 0.85 |
| `update_remedy` | update action + object, no relief, no prohibition | neutral | 0.60 |
| `archive_principle` | historical record / archive integrity / permanent record language with object or "we/our" | adverse | 0.65 |
| `harm_consideration` | reputational harm / search engines / digital permanence / right to be forgotten / minimize harm with object or request | neutral | 0.40 |
| otherwise `mention` | | neutral | 0.10 |

Extraction confidence per type ranges from 0.55 (ambiguous mixed modality) to 0.85 (clear prohibition or permission).

## 4. Evidence classes and attribution (`research.evidence_class_for`)

| Source | Statement | Class |
|---|---|---|
| First-party, guidance cohort (`support_org`, `press_association`, `journalism_school`) | practice_* (describes another newsroom) | `secondary_report`, `about_entity=0` |
| First-party, guidance cohort | other | `professional_guidance` |
| First-party newsroom | practice_* | `documented_practice` |
| First-party newsroom, page kind `policy`/`about` | other | `written_policy` |
| First-party newsroom, other page | other | `editorial_statement` |
| Third-party text naming the entity | practice_* | `documented_practice` |
| Third-party text naming the entity | other | `secondary_report` |
| Third-party, not naming the entity | any | `secondary_report` (precedent pool only) |

First-party means same site as the entity homepage; for publications hosted under a path (e.g. `university.edu/student-media`) only pages under that path count. Third-party sentences are kept only if the sentence names the entity, or opens with an anaphor ("It", "The paper", "Editors", …) after a sentence that names it.

**Authority scores**: written policy 1.0 on policy pages / 0.9 otherwise; professional guidance 1.0 / 0.9; editorial statement 0.8; documented practice 0.85; third-party on known journalism domains (Poynter, SPLC, RCFP, Nieman, CJR, SPJ, ONA, API, AP, …) 0.6; `.edu`/`.gov` 0.5; other 0.35.

## 5. Stance

Inputs: unique (non-duplicate) items with `about_entity=1`, class `written_policy` or `professional_guidance`, not `mention`, extraction confidence ≥ 0.5. Counts use one item per near-duplicate group.

Decision order:

1. any `changed_circumstance_relief` → **SUPPORTS_CHANGED_CIRCUMSTANCES** (a general no-removal rule is treated as the default the exception modifies, noted in rationale);
2. `relief_permitted` and rejections → **MIXED** (review reason `conflicting_policy_statements`);
3. `relief_permitted` → **SUPPORTS_RELIEF**;
4. `case_by_case` → **CASE_BY_CASE**;
5. rejections and updates → **UPDATE_ONLY**;
6. rejections → **STRICT_ARCHIVE**;
7. updates only → **UPDATE_ONLY** (reason `update_only_without_explicit_removal_language`);
8. archive-principle language only → **STRICT_ARCHIVE**, confidence 0.55 (reason `archive_principle_only`);
9. nothing: **NO_RELEVANT_GUIDANCE** if coverage requirements are met (`docs/methodology.md`), confidence 0.70 (0.60 if no policy/about page fetched); otherwise **UNDETERMINED**, confidence 0.

`POLICY_CLASSES` in `stance.py` is `written_policy`, `professional_guidance` and `editorial_statement` (policy stated in an editor's column on the entity's own site, authority 0.8). Documented practice, secondary reports and technical observations never drive the stance.

**Confidence** for determinate stances: `base + 0.04 × min(n − 1, 4) − 0.12 if conflicting`, clamped to [0.30, 0.95], where `n` is the number of distinct supporting statements and bases are 0.78 (changed circumstances, relief, strict archive), 0.72 (case-by-case), 0.75 (update-only with rejection), 0.62 (update-only alone), 0.60 (mixed).

Each stance stores strongest supportive and adverse excerpt ids (max relevance × authority, then similarity), a coverage record, a practice summary, and a technical summary (`noindex` pages among fetched pages).

## 6. Review triggers

Reasons stored in `entity_stances.review_reasons_json`: `determinate_finding_requires_verification` (every determinate stance), `low_confidence` (< 0.7), `conflicting_policy_statements`, `policy_practice_contradiction:*`, `high_similarity_precedent`, `documented_practice`, `update_only_without_explicit_removal_language`, `archive_principle_only`.

Queue priority (`review.py`): high-similarity precedent +15 + 15 × similarity/100; changed-criminal-outcome stance +25; documented practice +20; policy/practice contradiction +25; conflicting statements +20; major national organization (benchmark/seeded) +15; determinate finding +10; low confidence +10; ambiguous publication identification +20. Publications: base 5, +20 ambiguous, +10 near threshold.

## 7. Similarity

`score = 100 × Σ(weights of matched factors) / Σ(all positive weights)`, matched against the excerpt plus its neighbouring sentences. Weights live in `config.yml` under `case_profile`; each excerpt stores matched factors, the triggering text and basis (`text` or `cohort`).

| Factor | Weight key | Default |
|---|---|---|
| Student/university setting (text, or `cohort=student_media` for first-party pages) | `student_context_weight` | 8 |
| Criminal allegation / arrest | `criminal_allegation_weight` | 10 |
| Charges dismissed or dropped | `dismissed_charges_weight` | 15 |
| Acquittal or exoneration | `acquittal_exoneration_weight` | 12 |
| Conviction vacated / plea withdrawn | `vacated_conviction_weight` | 15 |
| Record expunged or sealed | `sealed_or_expunged_weight` | 12 |
| Substantial time passed | `long_time_passed_weight` | 8 |
| Private individual / no public role | `private_individual_weight` | 6 |
| Search-engine prominence | `search_prominence_weight` | 10 |
| Reputational consequences | `reputational_harm_weight` | 8 |
| Preserve article while suppressing name search (preserve cue + de-index/noindex/anonymize cue) | `name_search_suppression_weight` | 10 |
| De-indexing | `deindex_relief_weight` | 20 |
| Anonymization / name removal | `anonymization_weight` | 12 |
| Updated disposition | `update_context_weight` | 6 |

Total default weight 152. `high_similarity_threshold` (default 45) marks high-similarity items. Similarity ranks review priority; it does not establish authority or truth.

## Legacy v0.1 classes (deep crawl, `nsmpa classify`)

Retained for the `crawl`/`classify`/`export` pipeline:

- **A_EXPLICIT_RELIEF** — explicitly permits de-indexing, anonymization, unpublishing, removal or name suppression in some circumstances.
- **B_CHANGED_CIRCUMSTANCES** — recognizes changed circumstances, rehabilitation, acquittal, dismissal, expungement/sealing, privacy, safety or reputational harm as review factors.
- **C_CASE_BY_CASE** — discretionary review without enumerated grounds.
- **D_UPDATE_ONLY** — preserves publication; updates/notes/corrections as remedy.
- **E_STRICT_ARCHIVE** — rejects removal except narrow categories.
- **F_NO_PUBLIC_POLICY_FOUND** — inspected to the coverage threshold, no policy located.
- **U_UNDETERMINED** — could not be reliably classified (blocking, failures, insufficient coverage).

These legacy classes use v0.1 keyword logic and are not used by v0.3 reports.

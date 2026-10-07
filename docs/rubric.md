# Policy classification rubric

The classifier intentionally separates editorial-policy support from technical evidence.
A publication can receive multiple issue tags, but one primary class.

## Primary classes

- **A_EXPLICIT_RELIEF**: Explicitly permits deindexing, anonymization, unpublishing, removal,
  name suppression, or a comparable post-publication remedy in at least some circumstances.
- **B_CHANGED_CIRCUMSTANCES**: Explicitly recognizes changed circumstances, rehabilitation,
  acquittal, dismissal, expungement/sealing, disproportionality, privacy, safety, or reputational
  harm as a factor in post-publication review.
- **C_CASE_BY_CASE**: Provides discretionary or case-by-case post-publication review but does not
  clearly enumerate the relevant grounds.
- **D_UPDATE_ONLY**: Preserves the original publication but permits updates, editor's notes,
  corrections, contextualization, or follow-up coverage as the primary remedy.
- **E_STRICT_ARCHIVE**: Expressly rejects removal/deindexing except narrow categories such as
  factual error, legal obligation, or extraordinary safety risk.
- **F_NO_PUBLIC_POLICY_FOUND**: The publication was successfully inspected to the configured
  coverage threshold but no public policy was located.
- **U_UNDETERMINED**: The publication could not be reliably classified because of crawl failure,
  insufficient coverage, ambiguous ownership, authentication, persistent blocking, or another
  unresolved issue.

## Evidence tags

`deindex`, `noindex`, `anonymize`, `unpublish`, `remove`, `takedown`, `changed_circumstances`,
`dismissed_charges`, `expunged`, `sealed`, `acquitted`, `rehabilitation`, `privacy`, `safety`,
`reputational_harm`, `minimize_harm`, `public_interest`, `case_by_case`, `update`, `correction`,
`archive_integrity`, `search_engine`, `robots`.

## Methodological safeguards

1. Never infer `F_NO_PUBLIC_POLICY_FOUND` from a failed crawl.
2. Keep supportive and contrary evidence.
3. Preserve exact excerpts, URLs, retrieval timestamps, hashes, HTTP status, and technical headers.
4. Separate explicit written policy from observed technical implementation.
5. Record confidence and reasons for every classification.
6. Human-review all high-impact examples used in public statistics or advocacy.

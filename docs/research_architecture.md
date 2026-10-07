# NSMPA v0.2 research architecture

## Goal

Build an auditable evidence base for how U.S. journalism handles post-publication harm and changed circumstances, especially old criminal-justice reporting whose legal outcome later changed.

## Evidence hierarchy

1. First-party written policy or ethics guidance.
2. First-party editor's note, correction, archive notice or documented post-publication action.
3. Technical practice such as `noindex`/`X-Robots-Tag` captured directly from the page.
4. First-party discussion of an editorial decision.
5. Credible secondary reporting about an editorial decision.
6. Search-result snippets, used only to discover stronger sources.

## Separate analytical cohorts

### Student media
IPEDS is the institution denominator. Discovery identifies the primary student news publication. This is the strongest candidate for a census-like national result.

### Professional newsrooms
Use the union of explicit source directories and retain source provenance. De-duplicate by canonical domain/organization while preserving directory memberships. Report the source-defined panel until completeness is established.

### Support/advice organizations
National and state ethics, legal, professional, education, press-freedom, student-media and newsroom-support organizations. Their material is guidance/standards evidence, not newsroom-practice evidence.

## Search plan

For each entity, the engine searches multiple independent concepts:

- affirmative relief: unpublish, deindex, anonymize, takedown, content removal
- editorial standards: editorial policy, ethics, corrections
- changed circumstances: dismissed/dropped charges, expungement, sealing, acquittal, vacatur
- adverse rules: historical record, archive integrity, explicit no-removal language
- actual precedents: removal requests, criminal-outcome changes, editor decisions
- search prominence: Google/search-engine consequences and deindexing

Support/advice organizations receive additional ethics/minimize-harm queries.

## Anti-cherry-picking controls

- Run supportive and adverse query families for every entity.
- Preserve all qualified targets and query ledger entries.
- Cache identical searches rather than silently rerunning them.
- Keep snippets separate from fetched source evidence.
- Store exact excerpts and snapshots.
- Surface contradictory evidence as `MIXED`.
- Maintain a human-review queue.

## Similarity scoring

The configurable similarity score ranks evidence that resembles the target fact pattern, including student context, criminal allegation, dismissed charges, vacated outcome, expungement/sealing, long passage of time, search prominence, deindexing, anonymization and post-publication updates.

This is a triage score only. It must never be presented as a legal or ethical conclusion.

# Methodology

## Research question

How do U.S. journalism organizations handle requests for post-publication relief (unpublishing, de-indexing, anonymization, name removal, updates, editor's notes) when truthful historical reporting causes continuing harm — especially after underlying circumstances change (charges dismissed or dropped, acquittal, vacated conviction, withdrawn plea, expungement or sealing, exoneration, rehabilitation, passage of time)? What written policies, documented practices, technical implementations and professional guidance exist, and what arguments are made against relief (archive integrity, historical record)?

The instrument is built to find evidence on both sides. It is not designed to prove a conclusion.

## Cohorts and denominators

1. **Student journalism** — anchored to IPEDS HD institutions with `included=1` (ICLEVEL=1 four-year-and-above, public and private nonprofit by default). One primary publication per institution.
2. **Professional journalism** — a sourced union of directories and membership lists plus a labeled benchmark panel. Reported as a *panel*, not a census, unless its sources are shown to be exhaustive (`docs/professional_universe.md`).
3. **Support, standards, legal and advisory organizations** — analyzed as sources of guidance, not as newsrooms.

Each cohort has its own denominator. Percentages from different cohorts are never combined.

### Full IPEDS denominator vs core stratum

IPEDS `ICLEVEL=1` means "offers at least one bachelor's-level program." It therefore includes institutions unlikely to have a student newspaper: e.g., Washington community colleges offering applied bachelor's degrees, seminaries and medical/arts special-focus schools, and separately reported online/adult-degree divisions. NSMPA reports both:

- **Full denominator**: all included institutions (2,517 in the current database).
- **Core stratum**: Carnegie 2021 basic classification 15–23 (doctoral, master's and baccalaureate institutions; 1,627 in the current database), exposed as `v_institution_peer.core_stratum`.

Neither is silently substituted for the other.

## Four separate concepts

| Concept | Source | Evidence class |
|---|---|---|
| Written policy | Entity's own policy/about pages | `written_policy` |
| Editorial statement | Entity's own non-policy pages stating its rules (e.g., an editor's column) | `editorial_statement` |
| Documented practice | What a newsroom actually did, first-party or third-party text naming the entity | `documented_practice` |
| Technical implementation | `noindex` meta / `X-Robots-Tag`, canonical, redirects, 404/410 | page columns, `technical_summary` |
| Professional guidance | Support/standards organizations' own pages | `professional_guidance` |

A stance describes written policy or guidance only. Practice is summarized separately; a contradiction (e.g., strict-archive policy but documented removals) is flagged for human review, never merged.

## Absence of evidence vs evidence of absence

`NO_RELEVANT_GUIDANCE` is assigned only when **all** of these hold (implemented in `stance.py`):

- at least `no_guidance_min_first_party_pages` (default 2) first-party pages fetched successfully;
- the homepage or at least one first-party policy/about page was fetched successfully;
- tier-1 targeted searches were run with a real search provider and all completed (null-provider calls do not count);
- inaccessible first-party pages ≤ `no_guidance_max_inaccessible_ratio` (default 0.5) of first-party pages attempted.

Otherwise the result is `UNDETERMINED`, with the missing conditions listed in the rationale. Blocked (401/403/451), rate-limited, robots-disallowed, server-error, timeout and similar results always count as *inaccessible*, never as "no policy."

## Adverse-evidence symmetry

Tier-1 searches always include an adverse query (`never unpublish`, `do not remove`, `historical record`, `archive integrity`, `requests to remove`). Tier 2 adds request-handling and third-party "declined/refused to remove" queries; tier 3 issues each adverse phrase individually. Adverse statement types (`relief_rejected`, `relief_narrow_exceptions`, `archive_principle`, `practice_relief_denied`) are stored, scored and reported with the same weight and traceability as supportive ones, and every stance carries both its strongest supportive and strongest adverse excerpt.

## Evidence unit and deduplication

The unit is one sentence of main-content text. Each unique sentence is stored once per entity per run; concept tags are linked separately. Near-duplicate sentences (5-word shingle fingerprint) within an entity are marked `duplicate_of`. Reports count distinct near-duplicate groups.

## Similarity

A transparent weighted model (`docs/classification_rubric.md#similarity`) ranks excerpts for review. It is not a measure of authority or truth.

## Limitations

- **Site-inspection recall is low.** Free discovery via institution homepages, sitemaps and student-life pages often fails to reach the student newspaper (e.g., EWU's hub pages do not link *The Easterner*). Search is the primary discovery method.
- **Bot protection.** Some institutions and newsrooms return 403 to any non-browser client (e.g., Gonzaga's WAF). These are recorded as `blocked` and yield `UNDETERMINED`, not negative findings. NSMPA does not evade such protections.
- **Search engines are an imperfect lens.** Absence of a page from Google results is not evidence of de-indexing; only observed `noindex`/`X-Robots-Tag`, robots rules, 404/410, redirects or explicit statements count.
- **Rule-based classification** is deterministic and auditable but will miss unusual phrasing; every determinate finding enters the review queue.
- **Professional universe** coverage depends on imported directories.
- IPEDS fields (Carnegie, size, region) are as reported by IPEDS for the import year.

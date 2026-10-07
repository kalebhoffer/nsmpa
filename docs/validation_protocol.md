# Validation protocol for publishable findings

Automated output is a research index, not the final evidentiary record. Use this protocol before
publishing percentages, sending advocacy materials, or citing a publication as precedent.

## 1. Discovery QA

For a stratified sample by state, institution control, and enrollment tier:

- confirm that the selected publication is a current student news organization;
- confirm that the correct institution-publication relationship was assigned;
- check for missed independent-domain publications;
- record false positives and false negatives.

Do not describe the dataset as national until discovery recall is acceptably high and the unresolved
share is reported.

## 2. Classification QA

Human-review 100% of A, B, and E cases used externally. Review a random sample of C, D, and F cases.
For each reviewed case, confirm:

- exact wording against the stored snapshot;
- policy applicability and date;
- whether the policy is official, staff-authored, or merely descriptive reporting;
- whether relief language is affirmative, hypothetical, or quoted from a third party;
- whether technical noindex is intentional policy implementation or a site/configuration artifact.

## 3. Denominator discipline

Always report at least these counts:

- institutions in the target universe;
- institutions with a discovered publication;
- discovered publications successfully crawled;
- publications with determinate classifications;
- `U_UNDETERMINED` count;
- human-verified count.

Never place `U_UNDETERMINED` in the no-policy denominator.

## 4. Contradictory evidence

If a publication contains both permissive and restrictive language, retain both. Mark the case for
human review and quote the operative policy rather than forcing an advocacy-friendly interpretation.

## 5. Changed-outcome subset

For the Easterner research question, separately report policies mentioning:

- charges dismissed or dropped;
- acquittal or exoneration;
- expungement or record sealing;
- changed circumstances;
- rehabilitation;
- disproportionate or reputational harm;
- deindexing or anonymization as alternatives to deletion.

This subset should be reported in addition to, not instead of, the full national classification.

## 6. Reproducibility

Retain the run ID, config file, source snapshots, SHA-256 hashes, retrieval timestamps, and exported
row-level evidence. A later rerun should be treated as a new observation because policies and sites
can change.

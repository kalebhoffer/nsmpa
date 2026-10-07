# Validation

Before any cohort percentage appears in a report, the cohort must pass every gate below (`validate.py`; thresholds in `config.yml` under `validation`). If any gate fails, the cohort is labelled **PRELIMINARY — not valid for national claims**: counts are shown, percentages are printed as "withheld (gates failing)", and failing gates are listed.

Scope: by default the latest v0.3 stance per entity across all runs; `--run-id` restricts to one run. Legacy v0.2 runs are excluded.

## Gates

| Gate | Metric | Default | Applies to |
|---|---|---|---|
| publication discovery coverage | institutions with a primary publication ÷ included institutions | ≥ 0.80 (`min_discovery_coverage`) | student media |
| minimum researched entities | entities with a stance | ≥ 30 (`min_entities_for_percentages`) | all |
| research coverage of universe | researched ÷ cohort denominator | ≥ 0.80 (`min_discovery_coverage`) | all |
| first-party fetch success | first-party pages `ok` ÷ first-party pages attempted | ≥ 0.75 (`min_crawl_success`) | all |
| undetermined rate | `UNDETERMINED` ÷ researched | ≤ 0.25 (`max_undetermined_rate`) | all |
| human review of findings | determinate stances with `review_status ≠ unreviewed` ÷ determinate | ≥ 0.20 (`min_human_review_of_findings`) | all |
| source verification of evidence | substantive excerpts `verified` ÷ substantive excerpts | ≥ 0.10 (`min_source_verification`) | all |
| mean stance confidence | mean confidence of determinate stances | ≥ 0.65 (`min_mean_stance_confidence`) | all |
| duplicate evidence rate | items with `duplicate_of` ÷ all items | ≤ 0.35 (`max_duplicate_evidence_rate`) | all |

A gate with no data fails.

Additional student metrics reported (not gated): core-stratum discovery rate, discovery attempted, unresolved rate of attempted, publication verification rate, ambiguous identifications, institutions without a website.

## Human review workflow

```bash
nsmpa review                                   # prioritized open items
nsmpa review --cohort student_media --limit 50
nsmpa review --export review.csv               # full open queue
nsmpa review --decide 42 --decision accept --note "policy text matches live page and snapshot"
nsmpa review --decide 43 --decision correct --stance CASE_BY_CASE --note "…"
nsmpa review --decide 44 --decision reject --note "excerpt is about a different paper"
```

Decisions (`accept`, `reject`, `correct`, `skip`) are stored in `review_queue` with reviewer, note and timestamp, and propagate to `entity_stances.review_status`. Accepting or rejecting a stance marks its strongest supportive/adverse excerpts `verified` / `rejected`. For publications, `accept` sets `verification_status='human_verified'` (protected from later automatic promotion). Machine output is never deleted.

## Source verification

For each excerpt cited externally:

1. Open `source_url` and confirm the sentence is present (or note the change).
2. Compare with the snapshot (`research_snapshots/raw/<sha[:2]>/<page_sha256>`) and normalized text (`text/<text_sha256>.txt`).
3. Record: `nsmpa verify-evidence ID --status verified|rejected|disputed|unverified --note "…"`.

## Before external use

- Run `nsmpa validate` and `nsmpa report`; cite only VALIDATED cohort percentages.
- Report each cohort's denominator note verbatim, including the "panel, not a census" caveat for professional newsrooms.
- Distinguish written policy, documented practice, technical implementation and guidance.
- Treat `UNDETERMINED` as not inspected, never as "no policy."

# NSMPA v0.2 — National Journalism Post-Publication Policy Audit

NSMPA began as the National Student Media Policy Audit. v0.2 preserves that student-media census and adds a broader, Serper-assisted research engine for U.S. journalism.

The research question is broader than "does this newspaper delete stories?" NSMPA looks for written policy, actual post-publication practices, changed-circumstance precedents, archive restrictions, deindexing/anonymization, noindex behavior, criminal-case outcome changes, and guidance from journalism standards/support organizations.

## The three-cohort rule

NSMPA keeps three primary universes separate:

1. **Student media** — census-oriented, anchored to the IPEDS four-year institution universe.
2. **Professional newsrooms** — a sourced panel/union built from benchmark outlets and imported publisher/press-association directories. Do not call it an exhaustive national census until the underlying directories justify that denominator.
3. **Journalism support/advice organizations** — ethics, legal-support, training, research, membership, press-freedom and student-media organizations.

Evidence may be compared across cohorts. Percentages may not share a denominator across cohorts.

## What v0.2 adds

- Serper-first search in `auto` mode, with Brave still supported.
- Adaptive student-publication discovery that stops spending search credits once confidence is high.
- Live Rich progress dashboard for long discovery/research runs.
- `--verbose` and `--quiet` modes.
- Search query ledger with purpose, entity/institution, provider, result count and estimated credits.
- Durable search cache so repeated runs do not repay for identical queries.
- Per-run hard search-credit budget.
- Cross-journalism `research_entities` model.
- Built-in national journalism support/advice organization seed.
- Built-in professional newsroom benchmark panel.
- Generic CSV importer for larger newsroom, press-association or journalism-school universes.
- Targeted policy searches: unpublishing, deindexing, anonymization, takedown, editorial policy, corrections, archives and changed circumstances.
- Targeted adverse-evidence searches: no-removal rules, permanent archives, historical-record language and refused takedown requests.
- Fact-pattern precedent searches for dismissed/dropped charges, expungement, sealing, acquittal, vacated convictions and removal requests.
- Direct fetching of high-value search results, exact excerpt extraction, source snapshots and hashes.
- Cross-cohort stance classification: `SUPPORTS_RELIEF`, `SUPPORTS_CHANGED_CIRCUMSTANCES`, `CASE_BY_CASE`, `UPDATE_ONLY`, `STRICT_ARCHIVE`, `MIXED`, `NO_RELEVANT_GUIDANCE`, `UNDETERMINED`.
- Configurable case-similarity score to rank precedents for human review.
- Separate research exports for queries, targets, evidence, stances and human review.

The original hardened crawler remains in place for deep student-publication site crawling.

## Install / upgrade

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[pdf,dev]'
cp config.example.yml config.yml
```

Set an identifiable `user_agent` in `config.yml` before a large crawl.

If you upgraded an existing v0.1 database, v0.2 is additive. Opening the database creates the new tables without deleting IPEDS institutions, publication candidates, publications, pages or evidence.

## Search API

Serper is preferred for this project:

```bash
export SERPER_API_KEY='YOUR_KEY'
```

Do not store a key in the repository.

`search_provider: auto` now prefers Serper when present, then Brave.

## Student-media census

```bash
nsmpa init --config config.yml
nsmpa import-ipeds HD2024.zip --source-year 2024 --config config.yml
nsmpa discover --limit 50 --config config.yml
```

The discovery dashboard shows institution progress, current phase, candidates, high-confidence hits, live/cached searches, estimated credits, fetches and errors.

After QA:

```bash
nsmpa promote --config config.yml
nsmpa sync-student-entities --config config.yml
```

Deep crawl/classification is unchanged:

```bash
nsmpa crawl --config config.yml
nsmpa classify --run-id RUN_ID --config config.yml
nsmpa export --run-id RUN_ID --config config.yml
```

## Broader journalism setup

Seed the built-in support/advice organizations, professional benchmark panel, and already-promoted student media:

```bash
nsmpa research-setup --config config.yml
nsmpa status --config config.yml
```

Or seed separately:

```bash
nsmpa seed-support-orgs --config config.yml
nsmpa seed-benchmark-newsrooms --config config.yml
nsmpa sync-student-entities --config config.yml
```

## Import larger professional-newsroom universes

Create a CSV with at least `name,url` and optional fields such as `state`, `parent_name`, `source_key` and any other metadata columns.

```csv
name,url,state
Example News,https://example.org,CO
```

Import it:

```bash
nsmpa import-entities newsrooms.csv \
  --cohort professional_newsroom \
  --source inn_directory_2026 \
  --config config.yml
```

The same importer supports `press_association`, `journalism_school`, `support_org` and other research cohorts.

See `data/professional_universe_sources.csv` and `docs/professional_universe.md` for recommended expansion sources.

## Run the research engine

Pilot one cohort first:

```bash
nsmpa research --cohort support_org --limit 10 --config config.yml
```

Then student media:

```bash
nsmpa research --cohort student_media --limit 25 --config config.yml
```

Then professional newsrooms:

```bash
nsmpa research --cohort professional_newsroom --limit 25 --config config.yml
```

For unattended use:

```bash
nsmpa research --cohort support_org --quiet --config config.yml
```

For detailed diagnostics:

```bash
nsmpa research --cohort support_org --verbose --config config.yml
```

Each run automatically exports to `output/research_<RUN_ID>/`.

## Research output

- `entities.csv`
- `search_queries.csv`
- `research_targets.csv`
- `research_evidence.csv`
- `entity_stances.csv`
- `human_review_queue.csv`
- `summary.md`

Fetched research sources are snapshotted under `output/research_snapshots/<run_id>/<entity_id>/` when enabled.

## Search budget and cache

Important config values:

```yaml
search_credit_budget_per_run: 50000
search_cache_days: 3650
discovery_max_searches_per_institution: 4
research_max_searches_per_entity: 18
research_fetch_top_targets: 18
```

The query cache is keyed by provider + normalized query + requested result count. A cached hit is still written to the run ledger, but consumes zero estimated live-search credits.

## Research integrity

- Search snippets are discovery evidence, not final substantive evidence.
- A substantive finding should come from fetched source text and be manually verified before external citation.
- Supportive and adverse evidence are searched deliberately.
- `UNDETERMINED` is never converted into a negative finding because a source failed to load.
- Case-similarity scores prioritize review; they do not establish authority or truth.
- Student media, professional newsrooms and support/advice organizations must be reported separately.
- A professional-newsroom panel built from directories must not be described as "all U.S. journalism" unless its denominator and inclusion rules are demonstrably exhaustive.

## Testing

```bash
pytest -q
```

v0.2 includes tests for IPEDS ingestion, URL hardening, discovery scoring, noindex extraction, publication classification, crawl persistence, search caching/ledgers, support-org seeding, generic newsroom imports, student-entity syncing and case-similarity ranking.

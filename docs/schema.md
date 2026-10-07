# Database schema (v3)

SQLite at `output/nsmpa.sqlite3`, WAL journal, `foreign_keys=ON`, busy timeout 30 s.

## Migration framework (`migrations.py`, `db.py`)

- `schema_migrations(version, description, applied_at)` records applied versions. Databases from v0.1/v0.2 (which recorded only `meta.schema_version=2`) are registered as v2 automatically.
- Before the first pending migration runs on a database that contains institutions, a full SQLite backup is written to `output/backups/<db>-pre-migrate-v<N>-<UTC>.sqlite3`.
- Each migration runs statement-by-statement inside one transaction (not `executescript`, which commits implicitly); any failure rolls the whole migration back.
- Migrations are additive: new tables, `ALTER TABLE ADD COLUMN`, indexes, views. Nothing is dropped or rewritten. Views are refreshed on every open.
- `nsmpa init` applies pending migrations; `nsmpa doctor` reports pending migrations and integrity.

## Legacy tables (v0.1/v0.2, retained)

`meta`, `institutions`, `publication_candidates`, `publications`, `crawl_runs`, `pages`, `evidence`, `classifications`, `errors`, `research_runs`, `research_entities`, `search_cache`, `search_queries`, `search_results`, `research_targets`, `research_pages`, `research_evidence` (v0.2 evidence; legacy, not used by v0.3 reports), `entity_stances`.

## Columns added in v3

| Table | Added columns |
|---|---|
| `research_runs` | `engine_version` ('0.2' for legacy), `command`, `params_json`, `status_reason`, `last_checkpoint_at`, `max_searches` |
| `search_queries` | `produced_evidence`, `latency_ms`, `refresh` |
| `research_pages` | `canonical_url`, `access_class`, `page_kind`, `text_sha256`, `redirect_chain_json`, `in_sitemap`, `query_id`, `first_party` |
| `entity_stances` | `stance_version`, `strongest_supportive_id`, `strongest_adverse_id`, `coverage_json`, `review_reasons_json`, `review_status`, `practice_summary`, `technical_summary` |
| `research_entities` | `merged_into`, `priority_tier`, `entity_type` |
| `publications` | `relationship`, `discovery_method`, `candidate_id`, `ambiguous`, `verification_note`, `verified_at` |
| `publication_candidates` | `discovery_run_id`, `verification_json`, `verified_score` |
| `errors` | `entity_id`, `host`, `research_run_id` |

## New tables in v3

| Table | Purpose / key columns |
|---|---|
| `run_items` | Checkpoints. PK `(run_id, item_type, item_key)`; `status` (pending/running/done/failed), `attempts`, `error`, `result_json` |
| `snapshots` | Content-addressed files. PK `sha256`; `kind` (raw/text), `path`, `content_type`, `bytes`, `first_url` |
| `evidence_items` | One unique sentence per `(run_id, entity_id, excerpt_sha256)`: `cohort`, `source_url/title/domain`, `fetched_at`, `page_sha256`, `text_sha256`, `query_id`, `excerpt`, `context`, `near_dup_key`, `duplicate_of`, `first_party`, `about_entity`, `evidence_class`, `statement_type`, `direction`, `topic`, `authority_score`, `relevance_score`, `similarity_score`, `similarity_factors_json`, `extraction_confidence`, `rationale` (classifier cues), `verification_status`, `reviewer_note` |
| `evidence_item_tags` | Concept tags. PK `(evidence_id, tag)` |
| `review_queue` | `(run_id, item_type, item_id)` unique; `entity_id`, `cohort`, `priority`, `reasons_json`, `status`, `decision`, `reviewer`, `note`, `reviewed_at` |
| `entity_sources` | Provenance/membership per entity. Unique `(entity_id, source, source_key)`; `source_url`, `membership_label`, `raw_json` |
| `institution_attributes` | Peer attributes. PK `(unitid, key, source)`; `value` |

Indexes cover run/status, entity/direction, similarity, near-duplicate key, tags, review priority, sources, and join columns.

## Views

| View | Contents |
|---|---|
| `v_institution_peer` | Per institution: `control_label`, `obe_region`, `size_category`, `carnegie_basic`, `hbcu`, `land_grant`, `research_university` (Carnegie 15–16), `core_stratum` (Carnegie 15–23), `county` |
| `v_evidence_unique` | Non-duplicate evidence with entity name and `;`-joined tags |
| `v_cohort_stance_counts` | Stance counts by run and cohort |
| `v_query_usefulness` | Per run and purpose: queries, cached, credits, useful, useful rate |

## Traceability

Aggregate → `entity_stances` → `strongest_*_id` / `evidence_items` → `source_url`, `page_sha256` (→ `snapshots.path`), `query_id` (→ `search_queries` → `search_results`), `run_id` (→ `research_runs.config_json`, `params_json`).

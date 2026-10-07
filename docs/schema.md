# Data model summary

Core entities:

- `institutions`: source universe and filter fields.
- `publication_candidates`: every discovered student-media candidate with explainable score.
- `publications`: validated student news organizations.
- `crawl_runs`: immutable run metadata.
- `pages`: crawl status, canonical URL, content hash, headers, indexing signals.
- `evidence`: exact excerpts and normalized evidence tags.
- `classifications`: primary class, confidence, rationale, and inspection completeness.
- `errors`: structured failures for retry and audit.

SQLite is the default portable store. WAL mode and idempotent unique keys support resumable runs.

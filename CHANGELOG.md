# Changelog

## 0.5.0 — archive evidence, AI second opinion, measured accuracy

- Wayback Machine comparison (`wayback.py`, runs inside `research` and as `nsmpa wayback`): archived vs. current
  versions of sampled crime articles and now-404 article URLs; detects names removed, noindex added, headline and
  text changes, and unpublishing. Names of people are never stored (count + hash only); titles are not stored.
- AI second opinion (`nsmpa ai-review`, Claude API, default `claude-opus-5-5`, structured JSON output): every quote
  verified verbatim against the saved page, fabricated quotes discarded, agreement/disagreement with the rule-based
  classifier recorded, disagreements sent to review; neutral prompt; cached by model+prompt version+page hash;
  per-run call cap; never changes a stance. Server-side refusal fallback enabled by default (`ai_refusal_fallback`).
- Accuracy audit (`nsmpa audit sample|export|import|label|report`): seeded stratified sample, blind Excel/CSV or
  terminal labeling, relevance precision and direction accuracy with Wilson 95% intervals, confusion matrix,
  Cohen's kappa between two labelers, AI-vs-human accuracy.
- Packet: new sheets Archive Changes, AI Second Opinion, Accuracy; new accuracy slide.
- Schema v5 (additive): ai_cache, ai_reviews, ai_findings, wayback_checks, audits, audit_items, audit_labels.

## 0.4.0 — evidence packet

- Per-action positions: clause-level analysis separates unpublish / de-index / anonymize / update; `relief_mode`
  and `preserves_archive_relief` per organization (written policy, practice and technical kept separate).
- Deep archive practice digging: 4 practice searches per newsroom plus free oldest-to-newest sampling of
  crime/arrest articles from sitemaps; editor's notes, name-removal notes and targeted `noindex` (baseline-checked;
  site-wide noindex excluded) recorded as documented practice / technical evidence.
- Case profile (`my_case.yml`, gitignored) and per-excerpt `case_match_score` with matched facts.
- Named-precedent leads with automatic source finding and human-only verification (`verify-precedents`, `precedent`).
- Expert voices: attributed-quote extraction on every page, seeded experts, `research-experts`, `add-expert`, `voices`.
- `nsmpa packet`: Excel evidence workbook (13 sheets, linked sources) + PowerPoint deck (native charts) + summary.
- Classifier: imperative guidance ("Consider…", "Provide updated…"), should/must, "rather than" clauses,
  editor's-note practice, broader dismissed-charge and name-removal patterns, less aggressive chrome filter.
- Schema v4 (additive): voices, experts, precedent_seeds; new columns on evidence_items, entity_stances, research_pages.

## 0.3.0 — 2026-10-07

Hardening and methodology release. Existing data preserved; schema migrated additively (v2 → v3) with automatic backup.

### Bugs fixed
- **Redirect loop on most WordPress sites.** Identity normalization stripped trailing slashes and was applied to redirect targets (`/a` → 301 `/a/` → `/a` …). Fetching now uses `prepare_request_url` (path preserved); `normalize_url` is used only for de-duplication. Loop detection added.
- **Keyword false positives.** v0.2 scored "Please do not remove this page" as adverse, newsletter "archive" links as strict-archive, and a bibliography entry containing "Privacy" as supportive. Replaced by a sentence-level statement classifier (object + action + modality) with boilerplate stripping.
- **Third-party contamination.** Unrelated pages returned by name-only queries were attributed to the entity. Stances now use only first-party policy/guidance; third-party text must name the entity (or follow an anaphor that does) and is stored as documented practice or secondary report.
- **Evidence inflation.** One sentence matching five tags was five rows. Evidence is now one row per unique sentence per entity per run, tags in a join table, near-duplicates collapsed.
- **Null-provider `NO_RELEVANT_GUIDANCE`.** Without an API key, no-op searches counted as completed searches, producing negative findings. Searches are skipped without a real provider and coverage requires real core searches; regression test added.
- **403/bot blocks recorded as `failed` with no error.** Responses are now classified (`blocked`, `rate_limited`, `robots_disallowed`, `server_error`, `timeout`, …) and inaccessible classes count against coverage.
- **Migration atomicity.** `executescript` committed implicitly; migrations now run statement-by-statement inside one transaction each and roll back on failure.
- `Database.execute` turned named (dict) parameters into a tuple of keys.
- Fetcher deadlock risk (pacing waited on the robots lock while robots.txt was being fetched); fractional `Crawl-delay` was ignored.

### Added
- Versioned migration framework (`schema_migrations`), auto-backup before migrating, legacy v0.2 runs labeled `engine_version='0.2'` and excluded from reports.
- Hardened fetcher: DNS + connected-peer SSRF checks (rebinding), IPv4-mapped/6to4/Teredo unwrapping, numeric-host rejection, port allowlist, credential-URL rejection, no proxy env, content-type gate before buffering, streaming size cap, retries with jittered backoff for transport errors/429/5xx, Retry-After, RFC 9309 robots semantics (5xx = disallow), Crawl-delay, per-host circuit breaker, `fetch_safe`.
- Search broker: retries, auth-error stop, Serper credit tiers (1 credit ≤10 results, 2 above; provider-reported credits used when present), `--max-searches` (per invocation) plus cumulative run budget, `--refresh-search`, concurrent identical-query coalescing, `produced_evidence` and latency in the ledger, key redaction.
- Run lifecycle: `run_items` checkpoints, resume by run id, `nsmpa resume`, graceful Ctrl+C (first = finish and checkpoint, second = cancel), budget stops recorded as `budget_exhausted`.
- Dashboard v2 (Serper, Results, Fetching, Timing panels, ETA, last checkpoint, recent discoveries); non-TTY heartbeat; `--quiet` / `--verbose`.
- Main-content extraction (nav/header/footer/aside/cookie removal, encoding detection, lxml), content-addressed immutable snapshots of raw bytes and normalized text.
- Evidence engine: statement types and directions, evidence classes (written policy, editorial statement, documented practice, professional guidance, secondary report), authority/relevance/similarity scores, classifier cues stored per excerpt.
- Transparent similarity model with per-factor matches and configurable weights (four new factors).
- Stance v2 with strongest supportive/adverse excerpts, coverage record, policy-vs-practice contradiction flags, technical summary.
- Research planner v2: tiered adaptive queries (tier 1 always; tier 2 on signal / guidance orgs / deep; tier 3 deep), free first-party homepage policy-link discovery, bounded entity concurrency.
- Discovery v2: specified query ladder with early stop, free site inspection between ladder steps, candidate homepage verification (student identity vs institutional PR), ambiguity detection, cleaned publication names, relationship and discovery method, human/manual verifications never overwritten.
- Human review queue with documented priority scoring; `review`, `verify-evidence`.
- Validation gates and `validate`; national `report` (Markdown/JSON/CSV) with peer groups (WA, Eastern WA, EWU comparables proxy, control, region, size, R1/R2, core stratum, imported attributes) and precedent tables.
- Professional universe provenance (`entity_sources`), domain-level merge on import, `merge-duplicates`, `import-peer-attributes`.
- Commands: `doctor`, `status` (expanded), `runs`, `query-stats`, `errors`, `resume`, `validate`, `report`, `review`, `verify-evidence`, `version`; `.env` loading; `./config.yml` auto-detected.
- SQL views: `v_institution_peer`, `v_evidence_unique`, `v_cohort_stance_counts`, `v_query_usefulness`.
- Test suite expanded from 18 to 139 tests (fetcher/SSRF, statements, pipeline, budgets, resume, Ctrl+C, migrations, exports, report gating); no live credits used.

## 0.2.0

- Expanded from student-only policy audit to three separate journalism research cohorts.
- Added Serper-first adaptive discovery and research queries.
- Added live progress dashboard, quiet and verbose modes.
- Added durable search cache, query/result ledger and per-run credit budget.
- Added support/advice organization seed and professional newsroom benchmark panel.
- Added generic professional newsroom/support entity CSV importer.
- Added targeted affirmative, adverse, changed-circumstance and precedent search families.
- Added direct target fetching, research snapshots, evidence extraction and stance classification.
- Added configurable fact-pattern similarity ranking.
- Added cohort-specific research exports and methodology guardrails.
- Preserved v0.1 IPEDS, publication, crawl, noindex and A-F/U classification workflows.

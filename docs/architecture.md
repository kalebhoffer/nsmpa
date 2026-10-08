# Architecture

## Modules (`src/nsmpa/`)

| Module | Role |
|---|---|
| `config.py` | Pydantic settings, case-profile weights, validation thresholds, peer groups, `.env` loading |
| `db.py`, `migrations.py` | SQLite connection (WAL, foreign keys), versioned additive migrations, views |
| `utils.py` | URL identity vs request forms, SSRF host/IP checks, hashing, near-duplicate keys |
| `fetch.py` | Hardened async fetcher, robots cache, access classification |
| `search.py` | Serper/Brave/Null providers, `SearchBroker` (cache, ledger, budgets, retries) |
| `runs.py` | Run creation/resume, `run_items` checkpoints, `StopController` (Ctrl+C) |
| `progress.py` | Rich live dashboard / heartbeat / verbose log |
| `ingest.py` | IPEDS HD import and institution filters |
| `discovery.py` | Student-publication discovery, verification, promotion |
| `extract.py` | Main-content extraction, robots meta/X-Robots-Tag, canonical, links; legacy `analyze_page` |
| `evidence.py` | Sentence splitting, topic tags, statement classification, evidence extraction |
| `similarity.py` | Transparent fact-pattern similarity |
| `research.py` | Universe management, query planner, `EntityResearcher`, `research_all` |
| `stance.py` | Entity stance classification and coverage |
| `review.py` | Review queue priority, decisions, evidence verification |
| `snapshots.py` | Content-addressed immutable snapshots |
| `validate.py` | Cohort metrics and validation gates |
| `report.py`, `export.py` | National report; per-run exports |
| `doctor.py`, `cli.py` | Environment checks; Typer CLI |
| `crawl.py`, `classify.py` | Legacy v0.1 deep crawl and A–F/U classification (retained) |
| `models.py` | Plain dataclasses shared across modules (`FetchResult`, `SearchResult`, …) |
| `identity.py` | Website identity check before any credits are spent (repurposed/parked domains, student vs newsroom signals) |
| `fallback.py` | Sites that block robots: archived copies, search-snippet leads, AI search with word-for-word confirmation |
| `capture.py` | Researcher captures (pasted text with SHA-256 fingerprint), capture queue |
| `practice.py`, `wayback.py` | Documented practice in archived crime articles; Internet Archive comparisons (names removed, noindex added) |
| `seeds.py`, `evidence_index.py` | Precedent and expert seeds; importing a researcher's evidence index without biasing rates |
| `reclassify.py` | Re-judge stored excerpts with the current classifier and recompute stances (no fetching) |
| `benchmarks.py`, `support_orgs.py`, `directory.py` | Built-in benchmark newsrooms and standards organizations; newspaper-directory import and IPEDS matching |
| `ai_review.py`, `ai_discovery.py` | AI second opinion (quotes verified against fetched text); AI-assisted publication picks; shared AI cache |
| `audit.py` | Accuracy audits: stratified samples, human labels, Wilson intervals, Cohen's kappa |
| `packet.py`, `redact.py` | Excel/PowerPoint evidence packet; automated redaction of private individuals' names |
| `gui.py`, `notify.py` | Local GUI (localhost-only, token-guarded writes) and offline dashboard; finish notifications |
| `outreach.py`, `legal.py`, `recheck.py` | Draft-only outreach; legal-context leads; scheduled policy re-checks |
| `estimate.py`, `pilot.py` | Cost/time estimates; capped end-to-end pilot with a plain-language report |

## Data flow

```
IPEDS HD ─► institutions ─► discover ─► publication_candidates ─► promote ─► publications
                                                                              │ sync
seeds / CSV imports ─► research_entities (+ entity_sources) ◄─────────────────┘
                              │
             research: SearchBroker ─► search_queries/search_results ─► research_targets
                              │                        (search_cache)
                     HardenedFetcher ─► research_pages + snapshots
                              │
               extract_main_text ─► extract_evidence ─► evidence_items (+ evidence_item_tags)
                              │
                classify_entity ─► entity_stances ─► review_queue
                              │
                 validate / report / export
```

## Run lifecycle

- `create_or_resume_run` inserts a `research_runs` row (mode, config JSON, engine version, git SHA, params) or, for an existing id, sets it back to `running` and returns any `run_items` left `running` to `pending`.
- Each unit of work (institution for discovery, entity for research) is a `run_items` row: `pending → running → done | failed`. Items already `done` are skipped on resume; `--fresh` resets them to `pending`.
- Final status: `completed`, `interrupted` (Ctrl+C with work remaining), `budget_exhausted`, or `failed` (e.g. search auth error). `status_reason` records why.
- An entity interrupted by budget exhaustion returns to `pending`; its already-paid searches are cached, so re-processing costs no credits.

## Concurrency model

- Single asyncio event loop; all SQLite writes happen on that thread (no write contention).
- Worker pool: `discovery_concurrency` (default 4) or `research_concurrency` (default 3) workers pull from a queue.
- Fetcher limits: `max_concurrency` (16) global, `max_per_host_concurrency` (2) per host, `per_host_delay_seconds` (1.5 s) or robots `Crawl-delay` (capped at 30 s) between same-host requests.
- Identical concurrent search queries are coalesced so only one live call is made.
- `StopController`: first SIGINT sets `stop_requested` (workers finish their current item and stop taking new ones); second SIGINT cancels tasks, and cancelled items return to `pending`.

## Fetcher safety guarantees (`fetch.py`)

- Only `http`/`https`; ports limited to `allowed_ports` (80, 443, 8080, 8443); URLs with credentials, control characters, malformed hosts/IPv6 or invalid ports rejected.
- Every hop (initial URL and each redirect) is DNS-resolved and every resolved address must be globally routable; IPv4-mapped, 6to4 and Teredo IPv6 forms are unwrapped; numeric/hex host encodings are rejected. After connecting, the peer address is re-checked (defeats DNS rebinding). Proxy environment variables are ignored.
- Redirects bounded by `max_redirects` (8) with loop detection.
- Connect/read timeouts; `Content-Length` and streaming size limit `max_response_bytes` (10 MB); non-textual content types (other than PDF when `allow_pdf`) are not buffered.
- Retries (`max_retries`, default 3) with jittered exponential backoff for transport errors, 429 and 5xx; `Retry-After` honored (capped at 60 s).
- robots.txt (RFC 9309): 4xx/unreachable = no restrictions; 5xx = full disallow; fractional `Crawl-delay` parsed.
- Per-host circuit breaker after `host_failure_threshold` (6) consecutive failures.
- `fetch_safe` never raises; it returns an `access_class` (`ok`, `blocked`, `rate_limited`, `not_found`, `gone`, `server_error`, `robots_disallowed`, `timeout`, `network_error`, `too_large`, `unsafe_destination`, `redirect_error`, `invalid_url`, `host_unavailable`, `unsupported_content`, …). HTML parse failures are captured, not raised.

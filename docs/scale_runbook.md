# Scale runbook

## Credit budgeting (Serper, default settings: 8 results/query = 1 credit)

| Stage | Per item | Items | Typical | Ceiling |
|---|---|---|---|---|
| Student discovery | 1 query, escalating to ≤ 5 with early stop | 2,517 institutions | ~3,000–6,000 | 12,585 |
| Support orgs research (standard) | tier 1 = 3 + tier 2 = 6 | 46 | ~414 | 414 |
| Benchmark newsrooms (standard) | 3, or 3 + 4 = 7 on signal | 45 | ~150–300 | 315 |
| Student media research (standard) | 3, or 7 on signal | ~2,000 publications | ~6,000–14,000 | 14,000 |
| Deep research (tier 3 adds 40) | up to 47–49 (cap: `research_deep_max_searches_per_entity`, default 50) | per entity | — | — |

Ranges are estimates; `nsmpa query-stats` and the dashboard show actual spend. Re-runs cost nothing for previously cached queries.

## Staged national sequence

Always pilot first, inspect, then scale.

```bash
nsmpa doctor && nsmpa init && nsmpa status
# Pilot (≤ 50 credits)
nsmpa research --cohort support_org --limit 3 --max-searches 30
nsmpa discover --state WA --limit 10 --max-searches 40
nsmpa promote && nsmpa review --cohort student_media
# Support orgs (~420)
nsmpa research --cohort support_org --max-searches 500
# Benchmark newsrooms (~300)
nsmpa research --cohort professional_newsroom --max-searches 400
# Student discovery in budgeted chunks; rerun the printed resume command (or `nsmpa resume`) until remaining = 0
nsmpa discover --max-searches 2000
nsmpa resume --max-searches 2000
nsmpa promote ; nsmpa review --cohort student_media ; nsmpa sync-student-entities
# Student research in chunks
nsmpa research --cohort student_media --max-searches 3000
nsmpa resume --max-searches 3000
# Imported professional directories (see professional_universe.md), then research
nsmpa research --cohort professional_newsroom --max-searches 3000
# Review, verify, validate, report
nsmpa review ; nsmpa verify-evidence ID --status verified
nsmpa validate ; nsmpa report
```

## Politeness and concurrency

| Setting | Default | Notes |
|---|---|---|
| `user_agent` | — | Must identify the project and a contact address |
| `respect_robots_txt` | true | Includes `Crawl-delay` (≤ 30 s) |
| `per_host_delay_seconds` | 1.5 | Minimum gap between requests to one host |
| `max_per_host_concurrency` | 2 | |
| `max_concurrency` | 16 | Global fetch slots |
| `discovery_concurrency` | 4 | Institutions in flight |
| `research_concurrency` | 3 | Entities in flight (`--concurrency`) |
| `host_failure_threshold` | 6 | Circuit breaker per host |
| `request_timeout_seconds` / `connect_timeout_seconds` | 20 / 10 | |
| `max_response_bytes` | 10,000,000 | |

Raise worker concurrency before lowering per-host delays; most time is spent in polite per-host waits across many different hosts.

## Interruption and resume

- Ctrl+C once: workers finish their current item, everything is committed, run status `interrupted`, and the exact resume command is printed.
- Ctrl+C twice: in-flight items are cancelled and returned to `pending`; completed items remain.
- Budget exhaustion: status `budget_exhausted`; resume with a new `--max-searches`.
- `nsmpa resume` picks the most recently checkpointed resumable run (v0.3) and reuses its stored parameters. Or pass `--run-id` to `discover`/`research`.
- `--fresh` reprocesses completed items in that run (cached queries still cost nothing unless `--refresh-search`).

## Monitoring

- Live dashboard (default), `--verbose` for per-query/URL/decision lines, `--quiet` for cron/nohup. Non-TTY output prints a heartbeat line every 30 s.
- `nsmpa status` — universe, discovery, research, search, review counts; flags resumable runs.
- `nsmpa runs` — recent runs with items done, live/cached searches, credits, stop reason.
- `nsmpa errors [--run-id]` — errors by stage, page access classes, failed items.
- `nsmpa query-stats [--run-id]` — spend and usefulness by query purpose.

## Backups

- Automatic backup before each schema migration (`output/backups/`).
- Before a national stage, take a manual copy using SQLite's backup API (safe with WAL), e.g. `sqlite3 output/nsmpa.sqlite3 ".backup output/backups/manual-$(date +%Y%m%d).sqlite3"`.
- Snapshots are content-addressed and never overwritten; back up `output/research_snapshots/` with the database.

# NSMPA v0.3 — Audit findings and implementation plan

Audit date: 2026-10-07. Baseline: v0.2.0 (git commit "Baseline"). DB backup:
`~/nsmpa-backups/nsmpa-pre-v03-20261007.sqlite3`.

## State of the data at audit time

| Item | Count | Note |
|---|---|---|
| IPEDS rows / included 4-year | 6,072 / 2,517 | 15 included institutions have no website |
| Publication candidates / publications | 0 / 0 | **Student discovery has never been run** |
| Research entities | 91 | 46 support orgs + 45 benchmark newsrooms |
| Research runs | 1 (3 entities, 33 live Serper queries) | All 3 stances `MIXED` — artifacts, see below |
| Research evidence | 89 rows | Mostly boilerplate; treated as legacy v0.2 pilot data |

## Defects found (ordered by research impact)

1. **Redirect loop on most WordPress sites.** `normalize_url` strips trailing slashes and is
   applied to redirect targets, so `/page` → 301 `/page/` → normalized `/page` → loop.
   8 of 21 failed fetches in the pilot. Student newspapers are overwhelmingly WordPress.
2. **Keyword stance classification produces false findings.** "Please do not remove this page"
   was scored adverse; a newsletter "archive" link scored strict-archive; a citation to
   "Teens, Social Media and Privacy" scored supportive. Every pilot entity came out `MIXED`.
3. **Third-party pages contaminate entity stances.** Name-only queries return unrelated
   research PDFs and directories whose text is attributed to the entity.
4. **Evidence inflation.** Uniqueness is `(run, entity, tag, excerpt)` so one sentence matching
   five tags is five rows; 720-char windows overlap and include nav/footer boilerplate.
5. **No resumability.** No per-entity checkpoint; rerunning re-fetches everything. Ctrl+C
   (`KeyboardInterrupt`) leaves runs `running` with no message.
6. **403/bot blocks recorded as `failed` with NULL error**, not as access blockers.
7. **Fetcher gaps:** DNS-rebinding TOCTOU (validated IP not checked against connected peer),
   `httpx.RemoteProtocolError` etc. not retried/caught, any port allowed, binary bodies
   downloaded before content-type check, no `Crawl-delay`, snapshots overwritten in place.
8. **Search gaps:** no live-call retry/backoff for 429/5xx, no `--max-searches`, no
   `--refresh-search`, credit estimate ignores Serper's 2-credit `num>10` tier, no
   "produced useful evidence" flag in ledger.
9. **Discovery gaps:** query ladder differs from spec (no `site:<univ-domain>` step, spends a
   query on "editorial policy"); no candidate homepage verification; promotion uses raw page
   titles as names, has no relationship/method fields, no ambiguity detection.
10. **No schema migrations** — `CREATE IF NOT EXISTS` only; cannot evolve columns safely.
11. **Missing commands:** doctor, validate, report, review, query-stats, errors, resume.
12. **Missing outputs:** JSON/Markdown national report, validation gates, peer groups, SQL views.

## Plan (each stage tested before the next)

1. **Foundation** — migration framework (schema v3, additive, legacy tables preserved);
   fetcher fixes (#1, #6, #7); content-addressed immutable snapshots.
2. **Search + runs** — broker retries/budget/refresh/credit tiers; `run_items` checkpoint
   table; graceful SIGINT; dashboard v2 with all required counters; `--quiet/--verbose`.
3. **Evidence v2** — main-content extraction, sentence segmentation, boilerplate filter,
   one row per unique sentence with tag join table, near-duplicate detection, statement
   detector (content object + action + modality), direction, authority, similarity with
   per-factor explanation; stance v2 using first-party policy statements only.
4. **Discovery v2 + research planner v2** — spec query ladder with early stop, candidate
   verification fetch, ambiguity flag, richer promotion; tiered adaptive research plan
   (quick/standard/deep) with escalation on signals; bounded entity concurrency.
5. **Review, validation, reporting** — review queue with priority scoring, validation gates
   that block national percentages, Markdown/JSON/CSV report by cohort and peer group,
   SQL views; `doctor`, `query-stats`, `errors`, `resume`, `review` commands.
6. **Professional universe** — `entity_sources` provenance table + domain-level dedupe.
7. **Docs + smoke test** — small controlled live run (≤ 25 Serper credits), TUI verification.

## Explicit non-goals for this pass

- No LLM-based classification (keeps runs deterministic and auditable; can be added later as a
  second-opinion column, never as the sole classifier).
- No scraping of membership directories behind logins; professional-universe imports remain CSV.
- Journalism-school presence needs an external list (e.g. IPEDS Completions CIP 09 or ACEJMC);
  supported via `import-peer-attributes`, not inferred.

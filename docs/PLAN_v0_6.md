# NSMPA v0.6 plan — finish every recommendation + a local GUI

Each stage: build → tests → full suite → commit. Nothing spends search/AI credits in tests.

| # | Stage | Delivers | Check |
|---|---|---|---|
| 1 | Heartbeats & progress | Running jobs write live state to `run_heartbeats` every ~2 s; per-entity step bar; `nsmpa watch` (second terminal); macOS notification when a run ends | heartbeat rows during a fake run; watch renders; notifier called |
| 2 | Local GUI (`nsmpa gui`) | stdlib HTTP server on 127.0.0.1, single-page app: live run monitor, results by cohort, evidence browser, review queue (accept/reject), errors, search ledger, archive/AI/accuracy panels | API endpoint tests; CSRF/localhost guard tests; rendered screenshot |
| 3 | Shareable dashboard | `nsmpa dashboard` writes one offline HTML file (same UI, data embedded, read-only) | file opens with no network; contains data |
| 4 | AI attribution → Expert Voices | Verified AI findings with a speaker become voices (`attribution_method=ai`) linked to experts | voice rows created only from verified quotes |
| 5 | AI discovery assist | `nsmpa ai-discovery`: AI picks the student paper among existing candidates (must be one of them); `promote --use-ai` breaks ties, always queued for review | pick outside candidates rejected |
| 6 | AI slide summaries | `packet --ai-summaries`: neutral 2–3 sentence summaries; every sentence must cite valid evidence IDs or it is dropped; labelled "AI draft" | uncited/invalid sentences dropped |
| 7 | Outreach | contacts harvested from fetched pages (mailto); neutral survey drafts written as `.eml` files + CSV (never auto-sent); responses recorded as first-party evidence | drafts generated; response becomes evidence |
| 8 | Legal context | `legal_context` table: EU right-to-be-forgotten + U.S. record-clearing leads, every state a research lead; `nsmpa legal-research` finds sources; never self-verified; packet sheet | seed + search flow; status rules |
| 9 | Scheduled re-checks | `nsmpa recheck` re-fetches known policy pages and records text changes; `nsmpa schedule` prints/installs a launchd job | changed page detected; plist generated |
| 10 | Docs + final verification | README/commands/evidence_packet/CHANGELOG; full suite; GUI screenshot | all green |

## Status (2026-10-07)

All ten stages complete; 189 tests passing. Verification beyond unit tests:
- GUI: jsdom run of every tab (0 JavaScript errors), headless-Chrome screenshots of Live and Results, live server on the
  real database (endpoints 200; foreign Host 403; POST without token 403).
- Found and fixed during checks: ~35 s reverse-DNS stall on server start; clipped chart labels; GUI counts including
  excluded runs; a missing import in `schedule` (caught by the new CLI smoke tests); a CLI test that reached the internet.
- Not exercised against live services (no credits or keys used): Serper, Gemini/Claude, archive.org, real newsroom sites
  at scale. First live pilot is the next step.

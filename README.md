# NSMPA v0.3 — National Journalism Post-Publication Policy & Precedent Research

NSMPA (originally the *National Student Media Policy Audit*) is a local-first, reproducible evidence engine. It asks how U.S. journalism organizations handle requests for post-publication relief — unpublishing, de-indexing, anonymization, name removal, updates and editor's notes — when truthful historical reporting causes continuing harm, especially after criminal allegations end in dismissal, acquittal, expungement, sealing, vacatur or exoneration.

It searches deliberately for evidence **for and against** discretionary relief, keeps every excerpt traceable to a source URL, page hash and search query, and refuses to report national percentages until coverage and review thresholds are met.

## The three-cohort rule

| Cohort | Universe | Denominator |
|---|---|---|
| `student_media` | Primary student newspaper of each IPEDS four-year institution | IPEDS included institutions (2,517 in the current DB), plus a *core stratum* (Carnegie 2021 basic 15–23, 1,627) |
| `professional_newsroom` | Sourced union of directories/membership lists + labeled benchmark panel | Active, de-duplicated entities in the cohort — a **panel, not a census** |
| `support_org` (and `press_association`, `journalism_school`) | Ethics, legal, standards, training and advisory organizations — analyzed as *guidance*, not newsrooms | Active entities in the cohort |

Cohorts may be compared. **Their percentages are never pooled.**

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp config.example.yml config.yml        # then set user_agent with real contact info
nsmpa doctor
```

`config.yml` in the working directory is used automatically; `--config PATH` overrides it.

### Search API key

NSMPA reads `SERPER_API_KEY` from the environment, or from a local `.env` file (gitignored) in the working directory:

```bash
# from a shell where SERPER_API_KEY is already exported (keeps the key out of shell history):
printf 'SERPER_API_KEY=%s\n' "$SERPER_API_KEY" > .env && chmod 600 .env
```

The key is never printed, logged, stored in the database, or written to exports; provider error messages are redacted. `nsmpa doctor` reports only whether it is set. `nsmpa doctor --check-serper` spends exactly one credit to verify it.

Without a key, discovery and research still run (site inspection, homepage policy links), but **no entity can be classified `NO_RELEVANT_GUIDANCE`** — without targeted searches, inspection is incomplete and the result is `UNDETERMINED`.

## Quickstart (small, controlled)

```bash
nsmpa init                                     # migrate schema (auto-backup first)
nsmpa status
nsmpa research-setup                           # seed support orgs + benchmark panel, sync student pubs
nsmpa research --cohort support_org --limit 3 --max-searches 25
nsmpa discover --state WA --limit 5 --max-searches 25
nsmpa promote
nsmpa query-stats
nsmpa review
```

The default terminal mode is a live dashboard. `--verbose` adds queries, URLs, scoring decisions, retries and errors; `--quiet` prints only the final summary.

Ctrl+C once: finish in-flight items, checkpoint, print the resume command. Ctrl+C twice: cancel immediately (completed items are already saved). `nsmpa resume` continues the most recent interrupted or budget-stopped run; cached searches are never paid for twice.

## Watching it run

```bash
nsmpa gui          # local web monitor: live progress, results, evidence, review queue (see docs/gui.md)
nsmpa watch        # same live view in a second terminal
nsmpa dashboard    # one offline HTML file to share
```

## Evidence packet (Excel + PowerPoint)

```bash
nsmpa my-case --init                      # describe your situation (gitignored)
nsmpa verify-precedents --max-searches 15 # named programs: find sources (you verify)
nsmpa research-experts --max-searches 15  # attributed opinions, both directions
nsmpa packet                              # output/packet_<UTC>/NSMPA_evidence.xlsx + NSMPA_presentation.pptx
```

See `docs/evidence_packet.md`. Newsrooms without written policies are still examined: their archived crime
stories are sampled for editor's notes, name removals and targeted noindex tags.

## Archive evidence, AI second opinion, measured accuracy

```bash
nsmpa wayback --run-id RUN                  # archived vs. current crime articles (also runs inside `research`)
nsmpa ai-review --run-id RUN --max-calls 50 # optional; needs GEMINI_API_KEY
nsmpa audit sample --n 50 && nsmpa audit export --out audit.csv   # label in Excel, then `audit import`
nsmpa audit report
```

## Outreach, legal context, re-checks

```bash
nsmpa outreach harvest && nsmpa outreach draft --campaign pilot --limit 20   # drafts only; you send them
nsmpa outreach response --entity-id 12 --file reply.eml --campaign pilot     # reply becomes first-party evidence
nsmpa legal-research --max-searches 60 && nsmpa legal
nsmpa recheck && nsmpa schedule            # weekly re-check (prints the launchd job; --install to enable)
```

## Reproducing the national run

See `docs/scale_runbook.md` for credit estimates. The sequence is:

```bash
nsmpa doctor && nsmpa init
nsmpa import-ipeds HD2024.zip --source-year 2024
# 1. Student publication discovery (staged by budget; resumable)
nsmpa discover --max-searches 2000            # repeat / `nsmpa resume` until remaining = 0
nsmpa promote
nsmpa review --cohort student_media           # verify ambiguous / near-threshold publications
nsmpa sync-student-entities
# 2. Universes
nsmpa research-setup
nsmpa import-entities inn.csv  --cohort professional_newsroom --source inn_directory_2026  --membership "INN member"
nsmpa import-entities lion.csv --cohort professional_newsroom --source lion_members_2026   --membership "LION member"
nsmpa merge-duplicates
# 3. Research, one cohort at a time
nsmpa research --cohort support_org --max-searches 1000
nsmpa research --cohort professional_newsroom --max-searches 5000
nsmpa research --cohort student_media --max-searches 15000
# 4. Human review and source verification
nsmpa review ; nsmpa review --decide ID --decision accept --note "checked live page + snapshot"
nsmpa verify-evidence EVIDENCE_ID --status verified
# 5. Gates and report
nsmpa validate
nsmpa report
```

Every run records its configuration, engine version and git commit in `research_runs`.

## Outputs

| Location | Contents |
|---|---|
| `output/nsmpa.sqlite3` | All data (WAL mode). Never deleted by NSMPA. |
| `output/backups/` | Automatic pre-migration backups |
| `output/research_<RUN_ID>/` | Per-run `entities`, `search_queries`, `research_targets`, `research_pages`, `evidence`, `entity_stances`, `human_review_queue` (each `.csv` + `.jsonl`), `run_manifest.json`, `summary.md` |
| `output/report_<UTC>/` | `report.md`, `report.json`, precedent tables as CSV |
| `output/research_snapshots/raw/ab/<sha256>.html|.pdf` | Immutable, content-addressed source bytes |
| `output/research_snapshots/text/ab/<sha256>.txt` | Normalized main text that was analyzed |

## Research-integrity guardrails

- Supportive and adverse evidence are searched for with equal effort (`docs/query_strategy.md`).
- Keyword hits are not findings. A statement requires a journalism object + an action + (for direction) a modality (`docs/classification_rubric.md`).
- Written policy, documented practice, technical implementation and professional guidance are separate concepts and never inferred from one another.
- `UNDETERMINED` (not adequately inspected) is never converted into a negative finding; blocked, robots-disallowed, and failed sources count against coverage, not as "no policy."
- One sentence is one evidence item regardless of how many concepts it touches; near-duplicates and boilerplate are collapsed.
- Search snippets are discovery aids only; findings come from fetched text, with snapshots and hashes.
- Absence from Google is not evidence of de-indexing.
- Reports withhold cohort percentages until validation gates pass (`docs/validation.md`).
- Every finding intended for external use should be human-verified against the live page and snapshot.

## Documentation

`docs/methodology.md` · `docs/architecture.md` · `docs/classification_rubric.md` · `docs/validation.md` · `docs/schema.md` · `docs/query_strategy.md` · `docs/scale_runbook.md` · `docs/commands.md` · `docs/professional_universe.md` · `CHANGELOG.md`

## Tests

```bash
python -m pytest -q
```

All external HTTP and search calls in tests use deterministic mocks; tests remove `SERPER_API_KEY` from the environment and never spend credits.

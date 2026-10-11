# NSMPA — How U.S. newsrooms handle requests to unpublish, de-index or anonymize old stories

[![CI](https://github.com/kalebhoffer/nsmpa/actions/workflows/ci.yml/badge.svg)](https://github.com/kalebhoffer/nsmpa/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Old news stories follow people for decades. When a criminal charge is later dismissed, ends in acquittal, or is
expunged, the original article usually stays online and stays at the top of a name search. More and more newsrooms
now have a policy for that moment: some de-index old stories from Google while keeping them in the archive, some
remove names, some unpublish, and some refuse anything beyond an editor's note.

**NSMPA is an open, reproducible evidence engine that documents those policies and practices across U.S. journalism**:
student newspapers (every four-year college, from federal IPEDS data), professional newsrooms, broadcasters, and the
standards organizations that advise them. It searches with equal effort for evidence *for and against* post-publication
relief, quotes every finding word for word with its source, and refuses to report percentages until coverage and
review thresholds are met.

- **The evidence collected so far** is in [`evidence/`](evidence/): a redacted Excel workbook and slide deck,
  with every excerpt linked to its source.
- **How it decides** what counts as a policy is in [`docs/methodology.md`](docs/methodology.md) and
  [`docs/classification_rubric.md`](docs/classification_rubric.md).
- **Found an error?** See [CONTRIBUTING.md](CONTRIBUTING.md). Corrections are welcome, especially from the
  organizations described.

## Why this exists (disclosure)

<!-- DRAFT: the author reviews and edits this section before publishing. -->
I built this tool while preparing my own request to a student newspaper to de-index coverage about me. I have a
personal interest in the answer, so the tool is designed to make that interest irrelevant: it looks for opposing
evidence as hard as supporting evidence, it publishes the opposing evidence alongside the rest, it labels every
excerpt with how it was obtained, and it withholds conclusions until the data supports them. Anyone can rerun it and
check the results.

## What the evidence shows so far

This is a snapshot, not a national rate. As of 11 October 2026, 1,803 organizations have been researched and 150
publish a position clear enough to classify. Most publish nothing; among those that do, a majority allow some relief
(removal, de-indexing or anonymization) in limited cases, usually after charges are dropped or a record is expunged,
and a substantial minority refuse.

- **Professional newsrooms (Stage 2, 1,243 researched):** 42 allow relief in some cases (e.g. the Associated Press,
  14 Hearst papers, the Philadelphia Inquirer, the Chicago Sun-Times, Boston.com's Fresh Start), 15 refuse or offer
  only updates (e.g. The Denver Post, the Pittsburgh Post-Gazette) and 10 say both. 730 publish nothing the tool could
  find; 446 could not be determined.
- **Student media (Stage 3, 483 researched):** 24 allow relief (e.g. UMass's Daily Collegian name-removal form, the
  Diamondback on expungement, the Hoya), 27 refuse or offer only updates (e.g. the GW Hatchet, Vanderbilt Hustler,
  Daily Tar Heel) and 9 say both.
- **Broadcasters (27, hand-picked):** 17 allow relief, from 5 distinct policies; 13 NBCUniversal stations share one
  that excludes violent crimes, sex crimes, crimes against children and public corruption.
- **Standards and support organizations (Stage 1, all 50):** 6 take a position, mostly describing options
  (anonymizing, updating, de-indexing) rather than rules. 103 further named precedents are listed with the sources
  NSMPA read.
- **A hand-built index of 130 organizations was used as a check on the tool.** NSMPA read every linked source itself:
  it confirmed 63 of the 69 strongest entries, and from only each organization's name and homepage it found the policy
  page for 10 of 10 tested organizations.
- **Percentages are withheld.** Each group (student media, professional newsrooms, broadcasters, standards
  organizations) has its own denominator and its own validation gates; none has been researched widely enough yet.
  Organizations added *because* they were known to have policies are shown but never counted in a rate.
- **No excerpt has been human-verified yet.** Classification is automated and audited, but every excerpt should be
  checked against its source before it is quoted. The workbook marks each row's verification status.

## How it works

1. **Universe**: IPEDS four-year institutions and their student newspapers; sourced lists of professional and
   broadcast newsrooms; standards and support organizations. Each group is counted separately.
2. **Search**: a small set of targeted, cached searches per organization, aimed equally at relief policies and at
   refusals ("we never unpublish", "historical record").
3. **Read**: a hardened crawler (robots.txt honored, SSRF-safe) reads policy pages and samples archived crime stories
   for editor's notes and removed names. Sites that block robots are never bypassed: their archived copies, search
   snippets (as leads only), AI web search (quotes confirmed word for word) and researcher captures are used instead,
   each labelled with how it was obtained.
4. **Classify**: sentence-level rules separate written policy, documented practice, technical signals (noindex tags,
   Wayback Machine changes) and professional guidance, and record the position on each action (de-index, anonymize,
   unpublish, update). Before a position is accepted, an AI check reviews the excerpts that decide it and can
   only veto: anything that is not the organization's own policy, practice or guidance stops counting.
5. **Review and publish**: a human review queue, an accuracy audit with confidence intervals, and a packet whose
   shared copy withholds private individuals' names.

## Research-integrity guardrails

- Supportive and adverse evidence are searched for with equal effort (`docs/query_strategy.md`).
- Keyword hits are not findings: a statement needs a journalism object, an action and (for direction) a modality.
- Written policy, documented practice, technical implementation and professional guidance are never inferred from
  one another.
- "Undetermined" (not adequately inspected) is never counted as "no policy"; blocked sites count against coverage.
- Every excerpt keeps its source URL, fetch time, page hash and the search that found it.
- Reports withhold percentages until each group passes its validation gates (`docs/validation.md`).
- Shared files withhold names of private individuals (automated redaction; originals stay in the private database).

## Running it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,ai]'
cp config.example.yml config.yml        # set user_agent to include your own contact address
nsmpa doctor
```

Search uses [Serper](https://serper.dev) (`SERPER_API_KEY`); the optional AI features use Gemini (`GEMINI_API_KEY`).
Put keys in a local `.env` (git-ignored); they are never printed, logged or stored.

```bash
nsmpa estimate --cohort support_org          # credits, requests, time and AI calls before spending anything
nsmpa pilot --budget 25 --ai-calls 10        # capped end-to-end trial with a plain-language report
nsmpa gui                                    # local monitor: live progress, results, evidence, review, capture
nsmpa packet --redact-names                  # Excel workbook + PowerPoint deck for sharing
```

The full sequence for a national run is in [`docs/scale_runbook.md`](docs/scale_runbook.md); every command is listed in
[`docs/commands.md`](docs/commands.md). Runs are resumable (Ctrl+C checkpoints), budgeted, and cached, so no search is
paid for twice.

## Documentation

[methodology](docs/methodology.md) · [classification rubric](docs/classification_rubric.md) ·
[validation](docs/validation.md) · [architecture](docs/architecture.md) · [schema](docs/schema.md) ·
[query strategy](docs/query_strategy.md) · [scale runbook](docs/scale_runbook.md) · [commands](docs/commands.md) ·
[blocked sites](docs/blocked_sites.md) · [evidence index](docs/evidence_index.md) · [evidence packet](docs/evidence_packet.md) ·
[GUI](docs/gui.md) · [changelog](CHANGELOG.md)

## Tests

```bash
ruff check src tests && python -m pytest -q
```

All network and search calls in tests are mocked; tests need no API keys and never spend credits.

## License

Code: [MIT](LICENSE). The compiled evidence in `evidence/` (selection, classification, notes) is shared under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); quoted excerpts remain the work of their publishers and are
quoted for research and commentary, each with a link to its source.

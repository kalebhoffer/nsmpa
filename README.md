# NSMPA — How U.S. newsrooms handle requests to unpublish, de-index or anonymize old stories

[![CI](https://github.com/kalebhoffer/nsmpa/actions/workflows/ci.yml/badge.svg)](https://github.com/kalebhoffer/nsmpa/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Old news stories follow people for decades. When charges are dismissed, a case ends in acquittal, or a record is
expunged, the original article usually stays at the top of a search for the person's name. A growing number of
newsrooms have a written answer for that moment, and **most of those answers allow something**: de-indexing the story
from search engines, removing a name, or adding an update, while the archive itself stays intact. Refusing everything
is the minority position.

**NSMPA is an open, reproducible evidence engine that documents those policies across U.S. journalism**: student
newspapers (every four-year college, from federal IPEDS data), professional newsrooms, broadcasters, and the standards
organizations that advise them. It searches with equal effort for evidence *for and against* post-publication relief,
quotes every finding word for word with its source, and publishes the opposing evidence alongside the rest.

- **The evidence** is in [`evidence/`](evidence/): a redacted Excel workbook and slide deck, with every excerpt
  linked to its source.
- **How it decides** what counts as a policy is in [`docs/methodology.md`](docs/methodology.md) and
  [`docs/classification_rubric.md`](docs/classification_rubric.md).
- **Found an error?** See [CONTRIBUTING.md](CONTRIBUTING.md). Corrections are welcome, especially from the
  organizations described.

## Key findings (11 October 2026)

NSMPA has researched **1,805 organizations**. Of the newsrooms and journalism organizations among them, **136 publish
a position on old stories clear enough to classify** (hand-picked broadcasters are reported separately below):

| Published position | Organizations |
|---|---:|
| Offer some level of post-publication relief (removal, de-indexing, anonymization, case-by-case review, or updates and editor's notes) | 117 |
| Refuse any change beyond correcting errors | 19 |

- **A hard refusal is the minority position.** 117 of the 136 offer some level of post-publication relief. Only 19,
  about one in seven, rule out everything beyond correcting errors.
- **De-indexing is the least contested remedy.** 23 organizations explicitly offer it, including the Associated Press
  ("the AP would also request that Google 'deindex' the stories, removing them from its search results"), Hearst's
  newspapers (the San Francisco Chronicle, Houston Chronicle, San Antonio Express-News and others: "applying
  deindexing tags to an article"), The Oregonian and cleveland.com. **Only 3 explicitly refuse it**, all student
  papers, and two of those share one template. Most refusals are written about deleting or altering stories;
  de-indexing does neither. The story, its text and its place in the archive are unchanged.
- **The profession's own guidance lists de-indexing as an option.** The News Leaders Association (the merger of
  ASNE and APME): "You can also anonymize that person's name; update the old information; de-index/obscure it from
  Google search results; or add an editor's note to existing content." The Reynolds Journalism Institute: "the nature of
  the content may make anonymizing the person an innocuous change."
- **Most organizations publish nothing at all.** 1,021 have no relevant policy the tool could find, and 631 could
  not be read well enough to tell (most are dead, parked or blocked sites). Neither group is counted for or against
  relief. A policy that is not published cannot be checked, applied consistently, or cited by the person it is
  applied to.

By group:

- **Professional newsrooms (Stage 2, 1,244 researched):** 44 allow relief in some cases (e.g. the Associated Press,
  Hearst's papers, the Philadelphia Inquirer, the Chicago Sun-Times, Boston.com's Fresh Start), 10 say both, and 15
  refuse or offer only updates (e.g. The Denver Post, the Pittsburgh Post-Gazette). 763 publish nothing the tool could
  find; 412 could not be determined.
- **Student media (Stage 3, 484 researched):** 25 allow relief (e.g. UMass's Daily Collegian name-removal form, the
  Diamondback on expungement, the Hoya), 9 say both, and 27 refuse or offer only updates (e.g. the GW Hatchet,
  Vanderbilt Hustler, Daily Tar Heel). 225 publish nothing the tool could find; 198 could not be determined.
- **Broadcasters (27, hand-picked):** 17 allow relief, from 5 distinct policies; 13 NBCUniversal stations share one
  that excludes violent crimes, sex crimes, crimes against children and public corruption. None refuses.
- **Standards and support organizations (Stage 1, all 50):** 6 take a position, mostly describing options
  (anonymizing, updating, de-indexing) rather than rules; only one (the Committee to Protect Journalists, about its own
  data) limits itself to updates. 103 further named precedents are listed with the sources NSMPA read.
- **A hand-built index of 130 organizations was used as a check on the tool.** NSMPA read every linked source itself:
  it confirmed 63 of the 69 strongest entries, and from only each organization's name and homepage it found the policy
  page for 10 of 10 tested organizations.

How to read these numbers:

- **They are counts of published policies, not national rates.** Each group (student media, professional newsrooms,
  broadcasters, standards organizations) has its own denominator and validation gates (`docs/validation.md`), and
  formal percentages are withheld until each group passes them. Organizations added *because* they were known to have
  policies are shown but never counted in a rate.
- **The opposing evidence is published too.** Every refusal, "historical record" principle and errors-only exception
  the tool found is in its own sheet of the workbook, quoted in full.
- **No excerpt has been human-verified yet.** Classification is automated and audited, but every excerpt should be
  checked against its source before it is quoted. The workbook marks each row's verification status.

## Why this exists (disclosure)

<!-- DRAFT: the author reviews and edits this section before publishing. -->
I built this tool while preparing my own request to a student newspaper about a 2012 article that concerns me. **I am
asking for the article to be de-indexed from search engines, not unpublished.** It would stay on the newspaper's
website, unchanged, for anyone who looks for it there. The newspaper has told me that a policy rules out the request,
but that policy is not published, so it cannot be compared with the policies documented here.

I have a personal interest in the answer, so the tool is designed to make that interest irrelevant: it looks for
opposing evidence as hard as supporting evidence, publishes the opposing evidence alongside the rest, labels every
excerpt with how it was obtained, and withholds conclusions the data does not support. Anyone can rerun it and check
the results.

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

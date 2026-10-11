# Evidence

Each dated folder is a snapshot of what NSMPA has collected, generated from the research database with
`nsmpa packet --redact-names`. Nothing in these files is edited by hand.

## 2026-10-11 (NSMPA 0.8.5; includes Stages 1, 2 and 3 of the national run)

| File | What it is |
|---|---|
| [`NSMPA_evidence.xlsx`](2026-10-11/NSMPA_evidence.xlsx) | The workbook: every excerpt with its source link, fetch time, page hash, classification, and verification status. Start with the **Read Me** sheet. |
| [`NSMPA_presentation.pptx`](2026-10-11/NSMPA_presentation.pptx) | A 19-slide summary deck built from the same data. |
| [`summary.md`](2026-10-11/summary.md) | Counts by group. |
| [`evidence_index.csv`](2026-10-11/evidence_index.csv) | A hand-built list of 130 organizations with links to their policies or to reporting about them (tiers A–C), compiled independently of the tool. |
| [`evidence_index_check.csv`](2026-10-11/evidence_index_check.csv) | What NSMPA found when it read each of those links itself: sources found or not, and the strongest supportive excerpt. |

### What this snapshot is, and is not

- **Counts, not national rates.** 1,805 organizations have been researched (153 with a determined position): all
  1,244 professional newsrooms on the list (Stage 2, 69 determined), 484 student papers (Stage 3, 61 determined),
  27 hand-picked broadcasters (17 determined) and all 50 standards and support organizations (Stage 1, 6
  determined), plus 131 listed precedents whose sources it read. Of the 136 newsrooms and organizations with a
  determined position (broadcasters aside), 73 allow relief in some cases or review requests individually, 20 say
  both, 24 offer only updates and 19 refuse any change beyond correcting errors. 23 explicitly offer de-indexing;
  3 explicitly refuse it. Every group's percentages are withheld until it passes its validation gates
  (`docs/validation.md`).
- **Not yet human-verified.** Classification is automated. None of the 6,846 excerpts has been checked against its
  source by a person yet; each row's `Verified?` column says so. Check the source before quoting any excerpt.
- **Both directions.** 815 opposing excerpts (refusals, "historical record" principles, errors-only exceptions) are
  included and have their own sheet.
- **Selected organizations are labelled.** Organizations added because the hand-built index lists them are marked
  "Counted in rates? = no": they were chosen for having policies and are never part of a percentage.
- **How each excerpt was obtained** is recorded: read live, from the Internet Archive's copy (sites that block robots),
  a search snippet (a lead only, never decisive), or copied by hand.

### Privacy and withholding

- Names of private individuals are replaced with `[name withheld]` by automated redaction (10,595 replacements).
  Journalists, editors, experts and organizations quoted in their professional roles are kept. Links whose address
  contains a withheld name are also withheld.
- **3 rows are withheld from this copy because they concern the researcher's own case** (see the disclosure in the
  main README). The Read Me sheet states this too.
- The private database keeps original text, links and hashes, so any excerpt can be checked on request by its ID.

### Checking a row

Open the link in the `Source` column (or the `Archived copy` link for archived pages) and find the quoted sentence.
If it is missing, misquoted or mislabelled, please open an issue (see `CONTRIBUTING.md`) with the evidence ID.

### Reproducing it

```bash
nsmpa research ...                      # see docs/scale_runbook.md
nsmpa packet --redact-names --withhold "<url fragment>" --withhold-reason "<why>"
```

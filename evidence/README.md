# Evidence

Each dated folder is a snapshot of what NSMPA has collected, generated from the research database with
`nsmpa packet --redact-names`. Nothing in these files is edited by hand.

## 2026-10-08 (NSMPA 0.7.2)

| File | What it is |
|---|---|
| [`NSMPA_evidence.xlsx`](2026-10-08/NSMPA_evidence.xlsx) | The workbook: every excerpt with its source link, fetch time, page hash, classification, and verification status. Start with the **Read Me** sheet. |
| [`NSMPA_presentation.pptx`](2026-10-08/NSMPA_presentation.pptx) | A 19-slide summary deck built from the same data. |
| [`summary.md`](2026-10-08/summary.md) | Counts by group. |
| [`evidence_index.csv`](2026-10-08/evidence_index.csv) | A hand-built list of 130 organizations with links to their policies or to reporting about them (tiers A–C), compiled independently of the tool. |
| [`evidence_index_check.csv`](2026-10-08/evidence_index_check.csv) | What NSMPA found when it read each of those links itself: sources found or not, and the strongest supportive excerpt. |

### What this snapshot is, and is not

- **An early sample, not a national rate.** 21 organizations have been researched by the tool so far (19 with a
  determined position), plus 131 listed precedents whose sources it read. Every group's percentages are withheld until
  it passes its validation gates (`docs/validation.md`).
- **Not yet human-verified.** Classification is automated. None of the 1,077 excerpts has been checked against its
  source by a person yet; each row's `Verified?` column says so. Check the source before quoting any excerpt.
- **Both directions.** 313 opposing excerpts (refusals, "historical record" principles, errors-only exceptions) are
  included and have their own sheet.
- **Selected organizations are labelled.** Organizations added because the hand-built index lists them are marked
  "Counted in rates? = no": they were chosen for having policies and are never part of a percentage.
- **How each excerpt was obtained** is recorded: read live, from the Internet Archive's copy (sites that block robots),
  a search snippet (a lead only, never decisive), or copied by hand.

### Privacy and withholding

- Names of private individuals are replaced with `[name withheld]` by automated redaction (536 replacements).
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

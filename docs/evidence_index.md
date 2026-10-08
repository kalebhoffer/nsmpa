# Importing a hand-built evidence index

Researchers often already hold a list of organizations with links to their policies (for example, the 130-entry
"Easterner evidence index v6"). NSMPA can import such a list, read every source itself, and use it as a check on the tool.
Two rules keep this honest.

1. **A listed source is a lead, not evidence.** Each row becomes a precedent (`prior_confidence = user_supplied`).
   NSMPA reads the linked page (or its archived copy when the site blocks robots) and classifies it with the same rules as
   any research page. A secondary report (a feature or case study covering many outlets) counts for an organization only
   where the extracted text names that organization.
2. **A list of organizations chosen for having policies cannot be used to compute rates.** Organizations that the import
   adds to a group are flagged `selected_from_evidence_index` / `excluded_from_rates`. They are researched, shown in the
   GUI and packet (column "Counted in rates?" = no) and appear in validation as
   `selected_from_evidence_index_researched`, but they never enter a denominator or a percentage. Organizations already
   in a group's sourced list (for example from the newspaper directory) stay counted exactly as before.

## Steps

```bash
# 1. Prepare a CSV (columns below). Keep it under inputs/ (git-ignored).
nsmpa import-evidence-index inputs/evidence_index_2026-10-08.csv --name "Easterner evidence index v6"
# 2. Read every listed source; no search credits.
nsmpa verify-precedents --only-index --no-search
# 3. Compare each entry (its tier) with what NSMPA found.
nsmpa evidence-index-report --name "Easterner evidence index v6"     # writes output/evidence_index_check.csv
```

| Column | Meaning |
|---|---|
| `index_id`, `organization`, `tier`, `evidence_focus`, `source_url` | As in your list |
| `kind` | `newspaper`, `digital`, `wire`, `broadcast`, `student_college`, `standards_org` (become research entities) or `scholastic`, `research`, `reporting_outlet`, `group` (precedents only) |
| `homepage` | The organization's own site (decides whether a link is its own publication or someone else's report) |
| `group` | Shared ownership or standards (for example "NBCUniversal Local"), so one policy framework is not counted many times |
| `country`, `state` | Non-U.S. rows are precedents only |
| `source_kind` | `own_publication`, `secondary_report`, `group_standards`, `request_form` |

## Broadcast newsrooms

TV and radio newsrooms are their own group (`broadcast_newsroom`) with their own denominator, never pooled with
newspapers. No census of U.S. broadcast newsrooms is loaded yet, so the group currently holds only index-selected
stations and reports no percentages. To add a census, import a sourced list of stations with websites (for example
compiled from FCC facility data or a public-media member directory):

```bash
nsmpa import-entities stations.csv --cohort broadcast_newsroom --source fcc_tv_2026
```

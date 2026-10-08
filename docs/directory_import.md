# Importing the newspaper directory

```bash
nsmpa import-directory ~/Downloads/US_newspaper_directory.csv     # backup first, then import
nsmpa match-directory-homepages                                   # free: read unmatched student papers' homepages
# review output/US_newspaper_directory_ipeds_matches.csv in Excel (confirm y/n, correct UNITID), then:
nsmpa apply-directory-review output/US_newspaper_directory_ipeds_matches.csv ~/Downloads/US_newspaper_directory.csv
nsmpa research --max-priority 2 --max-searches 100                # Easterner + WA student peers first
nsmpa export-directory ~/Downloads/US_newspaper_directory.csv     # your CSV back, policy columns filled in
```

## What the import does

**Professional rows.** Rows sharing a website are editions or titles of one site (e.g. eight community titles under
`statesman.com/news/local/...`) and share its corporate policy, so they become one organization per website; every title
is kept as a provenance row (`entity_sources`) and listed in the organization's metadata. A row on a domain already in
the database (e.g. a benchmark newsroom) is attached to that organization.

**Student rows → IPEDS institutions**, in this order:

| Method | Rule | Confidence | Auto-matched? |
|---|---|---|---|
| `name` | institution named in the row equals an IPEDS name or alias, same state | 0.85 | yes |
| `name_campus` | named institution resolved to its main campus (explicit "main campus", flagship city, else largest enrollment; never online/global divisions) | 0.80 | yes |
| `name_fuzzy` | very close name (≥ 0.88 similarity, clear winner) | 0.78 | yes |
| `edu_domain` | paper's host is the institution's web domain or a subdomain of it | 0.75–0.80 | yes |
| `homepage_phrase` | paper's own homepage calls itself the student publication of exactly one in-state institution | 0.85 | yes (after `match-directory-homepages`) |
| `heuristic` | domain starts with the institution's acronym followed by a masthead word (`esubulletin`), or contains a distinctive name word | 0.55 | **no: suggestion for review** |
| `homepage_mention` | homepage mentions one institution at least twice as often as any other | 0.60 | **no: suggestion** |
| `out_of_scope` | paper's `.edu` domain belongs to an institution outside the included universe (e.g. two-year) | — | no |
| `ambiguous` / `unmatched` | tie or no evidence | — | no |

A row that names its institution is never matched by guesswork: if the name cannot be found it is reported as unmatched.

Unmatched, ambiguous, suggested and out-of-scope papers are **still researched** as student media, but they are excluded
from the IPEDS coverage percentages (which count each institution once, through its primary publication). A second paper
at an already-matched institution is kept as a *secondary* student publication, also excluded from those percentages.

Platform-hosted URLs (issuu, blogspot, wordpress.com, weebly, sites.google.com) are flagged: find the paper's own site.

**Research priority** from the file (1 = highest) orders every research run; `--max-priority N` restricts to it.

## Data vintage

Most rows come from the Leon Yin Local News Dataset (2018). Expect closed papers and moved sites; `match-directory-homepages`
and every research run record whether each site still responds, and `export-directory` writes that into *Operating status*.
For reporting, describe the professional group as "a 2018 national local-news dataset plus a 2026 curated supplement",
not as a census.

## Spot-check results at import (2026-10-07)

13/13 campus resolutions correct; heuristic suggestions about 90% correct in a 25-item sample (hence review-only);
errors found and fixed before import: acronym-inside-word matches ("pit" in `pittnews`), regional words ("mountain"),
named-but-missing institutions falling back to guesses, and second papers at one institution being dropped.

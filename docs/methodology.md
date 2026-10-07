# Methodology

NSMPA is designed as a reproducible national audit rather than an advocacy-first search.

## Unit of analysis

The institution universe is imported from NCES/IPEDS directory data. The default analytic
population is U.S. four-year public and private nonprofit institutions. Discovery attempts to
identify the primary general-interest student newspaper/news organization for each institution.
Multiple publications can be retained when a campus clearly supports more than one qualifying
newsroom.

## Stages

1. Import institutions and keep the original IPEDS identifiers.
2. Discover publication candidates from institution-site links and one or more search providers.
3. Score and validate publication candidates with explainable signals.
4. Crawl the confirmed publication using robots.txt-aware, per-host throttled requests.
5. Prioritize policy/about/corrections/archive/privacy pages plus sitemap pages with policy terms.
6. Extract candidate passages and technical indexing signals.
7. Classify with a deterministic evidence rubric. Optional model review can be added separately.
8. Export row-level evidence, unresolved cases, and aggregate counts.
9. Human-verify any evidence used in external claims.

## Important distinction

`NO_PUBLIC_POLICY_FOUND` means the publication was successfully inspected to a documented
threshold. A blocked, failed, or ambiguous site is `UNDETERMINED`, never "no policy."

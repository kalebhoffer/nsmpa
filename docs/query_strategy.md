# Query strategy

Search is adaptive: cheap, high-yield queries first; escalation only when results justify it. Every query — live, cached, failed or budget-skipped — is written to `search_queries` with purpose, entity/UNITID, provider, cache key, credits, status, latency, and (via `search_results`) every result URL. `produced_evidence=1` is set when a page reached through the query yields a substantive statement. `nsmpa query-stats` shows usefulness by purpose.

## Student-publication discovery (`discovery.py`)

Ladder (`DISCOVERY_LADDER`), truncated to `discovery_max_searches_per_institution` (default **5**, i.e. all five steps):

1. `"<institution>" student newspaper`
2. `"<institution>" student media`
3. `"<institution>" campus newspaper`
4. `"<institution>" independent student newspaper`
5. `site:<institution-domain> student newspaper`

Procedure per institution:

1. Run query 1; score results; verify the top distinct domains (up to `discovery_verify_top_candidates`, default 3, scoring ≥ 0.35) by fetching each candidate (its site root if independently hosted) and checking student-newsroom identity (+0.12), institution named (+0.05), news-site structure (+0.05), institutional-PR identity without student identity (−0.45).
2. If best effective score < `publication_early_stop_threshold` (0.84): free site inspection — homepage, sitemap navigation URLs, a few conventional paths (`/student-media`, `/studentmedia`, `/student-life/student-media`), and student-life/clubs/involvement pages up to `discovery_site_pages` (14), following external links from pages that describe student media.
3. Continue down the ladder only while best < 0.84.

Candidate scoring rewards explicit student-journalism signals and penalizes university PR/newsroom/pressroom/media-relations language; independent domains are allowed. Promotion (`nsmpa promote`) uses the verified score, prefers an independent domain over a university hub page within 0.10, flags rival domains within `publication_ambiguity_margin` (0.08) as ambiguous, and never overwrites `manual` or `human_verified` publications.

## Research tiers (`research.py`)

`{site}` is the entity host, plus the path prefix for path-hosted publications. `{name}` is the entity name with quotes removed.

**Tier 1 — always (3 queries):**

```
site:{site} (unpublish OR unpublishing OR deindex OR "de-index" OR takedown OR "remove an article" OR anonymize OR "removal request")
site:{site} ("editorial policy" OR "ethics policy" OR "corrections policy" OR "archive policy" OR "code of ethics" OR standards)
site:{site} ("never unpublish" OR "do not remove" OR "will not remove" OR "historical record" OR "archive integrity" OR "requests to remove")
```

Then free first-party discovery: the homepage and up to `research_first_party_link_pages` (6) linked policy/about pages.

**Tier 2 — escalation** when a real provider is configured, depth ≠ `quick`, and (a first-party tier-1 result shows a policy signal, or the cohort is a guidance cohort, or depth = `deep`). Newsrooms (4 queries):

```
site:{site} ("charges dismissed" OR "charges dropped" OR "case dismissed" OR expunged OR sealed OR acquitted OR exonerated OR vacated) ("editor's note" OR update OR removed OR anonymized OR unpublished)
site:{site} ("right to be forgotten" OR "digital permanence" OR "search engines" OR "reputational harm" OR "case by case") (remove OR unpublish OR archive OR name)
site:{site} ("requests to remove" OR "removal requests" OR "decline removal" OR embarrassment OR reputation) (remove OR unpublish OR delete)
"{name}" (unpublished OR "removed the article" OR "removed the story" OR anonymized OR deindexed OR "declined to remove" OR "refused to remove")
```

Guidance cohorts (`support_org`, `press_association`, `journalism_school`) drop the third-party query and add (3 + 3 = 6 queries):

```
site:{site} (unpublishing OR deindexing OR "right to be forgotten" OR "unpublish requests") journalism
site:{site} (arrest OR mugshot OR "criminal record" OR expunged OR "charges dismissed") (archive OR unpublish OR update OR name)
site:{site} ("minimize harm" OR "digital permanence" OR "long-term" OR "search engine") (archive OR unpublish OR remove)
```

**Tier 3 — `--depth deep` only (40 queries)**, each `site:{site} <term>`:

- policy: `unpublish`, `unpublishing`, `deindex`, `"de-index"`, `takedown`, `removal`, `anonymize`, `"archive policy"`, `"editorial policy"`, `"corrections policy"`, `"right to be forgotten"`, `"case by case" removal`, `"reputational harm"`, `"digital permanence"`, `"search engines"`
- adverse: `"never unpublish"`, `"do not remove"`, `"historical record"`, `"archive integrity"`, `"requests to remove"`, `"decline removal"`, `embarrassment removal`, `reputation removal`
- precedent: `"charges dismissed"`, `"charges dropped"`, `"case dismissed"`, `"conviction vacated"`, `"record expunged"`, `"record sealed"`, `acquitted`, `exonerated`, `"plea withdrawn"`, `"arrest record"`, `"changed circumstances"`, `rehabilitation`, `"request to remove"`, `"request to unpublish"`, `"request to deindex"`, `"name removed"`, `"name anonymized"`

Quick/standard research shares the per-entity cap `research_max_searches_per_entity` (default **18**). `--depth deep` uses `research_deep_max_searches_per_entity` instead (default **50**), which covers the full 47 (newsroom) or 49 (guidance) query plan.

Targets are scored (first-party +0.40; third-party must name the entity, +0.15; policy signal +0.30; policy page +0.20; precedent +0.05; rank bonus; listing pages −0.25) and kept at ≥ `research_candidate_threshold` (0.28). Up to `research_fetch_top_targets` (18) first-party and `research_third_party_fetch_limit` (4) third-party targets are fetched.

## Cache key

`sha256(lowercase(whitespace-collapsed query) + "|" + results_requested)`, scoped by provider. Cached entries are fresh for `search_cache_days` (3,650). A cache hit is still ledgered (with `was_cached=1`, 0 credits).

## Budgets

- `--max-searches N`: live credits **this invocation** may spend.
- `search_credit_budget_per_run` (50,000): cumulative live credits for a run id across all invocations that resume it.
- Serper credits: 1 for ≤ 10 results, 2 above (`search_results_per_query` default 8 → 1 credit); provider-reported credits are used when present. Failed live calls are counted conservatively.
- When a live call would exceed either budget, the query is ledgered as `budget_skipped`, the run stops issuing live requests, in-flight items finish or return to `pending`, and the run ends `budget_exhausted` with the reason in `status_reason`.

## Refresh

`--refresh-search` re-issues each distinct query once per invocation (spending credits) and replaces the cache entry. Without it, identical queries never spend credits again.

## Concurrency

Identical queries issued concurrently by parallel workers are coalesced: one live call, the rest are served from cache. Retryable provider errors (429, 5xx, transport) are retried `search_max_retries` (2) times with jittered backoff from `search_retry_backoff_seconds` (2 s); 401/403 (or out-of-credit 400) stops the run as `failed`.

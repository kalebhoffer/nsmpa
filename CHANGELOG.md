# Changelog

## Unreleased

- Shared packets (`--redact-names`) are shaped for publication: no case-profile slide, no "Closest to My Case" sheet
  and no match-to-my-case scores; news stories keep only their domain (address, headline and archived copy withheld),
  while policy pages and coverage of the topic itself are unchanged. The 2026-10-11 workbook and deck are regenerated
  this way (34,022 story rows masked).
- Five publications entered twice were merged into one entity each, with their provenance moved to the survivor: The
  Whitworthian, The Daily Texan, The Vista, The Pioneer Log (the directory's mistaken Idaho entry) and The Auburn
  Plainsman (also filed as a professional newsroom). The snapshot lists 1,800 organizations, one row each.
- Merged entities (`active=0`, `merged_into` set) are left out of current-stance counts, the evidence snapshot
  (entities, evidence, sources, archive checks, AI findings, captures, leads) and run exports.
- README spells out NSMPA: the National Student Media Policy Audit.
- Withholding a site from a shared packet also drops its organization row and every row that names it (search
  ledger, AI findings), not only rows whose links match.

## 0.8.5 — Second pass over undetermined organizations

- `research_probe_policy_paths`: after the identity check, read first-party policy and about pages listed in the
  site's sitemap; if the sitemap lists none, try common paths (`/ethics`, `/corrections`, `/standards`…). Policy pages
  that nothing links to and search does not index are now found.
- `research_retry_failed`: with `research --fresh`, pages that failed for a passing reason (timeout, network error,
  5xx, rate limit) and pages set aside by an identity check are fetched again. `--fresh` now resets only the selected
  organizations, not the whole run.
- `blocked_fallback_tier2`: for sites that block automated access, the search-snippet fallback also runs the second
  tier of first-party searches.
- Recovery never moves a student paper to its college's own homepage (the Wooster Voice had been moved to
  wooster.edu). A second recovery search (`"<paper>" <state> local news`) was tried and removed: it chose the wrong
  site 11 times out of 12.
- Second pass over 653 undetermined organizations (2,852 search credits): 37 now have enough of their site read to
  show no relevant guidance; one new position (Gonzales Tribune, the same chain policy as the Gilroy Dispatch). The
  false findings (headline rails, a casino page, a municipal code, a store refund policy) were vetoed by hand, and
  13 wrong site moves were reverted.
- The snapshot summary lists hand-picked groups (the 27 broadcasters) instead of showing them as 0 researched.
- README leads with the findings: of 136 organizations with a published position, 93 allow some relief, 19 refuse
  any change beyond corrections, and 3 explicitly refuse de-indexing.

## 0.8.4 — Stage 3 fixes

- Student identity: "student-led" / "student news site" wording, the paper's name words in the page title in any
  order ("Independent Alligator" in "The Independent Florida Alligator"), "&" read as "and", a college's own .edu
  domain, and "University"/"College" in the title outside the paper's own name all count; six live papers that had
  been rejected are now researched. Titles of taken-over domains (casinos, slots, essays, insurance, crypto) are
  rejected for every newsroom group.
- Classifier: "Under no circumstances will X remove" is a refusal; hypothetical arguments ("If these policies did
  not exist, people could alter…") and lists of refused requests ("…don't adhere to our policies. This includes
  taking down photos…") are not permissions; post-to-post navigation and teaser rails are not policy.
- Manual vetoes, recorded in `ai_gate_checks` with model `manual-review`, stand in for the AI check when it is
  unavailable.

## 0.8.3 — Stage 3 pilot fixes

- An athletics website ("UC Riverside Athletics - Official Athletics Website", which carries the team name and dated
  news) is never accepted as a publication's homepage.
- Student papers with no college on record are not moved to a new site by a name search: in the pilot it matched
  another school's paper 7 times out of 10 (Fullerton College's "Hornet" to Sacramento State's). A dead site stays
  undetermined instead.

## 0.8.2 — Redaction fixes; evidence snapshot with Stage 2

- Redaction trims words that only border a name ("By", "Updated", "If", "We", month abbreviations) and judges what
  remains, so "Updates We may update a developing story" is no longer withheld while a byline's name still is; a
  capitalized run that contains a kept organization name ("Davis Enterprise Announcements") is kept; a title before a
  surname ("Mr. Doggett") is withheld, along with later mentions of that surname.
- Evidence snapshot `evidence/2026-10-10`: Stages 1 and 2 (1,305 organizations researched, 82 with a determined
  position; 4,990 excerpts, 626 opposing), with the researcher's own case still withheld.

## 0.8.1 — Stage 2 completion fixes

- Site recovery ("where does this dead site live now?") no longer accepts a listing or an unrelated paper. The new
  site must carry something distinctive to the paper (the town in its old domain, a non-masthead name word), not just
  a shared masthead: "Herald Tribune" had moved Batesville, Indiana's paper to Sarasota's site, "Statesman" to
  Austin's, "Mercury" (San Marcos) to UT Dallas's student paper. A site that cannot be verified (blocks robots) must
  carry the mark in its own domain; listings and reference sites (Yelp, Muck Rack, newspapers.com, Ancestry, library
  archives, encyclopedias, booksellers) and, for professional newsrooms, college and library sites are skipped. 142
  Stage 2 newsrooms were reverted and re-researched; four false positions and two double counts disappeared.
- A listed homepage that redirects to the same publication is followed and adopted (old URL kept in metadata);
  a redirect to a different site stops with an identity mismatch.
- "Protect the privacy of the individual" in a story (Boston.com Fresh Start) is anonymizing relief, and "consider
  each case/request individually" is case-by-case review.

## 0.8.0 — AI veto on decisive excerpts; Stage 2 batch 1 fixes

- AI check (`ai_gate.py`, `nsmpa ai-gate`, on by default in research): before a position is accepted, the excerpts
  that decide it are shown to the AI with one question: is this the organization's own policy, practice or guidance
  about removing, de-indexing, anonymizing or updating its published journalism? Veto only: rejected excerpts stop
  counting (kept, labelled with the reason), the position is re-decided, newly decisive excerpts are checked (up to 3
  rounds). The AI never adds, upgrades or redirects a finding; errors leave excerpts unchecked; verdicts are cached
  and recorded in `ai_gate_checks`. Live test: all 9 Stage 2 batch-1 positions were false and all 9 were vetoed
  (including two traced to a directory site, thepaperboy.com, listed as the papers' homepage); the 10 recall-test
  newsrooms kept their positions; every one of 136 verdicts reviewed by hand.
- Directory/aggregator sites (thepaperboy.com, newspapers.com, issuu.com, ...) are never accepted as a homepage.
- Attribution ("who is speaking") shared by research and `reclassify`: bylines, letter writers and quoted sources
  are not the newsroom; dated or article addresses are stories; policy-named undated pages are policy; editor's
  columns and first-person policy statements count; precedent sources exempt.
- "Update only" requires a stated policy offered where removal is at issue, not routine correction notes.
- A site that blocks robots is never "no relevant guidance", on every path including `reclassify`.
- Fetcher circuit breaker reopens after a cooldown (it had disabled the Internet Archive for the rest of a run).
- `reclassify` re-decides every organization in a run. Schema v9.

## 0.7.4 — classifier fixes from the Stage 1 national run

Stage 1 (all 50 standards and support organizations, 361 credits) showed removal language used about things other than
news stories being read as policy. New scope rules, each checked against all 118,267 stored sentences with every
changed sentence reviewed:

- people removed from roles ("an Editor is removed", "remove an Editor"); reporters' unpublished notes and material;
  "case-by-case" without a removal/update topic in the sentence or its neighbours; government, court and legal
  records (including courts and legislatures sealing or redacting them); codes of conduct and abuse reports;
  removal named as a topic ("challenges related to unpublishing"); anonymity of sources or contributors; removing
  criteria, fields or URLs; third parties ("private parties") demanding removal.
- "Editorial" counts as a news object only as a noun ("editorial committee/discretion/standards" do not).
- "Rarely granted" removal requests are a narrow exception (restrictive), not neutral.
- Relief vocabulary adds "fresh start", "right to be forgotten" and "take-down".
- Five genuine findings that early versions of these rules suppressed are restored and kept as tests. Recall on the 10
  Tier-A organizations remains 10/10. 329 tests.

## 0.7.3 — prepared for public release

- Public README (purpose, author's disclosure, findings so far with their limits, how it works), MIT license,
  CONTRIBUTING (how to report an evidence error), CI (ruff + tests on Python 3.12–3.14), project metadata.
- `evidence/2026-10-08/`: redacted workbook and deck, the hand-built evidence index and NSMPA's check of it.
- Redaction no longer withholds capitalized headline phrases ("A Case Study of Ten College Newspapers", "Publishing
  Principles"): a phrase counts as a name only if at least one word never appears in lowercase in the collected text.
  Replacements on the current data fell from 2,448 to 536 with no private names exposed (audited).
- `nsmpa packet --withhold URL_FRAGMENT --withhold-reason TEXT`: drop rows from a shared copy and state the count and
  reason in the Read Me. `evidence-index-report --redact-names`.
- `config.yml` is no longer tracked (copy `config.example.yml`); test fixtures use invented names.

## 0.7.2 — polish and refactor

Verified behaviour-neutral for classification: the pre- and post-refactor classifiers agree on all 77,942 sentences of
every stored page (type, direction, confidence, tags, cues), and extraction is unchanged on a 436-page sample.

- Lint: explicit ruff rule set in `pyproject.toml` (pyflakes, bugbear, async, silent-except, imports, pyupgrade,
  simplify, comprehensions, ruff); clean. Broad `except Exception` blocks narrowed to the errors actually expected.
- `classify_statement` restructured: signals computed once (`_Signals`), an ordered, documented scope-rule table, and
  separate practice / relief-policy decisions. Action positions no longer computed twice per sentence.
- Shared helpers replace copies: `v_excluded_runs` view (21 queries), `counted_in_rates()` (3), `json_meta()` (11),
  AI cache primitives used by both the second opinion and blocked-site AI search, `EntityResearcher.offline()`.
- Docs: architecture module table covers every module.
- Small behaviour changes: `research_max_targets_per_entity` (documented but previously ignored) now caps unfetched
  candidates per organization, keeping the highest scores; malformed entity metadata reads as empty instead of raising;
  sitemap discovery uses the never-raising fetch path; packet cells and slides strip XML-illegal control characters
  (a scraped AP excerpt previously made the whole packet fail).

## 0.7.1 — validated against a hand-built evidence index

- `import-evidence-index`, `verify-precedents --only-index --no-search`, `evidence-index-report`: a researcher's list of
  organizations and source links becomes precedents NSMPA verifies by reading each source; organizations added this way
  are researched and shown but flagged `excluded_from_rates` (never in a denominator or percentage). Secondary reports
  count only where they name the organization. See `docs/evidence_index.md`.
- New `broadcast_newsroom` group (TV/radio), separate denominator; no census loaded yet, so no percentages.
- `nsmpa reclassify --run-id`: re-judge stored excerpts with the current classifier and recompute stances, no fetching.
- Extraction: article text embedded in page data (Arc XP `Fusion.globalContent`, Next.js, JSON-LD); config blobs removed.
- Search recall: wider tier-1 vocabulary ("fresh start", "right to be forgotten", "old crime stories", "de-indexed",
  "reporting policy", "publishing principles", "community guide"); individually blocked first-party policy pages read
  from the archive. Recall on 10 Tier-A organizations: 10/10 policies found.
- Classifier: news stories on a newsroom's own site speak for it only in first person, by name or in editor's notes;
  errors/legal-only removal is a narrow exception; "our main remedy is to deindex"; caveats ("does not necessarily
  remove") are not refusals; "Not one of the editors would remove"; court-record procedures; cooperation with Google;
  social-media post moderation; noun forms ("article removal requests"); removal "from Google" is de-indexing.
- Database: opening is write-free (views refreshed only when their definition changes), and long jobs commit before
  network waits, so read-only commands never time out behind a running job.

## 0.7.0 — sites that block automated access

- Blocked-site fallback (`docs/blocked_sites.md`): archived copies (identity-checked archived homepage, policy/about
  pages from links, search results and a top-level CDX listing), search snippets kept as leads, AI web search (Gemini
  Google Search grounding or Claude web search) whose quotes count only when found word for word in text NSMPA holds,
  and standards published elsewhere. Never bypasses a block. Silence from a blocked site is UNDETERMINED, never "no policy".
- Researcher captures: GUI **Capture** tab (blocked-site queue with AI leads, organization search, paste form) and
  `nsmpa capture`; verbatim text with SHA-256 fingerprint, captured-by and time; stance recomputed; pending AI leads
  confirmed by matching text. `nsmpa leads` lists AI leads.
- Provenance on every page and excerpt (`acquisition`: live, archive, snippet, capture; archive date and link) in the
  GUI, CSV exports and packet ("How obtained", "Archived copy"); packet adds Captures and AI Search Leads sheets.
- Schema v7. Accuracy fixes from the October pilots: Wayback comparison by sentence containment (no false "rewritten"
  reports after site redesigns), byline/headline-suffix false positives removed, article-body extraction fallback,
  identity checks (phrase-level institution match, acronyms, no-institution fallback), smarter site recovery, and
  classifier guards (requester vs. publisher, questions, comments, physical copies, non-journalism removals,
  negation scope, accuracy corrections).

## 0.6.1

- `--redact-names` for the packet and dashboard: private individuals' names withheld (organizations, institutions, cited
  experts and quoted speakers kept); bare-surname follow-ups caught; name-bearing URLs, archive links and snapshot paths
  withheld; masthead and place-name words protected from over-redaction.
- `nsmpa estimate`: cache-aware credit, page-request, time and AI-call estimates; dollar figures only from configured prices.
- `nsmpa pilot`: capped end-to-end trial (seed, estimate, discovery, research with archive/Wayback, AI review, accuracy
  sample, redacted packet + dashboard) with `pilot_report.md`; budget exhaustion explained with the resume command.
- GUI Verify tab: saved analysed text beside the excerpt with highlight, keyboard decisions, stance-driving items first,
  technical observations explained; snapshot pages are script-free, same-origin framed only.

## 0.6.0 — GUI, monitoring, outreach, legal context, re-checks

- Local GUI (`nsmpa gui`): localhost-only stdlib server; live job monitor from persisted heartbeats; results, evidence
  browser, review actions, archive/AI/accuracy and operations panels. Host-header and token guards, strict CSP.
- Offline shareable dashboard (`nsmpa dashboard`): same UI in one read-only HTML file.
- Heartbeats for every long command; per-organization step bar; `nsmpa watch`; macOS finish notifications.
- AI: verified AI attributions feed Expert Voices; `nsmpa ai-discovery` + `promote --use-ai` (model picks among numbered
  candidates only; confident "none" blocks promotion; all AI-assisted promotions reviewed); `packet --ai-summaries`
  (every sentence must cite provided evidence IDs and state no new numbers, else dropped).
- Outreach (`nsmpa outreach harvest|add-contact|dnc|draft|sent|response|status`): own-domain contacts only, neutral
  honestly-identified survey drafts as `.eml` (never auto-sent), replies stored as first-party `direct_response` evidence
  that refreshes the stance.
- Legal context (`nsmpa legal-research`, `nsmpa legal`): EU erasure law, Google Spain, Martin v. Hearst, Washington
  vacation statutes, Clean Slate and mugshot-site leads, one lead per state; never self-verified; packet sheet/slide.
- Policy re-checks (`nsmpa recheck`) with change detection and review items; weekly launchd job (`nsmpa schedule`).
- Fixes: GUI server startup stalled ~35 s on reverse DNS (bind override); GUI counts exclude excluded runs; restored a
  missing import found by new CLI smoke tests. Schema v6 (additive).

## 0.5.1

- AI second opinion now uses Google Gemini Flash by default (`ai_provider: gemini`, `ai_model: gemini-3.8-flash`,
  `google-genai` `generate_content` with JSON-schema structured output; stateless, retries on 429/5xx, blocked or
  truncated responses recorded as failures, never as findings). Claude remains available (`ai_provider: anthropic`).
  `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) read from the environment or `./.env`; keys redacted from all stored errors.

## 0.5.0 — archive evidence, AI second opinion, measured accuracy

- Wayback Machine comparison (`wayback.py`, runs inside `research` and as `nsmpa wayback`): archived vs. current
  versions of sampled crime articles and now-404 article URLs; detects names removed, noindex added, headline and
  text changes, and unpublishing. Names of people are never stored (count + hash only); titles are not stored.
- AI second opinion (`nsmpa ai-review`, Claude API, default `claude-opus-5-5`, structured JSON output): every quote
  verified verbatim against the saved page, fabricated quotes discarded, agreement/disagreement with the rule-based
  classifier recorded, disagreements sent to review; neutral prompt; cached by model+prompt version+page hash;
  per-run call cap; never changes a stance. Server-side refusal fallback enabled by default (`ai_refusal_fallback`).
- Accuracy audit (`nsmpa audit sample|export|import|label|report`): seeded stratified sample, blind Excel/CSV or
  terminal labeling, relevance precision and direction accuracy with Wilson 95% intervals, confusion matrix,
  Cohen's kappa between two labelers, AI-vs-human accuracy.
- Packet: new sheets Archive Changes, AI Second Opinion, Accuracy; new accuracy slide.
- Schema v5 (additive): ai_cache, ai_reviews, ai_findings, wayback_checks, audits, audit_items, audit_labels.

## 0.4.0 — evidence packet

- Per-action positions: clause-level analysis separates unpublish / de-index / anonymize / update; `relief_mode`
  and `preserves_archive_relief` per organization (written policy, practice and technical kept separate).
- Deep archive practice digging: 4 practice searches per newsroom plus free oldest-to-newest sampling of
  crime/arrest articles from sitemaps; editor's notes, name-removal notes and targeted `noindex` (baseline-checked;
  site-wide noindex excluded) recorded as documented practice / technical evidence.
- Case profile (`my_case.yml`, gitignored) and per-excerpt `case_match_score` with matched facts.
- Named-precedent leads with automatic source finding and human-only verification (`verify-precedents`, `precedent`).
- Expert voices: attributed-quote extraction on every page, seeded experts, `research-experts`, `add-expert`, `voices`.
- `nsmpa packet`: Excel evidence workbook (13 sheets, linked sources) + PowerPoint deck (native charts) + summary.
- Classifier: imperative guidance ("Consider…", "Provide updated…"), should/must, "rather than" clauses,
  editor's-note practice, broader dismissed-charge and name-removal patterns, less aggressive chrome filter.
- Schema v4 (additive): voices, experts, precedent_seeds; new columns on evidence_items, entity_stances, research_pages.

## 0.3.0 — 2026-10-07

Hardening and methodology release. Existing data preserved; schema migrated additively (v2 → v3) with automatic backup.

### Bugs fixed
- **Redirect loop on most WordPress sites.** Identity normalization stripped trailing slashes and was applied to redirect targets (`/a` → 301 `/a/` → `/a` …). Fetching now uses `prepare_request_url` (path preserved); `normalize_url` is used only for de-duplication. Loop detection added.
- **Keyword false positives.** v0.2 scored "Please do not remove this page" as adverse, newsletter "archive" links as strict-archive, and a bibliography entry containing "Privacy" as supportive. Replaced by a sentence-level statement classifier (object + action + modality) with boilerplate stripping.
- **Third-party contamination.** Unrelated pages returned by name-only queries were attributed to the entity. Stances now use only first-party policy/guidance; third-party text must name the entity (or follow an anaphor that does) and is stored as documented practice or secondary report.
- **Evidence inflation.** One sentence matching five tags was five rows. Evidence is now one row per unique sentence per entity per run, tags in a join table, near-duplicates collapsed.
- **Null-provider `NO_RELEVANT_GUIDANCE`.** Without an API key, no-op searches counted as completed searches, producing negative findings. Searches are skipped without a real provider and coverage requires real core searches; regression test added.
- **403/bot blocks recorded as `failed` with no error.** Responses are now classified (`blocked`, `rate_limited`, `robots_disallowed`, `server_error`, `timeout`, …) and inaccessible classes count against coverage.
- **Migration atomicity.** `executescript` committed implicitly; migrations now run statement-by-statement inside one transaction each and roll back on failure.
- `Database.execute` turned named (dict) parameters into a tuple of keys.
- Fetcher deadlock risk (pacing waited on the robots lock while robots.txt was being fetched); fractional `Crawl-delay` was ignored.

### Added
- Versioned migration framework (`schema_migrations`), auto-backup before migrating, legacy v0.2 runs labeled `engine_version='0.2'` and excluded from reports.
- Hardened fetcher: DNS + connected-peer SSRF checks (rebinding), IPv4-mapped/6to4/Teredo unwrapping, numeric-host rejection, port allowlist, credential-URL rejection, no proxy env, content-type gate before buffering, streaming size cap, retries with jittered backoff for transport errors/429/5xx, Retry-After, RFC 9309 robots semantics (5xx = disallow), Crawl-delay, per-host circuit breaker, `fetch_safe`.
- Search broker: retries, auth-error stop, Serper credit tiers (1 credit ≤10 results, 2 above; provider-reported credits used when present), `--max-searches` (per invocation) plus cumulative run budget, `--refresh-search`, concurrent identical-query coalescing, `produced_evidence` and latency in the ledger, key redaction.
- Run lifecycle: `run_items` checkpoints, resume by run id, `nsmpa resume`, graceful Ctrl+C (first = finish and checkpoint, second = cancel), budget stops recorded as `budget_exhausted`.
- Dashboard v2 (Serper, Results, Fetching, Timing panels, ETA, last checkpoint, recent discoveries); non-TTY heartbeat; `--quiet` / `--verbose`.
- Main-content extraction (nav/header/footer/aside/cookie removal, encoding detection, lxml), content-addressed immutable snapshots of raw bytes and normalized text.
- Evidence engine: statement types and directions, evidence classes (written policy, editorial statement, documented practice, professional guidance, secondary report), authority/relevance/similarity scores, classifier cues stored per excerpt.
- Transparent similarity model with per-factor matches and configurable weights (four new factors).
- Stance v2 with strongest supportive/adverse excerpts, coverage record, policy-vs-practice contradiction flags, technical summary.
- Research planner v2: tiered adaptive queries (tier 1 always; tier 2 on signal / guidance orgs / deep; tier 3 deep), free first-party homepage policy-link discovery, bounded entity concurrency.
- Discovery v2: specified query ladder with early stop, free site inspection between ladder steps, candidate homepage verification (student identity vs institutional PR), ambiguity detection, cleaned publication names, relationship and discovery method, human/manual verifications never overwritten.
- Human review queue with documented priority scoring; `review`, `verify-evidence`.
- Validation gates and `validate`; national `report` (Markdown/JSON/CSV) with peer groups (state, region, a comparables proxy, control, region, size, R1/R2, core stratum, imported attributes) and precedent tables.
- Professional universe provenance (`entity_sources`), domain-level merge on import, `merge-duplicates`, `import-peer-attributes`.
- Commands: `doctor`, `status` (expanded), `runs`, `query-stats`, `errors`, `resume`, `validate`, `report`, `review`, `verify-evidence`, `version`; `.env` loading; `./config.yml` auto-detected.
- SQL views: `v_institution_peer`, `v_evidence_unique`, `v_cohort_stance_counts`, `v_query_usefulness`.
- Test suite expanded from 18 to 139 tests (fetcher/SSRF, statements, pipeline, budgets, resume, Ctrl+C, migrations, exports, report gating); no live credits used.

## 0.2.0

- Expanded from student-only policy audit to three separate journalism research cohorts.
- Added Serper-first adaptive discovery and research queries.
- Added live progress dashboard, quiet and verbose modes.
- Added durable search cache, query/result ledger and per-run credit budget.
- Added support/advice organization seed and professional newsroom benchmark panel.
- Added generic professional newsroom/support entity CSV importer.
- Added targeted affirmative, adverse, changed-circumstance and precedent search families.
- Added direct target fetching, research snapshots, evidence extraction and stance classification.
- Added configurable fact-pattern similarity ranking.
- Added cohort-specific research exports and methodology guardrails.
- Preserved v0.1 IPEDS, publication, crawl, noindex and A-F/U classification workflows.

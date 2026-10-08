# Building the evidence packet

Goal: hand people the evidence (Excel) and a presentation (PowerPoint) built from it, so nobody has to take
anyone's word for anything. Every row links to its source URL, fetch time and page SHA-256.

## 1. Describe your situation — `my_case.yml`

```bash
nsmpa my-case --init        # creates my_case.yml (gitignored)
# edit it: set the facts that are true for you, relief_sought, and configured: true
nsmpa my-case               # shows the factors that will be matched
```

Each excerpt gets a **match to my case** score (0–100): the share of *your* facts it touches, weighted by
`case_profile` in config.yml. It ranks what you see first; it does not make a precedent authoritative.

## 2. De-indexing vs. unpublishing

Every sentence is split into clauses and each relief action gets its own position:
`unpublish`, `deindex`, `anonymize`, `update` → permitted / conditional / rejected / practiced.
"We do not unpublish, but may remove a name from search results" = unpublish **rejected**, de-index **permitted**.

Each organization then gets a `relief_mode`:

| relief_mode | Meaning |
|---|---|
| `DEINDEX_OR_ANONYMIZE_PRESERVING_ARCHIVE` | De-indexing or anonymization permitted or practiced while the article stays published |
| `UNPUBLISHING_PERMITTED` | Deletion permitted |
| `UPDATE_ONLY` | Only updates / editor's notes |
| `NO_RELIEF` | Relief rejected |
| `UNADDRESSED` | Nothing found (or not inspectable) |

Written policy, documented practice and technical observation are kept in separate columns
(`action_positions_json`); one is never inferred from another.

## 3. Digging past missing policies (archive practice)

`research_practice_dig: always` (default) does, for every newsroom:

1. **Practice searches** (4 credits): editor's notes about dismissed charges, "name has been removed" notes,
   "this story has been updated" + outcome, unpublishing/de-indexing notes.
2. **Archive sampling** (free): crime/arrest article URLs from the site's own sitemaps (keyword rule `CRIME_SLUG`
   in `practice.py`), sorted oldest-first and sampled evenly (`research_crime_article_sample`, default 20), plus
   `research_baseline_article_sample` (default 3) ordinary articles.
3. On each sampled crime article: editor's notes, name-removal notes and `noindex` directives are recorded.
   A `noindex` counts as **targeted de-indexing** only if the ordinary comparison articles on the same site are
   indexable; if everything is noindexed it is recorded as `technical_sitewide_noindex` and does not count.
   Absence from Google is never used.

Organizations with relief practice but no written policy are flagged `relief_practiced_without_written_policy`
and prioritized for review.

## 4. Named precedents

```bash
nsmpa verify-precedents --max-searches 15      # ~1 credit per lead
nsmpa precedent                                # list leads and status
nsmpa precedent --key boston_globe_fresh_start --status human_verified --note "read the Globe's announcement"
nsmpa precedent --add --key my_lead --org "Example Times" --title "Fresh-start policy" --claim "..." --url https://...
```

Seeded leads (`seeds.py`) include the SPJ Code (Minimize Harm), APME's *The Longtail of News* (2009), the Boston
Globe's Fresh Start initiative, cleveland.com's name-removal initiative, AP's minor-crime naming change,
Deborah Dwyer's unpublishing research, SPLC and Poynter guidance, plus two **unconfirmed candidate** leads.
Statuses: `unverified` → `sources_found` / `not_found` (automatic) → `human_verified` / `refuted` (you).
The tool never marks anything human-verified.

## 5. Expert voices

```bash
nsmpa research-experts --max-searches 15       # 1 credit per expert
nsmpa add-expert --name "Jane Doe" --role "Ethics chair" --affiliation "Example Assn." --note "why respected"
nsmpa voices                                   # list; --direction adverse to see dissent
nsmpa voices --verify 12 --status verified --note "quote confirmed on page"
```

Quotes are attributed automatically from "…," said NAME / NAME said / according to NAME patterns and from
first-person statements by the page's byline author. Voices are captured on **every** page the tool reads, not
only for seeded experts; those are labelled "captured from coverage (credentials unverified)". Both supportive
and adverse voices are kept. Roles in the seed list are as commonly reported — verify before presenting.

## 6. Wayback Machine comparison (free)

Runs automatically during `research` (and on demand: `nsmpa wayback --run-id RUN`). For each sampled crime/arrest
article, and each first-party article URL that now returns 404/410, the Internet Archive index is checked; when the
article has changed since its earliest capture, the two versions are compared:

| Observation | Counts as |
|---|---|
| names in the archived version no longer appear | anonymization practiced |
| current version has noindex, archived did not | de-indexing practiced |
| archived as live, now 404/410 | possible unpublishing (or a site migration — verify) |
| headline / text changed | neutral context |

**Privacy:** NSMPA never stores or displays the removed names — only how many. The archived copy (linked in the
"Archive Changes" sheet) may still show them; keep that in mind before sharing archive links.

## 7. AI second opinion (optional)

```bash
nsmpa ai-review --run-id RUN --max-calls 50     # needs GEMINI_API_KEY in ./.env
```

An independent reading of the same pages by Google Gemini Flash (`ai_model`, default `gemini-3.8-flash`; set `ai_provider: anthropic` to use Claude instead). Requests use the stateless `generate_content` call, so pages are not stored as server-side interactions. Every quote it returns
must appear word-for-word in the saved page or it is discarded. Each verified finding is compared with the rule-based
classifier (`agree` / `disagree` / `ai_only`); disagreements and AI-only directional findings go to the review queue.
AI output never changes a stance. The prompt asks only what the page says, in both directions, and never mentions
your case. Results are cached, so re-running costs nothing.

## 8. Measure accuracy

```bash
nsmpa audit sample --n 50                        # reproducible, stratified by direction
nsmpa audit export --out audit_alice.csv         # blind: the machine's answer is hidden
# fill "relevant (y/n)" and "direction" in Excel; a second person fills their own copy
nsmpa audit import audit_alice.csv --labeler alice
nsmpa audit import audit_bob.csv --labeler bob
nsmpa audit report                               # accuracy with 95% CI, confusion matrix, kappa, AI accuracy
```

Or label in the terminal: `nsmpa audit label --labeler alice`. The packet's "Accuracy" sheet and slide show the
latest labeled audit, or "not yet measured".

## 9. AI help with discovery and summaries (optional)

- `nsmpa ai-discovery --max-calls 100` then `nsmpa promote --use-ai`: for institutions where discovery was uncertain, the
  AI chooses among the numbered candidates already found (it cannot add a URL; out-of-range answers are rejected). A
  confident "none of these" blocks promotion. Every AI-assisted promotion is queued for review.
- `nsmpa packet --ai-summaries`: adds 2–3 sentence summaries for guidance, practice and opposing evidence. Every sentence
  must cite evidence IDs that were provided and may not state numbers absent from the evidence; anything else is dropped.
  Limitation: spelled-out numbers ("ninety-nine") are not caught; read each summary before presenting.
- Verified AI attributions (speaker + verbatim quote) are added to Expert Voices with `attribution_method = ai:<model>`.

## 10. Ask newsrooms directly

1. Set `outreach_sender_name`, `outreach_sender_email` (and optionally `outreach_affiliation`) in `config.yml`. Drafts are
   refused without them, so every message honestly identifies you.
2. `nsmpa outreach harvest` collects addresses published on each organization's own domain (no third-party addresses,
   no ad/sales/noreply). Add others with `outreach add-contact`; block any with `outreach dnc`.
3. `nsmpa outreach draft --campaign pilot --limit 20` writes neutral drafts to `output/outreach/pilot/` (open the `.eml` in
   Mail or Outlook, edit, send from your account) plus `mail_merge.csv`. **Nothing is sent by the tool.**
4. `nsmpa outreach sent --campaign pilot` after sending; `nsmpa outreach response --entity-id N --file reply.eml` for each
   reply. Replies become `direct_response` evidence (authority 1.0; quoted copies of the survey are ignored) and the
   organization's stance is recomputed.

## 11. Legal context

`nsmpa legal-research --max-searches 60` searches each lead and keeps the best authoritative excerpt; `nsmpa legal` lists
them. Leads include authority against a legal duty to remove (Martin v. Hearst) as well as erasure law. Not legal advice;
verify before citing (`nsmpa legal --key K --status human_verified`).

## 12. Keep it current

`nsmpa recheck` re-fetches every captured policy page and records changes (similarity and relief statements added/
removed), queuing changed organizations for review. `nsmpa schedule --install` runs it weekly via launchd.

## 13. Build the packet

```bash
nsmpa packet                     # output/packet_<UTC>/
```

- `NSMPA_evidence.xlsx` — sheets: Read Me, Summary, Closest to My Case, De-index vs Unpublish, Named Precedents,
  Professional Guidance, Expert Voices, Documented Practice, **Opposing Evidence**, Archive Changes, AI Second
  Opinion, Accuracy, Entities, All Evidence, Sources, Search Ledger. Clickable source links; filters and frozen headers on every sheet.
- `NSMPA_presentation.pptx` — title, your case profile, method, de-index vs. unpublish chart, position by group,
  named precedents, one slide per closest precedent, expert voices (supportive and cautionary), the
  archive-integrity objection with the data on archive-preserving relief, and limitations.
- `case_packet.md` — one-page summary.

Honesty rules built into the packet: percentages only for groups that pass validation (otherwise
"withheld"); anything not human-verified is labelled UNVERIFIED wherever quoted; slide titles are neutral;
opposing evidence always has its own sheet and slide.

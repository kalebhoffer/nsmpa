# Command reference

Every command accepts `--config PATH` (default `./config.yml` if present). A `.env` in the working directory is loaded without overriding existing environment variables. Opening the database applies pending migrations (with backup).

Common options: `--quiet/-q` minimal output · `--verbose/-v` queries, URLs, decisions, retries, errors · `--max-searches N` live credits this invocation may spend · `--refresh-search` bypass the search cache (spends credits) · `--fresh` reprocess items already done in this run id.

## Setup

| Command | Options | Purpose |
|---|---|---|
| `init` | | Create/migrate the database (backup first) |
| `doctor` | `--network/--no-network`, `--check-serper` (spends 1 credit) | Dependencies, config, user agent, DB integrity, migrations, FKs, writable dirs, API key presence (never value), cache, DNS/HTTPS |
| `version` | | Print version |
| `import-ipeds PATH` | `--source-year` | Upsert IPEDS HD CSV/ZIP; never deletes |
| `import-peer-attributes PATH` | `--source` (required) | CSV `unitid,key,value` for peer groups (e.g. journalism school, CMA membership) |

## Student discovery

| Command | Options | Purpose |
|---|---|---|
| `discover` | `--limit`, `--state` (repeatable), `--unitid` (repeatable), `--run-id`, `--max-searches`, `--refresh-search`, `--fresh`, `-q`, `-v` | Find primary student newspapers; resumable |
| `promote` | `--threshold` | Promote best verified candidate per institution; flags ambiguity; keeps manual/human-verified |
| `add-publication` | `--unitid`, `--url`, `--name`, `--primary/--not-primary` | Record a manually verified publication |

## Universe

| Command | Options | Purpose |
|---|---|---|
| `research-setup` | | Seed support orgs + benchmark newsrooms; sync student publications |
| `seed-support-orgs` | | Seed support/standards organizations |
| `seed-benchmark-newsrooms` | | Seed labeled professional benchmark panel |
| `sync-student-entities` | | Mirror primary publications into `research_entities` |
| `import-entities PATH` | `--cohort` (required), `--source` (required), `--membership` | Import directory CSV (`name,url[,state,parent_name,source_key,…]`); same-domain rows merge as extra provenance |
| `merge-duplicates` | `--cohort` | Merge active same-domain entities; provenance moves to survivor |

## Research

| Command | Options | Purpose |
|---|---|---|
| `research` | `--cohort`, `--limit`, `--entity-id` (repeatable), `--depth quick|standard|deep`, `--concurrency`, `--run-id`, `--max-searches`, `--refresh-search`, `--fresh`, `--no-export`, `-q`, `-v` | Policy, precedent, adverse and guidance research; exports at end |
| `resume` | `--max-searches`, `-q`, `-v` | Resume most recent interrupted/budget-stopped/failed v0.3 run with stored parameters |
| `research-export` | `--run-id` (required), `--out-dir` | Re-export a run |

## Review, validation, reporting

| Command | Options | Purpose |
|---|---|---|
| `review` | `--cohort`, `--run-id`, `--limit` (15), `--export CSV`; or `--decide ID --decision accept|reject|correct|skip [--stance S] [--note] [--reviewer]` | Show prioritized queue or record a decision |
| `verify-evidence ID` | `--status verified|rejected|disputed|unverified` (required), `--note` | Record source verification for an excerpt |
| `validate` | `--run-id` | Per-cohort gates |
| `report` | `--run-id`, `--out-dir` | National report (Markdown, JSON, CSV) |

## Operations

| Command | Options | Purpose |
|---|---|---|
| `status` | | Overview; flags resumable runs |
| `runs` | `--limit` (15) | Recent runs |
| `exclude-run` | `RUN_ID`, `--reason TEXT` (required), `--undo` | Exclude a run (e.g. a smoke test) from `validate`/`report` without deleting data |
| `query-stats` | `--run-id` | Search spend and usefulness by purpose |
| `errors` | `--run-id`, `--limit` (25) | Errors by stage, page access classes, failed items |

## Legacy v0.1 deep crawl

| Command | Options | Purpose |
|---|---|---|
| `crawl` | `--limit`, `--run-id` | Deep-crawl promoted student sites |
| `classify` | `--run-id` (required) | A–F/U classification |
| `export` | `--run-id` (required), `--out-dir` | Export crawl run |
| `pipeline` | `--limit`, `--run-id` | promote → crawl → classify → export |


## Evidence packet (v0.4)

| Command | Options | Purpose |
|---|---|---|
| `my-case` | `--init` | Show or create the `my_case.yml` fact profile |
| `verify-precedents` | `--max-searches`, `--run-id`, `-q`, `-v` | Find and snapshot sources for precedent leads (never self-verifies) |
| `precedent` | `--key --status --note`; `--add --key --org --title --claim (--url\|--query) [--year]` | List leads, record human verification, add a lead |
| `research-experts` | `--max-searches`, `--run-id`, `-q`, `-v` | Search each expert's statements; extract attributed quotes |
| `add-expert` | `--name --role --affiliation [--note]` | Add a respected practitioner/scholar |
| `voices` | `--person`, `--direction`, `--verify ID --status --note`, `--limit` | List attributed statements or verify one |
| `packet` | `--run-id`, `--out-dir`, `--title` | Build Excel workbook + PowerPoint deck + summary |


## Archive, AI and accuracy (v0.5)

| Command | Options | Purpose |
|---|---|---|
| `wayback` | `--run-id` (required), `--entity-id`, `-q`, `-v` | Wayback Machine comparison over an existing research run; refreshes stances |
| `ai-review` | `--run-id`, `--cohort`, `--max-calls`, `--limit`, `--provider gemini\|anthropic`, `--model`, `--resume`, `-q`, `-v` | AI second opinion with verbatim-quote verification |
| `audit sample` | `--n`, `--seed`, `--cohort`, `--description` | Draw a stratified, reproducible sample |
| `audit export` | `--out`, `--audit-id`, `--show-machine` | CSV for Excel labeling (blind by default) |
| `audit import` | `PATH`, `--labeler` | Import one person's labels |
| `audit label` | `--labeler`, `--audit-id`, `--show-machine` | Terminal labeling (q to stop; progress saved) |
| `audit report` | `--audit-id`, `--json` | Accuracy, confidence intervals, confusion matrix, kappa |


## Monitoring, outreach, legal, re-checks (v0.6)

| Command | Options | Purpose |
|---|---|---|
| `gui` | `--port`, `--no-browser` | Local web monitor (127.0.0.1 only) |
| `watch` | `--run-id`, `--interval` | Follow a running job's heartbeat in a terminal |
| `dashboard` | `--out` | Offline, read-only HTML dashboard |
| `ai-discovery` | `--max-calls`, `--limit`, `--unitid` | AI picks the student paper among existing candidates |
| `promote` | `--use-ai` (new) | Let confident AI picks break ties; all queued for review |
| `packet` | `--ai-summaries` (new) | Add citation-checked AI section summaries |
| `outreach harvest` | | Collect contacts published on organizations' own domains |
| `outreach add-contact` | `--entity-id --email [--name --role]` | Add a contact manually |
| `outreach dnc` | `--email` | Do-not-contact |
| `outreach draft` | `--campaign [--cohort --limit]` | Neutral survey drafts (.eml) + mail_merge.csv; never sends |
| `outreach sent` | `--campaign [--entity-id]` | Record that you sent drafts |
| `outreach response` | `--entity-id --file [--campaign]` | Store a reply as first-party evidence; refresh stance |
| `outreach status` | | Counts by campaign and status |
| `legal-research` | `--max-searches`, `--key`, `--run-id` | Find sources for legal-context leads |
| `legal` | `--key --status --note` | List leads / record human verification |
| `recheck` | `--limit`, `-q`, `-v` | Re-fetch known policy pages; record changes |
| `schedule` | `--install`, `--uninstall`, `--weekday`, `--hour` | Weekly launchd `recheck` job (prints by default) |


## Pilot, estimate, redaction, verification (v0.6.1)

| Command | Options | Purpose |
|---|---|---|
| `estimate` | `--cohort`, `--limit`, `--entity-id`, `--depth`, `--institutions`, `--state`, `--json` | Credits (cached queries free), page requests, time, AI calls; dollars only if prices set in config |
| `pilot` | `--budget`, `--ai-calls`, `--support`, `--newsrooms`, `--schools`, `--unitid`, `-q`, `-v` | Capped end-to-end trial with a plain-language report |
| `packet` / `dashboard` | `--redact-names` (new) | Withhold names of private individuals in shared files |
| `gui` → Verify tab | keys V / R / D / S / O | Source verification with the saved copy side by side |

## Blocked sites and captures (v0.7)

| Command | Options | Purpose |
|---|---|---|
| `capture` | `--entity` (id or name), `--url`, `--by`, `--file` (else stdin), `--title`, `--note` | Record policy text copied by hand from a site that blocks robots |
| `leads` | `--status unconfirmed\|confirmed\|no_policy_found\|all`, `--limit` | AI-search leads and whether NSMPA confirmed them |
| `gui` → Capture tab | | Blocked-site queue, capture form, leads, captures |

Research falls back automatically for blocked sites (`blocked_fallback*` settings); see `docs/blocked_sites.md`.

## Evidence index and broadcast group (v0.7.1)

| Command | Options | Purpose |
|---|---|---|
| `import-evidence-index` | `PATH`, `--name` | Import a hand-built list as precedents; new organizations are never counted in rates |
| `verify-precedents` | `--only-index`, `--no-search` (new) | Read listed sources only, with archive fallback; no credits |
| `evidence-index-report` | `--name`, `--out` | Each entry's tier vs. what NSMPA found |

See `docs/evidence_index.md`. Broadcast newsrooms are a separate group: `--cohort broadcast_newsroom`.

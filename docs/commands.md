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

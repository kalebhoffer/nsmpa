# GUI and monitoring

## `nsmpa gui` — live local monitor

```bash
nsmpa gui                 # opens http://127.0.0.1:8765/ in your browser
nsmpa gui --port 9000 --no-browser
```

Run it in its own terminal (or leave it open all day); start jobs in another terminal. Closing the GUI never stops a job.

| Tab | Shows |
|---|---|
| Live | Headline counts; a card per running job (progress, ETA, current organization, phase, per-organization step bar, searches/credits vs. budget, pages, evidence, errors, recent finds), refreshed every 2 s; recent runs table |
| Results | Per group: VALIDATED / PRELIMINARY badge, overall relief position and written-policy stance charts, validation gates |
| Evidence | Searchable, filterable excerpts (group, direction, evidence class), sorted by match to your case; click a row for source URL, page hash, classifier cues |
| Review | Prioritized human-review queue with Accept / Reject / Skip and a note |
| Verify | Source verification, one item at a time: excerpt and details on the left, the exact saved text NSMPA analysed on the right with the excerpt highlighted. Keys: **V** verified, **R** rejected, **D** disputed, **S** skip, **O** open the live page. Excerpts that drive a stance come first. Switch to "Expert voices" to verify quotes. Technical observations (noindex, Wayback) show the recorded directives instead of a quote. (Live GUI only.) |
| Capture | Sites that block automated access: a queue of blocked organizations with AI-search leads, a form to record text you copied by hand (organization search, page URL, pasted text, your initials), the AI leads table and your captures. See `docs/blocked_sites.md`. (Live GUI only.) |
| Archive · AI · Accuracy | Wayback results and changes, AI agreement/disagreements, accuracy audit, expert voices, precedents, legal context, policy changes, outreach |
| Operations | Page access results, errors, search usage by purpose, recent searches, failed items |

Tabs are linkable (`#results`, `#evidence`, ...). Dark mode follows your system setting.

**Security:** binds to 127.0.0.1 only; requests whose Host header is not `127.0.0.1:PORT`/`localhost:PORT` are refused
(defends against DNS rebinding); review decisions require a per-launch token sent in a custom header (defends against
cross-site requests); a strict Content-Security-Policy; read-only queries for everything except review decisions, verification marks and captures. Each
request opens its own database connection, so the GUI never blocks a running job.

## How it sees running jobs

Every long command (`research`, `discover`, `verify-precedents`, `research-experts`, `ai-review`, `wayback`,
`legal-research`, `recheck`) writes a heartbeat to the `run_heartbeats` table every `heartbeat_seconds` (default 2).
A job whose heartbeat is older than 60 seconds is shown as "no heartbeat" (it may have stopped).

## `nsmpa watch` — terminal follower

```bash
nsmpa watch                # follows the most recently active job
nsmpa watch --run-id RUN
```

Same layout as the live dashboard, read from the heartbeat. Ctrl+C stops watching, not the job.

## Notifications

On macOS, a Notification Center alert is shown when a long run finishes or stops on budget, and when `recheck` finds
changed policy pages. Turn off with `notify_on_finish: false`.

## `nsmpa dashboard` — shareable offline file

```bash
nsmpa dashboard --out output/dashboard.html --redact-names
```

One self-contained HTML file (data embedded, no network requests, read-only) with the same tabs. Suitable for emailing
alongside the workbook and deck. The evidence tab includes up to 500 excerpts; the workbook has everything.

# Sites that block automated access

Some newsrooms refuse automated readers: HTTP 401/403, a bot challenge, or a robots.txt rule. In the October 2026 pilot
that was 4 of 8 organizations (AP, Reuters, The Daily of the UW, The Western Front). NSMPA **never bypasses a block**:
no disguised browser, no rotating user agents, no proxies. Evidence obtained by evading a site's rules is easy to
discredit, and it is not ours to take. Instead, when the live site blocks NSMPA, research falls back through channels
that are open to it, cheapest first.

| Step | What happens | Cost | How the evidence is labelled |
|---|---|---|---|
| 1. Archived copies | The Internet Archive's latest capture of the homepage is identity-checked (a parked or repurposed domain stops research). Archived copies of policy/about pages are then read: links on the archived homepage, URLs from search results, and a listing of top-level policy-like paths on the domain | free | `archive`, with the capture date and archive link |
| 2. Search snippets | The usual three `site:` searches. Google has indexed the site even though NSMPA cannot fetch it | ≤ 3 credits | `snippet`: **a lead only**, never used to decide a stance |
| 3. AI search | Gemini with Google Search (or Claude with web search) is asked where the policy is and to quote it verbatim. Each quote must then be found **word for word** in text NSMPA holds. A claimed URL is read from the archive; failing that, one exact-phrase search finds the real page | 1 AI call; ≤ 2 credits | `ai_leads` table: `confirmed` (with where it was confirmed) or `unconfirmed`. Unconfirmed quotes are never evidence |
| 4. Published elsewhere | Professional and support organizations only: one search for their standards handbook or ethics policy on other sites, read live under the usual third-party rules | 1 credit | `live` (third-party source) |
| 5. You capture it | Open the page in your browser, copy the text, record it in the GUI's **Capture** tab or with `nsmpa capture` | free | `capture`, with who captured it, when, and a SHA-256 fingerprint of the exact text |

Rules that keep this honest:

- A blocked site where none of this found policy text is **UNDETERMINED**, never "no relevant guidance".
- Every stance built this way carries the review reason `site_blocks_robots_evidence_from_archive_or_leads`.
- Archived text is as of the capture date. The packet shows the date and links the archived copy, so readers can check it.
- AI answers are not evidence. In testing, Gemini quoted the Daily of the UW's archive policy correctly but gave the
  wrong URL for it. The confirmation step exists for exactly that.

## Capturing by hand

GUI: `nsmpa gui` → **Capture**. The left column lists blocked organizations, most useful first; where AI search found a
lead, it shows the quoted text and URL to look for. **Capture this** fills in the organization. Paste the page text (whole
paragraphs are fine), add the page URL and your name or initials, and save. The tool reports how many excerpts it found,
whether any AI leads were confirmed, and the organization's updated stance.

Command line:

```bash
nsmpa leads                                   # unconfirmed AI leads, with the URLs to open
pbpaste | nsmpa capture --entity 1931 --url https://www.dailyuw.com/page/policies --by KH
nsmpa capture --entity "Western Front" --url https://www.thefrontonline.com/page/about --file front.txt --by KH
```

A capture is attached to the organization's most recent research run, and that run's stance is recomputed. Capturing
the same text twice changes nothing. Captures appear in the packet's **Captures** sheet and as "How obtained: capture"
on every excerpt.

## Settings (`config.yml`)

```yaml
blocked_fallback: true                 # master switch
blocked_fallback_archive_pages: 8      # archived pages read per blocked organization
blocked_fallback_snippets: true
blocked_fallback_ai: true              # needs GEMINI_API_KEY (or the Claude provider)
blocked_fallback_ai_max_calls: 50      # per run
blocked_fallback_phrase_searches: 2    # credits spent locating AI-quoted text
blocked_fallback_other_sources: true
```

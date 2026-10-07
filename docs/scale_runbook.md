# Scale runbook

## Stage 0: configuration

Use an identifiable user agent with a real project contact. Keep robots.txt enabled. Start with the
default 1.5-second per-host delay and no more than two concurrent requests per host.

## Stage 1: institution universe

Import the latest official IPEDS `HD` directory file. Freeze the source file and SHA-256 it outside
NSMPA if the study will be published. Record the exact filters used for level and control.

## Stage 2: discovery pilot

Run discovery on 50-100 institutions across multiple states. Review candidate precision before
promoting nationally. If independent newspaper domains are frequently missed, enable a search API
and adjust discovery scoring rather than lowering the promotion threshold indiscriminately.

## Stage 3: national discovery

Run discovery for the full filtered institution universe. Export or query `publication_candidates`
for QA. Manually correct high-impact institutions and obvious false positives.

## Stage 4: crawl pilot

Crawl 25-50 verified publications. Inspect error rates, robots exclusions, snapshot volume, and the
number of pages per site. Adjust limits conservatively.

## Stage 5: national crawl

Use a persistent disk. Resume interrupted work with the same run ID. Do not delete error rows during
a run. They are part of the coverage record.

## Stage 6: classification and review

Run deterministic classification, then work through `human_review_queue.csv`. For advocacy or
publication, manually verify all positive changed-outcome examples and all restrictive counterexamples.

## Stage 7: reporting

Publish the universe, discovery coverage, crawl success, unresolved share, classification counts,
and human-verification method alongside any percentages.

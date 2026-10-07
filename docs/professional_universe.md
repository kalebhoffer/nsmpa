# Building the professional-newsroom universe

There is no single public registry that cleanly equals "all U.S. journalism." NSMPA therefore builds a sourced union and preserves provenance.

## High-priority sources

1. **Institute for Nonprofit News / Find Your News** for nonprofit member newsrooms.
2. **LION Publishers** for independent local digital publishers.
3. **State press/newspaper association member directories** for local and regional newspapers.
4. **National Newspaper Association and other national publisher associations** where member directories are publicly usable.
5. **Public-media and broadcast source lists** if the study expands beyond text/digital newsrooms.
6. **Professional newsroom benchmark panel** for early policy comparisons while directory ingestion is still underway.

`data/professional_universe_sources.csv` records the initial source registry.

## Required provenance

Every imported newsroom should retain:

- source directory/name
- source date/version if known
- publication name
- homepage URL/domain
- state/market if available
- membership/category metadata
- verification status

## Reporting rule

Until the union has a defensible denominator, report it as a "professional newsroom panel" or "directory-derived professional newsroom dataset," not as a census of all U.S. journalism.

## De-duplication

Prefer organization identity over URL identity. One newsroom can appear in INN, LION and a state association. Retain all memberships as metadata while collapsing the analytical entity to one newsroom.

## Broadcast caution

FCC licensing can help establish a universe of stations, but a broadcast license does not prove a currently staffed news operation. Broadcast inclusion therefore needs a newsroom-activity verification layer.

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

## Importing

```bash
nsmpa import-entities inn.csv --cohort professional_newsroom --source inn_directory_2026 --membership "INN member"
nsmpa import-entities lion.csv --cohort professional_newsroom --source lion_members_2026 --membership "LION member"
nsmpa import-entities wnpa.csv --cohort professional_newsroom --source wnpa_members_2026 --membership "WNPA member"
nsmpa merge-duplicates --cohort professional_newsroom
```

CSV columns: `name` and `url` (or `homepage_url`/`website`) required; optional `state`, `parent_name`, `source_key`; all other columns are kept as metadata. Use a dated `--source` label per directory version.

## Provenance and de-duplication

- Each import row is recorded in `entity_sources` (`source`, `source_key`, `source_url`, `membership_label`, raw row JSON).
- A row whose domain already belongs to an active entity in the same cohort is **not** created as a new entity; it is added to that entity's `entity_sources` (reported as `merged_as_additional_source`). One newsroom in INN, LION and a state association is one analytical entity with three memberships.
- `merge-duplicates` collapses pre-existing same-domain entities: the lowest id survives, others become `active=0, merged_into=<id>`, and their provenance moves to the survivor. Nothing is deleted.
- Domain identity is a heuristic; newsrooms sharing a corporate domain or using several domains need manual review.

## Reporting rule

The professional cohort denominator is the count of active, de-duplicated entities, and `nsmpa report` prints its source breakdown. Describe it as a "directory-derived professional newsroom panel," not a census of U.S. journalism, unless the sources are demonstrably exhaustive.

## Broadcast caution

FCC licensing can help establish a universe of stations, but a broadcast license does not prove a currently staffed news operation. Broadcast inclusion therefore needs a newsroom-activity verification layer.

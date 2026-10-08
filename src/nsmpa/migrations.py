"""Versioned, additive schema migrations.

Rules:
- Migrations never drop or rewrite research data. Legacy tables stay readable.
- Each migration runs in its own transaction and is recorded in ``schema_migrations``.
- Before the first pending migration is applied to a non-empty database, a full SQLite
  backup is written next to the database (``backups/``).
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable

BASELINE_V2 = r"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS institutions (
  unitid TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  city TEXT,
  state TEXT,
  website TEXT,
  control INTEGER,
  level INTEGER,
  source_year INTEGER,
  included INTEGER NOT NULL DEFAULT 1,
  raw_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS publication_candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  unitid TEXT NOT NULL REFERENCES institutions(unitid) ON DELETE CASCADE,
  url TEXT NOT NULL,
  domain TEXT NOT NULL,
  title TEXT,
  snippet TEXT,
  source TEXT NOT NULL,
  query TEXT,
  score REAL NOT NULL DEFAULT 0,
  score_reasons_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'candidate',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(unitid, url)
);

CREATE INDEX IF NOT EXISTS idx_candidates_unitid_score ON publication_candidates(unitid, score DESC);

CREATE TABLE IF NOT EXISTS publications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  unitid TEXT NOT NULL REFERENCES institutions(unitid) ON DELETE CASCADE,
  name TEXT,
  homepage_url TEXT NOT NULL,
  domain TEXT NOT NULL,
  confidence REAL NOT NULL,
  verification_status TEXT NOT NULL DEFAULT 'auto',
  is_primary INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(unitid, domain)
);

CREATE TABLE IF NOT EXISTS crawl_runs (
  id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TEXT,
  command TEXT,
  config_json TEXT NOT NULL,
  git_sha TEXT,
  status TEXT NOT NULL DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS pages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  publication_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL REFERENCES crawl_runs(id) ON DELETE CASCADE,
  requested_url TEXT NOT NULL,
  final_url TEXT,
  canonical_url TEXT,
  depth INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  http_status INTEGER,
  content_type TEXT,
  title TEXT,
  text_length INTEGER NOT NULL DEFAULT 0,
  content_sha256 TEXT,
  meta_robots TEXT,
  x_robots_tag TEXT,
  noindex INTEGER NOT NULL DEFAULT 0,
  nofollow INTEGER NOT NULL DEFAULT 0,
  policy_score REAL NOT NULL DEFAULT 0,
  evidence_tags_json TEXT NOT NULL DEFAULT '[]',
  headers_json TEXT NOT NULL DEFAULT '{}',
  snapshot_path TEXT,
  error TEXT,
  fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(publication_id, run_id, requested_url)
);

CREATE INDEX IF NOT EXISTS idx_pages_pub_policy ON pages(publication_id, policy_score DESC);
CREATE INDEX IF NOT EXISTS idx_pages_noindex ON pages(publication_id, noindex);

CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  publication_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
  page_id INTEGER REFERENCES pages(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL REFERENCES crawl_runs(id) ON DELETE CASCADE,
  tag TEXT NOT NULL,
  excerpt TEXT NOT NULL,
  source_url TEXT NOT NULL,
  evidence_type TEXT NOT NULL DEFAULT 'written_policy',
  supports_relief INTEGER,
  confidence REAL NOT NULL DEFAULT 0.5,
  excerpt_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(publication_id, run_id, tag, excerpt_sha256)
);

CREATE TABLE IF NOT EXISTS classifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  publication_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL REFERENCES crawl_runs(id) ON DELETE CASCADE,
  primary_class TEXT NOT NULL,
  confidence REAL NOT NULL,
  rationale TEXT NOT NULL,
  inspection_complete INTEGER NOT NULL DEFAULT 0,
  pages_attempted INTEGER NOT NULL DEFAULT 0,
  pages_fetched INTEGER NOT NULL DEFAULT 0,
  policy_pages_found INTEGER NOT NULL DEFAULT 0,
  blockers_json TEXT NOT NULL DEFAULT '[]',
  requires_human_review INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(publication_id, run_id)
);

CREATE TABLE IF NOT EXISTS errors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT REFERENCES crawl_runs(id) ON DELETE SET NULL,
  unitid TEXT,
  publication_id INTEGER,
  stage TEXT NOT NULL,
  url TEXT,
  error_type TEXT,
  message TEXT NOT NULL,
  retryable INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS research_runs (
  id TEXT PRIMARY KEY,
  mode TEXT NOT NULL DEFAULT 'full',
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TEXT,
  config_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'running',
  searches_live INTEGER NOT NULL DEFAULT 0,
  searches_cached INTEGER NOT NULL DEFAULT 0,
  credits_estimated INTEGER NOT NULL DEFAULT 0,
  entities_completed INTEGER NOT NULL DEFAULT 0,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS research_entities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cohort TEXT NOT NULL,
  source_key TEXT NOT NULL,
  name TEXT NOT NULL,
  homepage_url TEXT,
  domain TEXT,
  state TEXT,
  parent_name TEXT,
  source TEXT NOT NULL DEFAULT 'manual',
  verification_status TEXT NOT NULL DEFAULT 'seeded',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(cohort, source_key)
);

CREATE INDEX IF NOT EXISTS idx_research_entities_cohort ON research_entities(cohort, active, name);
CREATE INDEX IF NOT EXISTS idx_research_entities_domain ON research_entities(domain);

CREATE TABLE IF NOT EXISTS search_cache (
  provider TEXT NOT NULL,
  query_hash TEXT NOT NULL,
  query TEXT NOT NULL,
  count_requested INTEGER NOT NULL,
  response_json TEXT NOT NULL,
  result_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_used_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  uses INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY(provider, query_hash)
);

CREATE TABLE IF NOT EXISTS search_queries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
  entity_id INTEGER REFERENCES research_entities(id) ON DELETE SET NULL,
  unitid TEXT,
  provider TEXT NOT NULL,
  purpose TEXT NOT NULL,
  query TEXT NOT NULL,
  query_hash TEXT NOT NULL,
  count_requested INTEGER NOT NULL,
  result_count INTEGER NOT NULL DEFAULT 0,
  was_cached INTEGER NOT NULL DEFAULT 0,
  credits_estimated INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'completed',
  error TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_search_queries_run ON search_queries(run_id, purpose, entity_id, unitid);
CREATE INDEX IF NOT EXISTS idx_search_queries_hash ON search_queries(provider, query_hash);

CREATE TABLE IF NOT EXISTS search_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  query_id INTEGER NOT NULL REFERENCES search_queries(id) ON DELETE CASCADE,
  rank INTEGER NOT NULL,
  url TEXT NOT NULL,
  domain TEXT,
  title TEXT,
  snippet TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(query_id, rank, url)
);

CREATE TABLE IF NOT EXISTS research_targets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
  entity_id INTEGER NOT NULL REFERENCES research_entities(id) ON DELETE CASCADE,
  query_id INTEGER REFERENCES search_queries(id) ON DELETE SET NULL,
  purpose TEXT NOT NULL,
  topic TEXT NOT NULL,
  url TEXT NOT NULL,
  domain TEXT,
  title TEXT,
  snippet TEXT,
  score REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'candidate',
  score_reasons_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(run_id, entity_id, purpose, url)
);

CREATE INDEX IF NOT EXISTS idx_research_targets_entity_score ON research_targets(run_id, entity_id, score DESC);

CREATE TABLE IF NOT EXISTS research_pages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
  entity_id INTEGER NOT NULL REFERENCES research_entities(id) ON DELETE CASCADE,
  target_id INTEGER REFERENCES research_targets(id) ON DELETE SET NULL,
  requested_url TEXT NOT NULL,
  final_url TEXT,
  http_status INTEGER,
  status TEXT NOT NULL,
  content_type TEXT,
  title TEXT,
  text_length INTEGER NOT NULL DEFAULT 0,
  content_sha256 TEXT,
  meta_robots TEXT,
  x_robots_tag TEXT,
  noindex INTEGER NOT NULL DEFAULT 0,
  policy_score REAL NOT NULL DEFAULT 0,
  evidence_tags_json TEXT NOT NULL DEFAULT '[]',
  headers_json TEXT NOT NULL DEFAULT '{}',
  snapshot_path TEXT,
  error TEXT,
  fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(run_id, entity_id, requested_url)
);

CREATE INDEX IF NOT EXISTS idx_research_pages_entity ON research_pages(run_id, entity_id, policy_score DESC);

CREATE TABLE IF NOT EXISTS research_evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
  entity_id INTEGER NOT NULL REFERENCES research_entities(id) ON DELETE CASCADE,
  page_id INTEGER REFERENCES research_pages(id) ON DELETE CASCADE,
  topic TEXT,
  tag TEXT NOT NULL,
  excerpt TEXT NOT NULL,
  source_url TEXT NOT NULL,
  evidence_type TEXT NOT NULL DEFAULT 'written_policy',
  supports_relief INTEGER,
  confidence REAL NOT NULL DEFAULT 0.5,
  similarity_score REAL NOT NULL DEFAULT 0,
  excerpt_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(run_id, entity_id, tag, excerpt_sha256)
);

CREATE INDEX IF NOT EXISTS idx_research_evidence_similarity ON research_evidence(run_id, similarity_score DESC);

CREATE TABLE IF NOT EXISTS entity_stances (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
  entity_id INTEGER NOT NULL REFERENCES research_entities(id) ON DELETE CASCADE,
  stance TEXT NOT NULL,
  confidence REAL NOT NULL,
  rationale TEXT NOT NULL,
  evidence_count INTEGER NOT NULL DEFAULT 0,
  supportive_count INTEGER NOT NULL DEFAULT 0,
  adverse_count INTEGER NOT NULL DEFAULT 0,
  max_similarity_score REAL NOT NULL DEFAULT 0,
  requires_human_review INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(run_id, entity_id)
);

"""

def run_script(conn: sqlite3.Connection, script: str) -> None:
    """Execute a multi-statement script inside the caller's transaction.

    ``sqlite3.executescript`` commits implicitly, which would break per-migration atomicity.
    """
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            stmt = buf.strip()
            buf = ""
            if stmt and not stmt.upper().startswith("PRAGMA"):
                conn.execute(stmt)
    if buf.strip():
        raise ValueError(f"Incomplete SQL statement in migration: {buf[:120]!r}")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def migrate_v2(conn: sqlite3.Connection) -> None:
    """Baseline schema shipped with v0.1/v0.2 (idempotent)."""
    run_script(conn, BASELINE_V2)


V3_TABLES = r"""
CREATE TABLE IF NOT EXISTS run_items (
  run_id TEXT NOT NULL,
  item_type TEXT NOT NULL,
  item_key TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  started_at TEXT,
  completed_at TEXT,
  error TEXT,
  result_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(run_id, item_type, item_key)
);
CREATE INDEX IF NOT EXISTS idx_run_items_status ON run_items(run_id, item_type, status);

CREATE TABLE IF NOT EXISTS snapshots (
  sha256 TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  content_type TEXT,
  bytes INTEGER NOT NULL,
  first_url TEXT,
  first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS evidence_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
  entity_id INTEGER NOT NULL REFERENCES research_entities(id) ON DELETE CASCADE,
  page_id INTEGER REFERENCES research_pages(id) ON DELETE SET NULL,
  query_id INTEGER REFERENCES search_queries(id) ON DELETE SET NULL,
  cohort TEXT NOT NULL,
  source_url TEXT NOT NULL,
  source_title TEXT,
  source_domain TEXT,
  fetched_at TEXT,
  page_sha256 TEXT,
  text_sha256 TEXT,
  excerpt TEXT NOT NULL,
  context TEXT,
  excerpt_sha256 TEXT NOT NULL,
  near_dup_key TEXT NOT NULL,
  duplicate_of INTEGER REFERENCES evidence_items(id) ON DELETE SET NULL,
  first_party INTEGER NOT NULL DEFAULT 0,
  about_entity INTEGER NOT NULL DEFAULT 0,
  evidence_class TEXT NOT NULL,
  statement_type TEXT NOT NULL,
  direction TEXT NOT NULL,
  topic TEXT,
  authority_score REAL NOT NULL DEFAULT 0,
  relevance_score REAL NOT NULL DEFAULT 0,
  similarity_score REAL NOT NULL DEFAULT 0,
  similarity_factors_json TEXT NOT NULL DEFAULT '[]',
  extraction_confidence REAL NOT NULL DEFAULT 0,
  rationale TEXT,
  verification_status TEXT NOT NULL DEFAULT 'unverified',
  reviewer_note TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(run_id, entity_id, excerpt_sha256)
);
CREATE INDEX IF NOT EXISTS idx_evidence_items_entity ON evidence_items(run_id, entity_id, direction);
CREATE INDEX IF NOT EXISTS idx_evidence_items_similarity ON evidence_items(run_id, similarity_score DESC);
CREATE INDEX IF NOT EXISTS idx_evidence_items_neardup ON evidence_items(near_dup_key);
CREATE INDEX IF NOT EXISTS idx_evidence_items_cohort ON evidence_items(run_id, cohort, statement_type);

CREATE TABLE IF NOT EXISTS evidence_item_tags (
  evidence_id INTEGER NOT NULL REFERENCES evidence_items(id) ON DELETE CASCADE,
  tag TEXT NOT NULL,
  PRIMARY KEY(evidence_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_evidence_item_tags_tag ON evidence_item_tags(tag);

CREATE TABLE IF NOT EXISTS review_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  item_type TEXT NOT NULL,
  item_id INTEGER NOT NULL,
  entity_id INTEGER,
  cohort TEXT,
  priority REAL NOT NULL DEFAULT 0,
  reasons_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'open',
  decision TEXT,
  reviewer TEXT,
  note TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  reviewed_at TEXT,
  UNIQUE(run_id, item_type, item_id)
);
CREATE INDEX IF NOT EXISTS idx_review_queue_open ON review_queue(status, priority DESC);

CREATE TABLE IF NOT EXISTS entity_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_id INTEGER NOT NULL REFERENCES research_entities(id) ON DELETE CASCADE,
  source TEXT NOT NULL,
  source_key TEXT NOT NULL,
  source_url TEXT,
  membership_label TEXT,
  raw_json TEXT NOT NULL DEFAULT '{}',
  imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(entity_id, source, source_key)
);
CREATE INDEX IF NOT EXISTS idx_entity_sources_source ON entity_sources(source);

CREATE TABLE IF NOT EXISTS institution_attributes (
  unitid TEXT NOT NULL REFERENCES institutions(unitid) ON DELETE CASCADE,
  key TEXT NOT NULL,
  value TEXT,
  source TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(unitid, key, source)
);
CREATE INDEX IF NOT EXISTS idx_institution_attributes_key ON institution_attributes(key, value);

CREATE INDEX IF NOT EXISTS idx_institutions_included ON institutions(included, state);
CREATE INDEX IF NOT EXISTS idx_publications_unitid ON publications(unitid);
CREATE INDEX IF NOT EXISTS idx_research_pages_url ON research_pages(final_url);
CREATE INDEX IF NOT EXISTS idx_research_pages_status ON research_pages(run_id, status);
CREATE INDEX IF NOT EXISTS idx_errors_run ON errors(run_id, stage);
CREATE INDEX IF NOT EXISTS idx_search_results_query ON search_results(query_id);
CREATE INDEX IF NOT EXISTS idx_entity_stances_run ON entity_stances(run_id, stance);
"""


def migrate_v3(conn: sqlite3.Connection) -> None:
    """v0.3: checkpoints, unique evidence items, review queue, provenance, peer attributes."""
    run_script(conn, V3_TABLES)
    for col, decl in [
        ("engine_version", "TEXT NOT NULL DEFAULT '0.2'"),
        ("command", "TEXT"),
        ("params_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("status_reason", "TEXT"),
        ("last_checkpoint_at", "TEXT"),
        ("max_searches", "INTEGER"),
    ]:
        _add_column(conn, "research_runs", col, decl)
    for col, decl in [
        ("produced_evidence", "INTEGER NOT NULL DEFAULT 0"),
        ("latency_ms", "INTEGER"),
        ("refresh", "INTEGER NOT NULL DEFAULT 0"),
    ]:
        _add_column(conn, "search_queries", col, decl)
    for col, decl in [
        ("canonical_url", "TEXT"),
        ("access_class", "TEXT"),
        ("page_kind", "TEXT"),
        ("text_sha256", "TEXT"),
        ("redirect_chain_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("in_sitemap", "INTEGER"),
        ("query_id", "INTEGER"),
        ("first_party", "INTEGER NOT NULL DEFAULT 0"),
    ]:
        _add_column(conn, "research_pages", col, decl)
    for col, decl in [
        ("stance_version", "TEXT NOT NULL DEFAULT '0.2'"),
        ("strongest_supportive_id", "INTEGER"),
        ("strongest_adverse_id", "INTEGER"),
        ("coverage_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("review_reasons_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("review_status", "TEXT NOT NULL DEFAULT 'unreviewed'"),
        ("practice_summary", "TEXT"),
        ("technical_summary", "TEXT"),
    ]:
        _add_column(conn, "entity_stances", col, decl)
    for col, decl in [
        ("merged_into", "INTEGER"),
        ("priority_tier", "TEXT"),
        ("entity_type", "TEXT"),
    ]:
        _add_column(conn, "research_entities", col, decl)
    for col, decl in [
        ("relationship", "TEXT NOT NULL DEFAULT 'unknown'"),
        ("discovery_method", "TEXT"),
        ("candidate_id", "INTEGER"),
        ("ambiguous", "INTEGER NOT NULL DEFAULT 0"),
        ("verification_note", "TEXT"),
        ("verified_at", "TEXT"),
    ]:
        _add_column(conn, "publications", col, decl)
    for col, decl in [
        ("discovery_run_id", "TEXT"),
        ("verification_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("verified_score", "REAL"),
    ]:
        _add_column(conn, "publication_candidates", col, decl)
    for col, decl in [("entity_id", "INTEGER"), ("host", "TEXT"), ("research_run_id", "TEXT")]:
        _add_column(conn, "errors", col, decl)

    # Record provenance for pre-existing seeded/imported entities so that later merges keep it.
    conn.execute(
        """
        INSERT OR IGNORE INTO entity_sources(entity_id,source,source_key,source_url,raw_json)
        SELECT id, source, source_key, homepage_url, metadata_json FROM research_entities
        """
    )
    # Mark v0.2 research data as legacy so v0.3 reports never mix engines silently.
    conn.execute("UPDATE research_runs SET engine_version='0.2' WHERE engine_version IS NULL OR engine_version=''")
    _create_views(conn)


VIEWS = r"""
-- Runs excluded from reports (smoke tests, superseded pilots). Every report query filters with
-- "run_id NOT IN (SELECT id FROM v_excluded_runs)".
DROP VIEW IF EXISTS v_excluded_runs;
CREATE VIEW v_excluded_runs AS SELECT id FROM research_runs WHERE status='excluded';
DROP VIEW IF EXISTS v_institution_peer;
CREATE VIEW v_institution_peer AS
SELECT i.unitid, i.name, i.state, i.included,
  CASE i.control WHEN 1 THEN 'public' WHEN 2 THEN 'private_nonprofit' WHEN 3 THEN 'private_for_profit' ELSE 'unknown' END AS control_label,
  CAST(json_extract(i.raw_json,'$.OBEREG') AS INTEGER) AS obe_region,
  CAST(json_extract(i.raw_json,'$.INSTSIZE') AS INTEGER) AS size_category,
  CAST(json_extract(i.raw_json,'$.C21BASIC') AS INTEGER) AS carnegie_basic,
  CAST(json_extract(i.raw_json,'$.HBCU') AS INTEGER) AS hbcu,
  CAST(json_extract(i.raw_json,'$.LANDGRNT') AS INTEGER) AS land_grant,
  CASE WHEN CAST(json_extract(i.raw_json,'$.C21BASIC') AS INTEGER) IN (15,16) THEN 1 ELSE 0 END AS research_university,
  CASE WHEN CAST(json_extract(i.raw_json,'$.C21BASIC') AS INTEGER) BETWEEN 15 AND 23 THEN 1 ELSE 0 END AS core_stratum,
  json_extract(i.raw_json,'$.COUNTYNM') AS county
FROM institutions i;

DROP VIEW IF EXISTS v_evidence_unique;
CREATE VIEW v_evidence_unique AS
SELECT e.*, re.name AS entity_name, re.parent_name,
  (SELECT group_concat(tag, ';') FROM evidence_item_tags t WHERE t.evidence_id=e.id) AS tags
FROM evidence_items e JOIN research_entities re ON re.id=e.entity_id
WHERE e.duplicate_of IS NULL;

DROP VIEW IF EXISTS v_cohort_stance_counts;
CREATE VIEW v_cohort_stance_counts AS
SELECT s.run_id, re.cohort, s.stance, COUNT(*) AS n
FROM entity_stances s JOIN research_entities re ON re.id=s.entity_id
GROUP BY s.run_id, re.cohort, s.stance;

DROP VIEW IF EXISTS v_query_usefulness;
CREATE VIEW v_query_usefulness AS
SELECT run_id, purpose, COUNT(*) AS queries, SUM(was_cached) AS cached,
  SUM(credits_estimated) AS credits, SUM(produced_evidence) AS useful,
  ROUND(1.0*SUM(produced_evidence)/COUNT(*),3) AS useful_rate
FROM search_queries GROUP BY run_id, purpose;
"""


def _create_views(conn: sqlite3.Connection) -> None:
    run_script(conn, VIEWS)


V4_TABLES = r"""
CREATE TABLE IF NOT EXISTS voices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_key TEXT NOT NULL,
  person_name TEXT NOT NULL,
  role TEXT,
  affiliation TEXT,
  expert_id INTEGER REFERENCES experts(id) ON DELETE SET NULL,
  quote TEXT NOT NULL,
  quote_sha256 TEXT NOT NULL,
  context TEXT,
  source_url TEXT NOT NULL,
  source_title TEXT,
  source_domain TEXT,
  page_id INTEGER REFERENCES research_pages(id) ON DELETE SET NULL,
  run_id TEXT,
  entity_id INTEGER REFERENCES research_entities(id) ON DELETE SET NULL,
  statement_type TEXT NOT NULL,
  direction TEXT NOT NULL,
  actions_json TEXT NOT NULL DEFAULT '{}',
  attribution_method TEXT NOT NULL,
  attribution_confidence REAL NOT NULL DEFAULT 0,
  case_match_score REAL NOT NULL DEFAULT 0,
  verification_status TEXT NOT NULL DEFAULT 'unverified',
  reviewer_note TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(person_key, quote_sha256)
);
CREATE INDEX IF NOT EXISTS idx_voices_person ON voices(person_key);
CREATE INDEX IF NOT EXISTS idx_voices_direction ON voices(direction, case_match_score DESC);

CREATE TABLE IF NOT EXISTS experts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_key TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  role TEXT,
  affiliation TEXT,
  credential_note TEXT,
  source TEXT NOT NULL DEFAULT 'builtin_seed',
  entity_id INTEGER REFERENCES research_entities(id) ON DELETE SET NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS precedent_seeds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  seed_key TEXT NOT NULL UNIQUE,
  organization TEXT NOT NULL,
  title TEXT NOT NULL,
  approx_year TEXT,
  claim TEXT NOT NULL,
  actions TEXT,
  verification_query TEXT,
  primary_url TEXT,
  prior_confidence TEXT NOT NULL DEFAULT 'candidate',
  entity_id INTEGER REFERENCES research_entities(id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'unverified',
  status_note TEXT,
  last_checked_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def migrate_v4(conn: sqlite3.Connection) -> None:
    """v0.4: per-action positions, case-match scores, expert voices, precedent seeds, practice digging."""
    run_script(conn, V4_TABLES)
    for col, decl in [
        ("actions_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("case_match_score", "REAL NOT NULL DEFAULT 0"),
        ("case_match_factors_json", "TEXT NOT NULL DEFAULT '[]'"),
    ]:
        _add_column(conn, "evidence_items", col, decl)
    for col, decl in [
        ("action_positions_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("preserves_archive_relief", "INTEGER NOT NULL DEFAULT 0"),
        ("relief_mode", "TEXT"),
    ]:
        _add_column(conn, "entity_stances", col, decl)
    _add_column(conn, "research_pages", "author", "TEXT")
    _add_column(conn, "research_pages", "published_date", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_items_casematch ON evidence_items(case_match_score DESC)")
    _create_views(conn)


V5_TABLES = r"""
CREATE TABLE IF NOT EXISTS ai_cache (
  cache_key TEXT PRIMARY KEY,
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  response_json TEXT NOT NULL,
  input_tokens INTEGER,
  output_tokens INTEGER,
  stop_reason TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ai_reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  entity_id INTEGER REFERENCES research_entities(id) ON DELETE CASCADE,
  page_id INTEGER REFERENCES research_pages(id) ON DELETE CASCADE,
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  cache_key TEXT NOT NULL,
  was_cached INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  error TEXT,
  findings_total INTEGER NOT NULL DEFAULT 0,
  findings_verified INTEGER NOT NULL DEFAULT 0,
  summary TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(run_id, page_id, prompt_version, model)
);

CREATE TABLE IF NOT EXISTS ai_findings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  review_id INTEGER NOT NULL REFERENCES ai_reviews(id) ON DELETE CASCADE,
  entity_id INTEGER,
  page_id INTEGER,
  quote TEXT NOT NULL,
  quote_verified INTEGER NOT NULL,
  kind TEXT,
  action TEXT,
  position TEXT,
  direction TEXT,
  conditions TEXT,
  speaker TEXT,
  speaker_role TEXT,
  matched_evidence_id INTEGER REFERENCES evidence_items(id) ON DELETE SET NULL,
  agreement TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ai_findings_entity ON ai_findings(entity_id, agreement);

CREATE TABLE IF NOT EXISTS wayback_checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  entity_id INTEGER NOT NULL REFERENCES research_entities(id) ON DELETE CASCADE,
  page_id INTEGER REFERENCES research_pages(id) ON DELETE SET NULL,
  url TEXT NOT NULL,
  status TEXT NOT NULL,
  snapshots INTEGER NOT NULL DEFAULT 0,
  earliest_ts TEXT,
  compared_ts TEXT,
  archive_url TEXT,
  observations_json TEXT NOT NULL DEFAULT '[]',
  error TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(run_id, entity_id, url)
);

CREATE TABLE IF NOT EXISTS audits (
  id TEXT PRIMARY KEY,
  description TEXT,
  sample_size INTEGER NOT NULL,
  seed INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  audit_id TEXT NOT NULL REFERENCES audits(id) ON DELETE CASCADE,
  evidence_id INTEGER NOT NULL REFERENCES evidence_items(id) ON DELETE CASCADE,
  stratum TEXT NOT NULL,
  UNIQUE(audit_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS audit_labels (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  audit_item_id INTEGER NOT NULL REFERENCES audit_items(id) ON DELETE CASCADE,
  labeler TEXT NOT NULL,
  relevant INTEGER,
  direction TEXT,
  statement_type TEXT,
  note TEXT,
  labeled_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(audit_item_id, labeler)
);
"""


def migrate_v5(conn: sqlite3.Connection) -> None:
    """v0.5: AI second-opinion review, Wayback Machine comparisons, accuracy audits."""
    run_script(conn, V5_TABLES)
    _create_views(conn)


V6_TABLES = r"""
CREATE TABLE IF NOT EXISTS run_heartbeats (
  run_id TEXT PRIMARY KEY,
  title TEXT,
  state_json TEXT NOT NULL,
  pid INTEGER,
  finished INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_id INTEGER NOT NULL REFERENCES research_entities(id) ON DELETE CASCADE,
  email TEXT NOT NULL,
  name TEXT,
  role TEXT,
  source_url TEXT,
  source TEXT NOT NULL DEFAULT 'harvested',
  do_not_contact INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(entity_id, email)
);

CREATE TABLE IF NOT EXISTS outreach_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign TEXT NOT NULL,
  entity_id INTEGER NOT NULL REFERENCES research_entities(id) ON DELETE CASCADE,
  contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'drafted',
  draft_path TEXT,
  subject TEXT,
  drafted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  sent_at TEXT,
  responded_at TEXT,
  response_path TEXT,
  response_sha256 TEXT,
  note TEXT,
  UNIQUE(campaign, entity_id, contact_id)
);

CREATE TABLE IF NOT EXISTS legal_context (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL UNIQUE,
  jurisdiction TEXT NOT NULL,
  topic TEXT NOT NULL,
  title TEXT NOT NULL,
  claim TEXT NOT NULL,
  citation TEXT,
  primary_url TEXT,
  verification_query TEXT,
  prior_confidence TEXT NOT NULL DEFAULT 'lead',
  status TEXT NOT NULL DEFAULT 'unverified',
  status_note TEXT,
  best_excerpt TEXT,
  best_source TEXT,
  last_checked_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS policy_watch (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_id INTEGER NOT NULL REFERENCES research_entities(id) ON DELETE CASCADE,
  url TEXT NOT NULL,
  baseline_text_sha256 TEXT,
  last_text_sha256 TEXT,
  last_checked_at TEXT,
  last_status TEXT,
  changes INTEGER NOT NULL DEFAULT 0,
  UNIQUE(entity_id, url)
);

CREATE TABLE IF NOT EXISTS policy_changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  watch_id INTEGER NOT NULL REFERENCES policy_watch(id) ON DELETE CASCADE,
  detected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  old_text_sha256 TEXT,
  new_text_sha256 TEXT,
  similarity REAL,
  added_relief_statements INTEGER NOT NULL DEFAULT 0,
  removed_relief_statements INTEGER NOT NULL DEFAULT 0,
  summary TEXT
);
"""


def migrate_v6(conn: sqlite3.Connection) -> None:
    """v0.6: run heartbeats (GUI/watch), outreach, legal context, policy re-checks; AI discovery picks."""
    run_script(conn, V6_TABLES)
    _add_column(conn, "publication_candidates", "ai_pick_json", "TEXT")
    _create_views(conn)


V7_TABLES = """
CREATE TABLE IF NOT EXISTS captures (
  id INTEGER PRIMARY KEY,
  entity_id INTEGER NOT NULL REFERENCES research_entities(id),
  run_id TEXT,
  page_id INTEGER,
  url TEXT NOT NULL,
  title TEXT,
  text TEXT NOT NULL,
  text_sha256 TEXT NOT NULL,
  captured_by TEXT NOT NULL,
  note TEXT,
  first_party INTEGER NOT NULL DEFAULT 0,
  evidence_count INTEGER NOT NULL DEFAULT 0,
  captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(entity_id, text_sha256)
);
CREATE TABLE IF NOT EXISTS ai_leads (
  id INTEGER PRIMARY KEY,
  run_id TEXT,
  entity_id INTEGER NOT NULL REFERENCES research_entities(id),
  model TEXT,
  quote TEXT,
  claimed_url TEXT,
  summary TEXT,
  status TEXT NOT NULL DEFAULT 'unconfirmed',   -- unconfirmed | confirmed | contradicted
  confirmed_via TEXT,                            -- live | archive | snippet | capture
  confirmed_url TEXT,
  evidence_id INTEGER,
  grounding_json TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ai_leads_entity ON ai_leads(entity_id, status);
CREATE INDEX IF NOT EXISTS idx_captures_entity ON captures(entity_id);
"""


def migrate_v7(conn: sqlite3.Connection) -> None:
    """v0.7: how each page/excerpt was obtained (live, archive, snippet, capture), researcher captures, AI-search leads."""
    run_script(conn, V7_TABLES)
    for table in ("research_pages", "evidence_items"):
        _add_column(conn, table, "acquisition", "TEXT NOT NULL DEFAULT 'live'")
        _add_column(conn, table, "archive_ts", "TEXT")
        _add_column(conn, table, "archive_url", "TEXT")
    _create_views(conn)


def migrate_v8(conn: sqlite3.Connection) -> None:
    """v0.7.1: researcher evidence indexes imported as precedent seeds (tier, kind, source type, shared group)."""
    for col in ("index_id", "tier", "kind", "source_kind", "group_name", "country", "source_list"):
        _add_column(conn, "precedent_seeds", col, "TEXT")
    run_script(conn, "CREATE INDEX IF NOT EXISTS idx_precedent_index ON precedent_seeds(source_list, index_id);")


MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], None]]] = [
    (2, "baseline v0.1/v0.2 schema", migrate_v2),
    (3, "v0.3 checkpoints, unique evidence, review queue, provenance, views", migrate_v3),
    (4, "v0.4 action positions, case match, expert voices, precedent seeds", migrate_v4),
    (5, "v0.5 AI review, Wayback comparisons, accuracy audits", migrate_v5),
    (6, "v0.6 heartbeats, outreach, legal context, policy re-checks", migrate_v6),
    (7, "v0.7 acquisition provenance, researcher captures, AI-search leads", migrate_v7),
    (8, "v0.7.1 evidence-index precedents", migrate_v8),
]

LATEST_VERSION = MIGRATIONS[-1][0]

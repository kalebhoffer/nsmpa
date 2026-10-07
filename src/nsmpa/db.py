from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 2

SCHEMA = r"""
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


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @contextlib.contextmanager
    def transaction(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self.transaction():
            self.conn.executemany(sql, rows)

    def json(self, obj: Any) -> str:
        return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)

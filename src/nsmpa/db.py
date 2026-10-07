from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .migrations import LATEST_VERSION, MIGRATIONS

SCHEMA_VERSION = LATEST_VERSION


class Database:
    """SQLite connection with WAL, foreign keys and versioned additive migrations."""

    def __init__(self, path: str | Path, *, migrate: bool = True, backup_before_migrate: bool = True):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.last_backup: Path | None = None
        if migrate:
            self.migrate(backup=backup_before_migrate)

    # ------------------------------------------------------------------ migrations
    def _ensure_migration_table(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
              version INTEGER PRIMARY KEY,
              description TEXT NOT NULL,
              applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        # Databases created by v0.1/v0.2 recorded schema_version=2 in meta but had no migration log.
        row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row and str(row[0]).isdigit():
            legacy = int(row[0])
            for version, desc, _ in MIGRATIONS:
                if version <= legacy:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO schema_migrations(version,description) VALUES(?,?)",
                        (version, desc + " (pre-existing)"),
                    )
        self.conn.commit()

    def applied_versions(self) -> set[int]:
        self._ensure_migration_table()
        return {int(r[0]) for r in self.conn.execute("SELECT version FROM schema_migrations")}

    def pending_migrations(self) -> list[int]:
        applied = self.applied_versions()
        return [v for v, _, _ in MIGRATIONS if v not in applied]

    def _has_data(self) -> bool:
        try:
            row = self.conn.execute("SELECT COUNT(*) FROM institutions").fetchone()
            return bool(row and row[0])
        except sqlite3.OperationalError:
            return False

    def backup(self, dest_dir: Path | None = None, label: str = "backup") -> Path:
        if self.path == ":memory:":
            raise RuntimeError("Cannot back up an in-memory database")
        dest_dir = dest_dir or Path(self.path).parent / "backups"
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = dest_dir / f"{Path(self.path).stem}-{label}-{stamp}.sqlite3"
        target = sqlite3.connect(str(dest))
        try:
            self.conn.backup(target)
        finally:
            target.close()
        return dest

    def migrate(self, backup: bool = True) -> list[int]:
        pending = self.pending_migrations()
        if pending and backup and self.path != ":memory:" and self._has_data():
            self.last_backup = self.backup(label=f"pre-migrate-v{pending[0]}")
        applied: list[int] = []
        for version, desc, fn in MIGRATIONS:
            if version not in pending:
                continue
            try:
                self.conn.execute("BEGIN")
                fn(self.conn)
                self.conn.execute(
                    "INSERT OR REPLACE INTO schema_migrations(version,description) VALUES(?,?)", (version, desc)
                )
                self.conn.execute(
                    "INSERT INTO meta(key,value) VALUES('schema_version',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(version),),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            applied.append(version)
        # Views are cheap and may reference newly added columns; always refresh them.
        if not pending:
            from .migrations import _create_views
            _create_views(self.conn)
            self.conn.commit()
        return applied

    def schema_version(self) -> int:
        versions = self.applied_versions()
        return max(versions) if versions else 0

    # Backwards-compatible alias used by v0.2 callers/tests.
    def init_schema(self) -> None:
        self.migrate()

    # ------------------------------------------------------------------ helpers
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
        except BaseException:
            self.conn.rollback()
            raise

    def execute(self, sql: str, params: Iterable[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params if isinstance(params, dict) else tuple(params))

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self.transaction():
            self.conn.executemany(sql, rows)

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = 0) -> Any:
        row = self.conn.execute(sql, tuple(params)).fetchone()
        if row is None or row[0] is None:
            return default
        return row[0]

    def json(self, obj: Any) -> str:
        return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)

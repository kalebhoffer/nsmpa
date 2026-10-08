"""Run lifecycle: creation/resume, per-item checkpoints and graceful interruption.

A run is a row in ``research_runs``; each unit of work (an institution for discovery, an
entity for research) is a row in ``run_items``. Re-invoking a command with the same run
id skips items already ``done``. Items left ``running`` by a crash/interrupt are retried.
"""
from __future__ import annotations

import asyncio
import json
import signal
import subprocess
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from . import __version__
from .config import Settings
from .db import Database

ENGINE_VERSION = __version__

RESUMABLE_STATUSES = ("running", "interrupted", "budget_exhausted", "failed")


def _git_sha() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=3, check=False)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):  # no git, or not a checkout: provenance just omits the SHA
        return None


def create_or_resume_run(db: Database, settings: Settings, mode: str, run_id: str | None = None, *,
                         params: dict[str, Any] | None = None, command: str | None = None,
                         max_searches: int | None = None) -> tuple[str, bool]:
    """Return (run_id, resumed). An existing run id is resumed rather than duplicated."""
    rid = run_id or f"{mode.split('_')[0]}-{uuid.uuid4().hex[:12]}"
    existing = db.execute("SELECT id, mode FROM research_runs WHERE id=?", (rid,)).fetchone()
    if existing:
        db.execute(
            "UPDATE research_runs SET status='running', status_reason=NULL, completed_at=NULL, "
            "max_searches=?, last_checkpoint_at=CURRENT_TIMESTAMP WHERE id=?",
            (max_searches, rid),
        )
        # Items interrupted mid-flight are retried; completed items are kept.
        db.execute("UPDATE run_items SET status='pending' WHERE run_id=? AND status='running'", (rid,))
        db.conn.commit()
        return rid, True
    p = dict(params or {})
    p["git_sha"] = _git_sha()
    db.execute(
        "INSERT INTO research_runs(id,mode,config_json,status,engine_version,command,params_json,max_searches,last_checkpoint_at) "
        "VALUES(?,?,?,'running',?,?,?,?,CURRENT_TIMESTAMP)",
        (rid, mode, settings.model_dump_json(), ENGINE_VERSION, command, json.dumps(p, sort_keys=True, default=str), max_searches),
    )
    db.conn.commit()
    return rid, False


def finish_run(db: Database, run_id: str, status: str, reason: str | None = None) -> None:
    db.execute(
        "UPDATE research_runs SET status=?, status_reason=?, completed_at=CURRENT_TIMESTAMP, "
        "last_checkpoint_at=CURRENT_TIMESTAMP, entities_completed=(SELECT COUNT(*) FROM run_items WHERE run_id=? AND status='done') "
        "WHERE id=?",
        (status, reason, run_id, run_id),
    )
    db.conn.commit()


def register_items(db: Database, run_id: str, item_type: str, keys: Iterable[str]) -> None:
    db.conn.executemany(
        "INSERT OR IGNORE INTO run_items(run_id,item_type,item_key) VALUES(?,?,?)",
        ((run_id, item_type, k) for k in keys),
    )
    db.conn.commit()


def done_keys(db: Database, run_id: str, item_type: str) -> set[str]:
    return {r[0] for r in db.execute(
        "SELECT item_key FROM run_items WHERE run_id=? AND item_type=? AND status IN ('done','skipped')", (run_id, item_type))}


def mark_item(db: Database, run_id: str, item_type: str, key: str, status: str, *,
              error: str | None = None, result: dict | None = None) -> None:
    if status == "running":
        db.execute(
            "UPDATE run_items SET status='running', attempts=attempts+1, started_at=CURRENT_TIMESTAMP, error=NULL "
            "WHERE run_id=? AND item_type=? AND item_key=?", (run_id, item_type, key))
    else:
        db.execute(
            "UPDATE run_items SET status=?, completed_at=CURRENT_TIMESTAMP, error=?, result_json=? "
            "WHERE run_id=? AND item_type=? AND item_key=?",
            (status, error, json.dumps(result or {}, default=str), run_id, item_type, key))
        db.execute("UPDATE research_runs SET last_checkpoint_at=CURRENT_TIMESTAMP WHERE id=?", (run_id,))
    db.conn.commit()


def item_counts(db: Database, run_id: str, item_type: str | None = None) -> dict[str, int]:
    sql = "SELECT status, COUNT(*) n FROM run_items WHERE run_id=?"
    params: list[Any] = [run_id]
    if item_type:
        sql += " AND item_type=?"
        params.append(item_type)
    sql += " GROUP BY status"
    return {r["status"]: r["n"] for r in db.execute(sql, params)}


def latest_resumable_run(db: Database, mode_prefix: str | None = None):
    sql = f"SELECT * FROM research_runs WHERE status IN ({','.join('?' * len(RESUMABLE_STATUSES))}) AND engine_version!='0.2'"
    params: list[Any] = list(RESUMABLE_STATUSES)
    if mode_prefix:
        sql += " AND mode LIKE ?"
        params.append(mode_prefix + "%")
    sql += " ORDER BY COALESCE(last_checkpoint_at, started_at) DESC LIMIT 1"
    return db.execute(sql, params).fetchone()


@dataclass
class StopController:
    """First Ctrl+C: finish in-flight items, checkpoint, exit cleanly. Second: cancel now."""

    stop_requested: bool = False
    force: bool = False
    reason: str = ""
    _callbacks: list[Callable[[str], None]] = field(default_factory=list)
    _tasks: list[asyncio.Task] = field(default_factory=list)

    def on_stop(self, cb: Callable[[str], None]) -> None:
        self._callbacks.append(cb)

    def track(self, task: asyncio.Task) -> None:
        self._tasks.append(task)

    def request_stop(self, reason: str = "interrupted by user (Ctrl+C)") -> None:
        if self.stop_requested:
            self.force = True
            for t in self._tasks:
                t.cancel()
            for cb in self._callbacks:
                cb("Second Ctrl+C: cancelling in-flight work now (completed items are already saved)")
            return
        self.stop_requested = True
        self.reason = reason
        for cb in self._callbacks:
            cb("Stopping: finishing in-flight items and checkpointing… (Ctrl+C again to force)")

    def install(self) -> Callable[[], None]:
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGINT, self.request_stop)
            return lambda: loop.remove_signal_handler(signal.SIGINT)
        except (NotImplementedError, RuntimeError):  # non-main thread or unsupported platform
            prev = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(self.request_stop))
            return lambda: signal.signal(signal.SIGINT, prev)

"""Live terminal feedback for long-running jobs.

Modes:
- default: Rich live dashboard (TTY) or a periodic one-line heartbeat (non-TTY/log files).
- ``--verbose``: dashboard plus scrolling detail lines (queries, URLs, scoring, retries, errors).
- ``--quiet``: no live output; only the final summary printed by the CLI.
"""
from __future__ import annotations

import json
import os
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from time import monotonic

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


def _hms(seconds: float) -> str:
    seconds = int(max(0, seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


@dataclass
class DashboardState:
    title: str
    universe: str = ""
    total: int = 0
    completed: int = 0
    skipped_done: int = 0
    current: str = ""
    publication: str = ""
    phase: str = "starting"
    active: int = 0
    # search
    searches_live: int = 0
    searches_cached: int = 0
    searches_failed: int = 0
    credits_estimated: int = 0
    budget_limit: int | None = None
    # results
    candidates: int = 0
    high_confidence: int = 0
    publications: int = 0
    policies_found: int = 0
    stances: Counter = field(default_factory=Counter)
    evidence_unique: int = 0
    high_similarity: int = 0
    # fetching
    pages_fetched: int = 0
    skipped: int = 0
    robots_blocked: int = 0
    access_blocked: int = 0
    malformed_skipped: int = 0
    retries: int = 0
    errors: int = 0
    # per-entity step progress
    step_done: int = 0
    step_total: int = 0
    # lifecycle
    checkpoint: str = "-"
    stop_message: str = ""
    recent: deque = field(default_factory=lambda: deque(maxlen=8))


STANCE_ROWS = [
    ("Supportive", ("SUPPORTS_RELIEF", "SUPPORTS_CHANGED_CIRCUMSTANCES")),
    ("Case-by-case", ("CASE_BY_CASE",)),
    ("Update-only", ("UPDATE_ONLY",)),
    ("Restrictive", ("STRICT_ARCHIVE",)),
    ("Mixed", ("MIXED",)),
    ("No guidance", ("NO_RELEVANT_GUIDANCE",)),
    ("Unresolved", ("UNDETERMINED",)),
]


class RunDashboard:
    def __init__(self, title: str, total: int = 0, *, quiet: bool = False, verbose: bool = False,
                 universe: str = "", console: Console | None = None, heartbeat_seconds: float = 30.0,
                 force_terminal: bool | None = None, db=None, run_id: str | None = None, persist_seconds: float = 2.0):
        self.console = console or Console(stderr=False, force_terminal=force_terminal)
        self.state = DashboardState(title=title, total=total, universe=universe)
        self.quiet = quiet
        self.verbose = verbose
        self._started = monotonic()
        self._live: Live | None = None
        self._heartbeat = heartbeat_seconds
        self._last_beat = 0.0
        # Persisted heartbeat so `nsmpa watch` and the GUI can follow this run from elsewhere.
        self._db = db
        self._run_id = run_id
        self._persist_every = persist_seconds
        self._last_persist = 0.0

    # ------------------------------------------------------------------ lifecycle
    def __enter__(self) -> "RunDashboard":
        if not self.quiet and self.console.is_terminal:
            self._live = Live(self.render(), console=self.console, refresh_per_second=4, transient=False,
                              redirect_stdout=True, redirect_stderr=True)
            self._live.__enter__()
        elif not self.quiet:
            self.console.print(f"[bold]{self.state.title}[/bold] | {self.state.universe} | total={self.state.total}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.persist(force=True, finished=True)
        if self._live:
            self._live.update(self.render(), refresh=True)
            self._live.__exit__(exc_type, exc, tb)
        elif not self.quiet:
            self.console.print(self.summary_line())

    def snapshot(self) -> dict:
        s = self.state
        d = asdict(s)
        d["stances"] = dict(s.stances)
        d["recent"] = list(s.recent)
        d["elapsed_seconds"] = round(self._elapsed(), 1)
        d["rate_per_min"] = round(self.rate_per_min(), 2)
        d["eta_seconds"] = self.eta_seconds()
        return d

    def persist(self, force: bool = False, finished: bool = False) -> None:
        if self._db is None or not self._run_id:
            return
        now = monotonic()
        if not force and now - self._last_persist < self._persist_every:
            return
        self._last_persist = now
        try:
            self._db.execute(
                """INSERT INTO run_heartbeats(run_id,title,state_json,pid,finished,updated_at) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(run_id) DO UPDATE SET title=excluded.title,state_json=excluded.state_json,pid=excluded.pid,
                     finished=excluded.finished,updated_at=CURRENT_TIMESTAMP""",
                (self._run_id, self.state.title, json.dumps(self.snapshot(), default=str), os.getpid(), int(finished)))
            self._db.conn.commit()
        except Exception as exc:  # a heartbeat must never break a run
            self.log(f"heartbeat write failed: {exc}")

    def _refresh(self) -> None:
        self.persist()
        if self._live:
            self._live.update(self.render())
        elif not self.quiet:
            now = monotonic()
            if now - self._last_beat >= self._heartbeat:
                self._last_beat = now
                self.console.print(self.summary_line())

    # ------------------------------------------------------------------ updates
    def update(self, **kwargs) -> None:
        for key, value in kwargs.items():
            if hasattr(self.state, key):
                setattr(self.state, key, value)
        self._refresh()

    def increment(self, **kwargs) -> None:
        for key, value in kwargs.items():
            if hasattr(self.state, key):
                setattr(self.state, key, getattr(self.state, key) + value)
        self._refresh()

    def stance(self, stance: str) -> None:
        self.state.stances[stance] += 1
        self._refresh()

    def checkpoint(self) -> None:
        self.state.checkpoint = datetime.now().strftime("%H:%M:%S")
        self._refresh()

    def add_recent(self, message: str) -> None:
        self.state.recent.appendleft(message)
        if self._live:
            self._live.update(self.render())
        elif self.verbose and not self.quiet:
            self.console.print(message)

    def log(self, message: str) -> None:
        """Verbose-only detail line (queries, URLs, scoring decisions, retries, errors)."""
        if self.verbose and not self.quiet:
            if self._live:
                self._live.console.log(message, markup=False, highlight=False)
            else:
                self.console.print(message, markup=False, highlight=False)

    def notice(self, message: str) -> None:
        """Important lifecycle message shown in every non-quiet mode."""
        self.state.stop_message = message
        if self._live:
            self._live.update(self.render())
        elif not self.quiet:
            self.console.print(message)

    # ------------------------------------------------------------------ rendering
    def _elapsed(self) -> float:
        return max(0.001, monotonic() - self._started)

    def rate_per_min(self) -> float:
        return (self.state.completed - self.state.skipped_done) / self._elapsed() * 60.0

    def eta_seconds(self) -> float | None:
        rate = self.rate_per_min()
        remaining = self.state.total - self.state.completed
        if rate <= 0 or remaining <= 0 or self.state.completed - self.state.skipped_done < 2:
            return None
        return remaining / rate * 60.0

    def summary_line(self) -> str:
        s = self.state
        pct = (100.0 * s.completed / s.total) if s.total else 0.0
        return (
            f"[{_hms(self._elapsed())}] {s.completed}/{s.total} ({pct:.1f}%) | {s.phase} | {s.current[:40]} | "
            f"searches live {s.searches_live} cached {s.searches_cached} credits {s.credits_estimated} | "
            f"pages {s.pages_fetched} | evidence {s.evidence_unique} | errors {s.errors} | "
            f"rate {self.rate_per_min():.1f}/min"
        )

    def render(self):
        s = self.state
        elapsed = self._elapsed()
        pct = (100.0 * s.completed / s.total) if s.total else 0.0

        def grid() -> Table:
            t = Table.grid(padding=(0, 2))
            t.add_column(style="dim", min_width=16)
            t.add_column()
            return t

        top = grid()
        if s.universe:
            top.add_row("Universe", s.universe)
        bar_w = 24
        filled = int(bar_w * pct / 100)
        top.add_row("Progress", f"{s.completed:,} / {s.total:,}   {pct:5.1f}%   [cyan]{'█' * filled}{'░' * (bar_w - filled)}[/cyan]")
        active = f"  (+{s.active - 1} more in flight)" if s.active > 1 else ""
        top.add_row("Current", (s.current or "-") + active)
        if s.publication:
            top.add_row("Publication", s.publication)
        top.add_row("Phase", s.phase)
        if s.step_total:
            w = 16
            f = int(w * min(s.step_done, s.step_total) / s.step_total)
            top.add_row("Entity steps", f"{s.step_done}/{s.step_total}   [green]{'█' * f}{'░' * (w - f)}[/green]")

        search = grid()
        budget = f"{s.credits_estimated:,} / {s.budget_limit:,}" if s.budget_limit else f"{s.credits_estimated:,} (no --max-searches)"
        search.add_row("Queries used", f"{s.searches_live:,}")
        search.add_row("Cache hits", f"{s.searches_cached:,}")
        search.add_row("Credits / budget", budget)
        if s.searches_failed:
            search.add_row("Failed queries", f"[yellow]{s.searches_failed:,}[/yellow]")

        results = grid()
        if s.candidates or s.publications or s.high_confidence:
            results.add_row("Candidates", f"{s.candidates:,}  (high confidence {s.high_confidence:,})")
        if s.publications:
            results.add_row("Publications", f"{s.publications:,}")
        if s.policies_found or s.stances:
            results.add_row("Policies found", f"{s.policies_found:,}")
        for label, keys in STANCE_ROWS:
            n = sum(s.stances.get(k, 0) for k in keys)
            if n or s.stances:
                results.add_row(label, f"{n:,}")
        results.add_row("Evidence", f"{s.evidence_unique:,} unique excerpts")
        results.add_row("High similarity", f"{s.high_similarity:,}")

        fetch = grid()
        fetch.add_row("Pages fetched", f"{s.pages_fetched:,}")
        fetch.add_row("Robots blocks", f"{s.robots_blocked:,}")
        fetch.add_row("Access blocked", f"{s.access_blocked:,}")
        fetch.add_row("Malformed skipped", f"{s.malformed_skipped:,}")
        fetch.add_row("Retries", f"{s.retries:,}")
        fetch.add_row("Errors", f"[red]{s.errors:,}[/red]" if s.errors else "0")

        timing = grid()
        timing.add_row("Rate", f"{self.rate_per_min():.1f} entities/min")
        timing.add_row("Elapsed", _hms(elapsed))
        eta = self.eta_seconds()
        timing.add_row("Remaining (est.)", _hms(eta) if eta else "calculating…")
        timing.add_row("Last checkpoint", s.checkpoint)
        if s.skipped_done:
            timing.add_row("Resumed (skipped)", f"{s.skipped_done:,} already complete")

        cols = Table.grid(expand=True, padding=(0, 1))
        cols.add_column(ratio=1)
        cols.add_column(ratio=1)
        cols.add_row(Panel(search, title="Serper", border_style="blue"), Panel(results, title="Results", border_style="green"))
        cols.add_row(Panel(fetch, title="Fetching", border_style="magenta"), Panel(timing, title="Timing", border_style="cyan"))

        recent = Text("\n".join(s.recent) if s.recent else "No discoveries yet")
        parts = [Panel(top, title=s.title, border_style="bold cyan"), cols, Panel(recent, title="Recent", border_style="white")]
        if s.stop_message:
            parts.append(Text(s.stop_message, style="bold yellow"))
        return Group(*parts)


class HeartbeatView(RunDashboard):
    """Renders a persisted heartbeat (for `nsmpa watch`) using the same layout as the live dashboard."""

    def __init__(self, state: dict, title: str):
        super().__init__(title, quiet=True)
        st = self.state
        for k, v in state.items():
            if k == "stances":
                st.stances = Counter(v or {})
            elif k == "recent":
                st.recent = deque(v or [], maxlen=8)
            elif hasattr(st, k):
                setattr(st, k, v)
        self._elapsed_override = float(state.get("elapsed_seconds") or 0.001)
        self._rate_override = float(state.get("rate_per_min") or 0.0)
        self._eta_override = state.get("eta_seconds")

    def _elapsed(self) -> float:
        return self._elapsed_override

    def rate_per_min(self) -> float:
        return self._rate_override

    def eta_seconds(self):
        return self._eta_override

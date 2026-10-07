from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from time import monotonic

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


@dataclass
class DashboardState:
    title: str
    total: int = 0
    completed: int = 0
    current: str = ""
    phase: str = "starting"
    candidates: int = 0
    high_confidence: int = 0
    pages_fetched: int = 0
    skipped: int = 0
    errors: int = 0
    robots_blocked: int = 0
    searches_live: int = 0
    searches_cached: int = 0
    credits_estimated: int = 0
    recent: deque[str] = field(default_factory=lambda: deque(maxlen=6))


class RunDashboard:
    """Small Rich live dashboard for long-running discovery/research jobs."""

    def __init__(self, title: str, total: int = 0, *, quiet: bool = False, verbose: bool = False):
        self.console = Console()
        self.state = DashboardState(title=title, total=total)
        self.quiet = quiet
        self.verbose = verbose
        self._started = monotonic()
        self._live: Live | None = None

    def __enter__(self) -> "RunDashboard":
        if not self.quiet and self.console.is_terminal:
            self._live = Live(self.render(), console=self.console, refresh_per_second=5, transient=False)
            self._live.__enter__()
        elif not self.quiet:
            self.console.print(f"[bold]{self.state.title}[/bold] | total={self.state.total}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._live:
            self._live.update(self.render(), refresh=True)
            self._live.__exit__(exc_type, exc, tb)
        elif not self.quiet:
            self.console.print(self.summary_line())

    def update(self, **kwargs) -> None:
        for key, value in kwargs.items():
            if hasattr(self.state, key):
                setattr(self.state, key, value)
        if self._live:
            self._live.update(self.render())

    def increment(self, **kwargs) -> None:
        for key, value in kwargs.items():
            if hasattr(self.state, key):
                setattr(self.state, key, getattr(self.state, key) + value)
        if self._live:
            self._live.update(self.render())

    def add_recent(self, message: str) -> None:
        self.state.recent.appendleft(message)
        if self._live:
            self._live.update(self.render())
        elif self.verbose and not self.quiet:
            self.console.print(message)

    def log(self, message: str) -> None:
        if self.verbose and not self.quiet:
            if self._live:
                self.console.log(message)
            else:
                self.console.print(message)

    def _elapsed(self) -> float:
        return max(0.001, monotonic() - self._started)

    def summary_line(self) -> str:
        s = self.state
        rate = (s.completed / self._elapsed()) * 60.0
        return (
            f"{s.completed}/{s.total} complete | candidates={s.candidates} | "
            f"searches live/cached={s.searches_live}/{s.searches_cached} | "
            f"errors={s.errors} | rate={rate:.1f}/min"
        )

    def render(self):
        s = self.state
        elapsed = self._elapsed()
        rate = (s.completed / elapsed) * 60.0
        pct = (100.0 * s.completed / s.total) if s.total else 0.0

        table = Table.grid(expand=True)
        table.add_column(ratio=1)
        table.add_column(ratio=1)
        table.add_row("Progress", f"{s.completed:,} / {s.total:,}  ({pct:.1f}%)")
        table.add_row("Current", s.current or "-")
        table.add_row("Phase", s.phase)
        table.add_row("Candidates", f"{s.candidates:,}  | high confidence {s.high_confidence:,}")
        table.add_row("Searches", f"live {s.searches_live:,} | cached {s.searches_cached:,} | est. credits {s.credits_estimated:,}")
        table.add_row("Fetches", f"pages {s.pages_fetched:,} | skipped {s.skipped:,} | robots {s.robots_blocked:,}")
        table.add_row("Errors", f"{s.errors:,}")
        table.add_row("Rate", f"{rate:.1f} entities/min | elapsed {elapsed/60:.1f} min")

        recent = Text("\n".join(s.recent) if s.recent else "No discoveries yet")
        outer = Table.grid(expand=True)
        outer.add_row(Panel(table, title=s.title, border_style="cyan"))
        outer.add_row(Panel(recent, title="Recent findings", border_style="green"))
        return outer

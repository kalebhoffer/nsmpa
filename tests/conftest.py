from __future__ import annotations

import os
from pathlib import Path

import pytest

from nsmpa.config import Settings
from nsmpa.db import Database

# Automated tests must never spend live search credits.
os.environ.pop("SERPER_API_KEY", None)
os.environ.pop("BRAVE_SEARCH_API_KEY", None)
for _var in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ.pop(_var, None)


def public_resolver(host: str) -> list[str]:
    """Deterministic resolver for tests: every non-obviously-private name is public."""
    from nsmpa.utils import host_is_public
    if host.endswith(".internal-test"):
        raise ValueError(f"{host} resolves to non-public address 10.0.0.5")
    if not host_is_public(host):
        raise ValueError(f"host is not public: {host!r}")
    return ["93.184.216.34"]


def make_settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        database_path=tmp_path / "test.sqlite3",
        output_dir=tmp_path / "out",
        snapshot_dir=tmp_path / "snap",
        research_snapshot_dir=tmp_path / "rsnap",
        per_host_delay_seconds=0.0,
        archive_delay_seconds=0.0,
        ai_gate_enabled=False,          # tests that exercise the AI veto opt in
        retry_backoff_seconds=0.0,
        max_retries=2,
    )
    base.update(overrides)
    s = Settings(**base)
    s.ensure_dirs()
    return s


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def db(tmp_path: Path):
    d = Database(tmp_path / "test.sqlite3")
    yield d
    d.close()

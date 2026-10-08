"""CLI smoke tests: every command parses and runs its no-network path (catches wiring errors unit tests miss)."""
from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from nsmpa.cli import app

runner = CliRunner()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "config.yml"
    p.write_text(f"database_path: {tmp_path}/t.sqlite3\noutput_dir: {tmp_path}/out\nresearch_snapshot_dir: {tmp_path}/rs\n"
                 f"snapshot_dir: {tmp_path}/s\nsearch_provider: none\nnotify_on_finish: false\n", encoding="utf-8")
    return str(p)


def run(*args):
    r = runner.invoke(app, list(args))
    assert r.exit_code == 0, (args, r.output, r.exception)
    return r.output


def test_core_commands(cfg):
    run("init", "--config", cfg)
    run("research-setup", "--config", cfg)
    for cmd in (["status"], ["runs"], ["query-stats"], ["errors"], ["validate"], ["review"], ["precedent"], ["legal"],
                ["voices"], ["my-case"], ["outreach", "status"], ["audit", "--help"], ["version"]):
        run(*cmd, "--config", cfg) if cmd[-1] not in {"--help", "version"} else run(*cmd)


def test_schedule_prints_plist(cfg):
    out = run("schedule", "--config", cfg)
    assert "com.nsmpa.recheck" in out and "recheck" in out


def test_reports_and_exports(cfg, tmp_path):
    run("init", "--config", cfg)
    run("research-setup", "--config", cfg)
    run("report", "--config", cfg)
    run("packet", "--config", cfg)
    out = run("dashboard", "--config", cfg, "--out", str(tmp_path / "d.html"))
    assert os.path.exists(tmp_path / "d.html") and "Dashboard written" in out
    run("recheck", "--config", cfg, "--quiet")


def test_offline_research_command(cfg, monkeypatch):
    import httpx
    import nsmpa.research as research_mod
    from conftest import public_resolver
    real = research_mod.HardenedFetcher
    calls = []

    def offline_fetcher(settings, **kw):
        def handler(req):
            calls.append(str(req.url))
            return httpx.Response(404)
        return real(settings, transport=httpx.MockTransport(handler), resolver=public_resolver, on_event=kw.get("on_event"))

    monkeypatch.setattr(research_mod, "HardenedFetcher", offline_fetcher)
    run("init", "--config", cfg)
    run("research-setup", "--config", cfg)
    # search_provider none + an entity with no reachable site: must complete without network or credits
    out = run("research", "--config", cfg, "--cohort", "support_org", "--limit", "1", "--quiet", "--no-export",
              "--depth", "quick")
    assert '"status"' in out and calls  # every request went to the mock, none to the internet

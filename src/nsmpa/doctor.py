"""Environment and installation checks for ``nsmpa doctor``. Never prints secrets."""
from __future__ import annotations

import asyncio
import importlib
import os
import socket
import sqlite3
import sys
import tempfile
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import __version__

REQUIRED = ["httpx", "bs4", "pydantic", "yaml", "typer", "rich"]
OPTIONAL = {"lxml": "faster, more tolerant HTML parsing", "pypdf": "PDF policy documents", "h2": "HTTP/2 (unused by default)"}


def run_doctor(console: Console, config: Path | None, *, network: bool = True, check_serper: bool = False) -> bool:
    from .config import load_settings, resolve_config_path
    from .db import Database
    from .migrations import LATEST_VERSION

    t = Table(title=f"nsmpa doctor ({__version__})")
    t.add_column("Check")
    t.add_column("Result")
    t.add_column("Detail")
    ok = True

    def row(name: str, passed: bool | None, detail: str = "") -> None:
        nonlocal ok
        if passed is False:
            ok = False
        t.add_row(name, {True: "[green]ok[/green]", False: "[red]FAIL[/red]", None: "[yellow]warn[/yellow]"}[passed], detail)

    row("python", sys.version_info >= (3, 11), sys.version.split()[0])
    for mod in REQUIRED:
        try:
            m = importlib.import_module(mod)
            row(f"dependency {mod}", True, getattr(m, "__version__", ""))
        except ImportError as exc:
            row(f"dependency {mod}", False, str(exc))
    for mod, why in OPTIONAL.items():
        try:
            importlib.import_module(mod)
            row(f"optional {mod}", True, why)
        except ImportError:
            row(f"optional {mod}", None, f"not installed: {why}")

    cfg = resolve_config_path(config)
    try:
        settings = load_settings(config)
        row("config", True, str(cfg) if cfg else "defaults (no config.yml found)")
    except Exception as exc:
        row("config", False, f"{type(exc).__name__}: {exc}")
        console.print(t)
        return False
    if "example.org" in settings.user_agent or "contact=" not in settings.user_agent:
        row("user agent", None, "set an identifiable user_agent with contact info before national crawls")
    else:
        row("user agent", True, settings.user_agent[:60])

    try:
        exists = settings.database_path.exists()
        probe = sqlite3.connect(str(settings.database_path))
        applied = set()
        try:
            applied = {r[0] for r in probe.execute("SELECT version FROM schema_migrations")}
        except sqlite3.OperationalError:
            pass
        integrity = probe.execute("PRAGMA quick_check").fetchone()[0]
        probe.close()
        pending = [v for v in range(2, LATEST_VERSION + 1) if v not in applied]
        row("database", integrity == "ok", f"{settings.database_path} ({'exists' if exists else 'will be created'}; integrity {integrity})")
        row("migrations", None if pending else True,
            f"pending: {pending} (run `nsmpa init`; a backup is taken first)" if pending else f"schema v{max(applied)} current")
        if not pending:
            db = Database(settings.database_path)
            inst = db.scalar("SELECT COUNT(*) FROM institutions WHERE included=1")
            ent = db.scalar("SELECT COUNT(*) FROM research_entities WHERE active=1")
            fk = db.execute("PRAGMA foreign_key_check").fetchall()
            db.close()
            row("research data", True, f"{inst:,} included institutions, {ent:,} research entities")
            row("foreign keys", not fk, "consistent" if not fk else f"{len(fk)} violations")
    except Exception as exc:
        row("database", False, f"{type(exc).__name__}: {exc}")

    for label, path in (("output dir", settings.output_dir), ("snapshot dir", settings.research_snapshot_dir),
                        ("database dir", settings.database_path.parent)):
        try:
            path.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path, prefix=".nsmpa-doctor-"):
                pass
            row(f"{label} writable", True, str(path))
        except Exception as exc:
            row(f"{label} writable", False, f"{path}: {exc}")

    serper = bool(os.getenv("SERPER_API_KEY"))
    brave = bool(os.getenv("BRAVE_SEARCH_API_KEY"))
    provider = settings.search_provider
    if serper:
        row("SERPER_API_KEY", True, "set (value hidden)")
    else:
        row("SERPER_API_KEY", None if provider in {"auto", "none"} else False,
            "not set: export it or add it to ./.env (gitignored); discovery/research will run without search")
    if brave:
        row("BRAVE_SEARCH_API_KEY", True, "set (value hidden)")
    if os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"):
        row("Anthropic credentials (ai-review)", True, f"set (value hidden); model {settings.ai_model}")
    else:
        row("Anthropic credentials (ai-review)", None,
            "ANTHROPIC_API_KEY not set (optional; needed only for `nsmpa ai-review`; an `ant auth login` profile also works)")
    cached = 0
    try:
        conn = sqlite3.connect(str(settings.database_path))
        cached = conn.execute("SELECT COUNT(*) FROM search_cache").fetchone()[0]
        conn.close()
    except sqlite3.Error:
        pass
    row("search cache", True, f"{cached:,} cached queries reusable at zero credit cost")

    if network:
        try:
            socket.getaddrinfo("google.serper.dev", 443)
            row("DNS", True, "google.serper.dev resolves")
        except OSError as exc:
            row("DNS", False, str(exc))
        try:
            import httpx
            r = httpx.get("https://www.example.com/", timeout=10, headers={"User-Agent": settings.user_agent})
            row("HTTPS egress", r.status_code < 500, f"example.com HTTP {r.status_code}")
        except Exception as exc:
            row("HTTPS egress", False, f"{type(exc).__name__}: {exc}")
    if check_serper and serper:
        from .search import SearchError, SerperSearchProvider
        async def probe() -> str:
            prov = SerperSearchProvider(os.environ["SERPER_API_KEY"], settings.user_agent)
            try:
                res = await prov.search("Student Press Law Center", 1)
                return f"{len(res)} result(s); credits reported: {prov.last_credits}"
            finally:
                await prov.aclose()
        try:
            row("Serper live check (1 credit)", True, asyncio.run(probe()))
        except SearchError as exc:
            row("Serper live check (1 credit)", False, str(exc))
    console.print(t)
    console.print("[green]All required checks passed.[/green]" if ok else "[red]Some checks failed.[/red]")
    return ok

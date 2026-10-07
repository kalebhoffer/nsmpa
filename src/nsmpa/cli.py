from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .classify import classify_all
from .config import load_settings
from .crawl import complete_run, crawl_all, create_run
from .db import Database
from .discovery import discover_all, promote_candidates
from .export import export_run
from .ingest import import_ipeds
from .utils import normalize_url, registrableish_domain
from .research import (create_research_run, complete_research_run, export_research, import_entities_csv, research_all, sync_student_entities)
from .support_orgs import seed_support_orgs
from .benchmarks import seed_benchmark_newsrooms

app = typer.Typer(no_args_is_help=True, help="NSMPA: national journalism post-publication policy and precedent audit")
console = Console()


def _db(config: Path | None) -> tuple[Database, object]:
    settings = load_settings(config)
    return Database(settings.database_path), settings


@app.command("init")
def init(config: Path | None = typer.Option(None, "--config", exists=True)) -> None:
    db, settings = _db(config)
    db.close()
    console.print(f"Initialized database: {settings.database_path}")


@app.command("import-ipeds")
def import_ipeds_cmd(
    path: Path = typer.Argument(..., exists=True, readable=True, help="IPEDS HD directory CSV or ZIP"),
    source_year: int | None = typer.Option(None, "--source-year"),
    config: Path | None = typer.Option(None, "--config", exists=True),
) -> None:
    db, settings = _db(config)
    try:
        stats = import_ipeds(db, settings, path, source_year)
        console.print_json(json.dumps(stats))
    finally:
        db.close()


@app.command("discover")
def discover_cmd(
    limit: int | None = typer.Option(None, "--limit", min=1),
    run_id: str | None = typer.Option(None, "--run-id"),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress live progress output"),
    verbose: bool = typer.Option(False, "--verbose", help="Show detailed errors and decisions"),
    config: Path | None = typer.Option(None, "--config", exists=True),
) -> None:
    db, settings = _db(config)
    try:
        stats = asyncio.run(discover_all(db, settings, limit, run_id=run_id, quiet=quiet, verbose=verbose))
        console.print_json(json.dumps(stats))
    finally:
        db.close()


@app.command("promote")
def promote_cmd(
    threshold: float | None = typer.Option(None, "--threshold", min=0, max=1),
    config: Path | None = typer.Option(None, "--config", exists=True),
) -> None:
    db, settings = _db(config)
    try:
        stats = promote_candidates(db, threshold if threshold is not None else settings.publication_confidence_threshold)
        console.print_json(json.dumps(stats))
    finally:
        db.close()


@app.command("add-publication")
def add_publication(
    unitid: str = typer.Option(..., "--unitid"),
    url: str = typer.Option(..., "--url"),
    name: str | None = typer.Option(None, "--name"),
    primary: bool = typer.Option(True, "--primary/--not-primary"),
    config: Path | None = typer.Option(None, "--config", exists=True),
) -> None:
    db, _ = _db(config)
    try:
        clean = normalize_url(url)
        if not clean:
            raise typer.BadParameter("Invalid HTTP(S) URL")
        inst = db.execute("SELECT name FROM institutions WHERE unitid=?", (unitid,)).fetchone()
        if not inst:
            raise typer.BadParameter(f"Unknown UNITID {unitid}")
        db.execute(
            """
            INSERT INTO publications(unitid,name,homepage_url,domain,confidence,verification_status,is_primary)
            VALUES(?,?,?,?,1.0,'manual',?)
            ON CONFLICT(unitid,domain) DO UPDATE SET name=excluded.name,homepage_url=excluded.homepage_url,
              confidence=1.0,verification_status='manual',is_primary=excluded.is_primary,updated_at=CURRENT_TIMESTAMP
            """,
            (unitid, name or inst["name"], clean, registrableish_domain(clean), int(primary)),
        )
        db.conn.commit()
        console.print(f"Added publication for {unitid}: {clean}")
    finally:
        db.close()


@app.command("crawl")
def crawl_cmd(
    publication_limit: int | None = typer.Option(None, "--limit", min=1),
    run_id: str | None = typer.Option(None, "--run-id", help="Reuse this ID to resume an interrupted run"),
    config: Path | None = typer.Option(None, "--config", exists=True),
) -> None:
    db, settings = _db(config)
    rid = create_run(db, settings, "crawl", run_id)
    console.print(f"Run ID: {rid}")
    try:
        stats = asyncio.run(crawl_all(db, settings, rid, publication_limit))
        complete_run(db, rid, "crawled")
        console.print_json(json.dumps(stats))
    except Exception:
        complete_run(db, rid, "failed")
        raise
    finally:
        db.close()


@app.command("classify")
def classify_cmd(
    run_id: str = typer.Option(..., "--run-id"),
    config: Path | None = typer.Option(None, "--config", exists=True),
) -> None:
    db, _ = _db(config)
    try:
        stats = classify_all(db, run_id)
        console.print_json(json.dumps(stats))
    finally:
        db.close()


@app.command("export")
def export_cmd(
    run_id: str = typer.Option(..., "--run-id"),
    out_dir: Path | None = typer.Option(None, "--out-dir"),
    config: Path | None = typer.Option(None, "--config", exists=True),
) -> None:
    db, settings = _db(config)
    try:
        stats = export_run(db, run_id, out_dir or settings.output_dir)
        console.print_json(json.dumps(stats))
        console.print(f"Exported to {(out_dir or settings.output_dir) / run_id}")
    finally:
        db.close()


@app.command("pipeline")
def pipeline_cmd(
    publication_limit: int | None = typer.Option(None, "--limit", min=1),
    run_id: str | None = typer.Option(None, "--run-id"),
    config: Path | None = typer.Option(None, "--config", exists=True),
) -> None:
    """Promote candidates, crawl, classify, and export. Run import/discover first."""
    db, settings = _db(config)
    rid = create_run(db, settings, "pipeline", run_id)
    console.print(f"Run ID: {rid}")
    try:
        promoted = promote_candidates(db, settings.publication_confidence_threshold)
        crawled = asyncio.run(crawl_all(db, settings, rid, publication_limit))
        classes = classify_all(db, rid)
        exported = export_run(db, rid, settings.output_dir)
        complete_run(db, rid, "completed")
        console.print_json(json.dumps({"promoted": promoted, "crawled": crawled, "classes": classes, "exported": exported}))
    except Exception:
        complete_run(db, rid, "failed")
        raise
    finally:
        db.close()


@app.command("seed-support-orgs")
def seed_support_orgs_cmd(
    config: Path | None = typer.Option(None, "--config", exists=True),
) -> None:
    """Seed national journalism ethics, legal, training, research and membership organizations."""
    db, _ = _db(config)
    try:
        console.print_json(json.dumps(seed_support_orgs(db)))
    finally:
        db.close()


@app.command("seed-benchmark-newsrooms")
def seed_benchmark_newsrooms_cmd(
    config: Path | None = typer.Option(None, "--config", exists=True),
) -> None:
    """Seed a deliberately labeled professional-newsroom benchmark panel."""
    db, _ = _db(config)
    try:
        console.print_json(json.dumps(seed_benchmark_newsrooms(db)))
    finally:
        db.close()


@app.command("sync-student-entities")
def sync_student_entities_cmd(
    config: Path | None = typer.Option(None, "--config", exists=True),
) -> None:
    """Mirror promoted student publications into the cross-journalism research universe."""
    db, _ = _db(config)
    try:
        console.print_json(json.dumps(sync_student_entities(db)))
    finally:
        db.close()


@app.command("import-entities")
def import_entities_cmd(
    path: Path = typer.Argument(..., exists=True, readable=True),
    cohort: str = typer.Option(..., "--cohort", help="professional_newsroom, press_association, journalism_school, etc."),
    source: str = typer.Option("csv", "--source"),
    config: Path | None = typer.Option(None, "--config", exists=True),
) -> None:
    """Import a generic newsroom/support directory CSV. Required columns: name,url."""
    db, _ = _db(config)
    try:
        console.print_json(json.dumps(import_entities_csv(db, path, cohort, source)))
    finally:
        db.close()


@app.command("research")
def research_cmd(
    cohort: str | None = typer.Option(None, "--cohort"),
    limit: int | None = typer.Option(None, "--limit", min=1),
    run_id: str | None = typer.Option(None, "--run-id"),
    quiet: bool = typer.Option(False, "--quiet"),
    verbose: bool = typer.Option(False, "--verbose"),
    config: Path | None = typer.Option(None, "--config", exists=True),
) -> None:
    """Run Serper-assisted policy, precedent, adverse-evidence and guidance research."""
    db, settings = _db(config)
    rid = create_research_run(db, settings, "full_research", run_id)
    console.print(f"Research Run ID: {rid}")
    try:
        stats = asyncio.run(research_all(db, settings, rid, cohort=cohort, limit=limit, quiet=quiet, verbose=verbose))
        exported = export_research(db, rid, settings.output_dir)
        complete_research_run(db, rid, "completed")
        console.print_json(json.dumps({"research": stats, "exported": exported}))
        console.print(f"Exported to {settings.output_dir / ('research_' + rid)}")
    except Exception:
        complete_research_run(db, rid, "failed")
        raise
    finally:
        db.close()


@app.command("research-export")
def research_export_cmd(
    run_id: str = typer.Option(..., "--run-id"),
    out_dir: Path | None = typer.Option(None, "--out-dir"),
    config: Path | None = typer.Option(None, "--config", exists=True),
) -> None:
    db, settings = _db(config)
    try:
        stats = export_research(db, run_id, out_dir or settings.output_dir)
        console.print_json(json.dumps(stats))
    finally:
        db.close()


@app.command("research-setup")
def research_setup_cmd(
    config: Path | None = typer.Option(None, "--config", exists=True),
) -> None:
    """Seed support organizations and sync already-promoted student media into research_entities."""
    db, _ = _db(config)
    try:
        result = {"support_orgs": seed_support_orgs(db), "benchmark_newsrooms": seed_benchmark_newsrooms(db), "student_entities": sync_student_entities(db)}
        console.print_json(json.dumps(result))
    finally:
        db.close()


@app.command("status")
def status_cmd(config: Path | None = typer.Option(None, "--config", exists=True)) -> None:
    db, settings = _db(config)
    try:
        metrics = {
            "included institutions": db.execute("SELECT COUNT(*) n FROM institutions WHERE included=1").fetchone()["n"],
            "publication candidates": db.execute("SELECT COUNT(*) n FROM publication_candidates").fetchone()["n"],
            "promoted publications": db.execute("SELECT COUNT(*) n FROM publications").fetchone()["n"],
            "crawl runs": db.execute("SELECT COUNT(*) n FROM crawl_runs").fetchone()["n"],
            "pages": db.execute("SELECT COUNT(*) n FROM pages").fetchone()["n"],
            "evidence excerpts": db.execute("SELECT COUNT(*) n FROM evidence").fetchone()["n"],
            "classifications": db.execute("SELECT COUNT(*) n FROM classifications").fetchone()["n"],
            "research entities": db.execute("SELECT COUNT(*) n FROM research_entities WHERE active=1").fetchone()["n"],
            "research runs": db.execute("SELECT COUNT(*) n FROM research_runs").fetchone()["n"],
            "search queries": db.execute("SELECT COUNT(*) n FROM search_queries").fetchone()["n"],
            "research evidence": db.execute("SELECT COUNT(*) n FROM research_evidence").fetchone()["n"],
            "entity stances": db.execute("SELECT COUNT(*) n FROM entity_stances").fetchone()["n"],
        }
        table = Table(title=f"NSMPA status | {settings.database_path}")
        table.add_column("Metric")
        table.add_column("Count", justify="right")
        for k, v in metrics.items():
            table.add_row(k, str(v))
        console.print(table)
    finally:
        db.close()


if __name__ == "__main__":
    app()

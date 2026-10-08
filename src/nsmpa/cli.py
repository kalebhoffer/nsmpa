from __future__ import annotations

import asyncio
import csv
import json
import os
import shlex
import sys
from datetime import UTC
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .benchmarks import seed_benchmark_newsrooms
from .classify import classify_all
from .config import load_settings, resolve_config_path
from .crawl import complete_run, crawl_all, create_run
from .db import Database
from .discovery import discover_all, promote_candidates
from .export import export_research, export_run
from .ingest import import_ipeds
from .notify import run_finished
from .research import (
    COHORTS,
    import_entities_csv,
    merge_duplicate_entities,
    research_all,
    start_research,
    sync_student_entities,
)
from .runs import item_counts, latest_resumable_run
from .support_orgs import seed_support_orgs
from .utils import normalize_url, registrableish_domain

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help=f"NSMPA {__version__}: national journalism post-publication policy & precedent research")
console = Console()

ConfigOpt = typer.Option(None, "--config", help="Config file (default: ./config.yml if present)")
QuietOpt = typer.Option(False, "--quiet", "-q", help="Minimal output for unattended runs")
VerboseOpt = typer.Option(False, "--verbose", "-v", help="Show queries, URLs, scoring decisions, retries and errors")
MaxSearchesOpt = typer.Option(None, "--max-searches", min=0, help="Max live search credits this invocation may spend")
RefreshOpt = typer.Option(False, "--refresh-search", help="Bypass the search cache (spends credits again)")
FreshOpt = typer.Option(False, "--fresh", help="Re-process items already completed in this run id")


def _db(config: Path | None):
    settings = load_settings(config)
    db = Database(settings.database_path)
    if db.last_backup:
        console.print(f"[dim]Schema migrated; pre-migration backup written to {db.last_backup}[/dim]")
    return db, settings


def _cmdline() -> str:
    return "nsmpa " + " ".join(shlex.quote(a) for a in sys.argv[1:])


def _print_stop(result: dict, resume_cmd: str) -> None:
    status = result.get("status")
    if status in {"interrupted", "budget_exhausted", "failed"}:
        console.print(Panel(
            f"Run [bold]{result.get('run_id', '')}[/bold] stopped: [yellow]{status}[/yellow]"
            + (f" — {result.get('stop_reason')}" if result.get("stop_reason") else "")
            + f"\nCompleted work is saved. Remaining items: {result.get('remaining', '?')}"
            + f"\nResume with:  [bold]{resume_cmd}[/bold]   (or: nsmpa resume)",
            title="Checkpoint saved", border_style="yellow"))


# ============================================================================ setup

@app.command("init")
def init(config: Path | None = ConfigOpt) -> None:
    """Create or migrate the database (non-destructive; backs up before migrating)."""
    db, settings = _db(config)
    console.print(f"Database ready: {settings.database_path} (schema v{db.schema_version()})")
    db.close()


@app.command("import-ipeds")
def import_ipeds_cmd(
    path: Path = typer.Argument(..., exists=True, readable=True, help="IPEDS HD directory CSV or ZIP"),
    source_year: int | None = typer.Option(None, "--source-year"),
    config: Path | None = ConfigOpt,
) -> None:
    """Import/refresh the IPEDS institution universe (upsert; never deletes)."""
    db, settings = _db(config)
    try:
        console.print_json(json.dumps(import_ipeds(db, settings, path, source_year)))
    finally:
        db.close()


@app.command("import-peer-attributes")
def import_peer_attributes_cmd(
    path: Path = typer.Argument(..., exists=True, readable=True, help="CSV with unitid,key,value"),
    source: str = typer.Option(..., "--source", help="Provenance label, e.g. acejmc_2026 or cma_members_2026"),
    config: Path | None = ConfigOpt,
) -> None:
    """Import institution attributes for peer groups (journalism school, association membership...)."""
    db, _ = _db(config)
    n = skipped = 0
    try:
        with open(path, encoding="utf-8-sig", newline="") as f, db.transaction():
            for row in csv.DictReader(f):
                unitid = (row.get("unitid") or row.get("UNITID") or "").strip()
                key = (row.get("key") or "").strip()
                if not unitid or not key or not db.conn.execute("SELECT 1 FROM institutions WHERE unitid=?", (unitid,)).fetchone():
                    skipped += 1
                    continue
                db.conn.execute(
                    "INSERT INTO institution_attributes(unitid,key,value,source) VALUES(?,?,?,?) "
                    "ON CONFLICT(unitid,key,source) DO UPDATE SET value=excluded.value",
                    (unitid, key, (row.get("value") or "1").strip(), source))
                n += 1
        console.print_json(json.dumps({"imported": n, "skipped": skipped}))
    finally:
        db.close()


# ============================================================================ student discovery

@app.command("discover")
def discover_cmd(
    limit: int | None = typer.Option(None, "--limit", min=1),
    state: list[str] = typer.Option([], "--state", help="Restrict to state(s), e.g. --state WA"),
    unitid: list[str] = typer.Option([], "--unitid", help="Restrict to specific IPEDS UNITIDs"),
    run_id: str | None = typer.Option(None, "--run-id", help="Resume this run id"),
    max_searches: int | None = MaxSearchesOpt,
    refresh_search: bool = RefreshOpt,
    fresh: bool = FreshOpt,
    quiet: bool = QuietOpt,
    verbose: bool = VerboseOpt,
    config: Path | None = ConfigOpt,
) -> None:
    """Find each institution's primary student newspaper (adaptive search ladder + site inspection)."""
    db, settings = _db(config)
    try:
        stats = asyncio.run(discover_all(db, settings, limit, run_id=run_id, quiet=quiet, verbose=verbose,
                                         max_searches=max_searches, refresh_search=refresh_search, fresh=fresh,
                                         states=state or None, unitids=unitid or None, command=_cmdline()))
        console.print_json(json.dumps(stats))
        run_finished(settings, "discovery", stats)
        _print_stop(stats, f"nsmpa discover --run-id {stats['run_id']}" + (f" --max-searches {max_searches}" if max_searches is not None else ""))
    finally:
        db.close()


@app.command("promote")
def promote_cmd(
    threshold: float | None = typer.Option(None, "--threshold", min=0, max=1),
    use_ai: bool = typer.Option(False, "--use-ai", help="Let confident `ai-discovery` picks break ties (all queued for review)"),
    config: Path | None = ConfigOpt,
) -> None:
    """Promote best verified candidates to publications (keeps human/manual verifications)."""
    db, settings = _db(config)
    try:
        stats = promote_candidates(db, threshold if threshold is not None else settings.publication_confidence_threshold,
                                   settings.publication_ambiguity_margin, use_ai=use_ai)
        console.print_json(json.dumps(stats))
    finally:
        db.close()


@app.command("add-publication")
def add_publication(
    unitid: str = typer.Option(..., "--unitid"),
    url: str = typer.Option(..., "--url"),
    name: str | None = typer.Option(None, "--name"),
    primary: bool = typer.Option(True, "--primary/--not-primary"),
    config: Path | None = ConfigOpt,
) -> None:
    """Manually record a verified publication (overrides automatic promotion)."""
    db, _ = _db(config)
    try:
        clean = normalize_url(url)
        if not clean:
            raise typer.BadParameter("Invalid HTTP(S) URL")
        inst = db.execute("SELECT name, website FROM institutions WHERE unitid=?", (unitid,)).fetchone()
        if not inst:
            raise typer.BadParameter(f"Unknown UNITID {unitid}")
        if primary:
            db.execute("UPDATE publications SET is_primary=0 WHERE unitid=?", (unitid,))
        db.execute(
            """
            INSERT INTO publications(unitid,name,homepage_url,domain,confidence,verification_status,is_primary,discovery_method,verified_at)
            VALUES(?,?,?,?,1.0,'manual',?,'manual',CURRENT_TIMESTAMP)
            ON CONFLICT(unitid,domain) DO UPDATE SET name=excluded.name,homepage_url=excluded.homepage_url,
              confidence=1.0,verification_status='manual',is_primary=excluded.is_primary,discovery_method='manual',
              verified_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
            """,
            (unitid, name or inst["name"], clean, registrableish_domain(clean), int(primary)),
        )
        db.conn.commit()
        console.print(f"Added publication for {unitid}: {clean}")
    finally:
        db.close()


# ============================================================================ legacy deep crawl

@app.command("crawl")
def crawl_cmd(
    publication_limit: int | None = typer.Option(None, "--limit", min=1),
    run_id: str | None = typer.Option(None, "--run-id", help="Reuse this ID to resume an interrupted run"),
    config: Path | None = ConfigOpt,
) -> None:
    """Deep-crawl promoted student publication sites (v0.1 A-F/U pipeline)."""
    db, settings = _db(config)
    rid = create_run(db, settings, "crawl", run_id)
    console.print(f"Run ID: {rid}")
    try:
        stats = asyncio.run(crawl_all(db, settings, rid, publication_limit))
        complete_run(db, rid, "crawled")
        console.print_json(json.dumps(stats))
    except KeyboardInterrupt:
        complete_run(db, rid, "interrupted")
        console.print(f"[yellow]Interrupted. Resume with: nsmpa crawl --run-id {rid}[/yellow]")
    except Exception:
        complete_run(db, rid, "failed")
        raise
    finally:
        db.close()


@app.command("classify")
def classify_cmd(run_id: str = typer.Option(..., "--run-id"), config: Path | None = ConfigOpt) -> None:
    """Classify a deep-crawl run (v0.1 A-F/U classes)."""
    db, _ = _db(config)
    try:
        console.print_json(json.dumps(classify_all(db, run_id)))
    finally:
        db.close()


@app.command("export")
def export_cmd(run_id: str = typer.Option(..., "--run-id"), out_dir: Path | None = typer.Option(None, "--out-dir"),
               config: Path | None = ConfigOpt) -> None:
    """Export a deep-crawl run."""
    db, settings = _db(config)
    try:
        console.print_json(json.dumps(export_run(db, run_id, out_dir or settings.output_dir)))
    finally:
        db.close()


@app.command("pipeline")
def pipeline_cmd(publication_limit: int | None = typer.Option(None, "--limit", min=1),
                 run_id: str | None = typer.Option(None, "--run-id"), config: Path | None = ConfigOpt) -> None:
    """Promote, deep-crawl, classify and export (v0.1 student pipeline)."""
    db, settings = _db(config)
    rid = create_run(db, settings, "pipeline", run_id)
    try:
        promoted = promote_candidates(db, settings.publication_confidence_threshold, settings.publication_ambiguity_margin)
        crawled = asyncio.run(crawl_all(db, settings, rid, publication_limit))
        classes = classify_all(db, rid)
        exported = export_run(db, rid, settings.output_dir)
        complete_run(db, rid, "completed")
        console.print_json(json.dumps({"run_id": rid, "promoted": promoted, "crawled": crawled, "classes": classes, "exported": exported}))
    except Exception:
        complete_run(db, rid, "failed")
        raise
    finally:
        db.close()


# ============================================================================ research universe

@app.command("seed-support-orgs")
def seed_support_orgs_cmd(config: Path | None = ConfigOpt) -> None:
    """Seed journalism ethics, legal, training, research and membership organizations."""
    db, _ = _db(config)
    try:
        console.print_json(json.dumps(seed_support_orgs(db)))
    finally:
        db.close()


@app.command("seed-benchmark-newsrooms")
def seed_benchmark_newsrooms_cmd(config: Path | None = ConfigOpt) -> None:
    """Seed the labeled professional-newsroom benchmark panel."""
    db, _ = _db(config)
    try:
        console.print_json(json.dumps(seed_benchmark_newsrooms(db)))
    finally:
        db.close()


@app.command("sync-student-entities")
def sync_student_entities_cmd(config: Path | None = ConfigOpt) -> None:
    """Mirror promoted primary student publications into the research universe."""
    db, _ = _db(config)
    try:
        console.print_json(json.dumps(sync_student_entities(db)))
    finally:
        db.close()


@app.command("import-entities")
def import_entities_cmd(
    path: Path = typer.Argument(..., exists=True, readable=True),
    cohort: str = typer.Option(..., "--cohort", help=", ".join(sorted(COHORTS))),
    source: str = typer.Option(..., "--source", help="Provenance label, e.g. inn_directory_2026"),
    membership_label: str | None = typer.Option(None, "--membership", help="Association membership label, e.g. 'INN member'"),
    config: Path | None = ConfigOpt,
) -> None:
    """Import a directory/membership CSV (name,url[,state,...]); duplicates merge by domain, keeping provenance."""
    db, _ = _db(config)
    try:
        console.print_json(json.dumps(import_entities_csv(db, path, cohort, source, membership_label)))
    finally:
        db.close()


@app.command("import-directory")
def import_directory_cmd(path: Path = typer.Argument(..., exists=True, readable=True),
                         no_promote: bool = typer.Option(False, "--no-promote", help="Only stage student matches as candidates"),
                         config: Path | None = ConfigOpt) -> None:
    """Import the newspaper directory CSV (student + professional): IPEDS matching, domain merging, priorities."""
    from .directory import import_directory
    db, settings = _db(config)
    try:
        backup = db.backup(label="pre-directory-import")
        console.print(f"[dim]Backup written: {backup}[/dim]")
        console.print_json(json.dumps(import_directory(db, path, promote=not no_promote, report_dir=settings.output_dir), default=str))
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    finally:
        db.close()


@app.command("match-directory-homepages")
def match_directory_homepages_cmd(limit: int | None = typer.Option(None, "--limit"), quiet: bool = QuietOpt,
                                  config: Path | None = ConfigOpt) -> None:
    """Read unmatched student papers' homepages to identify their institution (free; also checks if sites still work)."""
    from .directory import match_from_homepages
    db, settings = _db(config)
    try:
        db.backup(label="pre-homepage-matching")
        console.print_json(json.dumps(asyncio.run(match_from_homepages(db, settings, limit=limit, quiet=quiet)), default=str))
    finally:
        db.close()


@app.command("apply-directory-review")
def apply_directory_review_cmd(review: Path = typer.Argument(..., exists=True, help="The edited *_ipeds_matches.csv"),
                               directory: Path = typer.Argument(..., exists=True, help="The original directory CSV"),
                               config: Path | None = ConfigOpt) -> None:
    """Apply your confirm (y/n) / correct UNITID edits from the match report."""
    from .directory import apply_match_review
    db, _ = _db(config)
    try:
        db.backup(label="pre-directory-review")
        console.print_json(json.dumps(apply_match_review(db, review, directory), default=str))
    finally:
        db.close()


@app.command("export-directory")
def export_directory_cmd(template: Path = typer.Argument(..., exists=True, readable=True, help="The original directory CSV"),
                         out: Path | None = typer.Option(None, "--out"), config: Path | None = ConfigOpt) -> None:
    """Write your directory CSV back with status, policy URLs, stance and noindex columns filled from the research."""
    from .directory import export_directory
    db, settings = _db(config)
    try:
        target = out or settings.output_dir / (template.stem + "_with_findings.csv")
        console.print_json(json.dumps(export_directory(db, template, target)))
    finally:
        db.close()


@app.command("merge-duplicates")
def merge_duplicates_cmd(cohort: str | None = typer.Option(None, "--cohort"), config: Path | None = ConfigOpt) -> None:
    """Merge active entities sharing a domain within a cohort (provenance preserved)."""
    db, _ = _db(config)
    try:
        console.print_json(json.dumps(merge_duplicate_entities(db, cohort)))
    finally:
        db.close()


@app.command("research-setup")
def research_setup_cmd(config: Path | None = ConfigOpt) -> None:
    """Seed support orgs + benchmark newsrooms and sync promoted student publications."""
    db, _ = _db(config)
    try:
        from .legal import seed_legal
        from .seeds import seed_experts, seed_precedents
        result = {"support_orgs": seed_support_orgs(db), "benchmark_newsrooms": seed_benchmark_newsrooms(db),
                  "student_entities": sync_student_entities(db), **seed_precedents(db), **seed_experts(db), **seed_legal(db)}
        console.print_json(json.dumps(result))
    finally:
        db.close()


# ============================================================================ research

@app.command("research")
def research_cmd(
    cohort: str | None = typer.Option(None, "--cohort", help=", ".join(sorted(COHORTS))),
    limit: int | None = typer.Option(None, "--limit", min=1),
    entity_id: list[int] = typer.Option([], "--entity-id", help="Research specific entity ids"),
    max_priority: int | None = typer.Option(None, "--max-priority", help="Only directory research priority <= N (1 = highest)"),
    depth: str | None = typer.Option(None, "--depth", help="quick | standard | deep (default from config)"),
    concurrency: int | None = typer.Option(None, "--concurrency", min=1, max=32),
    run_id: str | None = typer.Option(None, "--run-id", help="Resume this run id"),
    max_searches: int | None = MaxSearchesOpt,
    refresh_search: bool = RefreshOpt,
    fresh: bool = FreshOpt,
    no_export: bool = typer.Option(False, "--no-export"),
    quiet: bool = QuietOpt,
    verbose: bool = VerboseOpt,
    config: Path | None = ConfigOpt,
) -> None:
    """Policy, precedent, adverse-evidence and guidance research (resumable; Ctrl+C checkpoints)."""
    if cohort and cohort not in COHORTS:
        raise typer.BadParameter(f"cohort must be one of {sorted(COHORTS)}")
    db, settings = _db(config)
    if depth:
        if depth not in {"quick", "standard", "deep"}:
            raise typer.BadParameter("depth must be quick|standard|deep")
        settings.research_depth = depth  # type: ignore[assignment]
    if concurrency:
        settings.research_concurrency = concurrency
    try:
        rid, resumed = start_research(db, settings, cohort=cohort, limit=limit, run_id=run_id,
                                      max_searches=max_searches, command=_cmdline())
        if not quiet:
            console.print(f"{'Resuming' if resumed else 'Starting'} research run [bold]{rid}[/bold] (depth={settings.research_depth})")
        stats = asyncio.run(research_all(db, settings, rid, cohort=cohort, limit=limit, quiet=quiet, verbose=verbose,
                                         max_searches=max_searches, refresh_search=refresh_search, fresh=fresh,
                                         entity_ids=entity_id or None, max_priority=max_priority))
        stats["run_id"] = rid
        if not no_export:
            stats["exported"] = export_research(db, rid, settings.output_dir)
            stats["export_dir"] = str(settings.output_dir / f"research_{rid}")
        console.print_json(json.dumps(stats, default=str))
        run_finished(settings, "research", stats)
        _print_stop(stats, f"nsmpa research --run-id {rid}" + (f" --cohort {cohort}" if cohort else "")
                    + (f" --limit {limit}" if limit else "") + (f" --max-searches {max_searches}" if max_searches is not None else ""))
    finally:
        db.close()


@app.command("resume")
def resume_cmd(
    max_searches: int | None = MaxSearchesOpt,
    quiet: bool = QuietOpt,
    verbose: bool = VerboseOpt,
    config: Path | None = ConfigOpt,
) -> None:
    """Resume the most recent interrupted / budget-stopped run with its original parameters."""
    db, settings = _db(config)
    try:
        run = latest_resumable_run(db)
        if not run:
            console.print("No interrupted or budget-stopped runs to resume.")
            return
        params = json.loads(run["params_json"] or "{}")
        console.print(f"Resuming [bold]{run['id']}[/bold] ({run['mode']}, last status {run['status']}: {run['status_reason'] or '-'})")
        if run["mode"] == "publication_discovery":
            stats = asyncio.run(discover_all(db, settings, params.get("limit"), run_id=run["id"], quiet=quiet, verbose=verbose,
                                             max_searches=max_searches, states=params.get("states"), unitids=params.get("unitids")))
            stats_cmd = f"nsmpa discover --run-id {run['id']}"
        else:
            if params.get("depth"):
                settings.research_depth = params["depth"]
            start_research(db, settings, cohort=params.get("cohort"), limit=params.get("limit"), run_id=run["id"],
                           max_searches=max_searches, command=_cmdline())
            stats = asyncio.run(research_all(db, settings, run["id"], cohort=params.get("cohort"), limit=params.get("limit"),
                                             quiet=quiet, verbose=verbose, max_searches=max_searches))
            stats["exported"] = export_research(db, run["id"], settings.output_dir)
            stats_cmd = f"nsmpa research --run-id {run['id']}"
        stats["run_id"] = run["id"]
        console.print_json(json.dumps(stats, default=str))
        run_finished(settings, "resumed run", stats)
        _print_stop(stats, stats_cmd)
    finally:
        db.close()


@app.command("research-export")
def research_export_cmd(run_id: str = typer.Option(..., "--run-id"), out_dir: Path | None = typer.Option(None, "--out-dir"),
                        config: Path | None = ConfigOpt) -> None:
    """Export one research run (CSV, JSONL, manifest, summary)."""
    db, settings = _db(config)
    try:
        console.print_json(json.dumps(export_research(db, run_id, out_dir or settings.output_dir)))
        console.print(f"Exported to {(out_dir or settings.output_dir) / ('research_' + run_id)}")
    finally:
        db.close()


# ============================================================================ review / validation / reporting

@app.command("review")
def review_cmd(
    decide: int | None = typer.Option(None, "--decide", help="Review item id to record a decision for"),
    decision: str | None = typer.Option(None, "--decision", help="accept | reject | correct | skip"),
    stance: str | None = typer.Option(None, "--stance", help="Corrected stance when --decision correct"),
    note: str = typer.Option("", "--note"),
    reviewer: str = typer.Option(os.getenv("USER", ""), "--reviewer"),
    cohort: str | None = typer.Option(None, "--cohort"),
    run_id: str | None = typer.Option(None, "--run-id"),
    limit: int = typer.Option(15, "--limit"),
    export: Path | None = typer.Option(None, "--export", help="Write the open queue to this CSV"),
    config: Path | None = ConfigOpt,
) -> None:
    """Show the prioritized human-review queue, or record a decision."""
    from .review import open_items, record_decision
    db, _ = _db(config)
    try:
        if decide is not None:
            if not decision:
                raise typer.BadParameter("--decision is required with --decide")
            record_decision(db, decide, decision, reviewer=reviewer, note=note, corrected_stance=stance)
            console.print(f"Recorded {decision} for review item {decide}")
            return
        items = open_items(db, run_id, cohort, limit if not export else 1_000_000)
        if export:
            with open(export, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                cols = list(items[0].keys()) if items else ["id"]
                w.writerow(cols)
                for r in items:
                    w.writerow([r[c] for c in cols])
            console.print(f"Wrote {len(items)} open review items to {export}")
            return
        t = Table(title="Human review queue (highest priority first)")
        for c in ("id", "priority", "cohort", "entity", "stance", "conf", "reasons"):
            t.add_column(c)
        for r in items:
            t.add_row(str(r["id"]), f"{r['priority']:.0f}", r["cohort"] or "", (r["entity_name"] or f"{r['item_type']} {r['item_id']}")[:40],
                      r["stance"] or "-", f"{r['confidence']:.2f}" if r["confidence"] is not None else "-",
                      ", ".join(json.loads(r["reasons_json"] or "[]"))[:70])
        console.print(t)
        console.print("Record a decision: nsmpa review --decide ID --decision accept|reject|correct|skip [--stance S] [--note TEXT]")
    finally:
        db.close()


@app.command("verify-evidence")
def verify_evidence_cmd(evidence_id: int = typer.Argument(...), status: str = typer.Option(..., "--status",
                        help="verified | rejected | disputed | unverified"), note: str = typer.Option("", "--note"),
                        config: Path | None = ConfigOpt) -> None:
    """Record human source verification for one evidence excerpt."""
    from .review import verify_evidence
    db, _ = _db(config)
    try:
        verify_evidence(db, evidence_id, status, note)
        console.print(f"Evidence {evidence_id}: {status}")
    finally:
        db.close()


@app.command("validate")
def validate_cmd(run_id: str | None = typer.Option(None, "--run-id"), config: Path | None = ConfigOpt) -> None:
    """Show per-cohort quality metrics and whether national percentages are permitted."""
    from .validate import all_cohorts, cohort_metrics
    db, settings = _db(config)
    try:
        for c in all_cohorts(db):
            m = cohort_metrics(db, settings, c, run_id)
            t = Table(title=f"{m.label} — denominator {m.denominator:,} — "
                            + ("[green]VALIDATED[/green]" if m.valid_for_percentages else "[yellow]PRELIMINARY[/yellow]"))
            t.add_column("Gate")
            t.add_column("Value", justify="right")
            t.add_column("Threshold", justify="right")
            t.add_column("Result")
            for g in m.gates:
                t.add_row(g.name, "–" if g.value is None else f"{g.value:.3f}", f"{g.comparator} {g.threshold}",
                          "[green]pass[/green]" if g.passed else "[red]FAIL[/red]")
            console.print(t)
    finally:
        db.close()


@app.command("report")
def report_cmd(run_id: str | None = typer.Option(None, "--run-id", help="Restrict to one run (default: latest stance per entity)"),
               out_dir: Path | None = typer.Option(None, "--out-dir"), config: Path | None = ConfigOpt) -> None:
    """Build the national report (Markdown + JSON + CSV tables), gated by validation metrics."""
    from .report import build_report
    db, settings = _db(config)
    try:
        res = build_report(db, settings, out_dir or settings.output_dir, run_id)
        console.print_json(json.dumps(res))
        console.print(f"Report: {res['out_dir']}/report.md")
    finally:
        db.close()


# ============================================================================ operations

@app.command("status")
def status_cmd(config: Path | None = ConfigOpt) -> None:
    """Universe, discovery, research and review status at a glance."""
    db, settings = _db(config)
    try:
        q = db.scalar
        t = Table(title=f"NSMPA {__version__} status | {settings.database_path} | schema v{db.schema_version()}")
        t.add_column("Area")
        t.add_column("Metric")
        t.add_column("Value", justify="right")
        rows = [
            ("Student", "included institutions (IPEDS)", q("SELECT COUNT(*) FROM institutions WHERE included=1")),
            ("Student", "core stratum (Carnegie 15-23)", q("SELECT COUNT(*) FROM v_institution_peer WHERE included=1 AND core_stratum=1")),
            ("Student", "institutions with discovery done", q("SELECT COUNT(DISTINCT item_key) FROM run_items WHERE item_type='institution' AND status='done'")),
            ("Student", "publication candidates", q("SELECT COUNT(*) FROM publication_candidates")),
            ("Student", "primary publications", q("SELECT COUNT(*) FROM publications WHERE is_primary=1")),
            ("Student", "ambiguous identifications", q("SELECT COUNT(*) FROM publications WHERE ambiguous=1 AND is_primary=1")),
        ]
        for r in db.execute("SELECT cohort, COUNT(*) n FROM research_entities WHERE active=1 AND merged_into IS NULL GROUP BY cohort"):
            rows.append(("Universe", f"{r['cohort']} entities", r["n"]))
        rows += [
            ("Research", "v0.3 runs", q("SELECT COUNT(*) FROM research_runs WHERE engine_version!='0.2'")),
            ("Research", "legacy v0.2 runs (kept, excluded from reports)", q("SELECT COUNT(*) FROM research_runs WHERE engine_version='0.2'")),
            ("Research", "entities with a v0.3 stance", q("SELECT COUNT(DISTINCT entity_id) FROM entity_stances WHERE stance_version='0.3' "
                                                          "AND run_id NOT IN (SELECT id FROM v_excluded_runs)")),
            ("Research", "unique substantive excerpts", q("SELECT COUNT(DISTINCT near_dup_key) FROM evidence_items WHERE statement_type!='mention' "
                                                          "AND run_id NOT IN (SELECT id FROM v_excluded_runs)")),
            ("Search", "live queries (all time)", q("SELECT COUNT(*) FROM search_queries WHERE was_cached=0 AND status='completed'")),
            ("Search", "cache hits (all time)", q("SELECT COUNT(*) FROM search_queries WHERE was_cached=1")),
            ("Search", "estimated credits (all time)", q("SELECT SUM(credits_estimated) FROM search_queries")),
            ("Search", "cached distinct queries", q("SELECT COUNT(*) FROM search_cache")),
            ("Review", "open review items", q("SELECT COUNT(*) FROM review_queue WHERE status='open'")),
            ("Review", "reviewed items", q("SELECT COUNT(*) FROM review_queue WHERE status!='open'")),
            ("Ops", "errors logged", q("SELECT COUNT(*) FROM errors")),
        ]
        for a, m, v in rows:
            t.add_row(a, m, f"{v:,}" if isinstance(v, int) else str(v))
        console.print(t)
        run = latest_resumable_run(db)
        if run:
            c = item_counts(db, run["id"])
            console.print(f"[yellow]Resumable run {run['id']} ({run['mode']}, {run['status']}): {c}. Run `nsmpa resume`.[/yellow]")
    finally:
        db.close()


@app.command("runs")
def runs_cmd(limit: int = typer.Option(15, "--limit"), config: Path | None = ConfigOpt) -> None:
    """List recent discovery/research runs."""
    db, _ = _db(config)
    try:
        t = Table(title="Runs")
        for c in ("id", "mode", "engine", "status", "items done", "live", "cached", "credits", "started", "reason"):
            t.add_column(c)
        for r in db.execute("SELECT * FROM research_runs ORDER BY started_at DESC LIMIT ?", (limit,)):
            done = db.scalar("SELECT COUNT(*) FROM run_items WHERE run_id=? AND status='done'", (r["id"],))
            total = db.scalar("SELECT COUNT(*) FROM run_items WHERE run_id=?", (r["id"],))
            t.add_row(r["id"], r["mode"], r["engine_version"], r["status"], f"{done}/{total}", str(r["searches_live"]),
                      str(r["searches_cached"]), str(r["credits_estimated"]), r["started_at"], (r["status_reason"] or "")[:40])
        console.print(t)
    finally:
        db.close()


@app.command("query-stats")
def query_stats_cmd(run_id: str | None = typer.Option(None, "--run-id"), config: Path | None = ConfigOpt) -> None:
    """Search usage, cache efficiency and which query purposes actually produce evidence."""
    db, _ = _db(config)
    try:
        where, params = ("WHERE run_id=?", [run_id]) if run_id else ("", [])
        t = Table(title="Search ledger by purpose" + (f" (run {run_id})" if run_id else " (all runs)"))
        for c in ("purpose", "queries", "live", "cached", "failed", "credits", "produced evidence", "useful rate"):
            t.add_column(c, justify="right" if c != "purpose" else "left")
        for r in db.execute(f"""SELECT purpose, COUNT(*) n, SUM(was_cached=0 AND status='completed') live, SUM(was_cached) cached,
                               SUM(status!='completed') failed, SUM(credits_estimated) credits, SUM(produced_evidence) useful
                               FROM search_queries {where} GROUP BY purpose ORDER BY n DESC""", params):
            t.add_row(r["purpose"], str(r["n"]), str(r["live"]), str(r["cached"]), str(r["failed"]), str(r["credits"] or 0),
                      str(r["useful"] or 0), f"{(r['useful'] or 0) / r['n']:.0%}")
        console.print(t)
    finally:
        db.close()


@app.command("errors")
def errors_cmd(run_id: str | None = typer.Option(None, "--run-id"), limit: int = typer.Option(25, "--limit"),
               config: Path | None = ConfigOpt) -> None:
    """Summarize logged errors, failed run items and inaccessible pages."""
    db, _ = _db(config)
    try:
        rp = [run_id] if run_id else []
        t = Table(title="Errors by stage/type")
        for c in ("stage", "type", "count"):
            t.add_column(c)
        for r in db.execute(f"SELECT stage, error_type, COUNT(*) n FROM errors {'WHERE research_run_id=?' if run_id else ''} "
                            "GROUP BY 1,2 ORDER BY n DESC LIMIT ?", rp + [limit]):
            t.add_row(r["stage"], r["error_type"] or "", str(r["n"]))
        console.print(t)
        t2 = Table(title="Page access classes (research)")
        for c in ("access class", "count"):
            t2.add_column(c)
        for r in db.execute(f"SELECT COALESCE(access_class,status) ac, COUNT(*) n FROM research_pages {'WHERE run_id=?' if run_id else ''} "
                            "GROUP BY 1 ORDER BY n DESC", rp):
            t2.add_row(r["ac"], str(r["n"]))
        console.print(t2)
        t3 = Table(title="Failed run items (most recent)")
        for c in ("run", "type", "key", "error"):
            t3.add_column(c)
        for r in db.execute(f"SELECT * FROM run_items WHERE status='failed' {'AND run_id=?' if run_id else ''} "
                            "ORDER BY completed_at DESC LIMIT ?", rp + [limit]):
            t3.add_row(r["run_id"], r["item_type"], r["item_key"], (r["error"] or "")[:80])
        console.print(t3)
    finally:
        db.close()


@app.command("doctor")
def doctor_cmd(network: bool = typer.Option(True, "--network/--no-network", help="Check DNS/HTTPS reachability"),
               check_serper: bool = typer.Option(False, "--check-serper", help="Spend 1 credit to verify the Serper key"),
               config: Path | None = ConfigOpt) -> None:
    """Validate dependencies, config, schema, writability, search key and network."""
    from .doctor import run_doctor
    ok = run_doctor(console, config, network=network, check_serper=check_serper)
    raise typer.Exit(code=0 if ok else 1)


# ============================================================================ precedents, experts, case packet

@app.command("verify-precedents")
def verify_precedents_cmd(
    max_searches: int | None = MaxSearchesOpt, run_id: str | None = typer.Option(None, "--run-id"),
    only_index: bool = typer.Option(False, "--only-index", help="Only entries imported with import-evidence-index"),
    no_search: bool = typer.Option(False, "--no-search", help="Read the listed source links only (no search credits)"),
    quiet: bool = QuietOpt, verbose: bool = VerboseOpt, config: Path | None = ConfigOpt,
) -> None:
    """Search, fetch and snapshot sources for seeded precedents (Boston Globe Fresh Start, AP, SPJ...). Never self-verifies."""
    from .seeds import run_seeds
    db, settings = _db(config)
    try:
        res = asyncio.run(run_seeds(db, settings, "precedents", run_id=run_id, max_searches=max_searches, quiet=quiet,
                                    verbose=verbose, command=_cmdline(), only_index=only_index, search=not no_search))
        console.print_json(json.dumps(res, default=str))
        run_finished(settings, "precedent verification", res)
        _print_stop(res, f"nsmpa verify-precedents --run-id {res['run_id']}")
    finally:
        db.close()


@app.command("precedent")
def precedent_cmd(
    key: str | None = typer.Option(None, "--key", help="Seed key to update"),
    status: str | None = typer.Option(None, "--status", help="human_verified | refuted | ..."),
    note: str = typer.Option("", "--note"),
    add: bool = typer.Option(False, "--add", help="Add a new lead (with --key --org --title --claim and --url or --query)"),
    org: str | None = typer.Option(None, "--org"), title: str | None = typer.Option(None, "--title"),
    claim: str | None = typer.Option(None, "--claim"), url: str | None = typer.Option(None, "--url"),
    query: str | None = typer.Option(None, "--query"), year: str = typer.Option("", "--year"),
    config: Path | None = ConfigOpt,
) -> None:
    """List precedent leads, record human verification, or add your own lead."""
    from .seeds import add_precedent, seed_precedents, set_precedent_status
    db, _ = _db(config)
    try:
        seed_precedents(db)
        if add:
            if not (key and org and title and claim and (url or query)):
                raise typer.BadParameter("--add needs --key --org --title --claim and --url or --query")
            add_precedent(db, key, org, title, claim, url=url, query=query, year=year)
            console.print(f"Added precedent lead {key}")
            return
        if key and status:
            set_precedent_status(db, key, status, note)
            console.print(f"{key}: {status}")
            return
        t = Table(title="Precedent leads")
        for c in ("key", "organization", "title", "year", "status", "lead quality"):
            t.add_column(c)
        for r in db.execute("SELECT * FROM precedent_seeds ORDER BY id"):
            t.add_row(r["seed_key"], r["organization"], r["title"], r["approx_year"] or "", r["status"], r["prior_confidence"])
        console.print(t)
    finally:
        db.close()


@app.command("research-experts")
def research_experts_cmd(
    max_searches: int | None = MaxSearchesOpt, run_id: str | None = typer.Option(None, "--run-id"),
    quiet: bool = QuietOpt, verbose: bool = VerboseOpt, config: Path | None = ConfigOpt,
) -> None:
    """Find attributed statements by seeded/added experts (one search each); captures both directions."""
    from .seeds import run_seeds
    db, settings = _db(config)
    try:
        res = asyncio.run(run_seeds(db, settings, "experts", run_id=run_id, max_searches=max_searches, quiet=quiet,
                                    verbose=verbose, command=_cmdline()))
        console.print_json(json.dumps(res, default=str))
        run_finished(settings, "expert voices", res)
        _print_stop(res, f"nsmpa research-experts --run-id {res['run_id']}")
    finally:
        db.close()


@app.command("add-expert")
def add_expert_cmd(name: str = typer.Option(..., "--name"), role: str = typer.Option(..., "--role"),
                   affiliation: str = typer.Option(..., "--affiliation"),
                   note: str = typer.Option("", "--note", help="Why this person is respected (credentials)"),
                   config: Path | None = ConfigOpt) -> None:
    """Add a respected practitioner/scholar whose statements should be sought and linked."""
    from .seeds import add_expert
    db, _ = _db(config)
    try:
        console.print(f"Expert id {add_expert(db, name, role, affiliation, note)}: {name}")
    finally:
        db.close()


@app.command("voices")
def voices_cmd(person: str | None = typer.Option(None, "--person"), direction: str | None = typer.Option(None, "--direction"),
               verify: int | None = typer.Option(None, "--verify", help="Voice id to mark"),
               status: str = typer.Option("verified", "--status", help="verified | rejected | disputed"),
               note: str = typer.Option("", "--note"), limit: int = typer.Option(25, "--limit"),
               config: Path | None = ConfigOpt) -> None:
    """List attributed expert/practitioner statements, or record human verification of one."""
    db, _ = _db(config)
    try:
        if verify is not None:
            if status not in {"verified", "rejected", "disputed", "unverified"}:
                raise typer.BadParameter("status must be verified|rejected|disputed|unverified")
            db.execute("UPDATE voices SET verification_status=?, reviewer_note=? WHERE id=?", (status, note or None, verify))
            db.conn.commit()
            console.print(f"Voice {verify}: {status}")
            return
        sql = "SELECT v.*, x.role AS xrole FROM voices v LEFT JOIN experts x ON x.id=v.expert_id WHERE 1=1"
        params: list = []
        if person:
            sql += " AND v.person_name LIKE ?"
            params.append(f"%{person}%")
        if direction:
            sql += " AND v.direction=?"
            params.append(direction)
        sql += " ORDER BY v.expert_id IS NULL, v.case_match_score DESC LIMIT ?"
        t = Table(title="Voices")
        for c in ("id", "person", "role", "direction", "verified", "quote", "source"):
            t.add_column(c)
        for r in db.execute(sql, params + [limit]):
            t.add_row(str(r["id"]), r["person_name"], (r["xrole"] or r["role"] or "")[:24], r["direction"], r["verification_status"],
                      r["quote"][:90], r["source_domain"] or "")
        console.print(t)
    finally:
        db.close()


@app.command("my-case")
def my_case_cmd(init: bool = typer.Option(False, "--init", help="Create my_case.yml from the example"),
                config: Path | None = ConfigOpt) -> None:
    """Show (or create) the fact profile that precedents are matched against."""
    import shutil
    if init:
        if Path("my_case.yml").exists():
            console.print("my_case.yml already exists; edit it directly.")
        else:
            shutil.copy("my_case.example.yml", "my_case.yml")
            console.print("Created my_case.yml (gitignored). Edit it, then set configured: true.")
        return
    settings = load_settings(config)
    mc = settings.my_case
    console.print_json(json.dumps(mc.model_dump(), default=str))
    console.print(f"Active match factors: {', '.join(sorted(mc.active_factors()))}")
    if not mc.configured:
        console.print("[yellow]Using defaults. Run `nsmpa my-case --init` and edit my_case.yml.[/yellow]")


@app.command("packet")
def packet_cmd(run_id: str | None = typer.Option(None, "--run-id"), out_dir: Path | None = typer.Option(None, "--out-dir"),
               title: str = typer.Option("Post-publication relief in U.S. journalism: the evidence", "--title"),
               ai_summaries: bool = typer.Option(False, "--ai-summaries", help="Add AI-drafted, citation-checked section summaries"),
               redact_names: bool = typer.Option(False, "--redact-names", help="Withhold names of private individuals (use before sharing)"),
               withhold: list[str] = typer.Option([], "--withhold", help="Drop rows whose URL contains this text (repeatable)"),
               withhold_reason: str = typer.Option("withheld by the researcher", "--withhold-reason",
                                                   help="Stated in the packet's Read Me alongside the count"),
               config: Path | None = ConfigOpt) -> None:
    """Build the shareable case packet: Excel evidence workbook + PowerPoint deck + summary."""
    from .packet import build_packet
    db, settings = _db(config)
    try:
        res = build_packet(db, settings, out_dir or settings.output_dir, run_id=run_id, title=title, ai_summaries_on=ai_summaries,
                           redact=redact_names, withhold=withhold, withhold_reason=withhold_reason)
        if not redact_names:
            console.print("[yellow]Not redacted: excerpts may name private individuals. Use --redact-names before sharing.[/yellow]")
        console.print_json(json.dumps(res))
    finally:
        db.close()


# ============================================================================ v0.5: Wayback, AI review, audit

@app.command("wayback")
def wayback_cmd(run_id: str = typer.Option(..., "--run-id", help="Research run whose pages to compare"),
                entity_id: list[int] = typer.Option([], "--entity-id"), quiet: bool = QuietOpt, verbose: bool = VerboseOpt,
                config: Path | None = ConfigOpt) -> None:
    """Compare sampled crime articles (and now-404 article URLs) with Wayback Machine captures. Free."""
    from .wayback import run_wayback
    db, settings = _db(config)
    try:
        res = asyncio.run(run_wayback(db, settings, run_id, quiet=quiet, verbose=verbose, entity_ids=entity_id or None))
        console.print_json(json.dumps(res))
        run_finished(settings, "Wayback comparison", res)
    finally:
        db.close()


@app.command("ai-discovery")
def ai_discovery_cmd(max_calls: int = typer.Option(100, "--max-calls", min=0), limit: int | None = typer.Option(None, "--limit"),
                     unitid: list[str] = typer.Option([], "--unitid"), config: Path | None = ConfigOpt) -> None:
    """AI picks the student paper among already-found candidates for uncertain institutions (then `promote --use-ai`)."""
    from .ai_discovery import run_ai_discovery
    from .ai_review import AIUnavailable
    db, settings = _db(config)
    try:
        console.print_json(json.dumps(run_ai_discovery(db, settings, max_calls=max_calls, limit=limit, unitids=unitid or None)))
    except AIUnavailable as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    finally:
        db.close()


@app.command("ai-review")
def ai_review_cmd(
    run_id: str | None = typer.Option(None, "--run-id", help="Limit to pages from this research run"),
    cohort: str | None = typer.Option(None, "--cohort"),
    max_calls: int | None = typer.Option(None, "--max-calls", min=0, help="Max live AI calls this invocation"),
    limit: int | None = typer.Option(None, "--limit", min=1, help="Max pages to consider"),
    provider: str | None = typer.Option(None, "--provider", help="gemini (default) | anthropic"),
    model: str | None = typer.Option(None, "--model", help="Override ai_model (default gemini-3.8-flash)"),
    resume_id: str | None = typer.Option(None, "--resume", help="Resume an earlier ai-review run id"),
    quiet: bool = QuietOpt, verbose: bool = VerboseOpt, config: Path | None = ConfigOpt,
) -> None:
    """AI second opinion on fetched pages. Quotes are verified verbatim; never changes a stance on its own."""
    from .ai_review import AIUnavailable, run_ai_review
    db, settings = _db(config)
    if provider:
        if provider not in {"gemini", "anthropic"}:
            raise typer.BadParameter("provider must be gemini or anthropic")
        settings.ai_provider = provider  # type: ignore[assignment]
        if not model and provider == "anthropic":
            settings.ai_model = "claude-opus-5-5"
    if model:
        settings.ai_model = model
    try:
        res = asyncio.run(run_ai_review(db, settings, run_id=run_id, cohort=cohort, max_calls=max_calls, limit=limit,
                                        quiet=quiet, verbose=verbose, review_run_id=resume_id, command=_cmdline()))
        console.print_json(json.dumps(res, default=str))
        run_finished(settings, "AI review", res)
        if res["status"] in {"budget_exhausted", "interrupted"}:
            console.print(f"[yellow]Resume with: nsmpa ai-review --resume {res['run_id']}"
                          + (f" --run-id {run_id}" if run_id else "") + "[/yellow]")
    except AIUnavailable as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    finally:
        db.close()


audit_app = typer.Typer(help="Accuracy audit: sample, label (Excel or terminal), report")
app.add_typer(audit_app, name="audit")


@audit_app.command("sample")
def audit_sample(n: int = typer.Option(50, "--n", min=5), seed: int | None = typer.Option(None, "--seed"),
                 cohort: str | None = typer.Option(None, "--cohort"), description: str = typer.Option("", "--description"),
                 config: Path | None = ConfigOpt) -> None:
    """Draw a reproducible stratified sample of evidence to hand-check."""
    from .audit import create_sample
    db, _ = _db(config)
    try:
        console.print_json(json.dumps(create_sample(db, n, seed=seed, cohort=cohort, description=description)))
    finally:
        db.close()


@audit_app.command("export")
def audit_export(out: Path = typer.Option(..., "--out", help="CSV to open in Excel"), audit_id: str | None = typer.Option(None, "--audit-id"),
                 show_machine: bool = typer.Option(False, "--show-machine", help="Include the machine's answer (not blind)"),
                 config: Path | None = ConfigOpt) -> None:
    """Write the sample to CSV for labeling in Excel (blind by default)."""
    from .audit import export_csv, latest_audit
    db, _ = _db(config)
    try:
        aid = audit_id or latest_audit(db)
        if not aid:
            raise typer.BadParameter("No audit yet; run `nsmpa audit sample` first")
        console.print(f"Wrote {export_csv(db, aid, out, blind=not show_machine)} items for audit {aid} to {out}")
    finally:
        db.close()


@audit_app.command("import")
def audit_import(path: Path = typer.Argument(..., exists=True), labeler: str = typer.Option(..., "--labeler"),
                 config: Path | None = ConfigOpt) -> None:
    """Import a labeled CSV (each person imports their own copy under their own --labeler name)."""
    from .audit import import_csv
    db, _ = _db(config)
    try:
        console.print_json(json.dumps(import_csv(db, path, labeler)))
    finally:
        db.close()


@audit_app.command("label")
def audit_label(labeler: str = typer.Option(..., "--labeler"), audit_id: str | None = typer.Option(None, "--audit-id"),
                show_machine: bool = typer.Option(False, "--show-machine"), config: Path | None = ConfigOpt) -> None:
    """Label the sample in the terminal (progress is saved; q to stop)."""
    from .audit import label_interactive, latest_audit
    db, _ = _db(config)
    try:
        aid = audit_id or latest_audit(db)
        if not aid:
            raise typer.BadParameter("No audit yet; run `nsmpa audit sample` first")
        console.print(f"Labeled {label_interactive(db, aid, labeler, show_machine=show_machine)} items")
    finally:
        db.close()


@audit_app.command("report")
def audit_report(audit_id: str | None = typer.Option(None, "--audit-id"), as_json: bool = typer.Option(False, "--json"),
                 config: Path | None = ConfigOpt) -> None:
    """Accuracy with 95% intervals, confusion matrix, inter-rater kappa, AI-vs-human accuracy."""
    from .audit import format_report, latest_audit, report
    db, _ = _db(config)
    try:
        aid = audit_id or latest_audit(db)
        if not aid:
            raise typer.BadParameter("No audit yet; run `nsmpa audit sample` first")
        rep = report(db, aid)
        console.print_json(json.dumps(rep)) if as_json else console.print(format_report(rep))
    finally:
        db.close()


@app.command("legal-research")
def legal_research_cmd(max_searches: int | None = MaxSearchesOpt, key: list[str] = typer.Option([], "--key"),
                       run_id: str | None = typer.Option(None, "--run-id"), quiet: bool = QuietOpt, verbose: bool = VerboseOpt,
                       config: Path | None = ConfigOpt) -> None:
    """Find sources for legal-context leads (EU erasure law, state record clearing, key cases). Never self-verifies."""
    from .legal import run_legal_research
    db, settings = _db(config)
    try:
        res = asyncio.run(run_legal_research(db, settings, max_searches=max_searches, keys=key or None, quiet=quiet,
                                             verbose=verbose, run_id=run_id))
        console.print_json(json.dumps(res))
        run_finished(settings, "legal research", res)
    finally:
        db.close()


@app.command("legal")
def legal_cmd(key: str | None = typer.Option(None, "--key"), status: str | None = typer.Option(None, "--status"),
              note: str = typer.Option("", "--note"), config: Path | None = ConfigOpt) -> None:
    """List legal-context leads, or record human verification of one."""
    from .legal import seed_legal, set_legal_status
    db, _ = _db(config)
    try:
        seed_legal(db)
        if key and status:
            set_legal_status(db, key, status, note)
            console.print(f"{key}: {status}")
            return
        t = Table(title="Legal context (leads to verify; not legal advice)")
        for c in ("key", "jurisdiction", "title", "citation", "status"):
            t.add_column(c)
        for r in db.execute("SELECT * FROM legal_context ORDER BY CASE WHEN key LIKE 'state_%' THEN 1 ELSE 0 END, id"):
            t.add_row(r["key"], r["jurisdiction"], r["title"][:50], r["citation"] or "", r["status"])
        console.print(t)
    finally:
        db.close()


@app.command("recheck")
def recheck_cmd(limit: int | None = typer.Option(None, "--limit"), quiet: bool = QuietOpt, verbose: bool = VerboseOpt,
                config: Path | None = ConfigOpt) -> None:
    """Re-fetch known policy pages and record any changes (free; no search credits)."""
    from .recheck import run_recheck
    db, settings = _db(config)
    try:
        res = asyncio.run(run_recheck(db, settings, limit=limit, quiet=quiet, verbose=verbose))
        console.print_json(json.dumps(res))
        if res["changed"]:
            from .notify import notify
            notify("NSMPA: policy pages changed", f"{res['changed']} page(s) changed; see the review queue",
                   enabled=settings.notify_on_finish)
    finally:
        db.close()


@app.command("schedule")
def schedule_cmd(install: bool = typer.Option(False, "--install", help="Write and load the weekly launchd job"),
                 uninstall: bool = typer.Option(False, "--uninstall", help="Unload and remove the job"),
                 weekday: int = typer.Option(1, "--weekday", min=0, max=7, help="0/7=Sunday, 1=Monday ..."),
                 hour: int = typer.Option(3, "--hour", min=0, max=23), config: Path | None = ConfigOpt) -> None:
    """Weekly automatic `nsmpa recheck` via macOS launchd. Without flags, just prints the job definition."""
    import plistlib
    import subprocess

    from .recheck import LABEL, launchd_plist, plist_path, write_plist
    target = plist_path()
    uid = os.getuid()
    if uninstall:
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(target)], capture_output=True, check=False)
        target.unlink(missing_ok=True)
        console.print(f"Removed {LABEL}")
        return
    plist = launchd_plist(Path.cwd(), resolve_config_path(config), weekday=weekday, hour=hour)
    if not install:
        console.print(plistlib.dumps(plist).decode())
        console.print(f"[dim]Install with: nsmpa schedule --install  (writes {target})[/dim]")
        return
    write_plist(plist, target)
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(target)], capture_output=True, check=False)
    res = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(target)], capture_output=True, text=True, check=False)
    if res.returncode != 0:
        console.print(f"[yellow]Wrote {target} but launchctl reported: {res.stderr.strip()}[/yellow]")
    else:
        console.print(f"Installed {LABEL}: weekly recheck (weekday {weekday}, {hour:02d}:00). Logs: output/logs/")


outreach_app = typer.Typer(help="Ask newsrooms directly (drafts only; you send) and record their replies as evidence")
app.add_typer(outreach_app, name="outreach")


@outreach_app.command("harvest")
def outreach_harvest(config: Path | None = ConfigOpt) -> None:
    """Collect contact addresses published on organizations' own sites (already-fetched pages only)."""
    from .outreach import harvest_contacts
    db, _ = _db(config)
    try:
        console.print_json(json.dumps(harvest_contacts(db)))
    finally:
        db.close()


@outreach_app.command("add-contact")
def outreach_add_contact(entity_id: int = typer.Option(..., "--entity-id"), email_addr: str = typer.Option(..., "--email"),
                         name: str = typer.Option("", "--name"), role: str = typer.Option("", "--role"),
                         config: Path | None = ConfigOpt) -> None:
    """Add a contact you found yourself."""
    from .outreach import add_contact
    db, _ = _db(config)
    try:
        add_contact(db, entity_id, email_addr, name, role)
        console.print(f"Added {email_addr} for entity {entity_id}")
    finally:
        db.close()


@outreach_app.command("dnc")
def outreach_dnc(email_addr: str = typer.Option(..., "--email"), config: Path | None = ConfigOpt) -> None:
    """Mark an address do-not-contact (never drafted again)."""
    from .outreach import do_not_contact
    db, _ = _db(config)
    try:
        console.print(f"Marked {do_not_contact(db, email_addr)} contact(s) do-not-contact")
    finally:
        db.close()


@outreach_app.command("draft")
def outreach_draft(campaign: str = typer.Option(..., "--campaign"), cohort: str | None = typer.Option(None, "--cohort"),
                   limit: int | None = typer.Option(None, "--limit"), config: Path | None = ConfigOpt) -> None:
    """Write neutral survey drafts (.eml, open in Mail/Outlook to review and send) + mail_merge.csv. Never sends."""
    from .outreach import draft_campaign
    db, settings = _db(config)
    try:
        console.print_json(json.dumps(draft_campaign(db, settings, campaign, settings.output_dir / "outreach", cohort=cohort, limit=limit)))
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    finally:
        db.close()


@outreach_app.command("sent")
def outreach_sent(campaign: str = typer.Option(..., "--campaign"), entity_id: int | None = typer.Option(None, "--entity-id"),
                  config: Path | None = ConfigOpt) -> None:
    """Record that you sent drafts (all in the campaign, or one organization)."""
    from .outreach import mark_sent
    db, _ = _db(config)
    try:
        console.print(f"Marked {mark_sent(db, campaign, entity_id)} message(s) sent")
    finally:
        db.close()


@outreach_app.command("response")
def outreach_response(entity_id: int = typer.Option(..., "--entity-id"), file: Path = typer.Option(..., "--file", exists=True),
                      campaign: str = typer.Option("default", "--campaign"), config: Path | None = ConfigOpt) -> None:
    """Store a reply (.eml/.txt/.pdf/.html) as first-party evidence and refresh that organization's stance."""
    from .outreach import record_response
    db, settings = _db(config)
    try:
        console.print_json(json.dumps(record_response(db, settings, entity_id, file, campaign=campaign)))
    finally:
        db.close()


@outreach_app.command("status")
def outreach_status(config: Path | None = ConfigOpt) -> None:
    """Drafted / sent / responded counts per campaign."""
    from .outreach import status
    db, _ = _db(config)
    try:
        console.print_json(json.dumps(status(db)))
    finally:
        db.close()


@app.command("exclude-run")
def exclude_run_cmd(run_id: str = typer.Argument(...), reason: str = typer.Option(..., "--reason"),
                    undo: bool = typer.Option(False, "--undo", help="Restore the run to 'completed'"),
                    config: Path | None = ConfigOpt) -> None:
    """Exclude a run (e.g. a smoke test) from validation and reports without deleting any data."""
    db, _ = _db(config)
    try:
        if not db.scalar("SELECT COUNT(*) FROM research_runs WHERE id=?", (run_id,)):
            raise typer.BadParameter(f"Unknown run {run_id}")
        db.execute("UPDATE research_runs SET status=?, status_reason=? WHERE id=?",
                   ("completed" if undo else "excluded", None if undo else f"excluded: {reason}", run_id))
        db.conn.commit()
        console.print(f"Run {run_id} {'restored' if undo else 'excluded from reports'}")
    finally:
        db.close()


@app.command("reclassify")
def reclassify_cmd(run_id: list[str] = typer.Option(..., "--run-id", help="Repeatable"),
                   config: Path | None = ConfigOpt) -> None:
    """Re-judge stored excerpts with the current classifier and recompute stances (no fetching, no credits)."""
    from .reclassify import reclassify_run
    db, settings = _db(config)
    try:
        for rid in run_id:
            console.print_json(json.dumps(reclassify_run(db, settings, rid)))
    finally:
        db.close()


@app.command("import-evidence-index")
def import_evidence_index_cmd(path: Path = typer.Argument(..., exists=True, readable=True),
                              name: str = typer.Option(..., "--name", help="A label for this list, e.g. 'Easterner evidence index v6'"),
                              config: Path | None = ConfigOpt) -> None:
    """Import a hand-built list of organizations + source links as precedents (never counted in percentages)."""
    from .evidence_index import import_evidence_index
    db, _ = _db(config)
    try:
        console.print_json(json.dumps(import_evidence_index(db, path, name=name)))
        console.print("Next: nsmpa verify-precedents --only-index --no-search   (reads every listed source; no credits)")
    finally:
        db.close()


@app.command("evidence-index-report")
def evidence_index_report_cmd(name: str | None = typer.Option(None, "--name"),
                              out: Path = typer.Option(Path("output/evidence_index_check.csv"), "--out"),
                              redact_names: bool = typer.Option(False, "--redact-names", help="Withhold private individuals' names"),
                              config: Path | None = ConfigOpt) -> None:
    """Compare each imported entry (its tier) with what NSMPA found when it read the source."""
    import csv
    from collections import Counter

    from .evidence_index import index_report
    db, _ = _db(config)
    try:
        rows = index_report(db, name)
        if not rows:
            raise typer.BadParameter("no imported evidence-index entries")
        if redact_names:
            from .redact import Redactor, redact_rows
            r = Redactor.from_db(db)
            rows = [{**row, "best_supportive": redact_rows([{"excerpt": row["best_supportive"]}], r)[0]["excerpt"]}
                    for row in rows]
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        by = Counter((r["tier"], r["status"]) for r in rows)
        for tier in sorted({r["tier"] for r in rows}):
            n = sum(v for (t, _), v in by.items() if t == tier)
            console.print(f"Tier {tier}: {n} entries · " + ", ".join(f"{s} {v}" for (t, s), v in sorted(by.items()) if t == tier))
        console.print(f"Wrote {out}")
    finally:
        db.close()


@app.command("capture")
def capture_cmd(entity: str = typer.Option(..., "--entity", help="Organization id, or a unique part of its name"),
                url: str = typer.Option(..., "--url", help="The page you copied the text from"),
                text_file: Path | None = typer.Option(None, "--file", exists=True, readable=True,
                                                      help="Plain-text file with the copied page text (default: read stdin)"),
                by: str = typer.Option(..., "--by", help="Who captured it (name or initials)"),
                title: str = typer.Option("", "--title"), note: str = typer.Option("", "--note"),
                config: Path | None = ConfigOpt) -> None:
    """Record policy text you copied by hand from a site that blocks automated access."""
    import sys

    from .capture import CaptureError, add_capture
    db, settings = _db(config)
    try:
        if entity.isdigit():
            eid = int(entity)
        else:
            rows = db.execute("SELECT id, name FROM research_entities WHERE active=1 AND name LIKE ? LIMIT 6",
                              (f"%{entity}%",)).fetchall()
            if len(rows) != 1:
                names = ", ".join(f"{r['id']}={r['name']}" for r in rows) or "none"
                raise typer.BadParameter(f"'{entity}' matches {len(rows)} organizations ({names}); pass the id")
            eid = int(rows[0]["id"])
        text = text_file.read_text(encoding="utf-8", errors="replace") if text_file else sys.stdin.read()
        try:
            res = add_capture(db, settings, entity_id=eid, url=url, text=text, captured_by=by, title=title, note=note)
        except CaptureError as exc:
            raise typer.BadParameter(str(exc)) from None
        if res["duplicate"]:
            console.print(f"Already captured (capture #{res['capture_id']}); nothing changed.")
        else:
            console.print(f"Capture #{res['capture_id']}: {res['evidence']} excerpt(s), {res['substantive']} substantive"
                          f"{'' if res['first_party'] else ' (not on the organization’s own site)'}; "
                          f"{res['ai_leads_confirmed']} AI lead(s) confirmed. Stance now {res['stance']} "
                          f"({res['relief_mode']}) in run {res['run_id']}. Fingerprint {res['fingerprint'][:16]}…")
    finally:
        db.close()


@app.command("leads")
def leads_cmd(status: str = typer.Option("unconfirmed", "--status", help="unconfirmed | confirmed | no_policy_found | all"),
              limit: int = typer.Option(50, "--limit"), config: Path | None = ConfigOpt) -> None:
    """AI-search leads for blocked sites: what the AI says the policy is, and whether NSMPA could confirm it."""
    from rich.table import Table
    db, _ = _db(config)
    try:
        sql = ("SELECT l.id, re.id AS eid, re.name, l.status, l.confirmed_via, l.claimed_url, l.quote, l.summary FROM ai_leads l "
               "JOIN research_entities re ON re.id=l.entity_id")
        params: list = []
        if status != "all":
            sql += " WHERE l.status=?"
            params.append(status)
        rows = db.execute(sql + " ORDER BY l.id DESC LIMIT ?", params + [limit]).fetchall()
        t = Table(title=f"AI-search leads ({status})", show_lines=True)
        for col in ("id", "organization", "status", "claimed URL", "quote / summary"):
            t.add_column(col)
        for r in rows:
            t.add_row(str(r["id"]), f"{r['name']} (#{r['eid']})", f"{r['status']}{' via ' + r['confirmed_via'] if r['confirmed_via'] else ''}",
                      r["claimed_url"] or "", (r["quote"] or r["summary"] or "")[:300])
        console.print(t)
        if status == "unconfirmed" and rows:
            console.print("Confirm a lead by opening the URL in your browser and recording the text: "
                          "nsmpa capture --entity ID --url URL --by YOU < copied.txt  (or the GUI's Capture tab)")
    finally:
        db.close()


@app.command("gui")
def gui_cmd(port: int | None = typer.Option(None, "--port", help="Default from config gui_port (8765)"),
            no_browser: bool = typer.Option(False, "--no-browser"), config: Path | None = ConfigOpt) -> None:
    """Open the local monitor in your browser (live runs, results, evidence, review). Localhost only."""
    from .gui import serve
    serve(load_settings(config), port=port, open_browser=not no_browser)


@app.command("dashboard")
def dashboard_cmd(out: Path | None = typer.Option(None, "--out", help="Default output/dashboard_<UTC>.html"),
                  redact_names: bool = typer.Option(False, "--redact-names", help="Withhold names of private individuals"),
                  config: Path | None = ConfigOpt) -> None:
    """Write a shareable, offline, read-only HTML dashboard (one file, data embedded)."""
    from datetime import datetime

    from .gui import write_dashboard
    db, settings = _db(config)
    try:
        target = out or settings.output_dir / f"dashboard_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.html"
        console.print(f"Dashboard written: {write_dashboard(db, settings, target, redact=redact_names)}")
        if not redact_names:
            console.print("[yellow]Not redacted: use --redact-names before sharing outside your review team.[/yellow]")
    finally:
        db.close()


@app.command("estimate")
def estimate_cmd(cohort: str | None = typer.Option(None, "--cohort"), limit: int | None = typer.Option(None, "--limit"),
                 entity_id: list[int] = typer.Option([], "--entity-id"), depth: str | None = typer.Option(None, "--depth"),
                 institutions: int | None = typer.Option(None, "--institutions", help="Also estimate discovery for N institutions"),
                 state: list[str] = typer.Option([], "--state"), as_json: bool = typer.Option(False, "--json"),
                 config: Path | None = ConfigOpt) -> None:
    """Predict credits, page requests, time and AI calls before running (cached queries counted as free)."""
    from .estimate import estimate_all
    db, settings = _db(config)
    if depth:
        settings.research_depth = depth  # type: ignore[assignment]
    try:
        res = estimate_all(db, settings, cohort=cohort, limit=limit, entity_ids=entity_id or None, institutions=institutions,
                           states=state or None)
        if as_json:
            console.print_json(json.dumps(res))
            return
        t = Table(title=f"Estimate (depth={res['depth']}, practice digging={res['practice_dig']})")
        for c in ("Stage", "Items", "Search credits", "Already cached", "Page requests", "Time (min)"):
            t.add_column(c)
        r = res["research"]
        t.add_row("Research", str(r["entities"]), f"{r['credits_low']}–{r['credits_high']}", str(r["queries_already_cached"]),
                  f"{r['page_requests_low']}–{r['page_requests_high']}", f"{r['minutes_low']}–{r['minutes_high']}")
        if "discovery" in res:
            d = res["discovery"]
            t.add_row("Discovery", str(d["institutions"]), f"{d['credits_low']}–{d['credits_high']}", str(d["queries_already_cached"]),
                      "–", f"{d['minutes_low']}–{d['minutes_high']}")
        a = res["ai_review"]
        t.add_row("AI review (optional)", f"{a['calls_low']}–{a['calls_high']} calls", "–", "–", "–",
                  f"${a['usd_high']} max" if "usd_high" in a else "set ai_usd_per_million_* for $")
        console.print(t)
        console.print(f"Total search credits: {res['total_credits_low']}–{res['total_credits_high']}"
                      + (f"  (${res['serper_usd_low']}–${res['serper_usd_high']})" if "serper_usd_low" in res else ""))
    finally:
        db.close()


@app.command("pilot")
def pilot_cmd(budget: int = typer.Option(25, "--budget", min=0, help="Total search credits for the whole pilot"),
              ai_calls: int = typer.Option(10, "--ai-calls", min=0), support: int = typer.Option(1, "--support", min=0),
              newsrooms: int = typer.Option(2, "--newsrooms", min=0), schools: int = typer.Option(2, "--schools", min=0),
              unitid: list[str] = typer.Option([], "--unitid", help="Institutions for discovery (default EWU, WSU)"),
              quiet: bool = QuietOpt, verbose: bool = VerboseOpt, config: Path | None = ConfigOpt) -> None:
    """One capped end-to-end trial with a plain-language report (output/pilot_<UTC>/pilot_report.md)."""
    from .pilot import run_pilot
    db, settings = _db(config)
    try:
        rep = asyncio.run(run_pilot(db, settings, budget=budget, ai_calls=ai_calls, support=support, newsrooms=newsrooms,
                                    schools=schools, unitids=unitid or None, quiet=quiet, verbose=verbose))
        icon = {"ok": "[green]ok[/green]", "warning": "[yellow]warning[/yellow]", "failed": "[red]failed[/red]", "skipped": "[dim]skipped[/dim]"}
        t = Table(title=f"Pilot complete — {rep['credits_spent']} of {budget} credits spent")
        t.add_column("Step")
        t.add_column("Result")
        t.add_column("Detail")
        for s in rep["steps"]:
            t.add_row(s["step"], icon.get(s["status"], s["status"]), s["detail"])
        console.print(t)
        console.print(f"Report: {rep['out_dir']}/pilot_report.md")
        run_finished(settings, "pilot", {"status": "completed", "completed": len(rep.get("stances", []))})
    finally:
        db.close()


@app.command("watch")
def watch_cmd(run_id: str | None = typer.Option(None, "--run-id", help="Run to follow (default: most recently active)"),
              interval: float = typer.Option(1.0, "--interval", min=0.2), config: Path | None = ConfigOpt) -> None:
    """Follow a running job from another terminal (reads its heartbeat; Ctrl+C stops watching, not the job)."""
    import time as _time

    from rich.live import Live

    from .progress import HeartbeatView
    settings = load_settings(config)
    db = Database(settings.database_path)

    def latest():
        if run_id:
            return db.execute("SELECT * FROM run_heartbeats WHERE run_id=?", (run_id,)).fetchone()
        return db.execute("SELECT * FROM run_heartbeats ORDER BY finished, updated_at DESC LIMIT 1").fetchone()

    row = latest()
    if not row:
        console.print("No runs have reported progress yet. Start one (e.g. `nsmpa research ...`) and try again.")
        db.close()
        return
    try:
        with Live(console=console, refresh_per_second=4) as live:
            while True:
                row = latest()
                view = HeartbeatView(json.loads(row["state_json"]), row["title"] or "NSMPA")
                age = db.scalar("SELECT CAST((julianday('now') - julianday(?)) * 86400 AS INTEGER)", (row["updated_at"],), 0)
                stale = "" if row["finished"] or age < 30 else f"  [yellow](no heartbeat for {age}s — process may have stopped)[/yellow]"
                from rich.console import Group
                from rich.text import Text
                header = Text.from_markup(f"[dim]Watching {row['run_id']} · pid {row['pid']} · "
                                          f"{'finished' if row['finished'] else 'running'}[/dim]{stale}")
                live.update(Group(header, view.render()))
                if row["finished"]:
                    break
                _time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        db.close()


@app.command("version")
def version_cmd() -> None:
    console.print(f"nsmpa {__version__}")


def main() -> None:  # pragma: no cover
    try:
        app()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted. Completed work is saved; run `nsmpa resume` to continue.[/yellow]")
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()

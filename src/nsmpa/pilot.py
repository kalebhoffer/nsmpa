"""`nsmpa pilot`: one capped, end-to-end trial run with a plain-language "is it working?" report.

Steps (each isolated: a failure is recorded and the pilot continues):
  1. readiness checks (search key, AI key, sender config)       5. AI second opinion (if a key is set)
  2. seed universes + estimate                                  6. accuracy sample for you to label
  3. student discovery on a few institutions + promote          7. redacted packet + offline dashboard
  4. research on a few support orgs, newsrooms and the student publications found (includes archive + Wayback)
The search budget is split between discovery (~30%) and research (~70%) and never exceeded.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .db import Database

DEFAULT_SCHOOLS = ["235097", "236939"]


class _SkipDiscovery(Exception):
    pass  # Eastern Washington University, Washington State University


def _pick_entities(db: Database, cohort: str, n: int, prefer: list[str] | None = None) -> list[int]:
    ids: list[int] = []
    for name in prefer or []:
        r = db.execute("SELECT id FROM research_entities WHERE cohort=? AND name=? AND active=1", (cohort, name)).fetchone()
        if r and len(ids) < n:
            ids.append(int(r["id"]))
    for r in db.execute("SELECT id FROM research_entities WHERE cohort=? AND active=1 AND merged_into IS NULL ORDER BY id", (cohort,)):
        if len(ids) >= n:
            break
        if int(r["id"]) not in ids:
            ids.append(int(r["id"]))
    return ids


async def run_pilot(db: Database, settings: Settings, *, budget: int = 25, ai_calls: int = 10, support: int = 1, newsrooms: int = 2,
                    schools: int = 2, unitids: list[str] | None = None, quiet: bool = False, verbose: bool = False,
                    provider=None, fetcher_factory=None, ai_client=None, out_dir: Path | None = None) -> dict:
    from .ai_review import AIUnavailable, run_ai_review
    from .audit import create_sample
    from .discovery import discover_all, promote_candidates
    from .estimate import estimate_all
    from .gui import write_dashboard
    from .legal import seed_legal
    from .packet import build_packet
    from .research import research_all, start_research, sync_student_entities
    from .seeds import seed_experts, seed_precedents
    from .support_orgs import seed_support_orgs
    from .benchmarks import seed_benchmark_newsrooms

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(out_dir or settings.output_dir) / f"pilot_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    steps: list[dict] = []
    report: dict = {"started": stamp, "budget": budget, "steps": steps}

    def step(name: str, status: str, detail: str = "", **data) -> None:
        steps.append({"step": name, "status": status, "detail": detail, **data})

    # 1. readiness
    has_search = bool(os.getenv("SERPER_API_KEY")) or provider is not None
    has_ai = ai_client is not None or bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")) \
        if settings.ai_provider == "gemini" else bool(os.getenv("ANTHROPIC_API_KEY")) or ai_client is not None
    step("readiness", "ok" if has_search else "warning",
         ("search key found" if has_search else "SERPER_API_KEY missing: searches skipped, most results will be UNDETERMINED")
         + ("; AI key found" if has_ai else "; no AI key: AI step skipped"))

    # 2. seed + estimate
    try:
        seed_support_orgs(db), seed_benchmark_newsrooms(db), seed_precedents(db), seed_experts(db), seed_legal(db)
        school_ids = (unitids or DEFAULT_SCHOOLS)[:schools]
        est = estimate_all(db, settings, entity_ids=_pick_entities(db, "support_org", support, ["Student Press Law Center"])
                           + _pick_entities(db, "professional_newsroom", newsrooms), institutions=None)
        step("seed_and_estimate", "ok", f"research estimate {est['research']['credits_low']}–{est['research']['credits_high']} credits",
             estimate=est)
    except Exception as exc:
        school_ids = (unitids or DEFAULT_SCHOOLS)[:schools]
        step("seed_and_estimate", "failed", f"{type(exc).__name__}: {exc}")

    # 3. discovery (skipped for institutions whose paper is already known, e.g. from the directory import)
    disc_budget = max(0, int(budget * 0.3))
    spent = 0
    known = {r[0] for r in db.execute(f"SELECT unitid FROM publications WHERE is_primary=1 AND unitid IN ({','.join('?' * len(school_ids))})",
                                       school_ids)} if school_ids else set()
    to_discover = [u for u in school_ids if u not in known]
    try:
        if not to_discover:
            raise _SkipDiscovery()
        kw = {"provider": provider} if provider is not None else {}
        if fetcher_factory:
            kw["fetcher"] = fetcher_factory(settings)
        d = await discover_all(db, settings, unitids=to_discover, run_id=f"pilot-discovery-{stamp}", quiet=quiet, verbose=verbose,
                               max_searches=disc_budget, command="nsmpa pilot", **kw)
        spent += int(d.get("credits_estimated", 0))
        p = promote_candidates(db, settings.publication_confidence_threshold, settings.publication_ambiguity_margin)
        sync_student_entities(db)
        found = db.execute(f"SELECT i.name, p.name AS pub, p.homepage_url, p.confidence FROM publications p JOIN institutions i "
                           f"ON i.unitid=p.unitid WHERE p.is_primary=1 AND p.unitid IN ({','.join('?' * len(school_ids))})",
                           school_ids).fetchall()
        step("discovery", "ok" if found else "warning", f"{len(found)}/{len(school_ids)} student publications identified",
             credits=d.get("credits_estimated"), publications=[dict(r) for r in found], promoted=p)
    except _SkipDiscovery:
        step("discovery", "ok", f"skipped: all {len(school_ids)} institution(s) already have a matched paper (no credits spent)")
    except Exception as exc:
        step("discovery", "failed", f"{type(exc).__name__}: {exc}")

    # 4. research
    rid = f"pilot-research-{stamp}"
    try:
        student_ids = [int(r["id"]) for r in db.execute(
            f"SELECT id FROM research_entities WHERE cohort='student_media' AND json_extract(metadata_json,'$.unitid') IN "
            f"({','.join('?' * len(school_ids))})", school_ids)]
        ids = (_pick_entities(db, "support_org", support, ["Student Press Law Center"])
               + _pick_entities(db, "professional_newsroom", newsrooms) + student_ids)
        start_research(db, settings, cohort=None, limit=None, run_id=rid, max_searches=budget - spent, command="nsmpa pilot")
        kw = {"provider": provider} if provider is not None else {}
        if fetcher_factory:
            kw["fetcher"] = fetcher_factory(settings)
        r = await research_all(db, settings, rid, entity_ids=ids, quiet=quiet, verbose=verbose, max_searches=max(0, budget - spent), **kw)
        spent += int(r.get("credits_estimated", 0))
        detail = f"{r['completed']} of {len(ids)} researched"
        if r["status"] == "budget_exhausted":
            detail += (f"; stopped at the search budget ({r['remaining']} not finished). Re-run with a larger --budget "
                       f"or continue with `nsmpa research --run-id {rid} --max-searches N` (paid searches are reused)")
        elif r["status"] != "completed":
            detail += f"; status {r['status']}"
        step("research", "ok" if r["status"] == "completed" else "warning", detail, credits=r.get("credits_estimated"), result=r)
    except Exception as exc:
        step("research", "failed", f"{type(exc).__name__}: {exc}")

    # 5. AI second opinion
    if has_ai and ai_calls > 0:
        try:
            a = await run_ai_review(db, settings, run_id=rid, max_calls=ai_calls, quiet=quiet, verbose=verbose, client=ai_client)
            step("ai_review", "ok" if a["failed"] == 0 else "warning",
                 f"{a['reviewed']} pages reviewed, {a['verified']} verified findings, {a['disagreements']} disagreements, "
                 f"{a['unverified_discarded']} unverifiable quotes discarded", result=a)
        except AIUnavailable as exc:
            step("ai_review", "skipped", str(exc))
        except Exception as exc:
            step("ai_review", "failed", f"{type(exc).__name__}: {exc}")
    else:
        step("ai_review", "skipped", "no AI key or --ai-calls 0")

    # 6. accuracy sample
    try:
        n_ev = db.scalar("SELECT COUNT(*) FROM evidence_items WHERE run_id=? AND statement_type!='mention'", (rid,))
        if n_ev:
            smp = create_sample(db, min(20, n_ev), description=f"pilot {stamp}")
            step("accuracy_sample", "ok", f"{smp['sample_size']} items to label: nsmpa audit label --labeler YOURNAME", sample=smp)
        else:
            step("accuracy_sample", "warning", "no substantive evidence to sample yet")
    except Exception as exc:
        step("accuracy_sample", "failed", f"{type(exc).__name__}: {exc}")

    # 7. outputs
    try:
        pk = build_packet(db, settings, out, run_id=None, redact=True)
        dash = write_dashboard(db, settings, out / "dashboard.html", redact=True)
        step("outputs", "ok", f"redacted packet and dashboard in {out}", packet=pk, dashboard=str(dash))
    except Exception as exc:
        step("outputs", "failed", f"{type(exc).__name__}: {exc}")

    report.update(_findings(db, rid))
    report["credits_spent"] = spent
    report["out_dir"] = str(out)
    (out / "pilot_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out / "pilot_report.md").write_text(render_markdown(report), encoding="utf-8")
    return report


def _findings(db: Database, rid: str) -> dict:
    q = lambda sql, p=(): [dict(r) for r in db.execute(sql, p)]  # noqa: E731
    return {
        "stances": q("SELECT re.cohort, re.name, s.stance, s.relief_mode, s.confidence, s.rationale FROM entity_stances s "
                     "JOIN research_entities re ON re.id=s.entity_id WHERE s.run_id=? ORDER BY re.cohort, re.name", (rid,)),
        "evidence_by_class": q("SELECT evidence_class, direction, COUNT(*) n FROM evidence_items WHERE run_id=? AND duplicate_of IS NULL "
                               "AND statement_type!='mention' GROUP BY 1,2 ORDER BY n DESC", (rid,)),
        "access": q("SELECT access_class, COUNT(*) n FROM research_pages WHERE run_id=? GROUP BY 1 ORDER BY n DESC", (rid,)),
        "wayback": q("SELECT status, COUNT(*) n FROM wayback_checks WHERE run_id=? GROUP BY 1", (rid,)),
        "top_supportive": q("SELECT re.name, e.statement_type, e.excerpt, e.source_url FROM evidence_items e JOIN research_entities re "
                            "ON re.id=e.entity_id WHERE e.run_id=? AND e.direction='supportive' AND e.duplicate_of IS NULL "
                            "ORDER BY e.relevance_score*e.authority_score DESC LIMIT 5", (rid,)),
        "top_adverse": q("SELECT re.name, e.statement_type, e.excerpt, e.source_url FROM evidence_items e JOIN research_entities re "
                         "ON re.id=e.entity_id WHERE e.run_id=? AND e.direction='adverse' AND e.duplicate_of IS NULL "
                         "ORDER BY e.relevance_score*e.authority_score DESC LIMIT 5", (rid,)),
        "errors": q("SELECT stage, error_type, COUNT(*) n FROM errors WHERE research_run_id LIKE 'pilot-%' GROUP BY 1,2"),
    }


def render_markdown(r: dict) -> str:
    icon = {"ok": "✅", "warning": "⚠️", "failed": "❌", "skipped": "⏭️"}
    L = [f"# NSMPA pilot report ({r['started']})", "", f"Search budget {r['budget']} credits; spent {r.get('credits_spent', 0)}.", "",
         "## Steps", ""]
    for s in r["steps"]:
        L.append(f"- {icon.get(s['status'], '•')} **{s['step']}**: {s['detail']}")
    L += ["", "## Stances found (automated; verify before relying on them)", "", "| Group | Organization | Stance | Relief mode | Confidence |", "|---|---|---|---|---|"]
    L += [f"| {x['cohort']} | {x['name']} | {x['stance']} | {x['relief_mode']} | {x['confidence']:.2f} |" for x in r.get("stances", [])]
    L += ["", "## Page access", ""] + [f"- {x['access_class']}: {x['n']}" for x in r.get("access", [])]
    L += ["", "## Evidence by class", ""] + [f"- {x['evidence_class']} / {x['direction']}: {x['n']}" for x in r.get("evidence_by_class", [])]
    L += ["", "## Wayback checks", ""] + ([f"- {x['status']}: {x['n']}" for x in r.get("wayback", [])] or ["- none"])
    for key, title in (("top_supportive", "Strongest supportive excerpts"), ("top_adverse", "Strongest opposing excerpts")):
        L += ["", f"## {title}", ""] + ([f"- **{x['name']}** ({x['statement_type']}): {x['excerpt'][:240]} — {x['source_url']}"
                                          for x in r.get(key, [])] or ["- none"])
    L += ["", "## Is it working? Check these", "",
          "1. Open each excerpt above: is it real policy/practice language, correctly labelled supportive/opposing?",
          "2. `nsmpa audit label --labeler YOURNAME`, then `nsmpa audit report` for measured accuracy.",
          "3. `nsmpa gui` → Review tab; `nsmpa errors` for anything that failed.",
          "4. Were the student publications identified correctly? (discovery step above)",
          "5. Only after this looks right, scale up with `docs/scale_runbook.md`.", ""]
    return "\n".join(L)

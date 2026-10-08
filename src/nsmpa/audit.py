"""Accuracy audit: measure how often the classifier is right, and how often two people agree.

1. ``nsmpa audit sample --n 50`` draws a reproducible (seeded), stratified sample of substantive evidence:
   strata = machine direction (supportive / adverse / neutral), allocated as evenly as availability allows,
   so rare-but-important adverse items are not swamped by common ones.
2. Label in Excel (``audit export`` -> fill columns -> ``audit import``) or in the terminal (``audit label``).
   Labels: is this real evidence about post-publication relief? which direction is it, really?
3. ``nsmpa audit report``: relevance precision, direction accuracy (overall and per machine direction) with
   Wilson 95% intervals, a confusion matrix, Cohen's kappa between two labelers, and AI-vs-human accuracy
   when AI second opinions exist for sampled items.
"""
from __future__ import annotations

import csv
import json
import math
import random
import uuid
from collections import Counter, defaultdict
from pathlib import Path

from .db import Database

DIRECTIONS = ("supportive", "adverse", "neutral")
SAMPLE_WHERE = ("e.duplicate_of IS NULL AND e.statement_type NOT IN ('mention','technical_sitewide_noindex') "
                "AND e.run_id NOT IN (SELECT id FROM research_runs WHERE status='excluded')")


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float, float] | None:
    if n == 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return round(p, 3), round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)


def cohen_kappa(pairs: list[tuple[str, str]]) -> float | None:
    if not pairs:
        return None
    n = len(pairs)
    po = sum(1 for a, b in pairs if a == b) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[c] * cb[c] for c in set(ca) | set(cb)) / (n * n)
    return round((po - pe) / (1 - pe), 3) if pe < 1 else 1.0


def create_sample(db: Database, n: int = 50, *, seed: int | None = None, cohort: str | None = None,
                  description: str = "") -> dict:
    seed = seed if seed is not None else random.randrange(1, 10**9)
    rng = random.Random(seed)
    sql = f"SELECT e.id, e.direction FROM evidence_items e WHERE {SAMPLE_WHERE}"
    params: list = []
    if cohort:
        sql += " AND e.cohort=?"
        params.append(cohort)
    by_dir: dict[str, list[int]] = defaultdict(list)
    for r in db.execute(sql + " ORDER BY e.id", params):
        by_dir[r["direction"]].append(r["id"])
    for ids in by_dir.values():
        rng.shuffle(ids)
    picked: list[tuple[int, str]] = []
    # Round-robin across strata so each direction is represented as evenly as availability allows.
    while len(picked) < n and any(by_dir.values()):
        for d in DIRECTIONS:
            if by_dir.get(d) and len(picked) < n:
                picked.append((by_dir[d].pop(), d))
    aid = f"audit-{uuid.uuid4().hex[:8]}"
    with db.transaction():
        db.conn.execute("INSERT INTO audits(id,description,sample_size,seed) VALUES(?,?,?,?)", (aid, description, len(picked), seed))
        db.conn.executemany("INSERT INTO audit_items(audit_id,evidence_id,stratum) VALUES(?,?,?)", [(aid, e, d) for e, d in picked])
    return {"audit_id": aid, "sample_size": len(picked), "seed": seed, "strata": dict(Counter(d for _, d in picked))}


def latest_audit(db: Database) -> str | None:
    return db.scalar("SELECT id FROM audits ORDER BY created_at DESC, rowid DESC LIMIT 1", default=None)


ITEM_SQL = """SELECT ai.id AS audit_item_id, e.id AS evidence_id, e.cohort, re.name AS entity, e.excerpt, e.context,
                     e.source_url, e.statement_type AS machine_statement_type, e.direction AS machine_direction
              FROM audit_items ai JOIN evidence_items e ON e.id=ai.evidence_id JOIN research_entities re ON re.id=e.entity_id
              WHERE ai.audit_id=? ORDER BY ai.id"""


def export_csv(db: Database, audit_id: str, path: Path, *, blind: bool = True) -> int:
    """Blind mode (default) hides the machine's answer so the labeler is not anchored by it."""
    rows = db.execute(ITEM_SQL, (audit_id,)).fetchall()
    cols = ["audit_item_id", "cohort", "entity", "excerpt", "context", "source_url"]
    if not blind:
        cols += ["machine_statement_type", "machine_direction"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols + ["relevant (y/n)", "direction (supportive/adverse/neutral)", "note"])
        for r in rows:
            w.writerow([r[c] for c in cols] + ["", "", ""])
    return len(rows)


def _norm_dir(v: str) -> str | None:
    v = (v or "").strip().lower()
    for d in DIRECTIONS:
        if v and d.startswith(v[:3]):
            return d
    return None


def record_label(db: Database, audit_item_id: int, labeler: str, relevant: bool | None, direction: str | None, note: str = "") -> None:
    db.execute("""INSERT INTO audit_labels(audit_item_id,labeler,relevant,direction,note) VALUES(?,?,?,?,?)
                  ON CONFLICT(audit_item_id,labeler) DO UPDATE SET relevant=excluded.relevant,direction=excluded.direction,
                    note=excluded.note,labeled_at=CURRENT_TIMESTAMP""",
               (audit_item_id, labeler, None if relevant is None else int(relevant), direction, note or None))


def import_csv(db: Database, path: Path, labeler: str) -> dict:
    n = skipped = 0
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            rel = (row.get("relevant (y/n)") or "").strip().lower()
            d = _norm_dir(row.get("direction (supportive/adverse/neutral)") or "")
            if not rel and not d:
                skipped += 1
                continue
            record_label(db, int(row["audit_item_id"]), labeler, rel.startswith("y") if rel else None, d, row.get("note", ""))
            n += 1
    db.conn.commit()
    return {"labeled": n, "blank_rows_skipped": skipped}


def report(db: Database, audit_id: str) -> dict:
    items = {r["audit_item_id"]: r for r in db.execute(ITEM_SQL, (audit_id,))}
    labels: dict[int, dict[str, dict]] = defaultdict(dict)
    for lab in db.execute("SELECT * FROM audit_labels WHERE audit_item_id IN (SELECT id FROM audit_items WHERE audit_id=?)", (audit_id,)):
        labels[lab["audit_item_id"]][lab["labeler"]] = dict(lab)
    labelers = sorted({lb for d in labels.values() for lb in d})
    primary = labelers[0] if labelers else None
    out: dict = {"audit_id": audit_id, "sample_size": len(items), "labelers": labelers, "labeled_items": len(labels)}
    if not primary:
        return out
    rel_k = rel_n = dir_k = dir_n = 0
    per_dir: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    confusion: dict[str, Counter] = defaultdict(Counter)
    for iid, labs in labels.items():
        lab = labs.get(primary)
        if not lab or iid not in items:
            continue
        machine = items[iid]["machine_direction"]
        if lab["relevant"] is not None:
            rel_n += 1
            rel_k += int(bool(lab["relevant"]))
        if lab["direction"] and lab["relevant"] != 0:
            dir_n += 1
            ok = lab["direction"] == machine
            dir_k += int(ok)
            per_dir[machine][0] += int(ok)
            per_dir[machine][1] += 1
            confusion[machine][lab["direction"]] += 1
    out["relevance_precision"] = wilson(rel_k, rel_n)
    out["direction_accuracy"] = wilson(dir_k, dir_n)
    out["direction_accuracy_by_machine_label"] = {d: wilson(k, n) for d, (k, n) in per_dir.items()}
    out["confusion_machine_vs_human"] = {m: dict(c) for m, c in confusion.items()}
    if len(labelers) >= 2:
        a, b = labelers[0], labelers[1]
        pairs = [(labs[a]["direction"], labs[b]["direction"]) for labs in labels.values()
                 if a in labs and b in labs and labs[a]["direction"] and labs[b]["direction"]]
        out["inter_rater"] = {"labelers": [a, b], "items": len(pairs), "cohen_kappa_direction": cohen_kappa(pairs),
                              "raw_agreement": wilson(sum(1 for x, y in pairs if x == y), len(pairs))}
    # AI vs human, where AI findings match sampled evidence.
    ai_k = ai_n = 0
    for iid, labs in labels.items():
        lab = labs.get(primary)
        if not lab or not lab["direction"]:
            continue
        f = db.execute("SELECT direction FROM ai_findings WHERE matched_evidence_id=? AND quote_verified=1 ORDER BY id DESC LIMIT 1",
                       (items[iid]["evidence_id"],)).fetchone()
        if f:
            ai_n += 1
            ai_k += int(f["direction"] == lab["direction"])
    out["ai_direction_accuracy"] = wilson(ai_k, ai_n)
    return out


def label_interactive(db: Database, audit_id: str, labeler: str, *, show_machine: bool = False) -> int:
    """Terminal labeling loop: y/n relevance, s/a/n direction, Enter to skip, q to quit (progress is saved)."""
    from rich.console import Console
    from rich.panel import Panel
    console = Console()
    done = {r[0] for r in db.execute("SELECT audit_item_id FROM audit_labels WHERE labeler=?", (labeler,))}
    rows = [r for r in db.execute(ITEM_SQL, (audit_id,)) if r["audit_item_id"] not in done]
    n = 0
    for i, r in enumerate(rows, start=1):
        body = f"[bold]{r['entity']}[/bold] ({r['cohort']})\n\n{r['excerpt']}\n\n[dim]{r['context'][:600]}[/dim]\n\n{r['source_url']}"
        if show_machine:
            body += f"\n\n[yellow]Machine: {r['machine_direction']} / {r['machine_statement_type']}[/yellow]"
        console.print(Panel(body, title=f"Item {i}/{len(rows)}"))
        rel = console.input("Real evidence about removing/de-indexing/anonymizing/updating articles? [y/n, Enter=skip, q=quit] ").strip().lower()
        if rel == "q":
            break
        if not rel:
            continue
        direction = None
        if rel.startswith("y"):
            d = console.input("Direction? [s]upportive / [a]dverse / [n]eutral ").strip().lower()
            direction = {"s": "supportive", "a": "adverse", "n": "neutral"}.get(d[:1])
        note = console.input("Note (optional): ").strip()
        record_label(db, r["audit_item_id"], labeler, rel.startswith("y"), direction, note)
        db.conn.commit()
        n += 1
    return n


def summary_for_packet(db: Database) -> dict | None:
    aid = db.scalar("""SELECT a.id FROM audits a WHERE EXISTS (SELECT 1 FROM audit_items i JOIN audit_labels l ON l.audit_item_id=i.id
                       WHERE i.audit_id=a.id) ORDER BY a.created_at DESC, a.rowid DESC LIMIT 1""", default=None)
    if not aid:
        return None
    rep = report(db, aid)
    return rep if rep.get("labeled_items") else None


def _fmt(w) -> str:
    return "–" if not w else f"{w[0]:.0%} (95% CI {w[1]:.0%}–{w[2]:.0%})"


def format_report(rep: dict) -> str:
    lines = [f"Audit {rep['audit_id']}: {rep.get('labeled_items', 0)}/{rep['sample_size']} labeled by {', '.join(rep['labelers']) or 'nobody yet'}"]
    if "direction_accuracy" in rep:
        lines += [f"Relevance precision: {_fmt(rep['relevance_precision'])}",
                  f"Direction accuracy:  {_fmt(rep['direction_accuracy'])}"]
        for d, w in rep["direction_accuracy_by_machine_label"].items():
            lines.append(f"  when machine said {d}: {_fmt(w)}")
        lines.append("Confusion (machine -> human): " + json.dumps(rep["confusion_machine_vs_human"]))
        if rep.get("inter_rater"):
            ir = rep["inter_rater"]
            lines.append(f"Inter-rater ({' vs '.join(ir['labelers'])}, n={ir['items']}): kappa={ir['cohen_kappa_direction']}, "
                         f"raw agreement {_fmt(ir['raw_agreement'])}")
        if rep.get("ai_direction_accuracy"):
            lines.append(f"AI second-opinion direction accuracy: {_fmt(rep['ai_direction_accuracy'])}")
    return "\n".join(lines)

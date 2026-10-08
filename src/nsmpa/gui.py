"""Lightweight local GUI: `nsmpa gui` (live monitor + results + evidence + review) and `nsmpa dashboard`
(the same UI as one offline, read-only HTML file).

Standard library only (http.server). Security:
- binds to 127.0.0.1 only; requests with any other Host header are refused (DNS-rebinding guard);
- state-changing requests (review decisions) require a per-launch token sent in a custom header, which a
  cross-site page cannot set (CSRF guard); GET endpoints are read-only;
- every request uses its own SQLite connection, so the GUI never blocks a running job.
"""
from __future__ import annotations

import json
import secrets
import socketserver
import sqlite3
import threading
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .config import Settings
from .db import Database

TEMPLATE = Path(__file__).with_name("gui_template.html")
ACTIVE_SECONDS = 60
EXCL = "(SELECT id FROM research_runs WHERE status='excluded')"


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def _q(db: Database, sql: str, params=()) -> list[dict]:
    return _rows(db.execute(sql, params))


# =========================================================================== data API

def api_overview(db: Database, settings: Settings) -> dict:
    s = db.scalar
    hbs = _q(db, """SELECT run_id, title, state_json, pid, finished, started_at, updated_at,
                           CAST((julianday('now') - julianday(updated_at)) * 86400 AS INTEGER) AS age_seconds
                    FROM run_heartbeats ORDER BY updated_at DESC LIMIT 12""")
    for h in hbs:
        h["state"] = json.loads(h.pop("state_json") or "{}")
        h["active"] = (not h["finished"]) and h["age_seconds"] is not None and h["age_seconds"] < ACTIVE_SECONDS
    runs = _q(db, """SELECT id, mode, status, status_reason, engine_version, started_at, completed_at, searches_live,
                            searches_cached, credits_estimated,
                            (SELECT COUNT(*) FROM run_items i WHERE i.run_id=r.id AND i.status='done') AS done,
                            (SELECT COUNT(*) FROM run_items i WHERE i.run_id=r.id) AS total
                     FROM research_runs r ORDER BY COALESCE(last_checkpoint_at, started_at) DESC LIMIT 15""")
    return {
        "version": __version__, "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "database": str(settings.database_path), "schema": db.schema_version(),
        "counts": {
            "institutions": s("SELECT COUNT(*) FROM institutions WHERE included=1"),
            "publications": s("SELECT COUNT(*) FROM publications WHERE is_primary=1"),
            "entities": s("SELECT COUNT(*) FROM research_entities WHERE active=1 AND cohort NOT IN ('expert','precedent_case')"),
            "researched": s(f"SELECT COUNT(DISTINCT entity_id) FROM entity_stances WHERE stance_version='0.3' AND run_id NOT IN {EXCL}"),
            "evidence": s(f"SELECT COUNT(DISTINCT near_dup_key) FROM evidence_items WHERE statement_type!='mention' AND run_id NOT IN {EXCL}"),
            "voices": s("SELECT COUNT(*) FROM voices"),
            "open_reviews": s(f"SELECT COUNT(*) FROM review_queue WHERE status='open' AND run_id NOT IN {EXCL}"),
            "errors": s("SELECT COUNT(*) FROM errors"),
            "credits": s("SELECT SUM(credits_estimated) FROM search_queries"),
            "cache_hits": s("SELECT COUNT(*) FROM search_queries WHERE was_cached=1"),
        },
        "heartbeats": hbs, "runs": runs,
    }


def api_results(db: Database, settings: Settings) -> dict:
    from .validate import all_cohorts, cohort_metrics, latest_stances_sql
    sub, sp = latest_stances_sql(None)
    out = []
    for c in all_cohorts(db):
        m = cohort_metrics(db, settings, c)
        modes = {r["relief_mode"] or "UNADDRESSED": r["n"] for r in db.execute(
            f"SELECT s.relief_mode, COUNT(*) n FROM ({sub}) s JOIN research_entities re ON re.id=s.entity_id "
            f"WHERE re.cohort=? AND COALESCE(json_extract(re.metadata_json,'$.excluded_from_rates'), 0) IN (0, 'false') "
            f"GROUP BY s.relief_mode", sp + [c])}
        out.append({"cohort": c, "label": m.label, "denominator": m.denominator, "researched": m.researched,
                    "valid": m.valid_for_percentages, "stances": m.stance_counts, "relief_modes": modes,
                    "gates": [{"name": g.name, "value": g.value, "threshold": g.threshold, "comparator": g.comparator,
                               "passed": g.passed} for g in m.gates]})
    return {"cohorts": out}


def api_evidence(db: Database, params: dict) -> dict:
    where = ["e.duplicate_of IS NULL", "e.statement_type!='mention'",
             "e.run_id NOT IN (SELECT id FROM research_runs WHERE status='excluded')"]
    args: list = []
    if params.get("q"):
        where.append("(e.excerpt LIKE ? OR re.name LIKE ?)")
        args += [f"%{params['q']}%"] * 2
    for key, col in (("cohort", "e.cohort"), ("direction", "e.direction"), ("type", "e.statement_type"),
                     ("klass", "e.evidence_class"), ("verified", "e.verification_status")):
        if params.get(key):
            where.append(f"{col}=?")
            args.append(params[key])
    order = {"match": "e.case_match_score DESC", "authority": "e.authority_score DESC", "recent": "e.id DESC"}.get(
        params.get("sort", "match"), "e.case_match_score DESC")
    limit = min(int(params.get("limit", 100) or 100), 500)
    offset = max(int(params.get("offset", 0) or 0), 0)
    w = " AND ".join(where)
    total = db.scalar(f"SELECT COUNT(*) FROM evidence_items e JOIN research_entities re ON re.id=e.entity_id WHERE {w}", args)
    rows = _q(db, f"""SELECT e.id, e.cohort, re.name AS entity, re.parent_name, e.direction, e.statement_type, e.evidence_class,
                             e.actions_json, e.case_match_score, e.authority_score, e.verification_status, e.excerpt,
                             e.source_url, e.fetched_at, e.page_sha256, e.rationale, e.acquisition, e.archive_ts, e.archive_url
                      FROM evidence_items e JOIN research_entities re ON re.id=e.entity_id WHERE {w}
                      ORDER BY {order}, e.id LIMIT ? OFFSET ?""", args + [limit, offset])
    return {"total": total, "rows": rows}


def api_review(db: Database) -> dict:
    from .review import open_items
    return {"items": [dict(r) for r in open_items(db, limit=200)]}


def api_ops(db: Database) -> dict:
    return {
        "errors": _q(db, "SELECT stage, error_type, COUNT(*) n FROM errors GROUP BY 1,2 ORDER BY n DESC LIMIT 30"),
        "access": _q(db, "SELECT COALESCE(access_class,status) access, COUNT(*) n FROM research_pages GROUP BY 1 ORDER BY n DESC"),
        "failed_items": _q(db, "SELECT run_id, item_type, item_key, error, completed_at FROM run_items WHERE status='failed' "
                               "ORDER BY completed_at DESC LIMIT 30"),
        "queries": _q(db, """SELECT purpose, COUNT(*) n, SUM(was_cached) cached, SUM(status!='completed') failed,
                                    SUM(credits_estimated) credits, SUM(produced_evidence) useful
                             FROM search_queries GROUP BY purpose ORDER BY n DESC LIMIT 40"""),
        "recent_queries": _q(db, "SELECT created_at, purpose, query, status, was_cached, result_count, produced_evidence "
                                 "FROM search_queries ORDER BY id DESC LIMIT 40"),
    }


def api_insights(db: Database) -> dict:
    from .audit import format_report, summary_for_packet
    audit = summary_for_packet(db)
    return {
        "wayback": {"by_status": _q(db, "SELECT status, COUNT(*) n FROM wayback_checks GROUP BY status ORDER BY n DESC"),
                    "changed": _q(db, """SELECT re.name AS entity, w.url, w.earliest_ts, w.archive_url, w.observations_json
                                         FROM wayback_checks w JOIN research_entities re ON re.id=w.entity_id
                                         WHERE w.status='changed' ORDER BY w.id DESC LIMIT 50""")},
        "ai": {"by_agreement": _q(db, "SELECT agreement, COUNT(*) n FROM ai_findings GROUP BY agreement ORDER BY n DESC"),
               "reviews": _q(db, "SELECT status, COUNT(*) n FROM ai_reviews GROUP BY status"),
               "disagreements": _q(db, """SELECT re.name AS entity, f.action, f.position, f.direction, f.quote
                                          FROM ai_findings f JOIN research_entities re ON re.id=f.entity_id
                                          WHERE f.quote_verified=1 AND f.agreement IN ('disagree','ai_only')
                                          ORDER BY f.id DESC LIMIT 50""")},
        "accuracy": {"report": audit, "text": format_report(audit) if audit else None},
        "voices": _q(db, """SELECT v.person_name, COALESCE(x.role, v.role) role, v.direction, v.verification_status, v.quote, v.source_url
                            FROM voices v LEFT JOIN experts x ON x.id=v.expert_id
                            ORDER BY v.expert_id IS NULL, v.case_match_score DESC LIMIT 60"""),
        "precedents": _q(db, "SELECT organization, title, approx_year, status, prior_confidence FROM precedent_seeds ORDER BY id"),
        "legal": _q(db, "SELECT jurisdiction, topic, title, status, citation, best_source FROM legal_context ORDER BY jurisdiction, id"),
        "policy_changes": _q(db, """SELECT re.name AS entity, w.url, c.detected_at, c.similarity, c.added_relief_statements,
                                           c.removed_relief_statements, c.summary
                                    FROM policy_changes c JOIN policy_watch w ON w.id=c.watch_id
                                    JOIN research_entities re ON re.id=w.entity_id ORDER BY c.id DESC LIMIT 50"""),
        "outreach": _q(db, "SELECT campaign, status, COUNT(*) n FROM outreach_messages GROUP BY campaign, status"),
    }


def api_entity(db: Database, entity_id: int) -> dict:
    ent = db.execute("SELECT * FROM research_entities WHERE id=?", (entity_id,)).fetchone()
    if not ent:
        return {}
    stance = db.execute("SELECT * FROM entity_stances WHERE entity_id=? ORDER BY id DESC LIMIT 1", (entity_id,)).fetchone()
    return {"entity": dict(ent), "stance": dict(stance) if stance else None,
            "evidence": _q(db, "SELECT id, direction, statement_type, evidence_class, excerpt, source_url FROM evidence_items "
                               "WHERE entity_id=? AND duplicate_of IS NULL AND statement_type!='mention' ORDER BY authority_score DESC LIMIT 100",
                           (entity_id,)),
            "pages": _q(db, "SELECT page_kind, access_class, final_url, noindex FROM research_pages WHERE entity_id=? ORDER BY id DESC LIMIT 100",
                        (entity_id,))}


VERIFY_STATUSES = {"verified", "rejected", "disputed"}


def api_verify(db: Database, params: dict) -> dict:
    """Unverified items in priority order: the excerpts that drive determinate stances first, then closest to your case."""
    kind = params.get("kind", "evidence")
    limit = min(int(params.get("limit", 40) or 40), 200)
    if kind == "voice":
        rows = _q(db, f"""SELECT v.id, v.person_name AS entity, COALESCE(x.role, v.role) AS role, v.direction, v.statement_type,
                                'voice' AS evidence_class, v.quote AS excerpt, v.context, v.source_url, v.created_at AS fetched_at,
                                NULL AS page_sha256, v.attribution_method, v.page_id
                         FROM voices v LEFT JOIN experts x ON x.id=v.expert_id
                         WHERE v.verification_status='unverified' AND (v.run_id IS NULL OR v.run_id NOT IN {EXCL})
                         ORDER BY v.expert_id IS NULL, v.case_match_score DESC, v.id LIMIT ?""", (limit,))
        counts = _q(db, "SELECT verification_status AS status, COUNT(*) n FROM voices GROUP BY 1")
    else:
        rows = _q(db, f"""SELECT e.id, re.name AS entity, e.cohort, e.direction, e.statement_type, e.evidence_class, e.excerpt,
                                e.context, e.source_url, e.fetched_at, e.page_sha256, e.case_match_score, e.page_id,
                                e.acquisition, e.archive_ts, e.archive_url,
                                (SELECT p.text_sha256 IS NOT NULL FROM research_pages p WHERE p.id=e.page_id) AS has_snapshot,
                                EXISTS (SELECT 1 FROM entity_stances s WHERE s.strongest_supportive_id=e.id OR s.strongest_adverse_id=e.id)
                                  AS drives_stance
                         FROM evidence_items e JOIN research_entities re ON re.id=e.entity_id
                         WHERE e.verification_status='unverified' AND e.duplicate_of IS NULL
                           AND e.statement_type NOT IN ('mention','technical_sitewide_noindex') AND e.run_id NOT IN {EXCL}
                         ORDER BY drives_stance DESC, e.case_match_score DESC, e.authority_score DESC, e.id LIMIT ?""", (limit,))
        counts = _q(db, f"""SELECT verification_status AS status, COUNT(*) n FROM evidence_items
                            WHERE duplicate_of IS NULL AND statement_type NOT IN ('mention','technical_sitewide_noindex')
                              AND run_id NOT IN {EXCL} GROUP BY 1""")
    return {"kind": kind, "items": rows, "counts": {c["status"]: c["n"] for c in counts}}


def api_capture(db: Database) -> dict:
    from .capture import capture_queue, list_captures
    return {
        "queue": capture_queue(db),
        "captures": list_captures(db),
        "leads": _q(db, """SELECT l.id, l.entity_id, re.name AS entity, l.status, l.confirmed_via, l.claimed_url, l.confirmed_url,
                                  l.quote, l.summary, l.created_at
                           FROM ai_leads l JOIN research_entities re ON re.id=l.entity_id
                           ORDER BY l.status='unconfirmed' DESC, l.id DESC LIMIT 200"""),
    }


def api_entities(db: Database, params: dict) -> dict:
    q = (params.get("q") or "").strip()
    if len(q) < 2:
        return {"items": []}
    return {"items": _q(db, """SELECT id, name, cohort, parent_name, homepage_url FROM research_entities
                               WHERE active=1 AND merged_into IS NULL AND (name LIKE ? OR parent_name LIKE ? OR homepage_url LIKE ?)
                               ORDER BY cohort, name LIMIT 25""", (f"%{q}%",) * 3)}


def snapshot_html(db: Database, kind: str, item_id: int) -> str:
    """The saved, analysed page text with the excerpt highlighted (anchor #hit). No scripts."""
    import html as _html
    if kind == "voice":
        row = db.execute("SELECT quote AS excerpt, page_id, source_url FROM voices WHERE id=?", (item_id,)).fetchone()
    else:
        row = db.execute("SELECT excerpt, page_id, source_url, evidence_class FROM evidence_items WHERE id=?", (item_id,)).fetchone()
    style = ("<style>body{font:15px/1.6 -apple-system,Arial,sans-serif;margin:16px;color:#222;background:#fff}"
             "mark{background:#ffe08a;padding:2px 0}.meta{color:#666;font-size:12px;margin-bottom:12px}"
             "@media (prefers-color-scheme:dark){body{background:#1a1a19;color:#ddd}.meta{color:#999}mark{background:#6b5300;color:#fff}}</style>")
    if not row:
        return f"<!doctype html><meta charset=utf-8>{style}<p>Not found.</p>"
    page = db.execute("SELECT * FROM research_pages WHERE id=?", (row["page_id"],)).fetchone() if row["page_id"] else None
    text = None
    if page and page["text_sha256"]:
        path = db.scalar("SELECT path FROM snapshots WHERE sha256=? AND kind='text'", (page["text_sha256"],), None)
        if path and Path(path).exists():
            text = Path(path).read_text(encoding="utf-8", errors="replace")
    meta = (f"<div class=meta>Saved copy analysed by NSMPA · fetched {_html.escape(str(page['fetched_at'] if page else '–'))} · "
            f"page SHA-256 {_html.escape(str(page['content_sha256'] if page else '–'))}</div>")
    if kind != "voice" and row["evidence_class"] == "technical":
        directives = (f"meta robots = <b>{_html.escape(str(page['meta_robots'] or 'none'))}</b> · "
                      f"X-Robots-Tag = <b>{_html.escape(str(page['x_robots_tag'] or 'none'))}</b>") if page else "page record missing"
        box = ("<div style='border:1px solid #888;border-radius:6px;padding:10px;margin-bottom:12px'><b>Technical observation, not a "
               "quote.</b> Confirm it from what NSMPA recorded for this page: " + directives + ". For Wayback items, compare the "
               "archived copy with the live page.</div>")
        return f"<!doctype html><meta charset=utf-8>{style}{meta}{box}<div style='white-space:pre-wrap'>{_html.escape(text or '')}</div>"
    if not text:
        note = ("This item is a technical observation (e.g. noindex or Wayback comparison); there is no quoted text. "
                "Use the live source link to check it.") if (kind != "voice" and row["evidence_class"] == "technical") else             "No saved text snapshot for this item; check the live source."
        return f"<!doctype html><meta charset=utf-8>{style}{meta}<p>{_html.escape(note)}</p>"
    ex = row["excerpt"] or ""
    i = text.find(ex)
    if i < 0:
        i = text.lower().find(ex.lower()[:120])
        n = len(ex[:120]) if i >= 0 else 0
    else:
        n = len(ex)
    if i < 0:
        body = "<p class=meta>(Excerpt text not found verbatim in the saved copy — mark as disputed if it cannot be confirmed.)</p>" \
               + _html.escape(text)
    else:
        body = _html.escape(text[:i]) + f"<mark id=hit>{_html.escape(text[i:i + n])}</mark>" + _html.escape(text[i + n:])
    return f"<!doctype html><meta charset=utf-8>{style}{meta}<div style='white-space:pre-wrap'>{body}</div>"


def snapshot_payload(db: Database, settings: Settings) -> dict:
    """Everything the UI needs, for the offline dashboard file."""
    return {"overview": api_overview(db, settings), "results": api_results(db, settings),
            "evidence": api_evidence(db, {"limit": 500}), "review": api_review(db), "ops": api_ops(db),
            "insights": api_insights(db)}


def render_page(token: str | None, embedded: dict | None = None) -> str:
    html = TEMPLATE.read_text(encoding="utf-8")
    cfg = {"token": token, "static": embedded is not None, "version": __version__}
    data = json.dumps(embedded, default=str).replace("</", "<\\/") if embedded is not None else "null"
    return (html.replace("/*__NSMPA_CONFIG__*/null", json.dumps(cfg))
                .replace("/*__NSMPA_DATA__*/null", data))


def redact_payload(db: Database, payload: dict) -> dict:
    from .redact import Redactor, redact_rows
    r = Redactor.from_db(db)
    payload["evidence"]["rows"] = redact_rows(payload["evidence"]["rows"], r)
    payload["review"]["items"] = redact_rows(payload["review"]["items"], r)
    ins = payload["insights"]
    for k in ("voices", "legal", "policy_changes"):
        ins[k] = redact_rows(ins[k], r)
    ins["ai"]["disagreements"] = redact_rows(ins["ai"]["disagreements"], r)
    ins["wayback"]["changed"] = redact_rows(ins["wayback"]["changed"], r)
    payload["ops"]["recent_queries"] = redact_rows(payload["ops"]["recent_queries"], r)
    payload["ops"]["failed_items"] = [dict(x, error="[withheld in shared copy]") for x in payload["ops"]["failed_items"]]
    payload["overview"]["database"] = "[withheld]"
    payload["redacted"] = True
    return payload


def write_dashboard(db: Database, settings: Settings, out: Path, *, redact: bool = False) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = snapshot_payload(db, settings)
    if redact:
        payload = redact_payload(db, payload)
    out.write_text(render_page(None, payload), encoding="utf-8")
    return out


# =========================================================================== server

class _Handler(BaseHTTPRequestHandler):
    settings: Settings
    token: str
    port: int
    server_version = "NSMPA-GUI"

    def log_message(self, fmt, *args):  # quiet by default
        return

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return host in {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def _send(self, status: int, body: bytes, ctype: str, *, frameable: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if frameable:  # saved-copy pages: framed only by our own UI, and never run scripts
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'self'")
        else:
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                             "img-src 'self' data:; connect-src 'self'; frame-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(obj, default=str).encode(), "application/json; charset=utf-8")

    def _db(self) -> Database:
        return Database(self.settings.database_path, migrate=False)

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._send(HTTPStatus.FORBIDDEN, b"forbidden host", "text/plain")
        url = urlsplit(self.path)
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path in {"/", "/index.html"}:
            return self._send(200, render_page(self.token).encode(), "text/html; charset=utf-8")
        if url.path.startswith("/snapshot/"):
            parts = url.path.strip("/").split("/")
            if len(parts) != 3 or parts[1] not in {"evidence", "voice"} or not parts[2].isdigit():
                return self._send(404, b"not found", "text/plain")
            db = self._db()
            try:
                return self._send(200, snapshot_html(db, parts[1], int(parts[2])).encode(), "text/html; charset=utf-8",
                                  frameable=True)
            finally:
                db.close()
        routes = {
            "/api/overview": lambda db: api_overview(db, self.settings),
            "/api/results": lambda db: api_results(db, self.settings),
            "/api/evidence": lambda db: api_evidence(db, params),
            "/api/review": api_review,
            "/api/ops": api_ops,
            "/api/insights": api_insights,
            "/api/verify": lambda db: api_verify(db, params),
            "/api/capture": api_capture,
            "/api/entities": lambda db: api_entities(db, params),
        }
        if url.path.startswith("/api/entity/"):
            try:
                eid = int(url.path.rsplit("/", 1)[1])
            except ValueError:
                return self._json({"error": "bad id"}, 400)
            routes[url.path] = lambda db: api_entity(db, eid)
        fn = routes.get(url.path)
        if not fn:
            return self._json({"error": "not found"}, 404)
        db = self._db()
        try:
            return self._json(fn(db))
        except sqlite3.Error as exc:
            return self._json({"error": f"database: {exc}"}, 500)
        finally:
            db.close()

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._send(HTTPStatus.FORBIDDEN, b"forbidden host", "text/plain")
        if not secrets.compare_digest(self.headers.get("X-NSMPA-Token", ""), self.token):
            return self._json({"error": "missing or bad token"}, 403)
        url = urlsplit(self.path)
        limit = 2_100_000 if url.path == "/api/capture" else 100_000  # captures carry whole pages of pasted text
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            return self._json({"error": f"request too large (max {limit:,} bytes)"}, 413)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json({"error": "bad json"}, 400)
        if url.path == "/api/capture":
            from .capture import CaptureError, add_capture
            db = self._db()
            try:
                res = add_capture(db, self.settings, entity_id=int(body.get("entity_id") or 0), url=str(body.get("url", "")),
                                  text=str(body.get("text", "")), captured_by=str(body.get("captured_by", "")),
                                  title=str(body.get("title", "")), note=str(body.get("note", "")))
                return self._json({"ok": True, **res})
            except (CaptureError, ValueError) as exc:
                return self._json({"error": str(exc)}, 400)
            except sqlite3.Error as exc:
                return self._json({"error": f"database: {exc}"}, 500)
            finally:
                db.close()
        if url.path.startswith("/api/review/"):
            from .review import record_decision
            db = self._db()
            try:
                record_decision(db, int(url.path.rsplit("/", 1)[1]), str(body.get("decision", "")),
                                reviewer=str(body.get("reviewer", "gui"))[:80], note=str(body.get("note", ""))[:2000],
                                corrected_stance=body.get("stance") or None)
                return self._json({"ok": True})
            except (ValueError, sqlite3.Error) as exc:
                return self._json({"error": str(exc)}, 400)
            finally:
                db.close()
        if url.path.startswith("/api/verify/"):
            parts = url.path.strip("/").split("/")
            status = str(body.get("status", ""))
            if len(parts) != 4 or parts[2] not in {"evidence", "voice"} or not parts[3].isdigit() or status not in VERIFY_STATUSES:
                return self._json({"error": "bad request"}, 400)
            db = self._db()
            try:
                table = "evidence_items" if parts[2] == "evidence" else "voices"
                n = db.execute(f"UPDATE {table} SET verification_status=?, reviewer_note=? WHERE id=?",
                               (status, str(body.get("note", ""))[:2000] or None, int(parts[3]))).rowcount
                db.conn.commit()
                return self._json({"ok": bool(n)})
            finally:
                db.close()
        return self._json({"error": "not found"}, 404)


class _LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        # HTTPServer.server_bind() calls socket.getfqdn(), a reverse-DNS lookup that can stall for ~30 s on macOS.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


def make_server(settings: Settings, port: int | None = None) -> tuple[ThreadingHTTPServer, str]:
    port = port if port is not None else settings.gui_port
    token = secrets.token_urlsafe(24)
    handler = type("Handler", (_Handler,), {"settings": settings, "token": token, "port": port})
    httpd = _LocalServer(("127.0.0.1", port), handler)
    handler.port = httpd.server_address[1]
    return httpd, token


def serve(settings: Settings, *, port: int | None = None, open_browser: bool = True) -> None:
    Database(settings.database_path).close()  # ensure schema is current before serving read-only
    httpd, _ = make_server(settings, port)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"NSMPA GUI running at {url}  (Ctrl+C to stop; jobs keep running independently)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

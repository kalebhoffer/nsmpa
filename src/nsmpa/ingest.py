from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from pathlib import Path

from .config import Settings
from .db import Database
from .utils import ensure_scheme


def _open_ipeds_csv(path: Path) -> tuple[io.TextIOBase, zipfile.ZipFile | None]:
    if path.suffix.lower() == ".zip":
        zf = zipfile.ZipFile(path)
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv") and not n.startswith("__MACOSX")]
        if not csv_names:
            zf.close()
            raise ValueError("ZIP contains no CSV file")
        exact = [n for n in csv_names if re.search(r"(?:^|/)HD20\d{2}\.csv$", n, re.I)]
        preferred = exact[0] if exact else sorted(csv_names, key=len)[0]
        raw = zf.open(preferred, "r")
        return io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline=""), zf
    return open(path, encoding="utf-8-sig", errors="replace", newline=""), None


def _to_int(value: str | None) -> int | None:
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


def _included(control: int | None, level: int | None, state: str | None, s: Settings) -> bool:
    f = s.institution_filters
    level_ok = (
        (level == 1 and f.include_four_year)
        or (level == 2 and f.include_two_year)
        or (level == 3 and f.include_less_than_two_year)
    )
    control_ok = (
        (control == 1 and f.include_public)
        or (control == 2 and f.include_private_nonprofit)
        or (control == 3 and f.include_private_for_profit)
    )
    state_ok = not s.allowed_states or (state or "").upper() in {x.upper() for x in s.allowed_states}
    return bool(level_ok and control_ok and state_ok)


def import_ipeds(db: Database, settings: Settings, path: str | Path, source_year: int | None = None) -> dict[str, int]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    if source_year is None:
        m = re.search(r"20\d{2}", p.name)
        source_year = int(m.group(0)) if m else None
    f, zf = _open_ipeds_csv(p)
    total = included = 0
    try:
        reader = csv.DictReader(f)
        required = {"UNITID", "INSTNM"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Not an IPEDS directory CSV; missing columns: {sorted(missing)}")
        rows = []
        for row in reader:
            total += 1
            unitid = (row.get("UNITID") or "").strip()
            if not unitid:
                continue
            control = _to_int(row.get("CONTROL"))
            level = _to_int(row.get("ICLEVEL") or row.get("LEVEL"))
            state = (row.get("STABBR") or "").strip() or None
            keep = _included(control, level, state, settings)
            included += int(keep)
            website = ensure_scheme((row.get("WEBADDR") or "").strip())
            rows.append((
                unitid, (row.get("INSTNM") or "").strip(), (row.get("CITY") or "").strip() or None,
                state, website, control, level, source_year, int(keep),
                json.dumps(row, ensure_ascii=False, sort_keys=True),
            ))
        with db.transaction():
            db.conn.executemany(
                """
                INSERT INTO institutions(unitid,name,city,state,website,control,level,source_year,included,raw_json)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(unitid) DO UPDATE SET
                  name=excluded.name,city=excluded.city,state=excluded.state,website=excluded.website,
                  control=excluded.control,level=excluded.level,source_year=excluded.source_year,
                  included=excluded.included,raw_json=excluded.raw_json,updated_at=CURRENT_TIMESTAMP
                """, rows,
            )
    finally:
        f.close()
        if zf:
            zf.close()
    return {"rows": total, "included": included}

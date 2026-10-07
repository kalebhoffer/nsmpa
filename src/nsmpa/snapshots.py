"""Content-addressed, immutable source snapshots.

Raw bytes are stored at ``<root>/raw/ab/<sha256><ext>`` and normalized main text at
``<root>/text/ab/<sha256>.txt``. Files are named by their own SHA-256, written atomically and
never overwritten, so a stored hash always identifies exactly the bytes that were analysed.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .db import Database
from .utils import sha256_bytes, sha256_text


def _atomic_write(path: Path, data: bytes) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def store_raw(db: Database, root: Path, content: bytes, content_type: str, url: str) -> tuple[str, str]:
    digest = sha256_bytes(content)
    ext = ".pdf" if content_type == "application/pdf" else ".html"
    path = root / "raw" / digest[:2] / f"{digest}{ext}"
    _atomic_write(path, content)
    db.execute(
        "INSERT OR IGNORE INTO snapshots(sha256,kind,path,content_type,bytes,first_url) VALUES(?,?,?,?,?,?)",
        (digest, "raw", str(path), content_type, len(content), url),
    )
    return digest, str(path)


def store_text(db: Database, root: Path, text: str, url: str) -> str:
    data = text.encode("utf-8", errors="replace")
    digest = sha256_text(text)
    path = root / "text" / digest[:2] / f"{digest}.txt"
    _atomic_write(path, data)
    db.execute(
        "INSERT OR IGNORE INTO snapshots(sha256,kind,path,content_type,bytes,first_url) VALUES(?,?,?,?,?,?)",
        (digest, "text", str(path), "text/plain", len(data), url),
    )
    return digest

"""Authoritative transactional SQLite ledger and FTS5 index."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING

from .lexical import fts_expression
from .state_io import TrustedStateDirectory

if TYPE_CHECKING:
    from .config import Settings


class CompatibilityError(RuntimeError):
    """The database belongs to an incompatible local index generation."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notes (
 path TEXT PRIMARY KEY, source_sha256 TEXT, status TEXT NOT NULL CHECK(status IN ('active','excluded')),
 exclusion_reason TEXT, title TEXT, mtime_ns INTEGER, size_bytes INTEGER, scan_generation INTEGER NOT NULL DEFAULT 0,
 indexed_at TEXT NOT NULL,
 CHECK((status='active' AND source_sha256 IS NOT NULL AND exclusion_reason IS NULL) OR
       (status='excluded' AND source_sha256 IS NULL AND exclusion_reason IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS chunks (
 chunk_id TEXT PRIMARY KEY, note_path TEXT NOT NULL REFERENCES notes(path) ON DELETE CASCADE,
 ordinal INTEGER NOT NULL, title TEXT NOT NULL, heading_path TEXT NOT NULL,
 start_line INTEGER NOT NULL, end_line INTEGER NOT NULL, content TEXT NOT NULL, normalized_text TEXT NOT NULL,
 snippet TEXT NOT NULL, content_sha256 TEXT NOT NULL, source_sha256 TEXT NOT NULL,
 UNIQUE(note_path, ordinal)
);
CREATE INDEX IF NOT EXISTS chunks_note_path ON chunks(note_path);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
 chunk_id UNINDEXED, title, heading, path, content, tokenize='unicode61'
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Store:
    def __init__(self, path: str | Path, *, settings: "Settings | None" = None, read_only: bool = False,
                 immutable: bool = False, state_directory: TrustedStateDirectory | None = None):
        self.path, self.settings, self._scan_transaction = Path(path), settings, False
        if settings is None and state_directory is None:
            # Compatibility for low-level tests still requires a vault boundary
            # whenever writes are possible. Read-only inspection without settings
            # retains the historical direct path behavior.
            if not read_only:
                raise OSError("writable Store requires trusted settings")
            self.state_directory = None
            self._owns_state_directory = False
            self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)
        else:
            assert settings is not None
            self.state_directory = state_directory or TrustedStateDirectory(
                self.path, settings.vault, create=not read_only)
            self._owns_state_directory = state_directory is None
            try:
                guard = self.state_directory.open_regular(read_only=read_only)
                try:
                    stable_path = self.state_directory.proc_path()
                    if read_only:
                        suffix = "&immutable=1" if immutable else ""
                        escaped = stable_path.replace("%", "%25").replace("?", "%3f").replace("#", "%23")
                        self.conn = sqlite3.connect(f"file:{escaped}?mode=ro{suffix}", uri=True, timeout=5)
                    else:
                        self.conn = sqlite3.connect(stable_path, timeout=5)
                    if not self.state_directory.same_entry(guard):
                        self.conn.close()
                        raise OSError("SQLite state entry changed during open")
                    self.state_directory.assert_boundary()
                finally:
                    os.close(guard)
            except Exception:
                if hasattr(self, "conn"):
                    self.conn.close()
                if self._owns_state_directory:
                    self.state_directory.close()
                raise
        try:
            if not read_only:
                self.conn.execute("PRAGMA journal_mode=WAL")
                assert self.state_directory is not None
                self.state_directory.assert_boundary()
                self.conn.executescript(SCHEMA)
                self.conn.commit()
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA busy_timeout=5000")
            self._validate_or_initialize(read_only)
        except Exception:
            self.conn.close()
            if self._owns_state_directory and self.state_directory is not None:
                self.state_directory.close()
            raise

    def _validate_or_initialize(self, read_only: bool) -> None:
        try: metadata = dict(self.conn.execute("SELECT key,value FROM metadata"))
        except sqlite3.Error as exc: raise CompatibilityError("missing or incompatible SQLite schema") from exc
        expected = self.settings.compatibility_signature() if self.settings else None
        if not metadata and not read_only:
            values = {"schema_version": "4", "generated_at": _now()}
            if expected:
                assert self.settings is not None
                values["compatibility_signature"] = expected
                values["compatibility_json"] = json.dumps(self.settings.compatibility(), sort_keys=True, separators=(",", ":"))
            self.conn.executemany("INSERT INTO metadata(key,value) VALUES (?,?)", values.items())
            self.conn.commit(); metadata = values
        if metadata.get("schema_version") != "4":
            raise CompatibilityError("SQLite schema version mismatch; create a fresh side-by-side index")
        actual = metadata.get("compatibility_signature")
        if expected and actual != expected:
            raise CompatibilityError("index compatibility signature mismatch")
        if self.settings is not None:
            expected_json = json.dumps(self.settings.compatibility(), sort_keys=True, separators=(",", ":"))
            if metadata.get("compatibility_json") != expected_json:
                raise CompatibilityError("index compatibility metadata mismatch")

    def close(self):
        self.conn.close()
        if self._owns_state_directory and self.state_directory is not None:
            self.state_directory.close()

    def _assert_boundary(self) -> None:
        if self.state_directory is not None:
            self.state_directory.assert_boundary()

    @contextmanager
    def scan_transaction(self):
        if self._scan_transaction: raise RuntimeError("nested scan transaction")
        self._assert_boundary()
        self.conn.execute("BEGIN IMMEDIATE"); self._scan_transaction = True
        try: yield
        except Exception:
            self.conn.rollback(); raise
        else:
            self._assert_boundary()
            self.conn.commit()
        finally: self._scan_transaction = False

    def _write(self): return nullcontext() if self._scan_transaction else self.conn
    def metadata(self, key: str) -> str | None:
        self._assert_boundary()
        row = self.conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    def set_metadata(self, key: str, value: str) -> None:
        self._assert_boundary()
        self.conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)", (key, value))
        if not self._scan_transaction: self.conn.commit()
    def paths(self) -> set[str]:
        self._assert_boundary()
        return {row[0] for row in self.conn.execute("SELECT path FROM notes")}
    def complete_scan(self) -> None:
        generation = int(self.metadata("scan_generation") or "0") + 1
        self.conn.execute("UPDATE notes SET scan_generation=?", (generation,))
        self.set_metadata("scan_generation", str(generation)); self.set_metadata("generated_at", _now())
    def note(self, path: str):
        self._assert_boundary()
        return self.conn.execute("SELECT * FROM notes WHERE path=?", (path,)).fetchone()

    def replace_note(self, path: str, source_sha: str, chunks: list[dict], *, mtime_ns: int = 0, size_bytes: int = 0):
        self._assert_boundary()
        now = _now()
        with self._write():
            old_ids = [row[0] for row in self.conn.execute("SELECT chunk_id FROM chunks WHERE note_path=?", (path,))]
            self.conn.executemany("DELETE FROM chunks_fts WHERE chunk_id=?", ((item,) for item in old_ids))
            self.conn.execute("DELETE FROM notes WHERE path=?", (path,))
            title = chunks[0]["title"] if chunks else Path(path).stem
            self.conn.execute("INSERT INTO notes VALUES (?,?,?,?,?,?,?,?,?)", (path, source_sha, "active", None, title, mtime_ns, size_bytes, 0, now))
            for ordinal, chunk in enumerate(chunks):
                heading = " > ".join(chunk["heading_path"]); normalized = " ".join(chunk["content"].split())
                values = (chunk["chunk_id"], path, chunk.get("ordinal", ordinal), chunk["title"], json.dumps(chunk["heading_path"]),
                          chunk["start_line"], chunk["end_line"], chunk["content"], normalized, chunk["snippet"],
                          chunk["content_sha256"], source_sha)
                self.conn.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", values)
                self.conn.execute("INSERT INTO chunks_fts VALUES (?,?,?,?,?)", (chunk["chunk_id"], chunk["title"], heading, path, normalized))
        self.set_metadata("generated_at", now)

    def exclude(self, path: str, reason: str):
        self._assert_boundary()
        now = _now()
        with self._write():
            rows = list(self.conn.execute("SELECT chunk_id FROM chunks WHERE note_path=?", (path,)))
            self.conn.executemany("DELETE FROM chunks_fts WHERE chunk_id=?", rows)
            self.conn.execute("DELETE FROM notes WHERE path=?", (path,))
            self.conn.execute("INSERT INTO notes(path,status,exclusion_reason,indexed_at) VALUES (?,'excluded',?,?)", (path, reason, now))
        self.set_metadata("generated_at", now)

    def delete(self, path: str):
        self._assert_boundary()
        with self._write():
            rows = list(self.conn.execute("SELECT chunk_id FROM chunks WHERE note_path=?", (path,)))
            self.conn.executemany("DELETE FROM chunks_fts WHERE chunk_id=?", rows)
            self.conn.execute("DELETE FROM notes WHERE path=?", (path,))
        self.set_metadata("generated_at", _now())

    def lexical(self, query: str, limit: int, path_prefix: str | None = None, *, offset: int = 0) -> list[dict]:
        self._assert_boundary()
        expression = fts_expression(query)
        if not expression: return []
        sql = """SELECT c.*, chunks_fts.title AS fts_title, chunks_fts.heading AS fts_heading,
                        chunks_fts.path AS fts_path, chunks_fts.content AS fts_content,
                        bm25(chunks_fts, 0.0, 2.0, 1.5, 0.5, 1.0) AS lexical_score
                 FROM chunks_fts JOIN chunks c USING(chunk_id) JOIN notes n ON n.path=c.note_path
                 WHERE chunks_fts MATCH ? AND n.status='active' AND n.source_sha256=c.source_sha256"""
        args: list[object] = [expression]
        if path_prefix:
            escaped = path_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            sql += " AND (c.note_path=? OR c.note_path LIKE ? ESCAPE '\\')"; args.extend([path_prefix, escaped + "/%"])
        sql += " ORDER BY lexical_score ASC,c.note_path ASC,c.start_line ASC,c.chunk_id ASC LIMIT ? OFFSET ?"
        args.extend([limit, offset])
        return [dict(row) for row in self.conn.execute(sql, args)]

    def assert_search_integrity(self) -> None:
        """Reject relational corruption that could suppress or forge candidates."""
        self._assert_boundary()
        chunks = self.conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
        fts = self.conn.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
        orphan_fts = self.conn.execute(
            "SELECT count(*) FROM chunks_fts f LEFT JOIN chunks c ON c.chunk_id=f.chunk_id WHERE c.chunk_id IS NULL"
        ).fetchone()[0]
        invalid_chunks = self.conn.execute(
            """SELECT count(*) FROM chunks c LEFT JOIN notes n ON n.path=c.note_path
               WHERE n.path IS NULL OR n.status!='active' OR n.source_sha256!=c.source_sha256"""
        ).fetchone()[0]
        duplicate_fts = self.conn.execute(
            "SELECT count(*) FROM (SELECT chunk_id FROM chunks_fts GROUP BY chunk_id HAVING count(*)!=1)"
        ).fetchone()[0]
        if chunks != fts or orphan_fts or invalid_chunks or duplicate_fts:
            raise CompatibilityError("logically corrupt SQLite search projection")

    def counts(self) -> dict:
        self._assert_boundary()
        return {"active_notes": self.conn.execute("SELECT count(*) FROM notes WHERE status='active'").fetchone()[0],
                "excluded_notes": self.conn.execute("SELECT count(*) FROM notes WHERE status='excluded'").fetchone()[0],
                "chunks": self.conn.execute("SELECT count(*) FROM chunks").fetchone()[0]}

    def status(self) -> dict:
        generated = self.metadata("generated_at"); age = None
        if generated:
            if not isinstance(generated, str):
                raise CompatibilityError("invalid index generation timestamp")
            try: age = max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(generated.replace("Z", "+00:00"))).total_seconds()))
            except (ValueError, OverflowError) as exc:
                raise CompatibilityError("invalid index generation timestamp") from exc
        return {**self.counts(), "generated_at": generated, "age_seconds": age, "stale": False,
                "current": True, "compatibility": "current"}

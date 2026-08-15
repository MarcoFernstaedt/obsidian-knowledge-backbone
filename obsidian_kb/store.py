"""Authoritative transactional SQLite metadata and FTS5 index."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS notes (
 path TEXT PRIMARY KEY, source_sha256 TEXT, status TEXT NOT NULL CHECK(status IN ('active','excluded')),
 exclusion_reason TEXT, indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 CHECK((status='active' AND source_sha256 IS NOT NULL AND exclusion_reason IS NULL) OR
       (status='excluded' AND source_sha256 IS NULL AND exclusion_reason IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS chunks (
 chunk_id TEXT PRIMARY KEY, note_path TEXT NOT NULL REFERENCES notes(path) ON DELETE CASCADE,
 title TEXT NOT NULL, heading_path TEXT NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
 content TEXT NOT NULL, snippet TEXT NOT NULL, source_sha256 TEXT NOT NULL,
 point_id TEXT NOT NULL UNIQUE, semantic_ready INTEGER NOT NULL DEFAULT 0 CHECK(semantic_ready IN (0,1))
);
CREATE INDEX IF NOT EXISTS chunks_note_path ON chunks(note_path);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(chunk_id UNINDEXED, title, heading, content, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: str | Path, *, read_only: bool = False):
        self.path = Path(path)
        if read_only:
            self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path)
            self.conn.executescript(SCHEMA)
            self.conn.execute("INSERT OR REPLACE INTO metadata VALUES ('schema_version','1')")
            self.conn.commit()
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")

    def close(self): self.conn.close()

    def paths(self) -> set[str]:
        return {row[0] for row in self.conn.execute("SELECT path FROM notes")}

    def note(self, path: str):
        return self.conn.execute("SELECT * FROM notes WHERE path=?", (path,)).fetchone()

    def point_ids(self, path: str) -> list[str]:
        return [row[0] for row in self.conn.execute("SELECT point_id FROM chunks WHERE note_path=?", (path,))]

    def replace_note(self, path: str, source_sha: str, chunks: list[dict], semantic_ready: bool):
        with self.conn:
            old_ids = [row[0] for row in self.conn.execute("SELECT chunk_id FROM chunks WHERE note_path=?", (path,))]
            if old_ids:
                self.conn.executemany("DELETE FROM chunks_fts WHERE chunk_id=?", ((value,) for value in old_ids))
            self.conn.execute("DELETE FROM notes WHERE path=?", (path,))
            self.conn.execute("INSERT INTO notes(path,source_sha256,status) VALUES (?,?,'active')", (path, source_sha))
            for chunk in chunks:
                heading = " > ".join(chunk["heading_path"])
                values = (chunk["chunk_id"], path, chunk["title"], json.dumps(chunk["heading_path"]),
                          chunk["start_line"], chunk["end_line"], chunk["content"], chunk["snippet"],
                          source_sha, chunk["chunk_id"], int(semantic_ready))
                self.conn.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?)", values)
                self.conn.execute("INSERT INTO chunks_fts(chunk_id,title,heading,content) VALUES (?,?,?,?)",
                                  (chunk["chunk_id"], chunk["title"], heading, chunk["content"]))

    def exclude(self, path: str, reason: str):
        with self.conn:
            ids = [row[0] for row in self.conn.execute("SELECT chunk_id FROM chunks WHERE note_path=?", (path,))]
            self.conn.executemany("DELETE FROM chunks_fts WHERE chunk_id=?", ((value,) for value in ids))
            self.conn.execute("DELETE FROM notes WHERE path=?", (path,))
            self.conn.execute("INSERT INTO notes(path,status,exclusion_reason) VALUES (?,'excluded',?)", (path, reason))

    def delete(self, path: str):
        with self.conn:
            ids = [row[0] for row in self.conn.execute("SELECT chunk_id FROM chunks WHERE note_path=?", (path,))]
            self.conn.executemany("DELETE FROM chunks_fts WHERE chunk_id=?", ((value,) for value in ids))
            self.conn.execute("DELETE FROM notes WHERE path=?", (path,))

    def lexical(self, query: str, limit: int) -> list[dict]:
        terms = [part for part in query.replace('"', ' ').split() if any(ch.isalnum() for ch in part)]
        if not terms: return []
        expression = " OR ".join('"' + ''.join(ch for ch in term if ch.isalnum() or ch in '_-') + '"' for term in terms)
        rows = self.conn.execute("""SELECT c.*, bm25(chunks_fts) AS lexical_score FROM chunks_fts
            JOIN chunks c ON c.chunk_id=chunks_fts.chunk_id WHERE chunks_fts MATCH ?
            ORDER BY lexical_score, c.note_path, c.start_line, c.chunk_id LIMIT ?""", (expression, limit)).fetchall()
        return [dict(row) for row in rows]

    def active_semantic(self, chunk_ids: list[str]) -> dict[str, dict]:
        if not chunk_ids: return {}
        marks = ",".join("?" for _ in chunk_ids)
        rows = self.conn.execute(f"""SELECT c.* FROM chunks c JOIN notes n ON n.path=c.note_path
            WHERE c.chunk_id IN ({marks}) AND c.semantic_ready=1 AND n.status='active'
            AND n.source_sha256=c.source_sha256""", chunk_ids).fetchall()
        return {row["chunk_id"]: dict(row) for row in rows}

    def counts(self) -> dict:
        return {
            "active_notes": self.conn.execute("SELECT count(*) FROM notes WHERE status='active'").fetchone()[0],
            "excluded_notes": self.conn.execute("SELECT count(*) FROM notes WHERE status='excluded'").fetchone()[0],
            "chunks": self.conn.execute("SELECT count(*) FROM chunks").fetchone()[0],
            "semantic_ready": self.conn.execute("SELECT count(*) FROM chunks WHERE semantic_ready=1").fetchone()[0],
        }

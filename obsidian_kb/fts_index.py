import sqlite3
from typing import List, Dict, Any, Optional
import os
import hashlib

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
    file_path, heading_path, start_line, end_line, content, file_sha256, snippet,
    tokenize = "porter"
);
CREATE TABLE IF NOT EXISTS chunkmeta (
    chunk_id INTEGER PRIMARY KEY,
    file_path TEXT, start_line INTEGER, end_line INTEGER, file_sha256 TEXT,
    exclusion_reason TEXT DEFAULT NULL
);
"""

class FTSIndex:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.ensure_schema()
    def ensure_schema(self):
        for stmt in FTS_SCHEMA.strip().split(';'):
            if stmt.strip():
                self.conn.execute(stmt.strip())
        self.conn.commit()
    def upsert_chunks(self, chunks: List[Dict[str, Any]]):
        cur = self.conn.cursor()
        for c in chunks:
            # Remove stale (same file, overlapping start_line)
            cur.execute("DELETE FROM chunks WHERE file_path=? AND start_line=?", (c["file_path"], c["start_line"]))
            cur.execute("INSERT INTO chunks (file_path, heading_path, start_line, end_line, content, file_sha256, snippet) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (c["file_path"], ",".join(c["heading_path"]), c["start_line"], c["end_line"], c["content"], c["file_sha256"], c["snippet"]))
        self.conn.commit()
    def delete_chunks_by_file(self, file_path: str):
        self.conn.execute("DELETE FROM chunks WHERE file_path=?", (file_path,))
        self.conn.commit()
    def delete_all(self):
        self.conn.execute("DELETE FROM chunks"); self.conn.commit()
    def query(self, query: str, k: int = 5) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        rows = cur.execute("SELECT rowid, * FROM chunks WHERE chunks MATCH ? LIMIT ?", (query, k)).fetchall()
        return [dict(r) for r in rows]
    def close(self):
        self.conn.close()

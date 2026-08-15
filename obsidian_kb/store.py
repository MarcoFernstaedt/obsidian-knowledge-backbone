"""Authoritative transactional SQLite ledger and FTS5 index."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings


class CompatibilityError(RuntimeError):
    """The database belongs to an incompatible corpus/index signature."""


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
 snippet TEXT NOT NULL, content_sha256 TEXT NOT NULL, source_sha256 TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
 point_id TEXT NOT NULL UNIQUE, UNIQUE(note_path, ordinal)
);
CREATE INDEX IF NOT EXISTS chunks_note_path ON chunks(note_path);
CREATE TABLE IF NOT EXISTS semantic_projection (
 chunk_id TEXT PRIMARY KEY REFERENCES chunks(chunk_id) ON DELETE CASCADE,
 desired_sha256 TEXT NOT NULL, applied_sha256 TEXT, status TEXT NOT NULL CHECK(status IN ('pending','applied')),
 last_error_class TEXT, attempt_count INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tombstones (
 chunk_id TEXT PRIMARY KEY, path TEXT NOT NULL, created_at TEXT NOT NULL, qdrant_deleted_at TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
 chunk_id UNINDEXED, title, heading, path, content, tokenize='porter unicode61'
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Store:
    def __init__(self, path: str | Path, *, settings: "Settings | None" = None, read_only: bool = False):
        self.path = Path(path); self.settings = settings
        if read_only:
            self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try: self.path.parent.chmod(0o700)
            except OSError: pass
            self.conn = sqlite3.connect(self.path, timeout=5)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            self.conn.commit()
            try: self.path.chmod(0o600)
            except OSError: pass
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        try:self._validate_or_initialize(read_only)
        except Exception:
            self.conn.close()
            raise

    def _validate_or_initialize(self, read_only: bool) -> None:
        try:
            metadata = dict(self.conn.execute("SELECT key,value FROM metadata"))
        except sqlite3.Error as exc:
            raise CompatibilityError("missing or incompatible SQLite schema") from exc
        expected = self.settings.compatibility_signature() if self.settings else None
        if not metadata and not read_only:
            values = {"schema_version":"2", "generated_at":_now()}
            if expected:
                values["compatibility_signature"] = expected
                values["compatibility_json"] = json.dumps(self.settings.compatibility(),sort_keys=True,separators=(",",":"))
            self.conn.executemany("INSERT INTO metadata(key,value) VALUES (?,?)", values.items()); self.conn.commit()
            metadata = values
        if metadata.get("schema_version") != "2": raise CompatibilityError("SQLite schema version mismatch; use a fresh side-by-side index")
        actual = metadata.get("compatibility_signature")
        if expected and actual and actual != expected:
            raise CompatibilityError("index compatibility signature mismatch; vectors must not be mixed")
        if expected and not actual:
            if read_only: raise CompatibilityError("index compatibility signature missing")
            self.set_metadata("compatibility_signature",expected)
            self.set_metadata("compatibility_json",json.dumps(self.settings.compatibility(),sort_keys=True,separators=(",",":")))

    def close(self): self.conn.close()
    def metadata(self,key: str) -> str | None:
        row=self.conn.execute("SELECT value FROM metadata WHERE key=?",(key,)).fetchone()
        return row[0] if row else None
    def set_metadata(self,key: str,value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)",(key,value)); self.conn.commit()
    def paths(self) -> set[str]: return {row[0] for row in self.conn.execute("SELECT path FROM notes")}
    def note(self,path: str): return self.conn.execute("SELECT * FROM notes WHERE path=?",(path,)).fetchone()
    def point_ids(self,path: str) -> list[str]: return [r[0] for r in self.conn.execute("SELECT point_id FROM chunks WHERE note_path=?",(path,))]
    def needs_semantic(self,path: str) -> bool:
        return bool(self.conn.execute("SELECT 1 FROM semantic_projection p JOIN chunks c USING(chunk_id) WHERE c.note_path=? AND p.status='pending' LIMIT 1",(path,)).fetchone())

    def replace_note(self,path: str,source_sha: str,chunks: list[dict],semantic_ready: bool=False,
                     *,mtime_ns: int=0,size_bytes: int=0,generation: int=0):
        now=_now()
        with self.conn:
            old={r["chunk_id"]:dict(r) for r in self.conn.execute("SELECT c.*,p.applied_sha256 FROM chunks c LEFT JOIN semantic_projection p USING(chunk_id) WHERE note_path=?",(path,))}
            new_ids={c["chunk_id"] for c in chunks}
            for chunk_id in sorted(set(old)-new_ids):
                self.conn.execute("INSERT OR IGNORE INTO tombstones(chunk_id,path,created_at) VALUES (?,?,?)",(chunk_id,path,now))
            old_ids=list(old)
            self.conn.executemany("DELETE FROM chunks_fts WHERE chunk_id=?",((x,) for x in old_ids))
            self.conn.execute("DELETE FROM notes WHERE path=?",(path,))
            title=chunks[0]["title"] if chunks else Path(path).stem
            self.conn.execute("INSERT INTO notes VALUES (?,?,?,?,?,?,?,?,?)",(path,source_sha,"active",None,title,mtime_ns,size_bytes,generation,now))
            for ordinal,chunk in enumerate(chunks):
                # Recreating the same corpus/path/ordinal UUID cancels an unapplied stale deletion.
                self.conn.execute("DELETE FROM tombstones WHERE chunk_id=?", (chunk["chunk_id"],))
                content_sha=chunk.get("content_sha256") or __import__("hashlib").sha256(chunk["content"].encode()).hexdigest()
                previous=old.get(chunk["chunk_id"])
                applied=content_sha if semantic_ready else (previous.get("applied_sha256") if previous and previous.get("content_sha256")==content_sha else None)
                status="applied" if applied==content_sha else "pending"
                heading=" > ".join(chunk["heading_path"]); normalized=" ".join(chunk["content"].split())
                values=(chunk["chunk_id"],path,chunk.get("ordinal",ordinal),chunk["title"],json.dumps(chunk["heading_path"]),
                        chunk["start_line"],chunk["end_line"],chunk["content"],normalized,chunk["snippet"],content_sha,
                        source_sha,1,chunk["point_id"])
                self.conn.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",values)
                self.conn.execute("INSERT INTO chunks_fts VALUES (?,?,?,?,?)",(chunk["chunk_id"],chunk["title"],heading,path,normalized))
                self.conn.execute("INSERT INTO semantic_projection VALUES (?,?,?,?,?,?,?)",(chunk["chunk_id"],content_sha,applied,status,None,0,now))
        self.set_metadata("generated_at",now)

    def exclude(self,path: str,reason: str):
        now=_now()
        with self.conn:
            rows=list(self.conn.execute("SELECT chunk_id FROM chunks WHERE note_path=?",(path,)))
            for row in rows: self.conn.execute("INSERT OR IGNORE INTO tombstones VALUES (?,?,?,NULL)",(row[0],path,now))
            self.conn.executemany("DELETE FROM chunks_fts WHERE chunk_id=?",rows)
            self.conn.execute("DELETE FROM notes WHERE path=?",(path,))
            self.conn.execute("INSERT INTO notes(path,status,exclusion_reason,indexed_at) VALUES (?,'excluded',?,?)",(path,reason,now))
        self.set_metadata("generated_at",now)

    def delete(self,path: str):
        now=_now()
        with self.conn:
            rows=list(self.conn.execute("SELECT chunk_id FROM chunks WHERE note_path=?",(path,)))
            for row in rows: self.conn.execute("INSERT OR IGNORE INTO tombstones VALUES (?,?,?,NULL)",(row[0],path,now))
            self.conn.executemany("DELETE FROM chunks_fts WHERE chunk_id=?",rows)
            self.conn.execute("DELETE FROM notes WHERE path=?",(path,))
        self.set_metadata("generated_at",now)

    def lexical(self,query: str,limit: int,path_prefix: str|None=None) -> list[dict]:
        terms=["".join(ch for ch in part if ch.isalnum() or ch in "_-") for part in query.replace('"',' ').split()]
        terms=[x for x in terms if x and any(ch.isalnum() for ch in x)]
        if not terms:return []
        expression=" OR ".join(f'"{term}"' for term in terms)
        sql="""SELECT c.*, bm25(chunks_fts) AS lexical_score FROM chunks_fts JOIN chunks c USING(chunk_id)
               JOIN notes n ON n.path=c.note_path WHERE chunks_fts MATCH ? AND c.active=1 AND n.status='active'"""
        args:list[object]=[expression]
        if path_prefix:
            escaped=path_prefix.replace("\\","\\\\").replace("%","\\%").replace("_","\\_")
            sql += " AND (c.note_path=? OR c.note_path LIKE ? ESCAPE '\\')"; args.extend([path_prefix,escaped+"/%"])
        sql += " ORDER BY lexical_score,c.note_path,c.start_line,c.chunk_id LIMIT ?"; args.append(limit)
        return [dict(r) for r in self.conn.execute(sql,args)]

    def active_semantic(self,chunk_ids:list[str],path_prefix:str|None=None)->dict[str,dict]:
        if not chunk_ids:return {}
        marks=",".join("?" for _ in chunk_ids)
        sql=f"""SELECT c.* FROM chunks c JOIN notes n ON n.path=c.note_path JOIN semantic_projection p USING(chunk_id)
                  WHERE c.chunk_id IN ({marks}) AND c.active=1 AND n.status='active' AND p.status='applied'
                  AND p.applied_sha256=p.desired_sha256 AND n.source_sha256=c.source_sha256"""
        args:list[object]=list(chunk_ids)
        if path_prefix:
            escaped=path_prefix.replace("\\","\\\\").replace("%","\\%").replace("_","\\_")
            sql += " AND (c.note_path=? OR c.note_path LIKE ? ESCAPE '\\')"; args.extend([path_prefix,escaped+"/%"])
        rows=self.conn.execute(sql,args).fetchall(); return {r["chunk_id"]:dict(r) for r in rows}

    def pending(self)->list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT c.*,p.desired_sha256,p.attempt_count FROM chunks c JOIN semantic_projection p USING(chunk_id) WHERE p.status='pending' ORDER BY c.note_path,c.ordinal")]
    def mark_projection(self,chunk_id:str,success:bool,error_class:str|None=None):
        now=_now()
        with self.conn:
            if success:self.conn.execute("UPDATE semantic_projection SET applied_sha256=desired_sha256,status='applied',last_error_class=NULL,attempt_count=attempt_count+1,updated_at=? WHERE chunk_id=?",(now,chunk_id))
            else:self.conn.execute("UPDATE semantic_projection SET status='pending',last_error_class=?,attempt_count=attempt_count+1,updated_at=? WHERE chunk_id=?",(error_class,now,chunk_id))
    def pending_tombstones(self)->list[str]: return [r[0] for r in self.conn.execute("SELECT chunk_id FROM tombstones WHERE qdrant_deleted_at IS NULL ORDER BY chunk_id")]
    def mark_tombstones(self,ids:list[str]):
        if ids:
            with self.conn:self.conn.executemany("UPDATE tombstones SET qdrant_deleted_at=? WHERE chunk_id=?",((_now(),x) for x in ids))
    def active_ids(self)->set[str]: return {r[0] for r in self.conn.execute("SELECT chunk_id FROM chunks WHERE active=1")}
    def counts(self)->dict:
        return {"active_notes":self.conn.execute("SELECT count(*) FROM notes WHERE status='active'").fetchone()[0],
                "excluded_notes":self.conn.execute("SELECT count(*) FROM notes WHERE status='excluded'").fetchone()[0],
                "chunks":self.conn.execute("SELECT count(*) FROM chunks WHERE active=1").fetchone()[0],
                "semantic_ready":self.conn.execute("SELECT count(*) FROM semantic_projection WHERE status='applied'").fetchone()[0],
                "pending_vectors":self.conn.execute("SELECT count(*) FROM semantic_projection WHERE status='pending'").fetchone()[0],
                "pending_tombstones":self.conn.execute("SELECT count(*) FROM tombstones WHERE qdrant_deleted_at IS NULL").fetchone()[0]}
    def status(self)->dict:
        generated=self.metadata("generated_at"); age=None
        if generated:
            try:age=max(0,int((datetime.now(timezone.utc)-datetime.fromisoformat(generated.replace("Z","+00:00"))).total_seconds()))
            except ValueError:age=None
        counts=self.counts(); return {**counts,"generated_at":generated,"age_seconds":age,
                                      "stale":bool(counts["pending_vectors"] or counts["pending_tombstones"]),
                                      "compatibility_signature":self.metadata("compatibility_signature")}

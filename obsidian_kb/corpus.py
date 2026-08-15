"""Bounded live corpus scan and private process-local SQLite FTS5 query."""
from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
import hashlib
from pathlib import Path
import sqlite3
import time

from .chunker import chunk_markdown, is_frontmatter_excluded
from .config import Settings
from .lexical import fts_expression, lexical_projection
from .privacy import contains_secret
from .vault_io import (InventoryEntry, TrustedVault, VaultInventoryOverflow,
                       VaultOversizeError, VaultPolicyError)


class CorpusError(RuntimeError):
    """A complete authoritative corpus could not be constructed."""


class CorpusScanError(CorpusError):
    """An unknown or transient vault read prevented a complete scan."""


class CorpusLimitError(CorpusError):
    """A configured resource bound prevented a complete scan."""


@dataclass(frozen=True)
class LiveCorpus:
    chunks: tuple[dict, ...]
    eligible_notes: int
    excluded_notes: int
    total_bytes: int
    scan_duration_ms: int

    def status(self) -> dict:
        return {
            "eligible_notes": self.eligible_notes,
            "excluded_notes": self.excluded_notes,
            "chunks": len(self.chunks),
            "scan_duration_ms": self.scan_duration_ms,
            "source_inventory_complete": True,
            "current": True,
            "compatibility": "ephemeral-live",
        }


def source_sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def path_exclusion_reason(path: str, settings: Settings) -> str | None:
    parts = Path(path).parts
    if not any(fnmatch(path, pattern) for pattern in settings.include_globs):
        return "not-included"
    if settings.exclude_hidden and any(part.startswith(".") for part in parts):
        return "hidden-path"
    if any(part in settings.excluded_folders for part in parts[:-1]):
        return "excluded-folder"
    if any(fnmatch(path, pattern) for pattern in settings.excluded_globs):
        return "excluded-glob"
    return None


def content_exclusion_reason(text: str, settings: Settings) -> str | None:
    if is_frontmatter_excluded(text, settings.frontmatter_false_keys):
        return "frontmatter-excluded"
    if contains_secret(text, settings.extra_secret_patterns):
        return "credential-content"
    return None


def scan(settings: Settings) -> LiveCorpus:
    """Build one exact, complete corpus or fail without returning partial data."""
    started = time.perf_counter_ns()
    chunks: list[dict] = []
    eligible = excluded = total_bytes = 0
    eligible_sources: dict[str, tuple[InventoryEntry, str]] = {}
    try:
        identity = (settings.vault_device, settings.vault_inode)
        with TrustedVault(settings.vault, identity) as vault:
            try:
                before = vault.inventory(settings.maximum_files)
            except VaultInventoryOverflow as exc:
                raise CorpusLimitError("maximum_files exceeded; source inventory is incomplete") from exc
            sources = [item for item in before if item.path.endswith(".md")]
            for source in sources:
                path = source.path
                reason = path_exclusion_reason(path, settings)
                if reason:
                    excluded += 1
                    continue
                if source.kind != "regular":
                    excluded += 1
                    continue
                if source.size > settings.maximum_note_bytes:
                    raise CorpusLimitError("maximum_note_bytes exceeded; source inventory is incomplete")
                try:
                    raw, opened = vault.read(path, settings.maximum_note_bytes)
                    if not source.same_open_file(opened):
                        raise CorpusScanError("vault source changed; no partial corpus returned")
                except VaultOversizeError as exc:
                    raise CorpusLimitError("maximum_note_bytes exceeded; source inventory is incomplete") from exc
                except VaultPolicyError as exc:
                    raise CorpusScanError("vault source changed; no partial corpus returned") from exc
                except OSError as exc:
                    raise CorpusScanError("vault read failed; no partial corpus returned") from exc
                total_bytes += len(raw)
                if total_bytes > settings.maximum_total_bytes:
                    raise CorpusLimitError("maximum_total_bytes exceeded; source inventory is incomplete")
                try:
                    text = raw.decode("utf-8")
                except UnicodeError:
                    excluded += 1
                    continue
                if content_exclusion_reason(text, settings):
                    excluded += 1
                    continue
                note_chunks = chunk_markdown(
                    text, source_sha(raw), path,
                    max_lines=settings.max_lines,
                    max_chars=settings.max_chars,
                    overlap_lines=settings.overlap_lines,
                    corpus_id=settings.corpus_id,
                    compatibility_signature=settings.compatibility_signature(),
                )
                if len(chunks) + len(note_chunks) > settings.maximum_chunks:
                    raise CorpusLimitError("maximum_chunks exceeded; source inventory is incomplete")
                chunks.extend(note_chunks)
                eligible += 1
                eligible_sources[path] = (source, source_sha(raw))

            try:
                after_scan = vault.inventory(settings.maximum_files)
            except VaultInventoryOverflow as exc:
                raise CorpusLimitError("maximum_files exceeded; source inventory is incomplete") from exc
            if before != after_scan:
                raise CorpusScanError("vault inventory changed; no partial corpus returned")

            # Re-read every included source at the final boundary. Metadata alone is
            # insufficient when an attacker can preserve size and timestamps.
            for path, (source, expected_sha) in eligible_sources.items():
                try:
                    current, opened = vault.read(path, settings.maximum_note_bytes)
                except (VaultPolicyError, VaultOversizeError, OSError) as exc:
                    raise CorpusScanError("vault source changed; no partial corpus returned") from exc
                if not source.same_open_file(opened) or source_sha(current) != expected_sha:
                    raise CorpusScanError("vault source changed; no partial corpus returned")
            try:
                final = vault.inventory(settings.maximum_files)
            except VaultInventoryOverflow as exc:
                raise CorpusLimitError("maximum_files exceeded; source inventory is incomplete") from exc
            if before != final:
                raise CorpusScanError("vault inventory changed; no partial corpus returned")
    except CorpusError:
        raise
    except OSError as exc:
        raise CorpusScanError("vault scan failed; no partial corpus returned") from exc
    duration = max(0, (time.perf_counter_ns() - started) // 1_000_000)
    return LiveCorpus(tuple(chunks), eligible, excluded, total_bytes, duration)


class MemoryFTS:
    """A narrowly scoped FTS5 database whose only database is SQLite :memory:."""

    def __init__(self, corpus: LiveCorpus):
        self.conn: sqlite3.Connection | None = sqlite3.connect(":memory:")
        try:
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA temp_store=MEMORY")
            self.conn.execute(
                "CREATE VIRTUAL TABLE corpus_fts USING fts5("
                "chunk_id UNINDEXED,exact_path UNINDEXED,title,heading,path,content,"
                "tokenize='unicode61 remove_diacritics 2')"
            )
            self._chunks = {chunk["chunk_id"]: chunk for chunk in corpus.chunks}
            self.conn.executemany(
                "INSERT INTO corpus_fts(chunk_id,exact_path,title,heading,path,content) VALUES (?,?,?,?,?,?)",
                ((chunk["chunk_id"], chunk["file_path"], lexical_projection(chunk["title"]),
                  lexical_projection(" > ".join(chunk["heading_path"])),
                  lexical_projection(chunk["file_path"]), lexical_projection(chunk["content"]))
                 for chunk in corpus.chunks),
            )
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "MemoryFTS":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def query(self, question: str, limit: int, path_prefix: str | None) -> list[tuple[dict, float]]:
        if self.conn is None:
            raise RuntimeError("in-memory corpus is closed")
        expression = fts_expression(question)
        if not expression:
            return []
        sql = (
            "SELECT chunk_id,exact_path,bm25(corpus_fts,0.0,0.0,8.0,5.0,3.0,1.0) AS score "
            "FROM corpus_fts WHERE corpus_fts MATCH ?"
        )
        args: list[object] = [expression]
        if path_prefix:
            escaped = path_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            sql += " AND (exact_path=? OR exact_path LIKE ? ESCAPE '\\')"
            args.extend((path_prefix, escaped + "/%"))
        sql += " ORDER BY score ASC,exact_path ASC,rowid ASC LIMIT ?"
        args.append(limit)
        output = []
        for row in self.conn.execute(sql, args):
            chunk = self._chunks.get(row["chunk_id"])
            if chunk is None:
                raise CorpusError("in-memory FTS returned an unknown exact-source chunk")
            output.append((chunk, float(row["score"])))
        return output


def live_status(settings: Settings) -> dict:
    return scan(settings).status()


def audit(settings: Settings) -> dict:
    """Compatibility audit: validate a complete live corpus and ephemeral FTS construction."""
    try:
        corpus = scan(settings)
        with MemoryFTS(corpus):
            pass
        return {"ok": True, "audit": True, "ephemeral": True, "persistence": False,
                **corpus.status()}
    except (CorpusError, sqlite3.Error) as exc:
        return {
            "ok": False,
            "audit": True,
            "ephemeral": True,
            "persistence": False,
            "eligible_notes": 0,
            "excluded_notes": 0,
            "chunks": 0,
            "scan_duration_ms": 0,
            "source_inventory_complete": False,
            "current": False,
            "compatibility": "ephemeral-live",
            "error_class": type(exc).__name__,
        }

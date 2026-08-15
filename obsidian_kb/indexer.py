"""Read-only vault indexing with atomic local publication and durable projection recovery."""
from __future__ import annotations

import fcntl
from fnmatch import fnmatch
import hashlib
import os
from pathlib import Path

from .chunker import chunk_markdown, is_frontmatter_excluded
from .config import Settings
from .privacy import contains_secret
from .remote import OllamaClient, QdrantClient, RemoteError
from .store import Store
from .vault_io import TrustedVault, VaultPolicyError


class IndexLockError(RuntimeError):
    """Another indexer owns the single-writer lock."""


def source_sha(source: str | bytes) -> str:
    raw = source if isinstance(source, bytes) else source.encode()
    return hashlib.sha256(raw).hexdigest()


def path_exclusion_reason(path: str, settings: Settings) -> str | None:
    parts = Path(path).parts
    if not any(fnmatch(path, pattern) for pattern in settings.include_globs): return "not-included"
    if settings.exclude_hidden and any(part.startswith(".") for part in parts): return "hidden-path"
    if any(part in settings.excluded_folders for part in parts[:-1]): return "excluded-folder"
    if any(fnmatch(path, pattern) for pattern in settings.excluded_globs): return "excluded-glob"
    return None


def content_exclusion_reason(text: str, settings: Settings) -> str | None:
    if is_frontmatter_excluded(text, settings.frontmatter_false_keys): return "frontmatter-excluded"
    if contains_secret(text, settings.extra_secret_patterns): return "credential-content"
    return None


class Indexer:
    def __init__(self, settings: Settings, *, ollama=None, qdrant=None):
        self.settings = settings
        self.store: Store | None = None
        self.ollama = ollama or (OllamaClient(settings.ollama_url, settings.ollama_model, settings.timeout,
                                               settings.response_max_bytes) if settings.ollama_url else None)
        self.qdrant = qdrant or (QdrantClient(settings.qdrant_url, settings.qdrant_collection,
                                               settings.vector_size, settings.timeout,
                                               settings.response_max_bytes) if settings.qdrant_url else None)
        self._lock_handle = None

    @property
    def lock_path(self) -> Path: return self.settings.state.with_suffix(self.settings.state.suffix + ".lock")

    def acquire_lock(self) -> None:
        if self._lock_handle: return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = self.lock_path.open("a+"); os.chmod(self.lock_path, 0o600)
        try: fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close(); raise IndexLockError("knowledge index is already running") from exc
        self._lock_handle = handle

    def release_lock(self) -> None:
        if self._lock_handle:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
            self._lock_handle.close(); self._lock_handle = None

    def close(self):
        self.release_lock()
        if self.store:
            self.store.close(); self.store = None

    def _s(self) -> Store:
        if not self.store: raise RuntimeError("index store is not open")
        return self.store

    def _ensure_remote_generation(self) -> None:
        if self.qdrant:
            self.qdrant.ensure(self.settings.corpus_id, self.settings.compatibility_signature(), self.settings.model_digest)

    def _project_pending(self, warnings: list[str]) -> None:
        pending = self._s().pending()
        if not pending: return
        if not (self.ollama and self.qdrant):
            warnings.append("semantic projection unavailable; deterministic pending work retained"); return
        batch_size = self.settings.embedding_batch_size
        try:
            self._ensure_remote_generation()
            for offset in range(0, len(pending), batch_size):
                batch = pending[offset:offset + batch_size]
                vectors = self.ollama.embed([row["content"] for row in batch])
                if len(vectors) != len(batch) or any(len(v) != self.settings.vector_size for v in vectors):
                    raise RemoteError("embedding vector size mismatch")
                points = []
                for row, vector in zip(batch, vectors):
                    payload = {"corpus_id": self.settings.corpus_id, "schema_version": 2,
                               "chunk_id": row["chunk_id"], "content_sha256": row["desired_sha256"],
                               "embedding_model": self.settings.ollama_model,
                               "model_digest": self.settings.model_digest,
                               "compatibility_signature": self.settings.compatibility_signature()}
                    points.append({"id": row["point_id"], "vector": vector, "payload": payload})
                self.qdrant.upsert(points, self.settings.compatibility_signature())
                for row in batch: self._s().mark_projection(row["chunk_id"], True)
        except RemoteError as exc:
            for row in pending: self._s().mark_projection(row["chunk_id"], False, type(exc).__name__)
            warnings.append("semantic projection unavailable; deterministic pending work retained")

    def _apply_tombstones(self, warnings: list[str]) -> None:
        ids = self._s().pending_tombstones()
        if not ids: return
        if not self.qdrant:
            warnings.append("semantic deletion unavailable; tombstones retained"); return
        try:
            self._ensure_remote_generation()
            for offset in range(0, len(ids), self.settings.embedding_batch_size):
                batch = ids[offset:offset + self.settings.embedding_batch_size]
                self.qdrant.delete(batch,self.settings.corpus_id,self.settings.compatibility_signature()); self._s().mark_tombstones(batch)
        except RemoteError:
            warnings.append("semantic deletion unavailable; tombstones retained")

    def _reconcile_remote(self, warnings: list[str]) -> None:
        if not self.qdrant:
            warnings.append("full reconciliation unavailable without Qdrant"); return
        try:
            self._ensure_remote_generation()
            orphans = sorted(set(self.qdrant.list_ids(self.settings.corpus_id,
                                                       self.settings.compatibility_signature())) - self._s().active_ids())
            for offset in range(0, len(orphans), self.settings.embedding_batch_size):
                self.qdrant.delete(orphans[offset:offset + self.settings.embedding_batch_size],
                                    self.settings.corpus_id,self.settings.compatibility_signature())
        except RemoteError:
            warnings.append("full reconciliation unavailable; local postfilter remains authoritative")

    def _scan(self, trusted: TrustedVault, dry_run: bool) -> tuple[dict[str, int], list[str]]:
        store = self.store
        seen: set[str] = set(); changed = excluded = unchanged = removed = 0
        candidates = trusted.markdown_paths()
        for path in candidates:
            seen.add(path); reason = path_exclusion_reason(path, self.settings)
            raw = None; info = None; text = None
            if not reason:
                try:
                    raw, info = trusted.read(path, self.settings.maximum_note_bytes)
                    text = raw.decode("utf-8")
                except UnicodeError: reason = "unreadable"
                except VaultPolicyError as exc:
                    message = str(exc)
                    if "size limit" in message:
                        reason = "oversized"
                    else:
                        reason = "unsafe-path"
            if not reason and text is not None: reason = content_exclusion_reason(text, self.settings)
            row = store.note(path) if store else None
            if reason:
                if not row or row["status"] != "excluded" or row["exclusion_reason"] != reason:
                    if store and not dry_run: store.exclude(path, reason)
                    excluded += 1
                else: unchanged += 1
                continue
            assert raw is not None and text is not None and info is not None
            digest = source_sha(raw)
            if row and row["status"] == "active" and row["source_sha256"] == digest:
                unchanged += 1; continue
            chunks = chunk_markdown(text, digest, path, max_lines=self.settings.max_lines,
                                    max_chars=self.settings.max_chars, overlap_lines=self.settings.overlap_lines,
                                    corpus_id=self.settings.corpus_id,
                                    compatibility_signature=self.settings.compatibility_signature())
            if store and not dry_run:
                store.replace_note(path, digest, chunks, mtime_ns=info.st_mtime_ns, size_bytes=info.st_size)
            changed += 1
        previous = store.paths() if store else set()
        for path in sorted(previous - seen):
            if store and not dry_run: store.delete(path)
            removed += 1
        return {"changed": changed, "excluded": excluded, "unchanged": unchanged, "removed": removed}, candidates

    def run(self, *, full_reconcile: bool = False, dry_run: bool = False) -> dict:
        warnings: list[str] = []
        if not dry_run: self.acquire_lock()
        try:
            if dry_run:
                if self.settings.state.is_file():
                    self.store = Store(self.settings.state, settings=self.settings, read_only=True, immutable=True)
                with TrustedVault(self.settings.vault) as trusted:
                    counts, _ = self._scan(trusted, True)
                status = self.store.status() if self.store else {"active_notes": 0, "excluded_notes": 0, "chunks": 0,
                    "semantic_ready": 0, "pending_vectors": 0, "pending_tombstones": 0, "generated_at": None,
                    "age_seconds": None, "stale": bool(counts["changed"] or counts["excluded"] or counts["removed"]),
                    "compatibility_signature": self.settings.compatibility_signature()}
                return {"ok": True, **counts, "warnings": [], "dry_run": True, **status}
            self.store = Store(self.settings.state, settings=self.settings)
            with TrustedVault(self.settings.vault) as trusted, self.store.scan_transaction():
                counts, _ = self._scan(trusted, False)
                self.store.complete_scan()
            # Network projection is intentionally after the complete local generation commit.
            self._project_pending(warnings); self._apply_tombstones(warnings)
            if full_reconcile: self._reconcile_remote(warnings)
            return {"ok": True, **counts, "warnings": sorted(set(warnings)), "dry_run": False, **self.store.status()}
        finally:
            self.release_lock()

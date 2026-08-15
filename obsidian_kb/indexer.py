"""Read-only vault indexing and reconciliation."""
from __future__ import annotations

from fnmatch import fnmatch
import hashlib
from pathlib import Path

from .chunker import chunk_markdown, is_frontmatter_excluded
from .config import Settings
from .privacy import contains_secret
from .remote import OllamaClient, QdrantClient, RemoteError
from .store import Store


def source_sha(source: str | bytes) -> str:
    raw = source if isinstance(source, bytes) else source.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def path_exclusion_reason(path: str, settings: Settings) -> str | None:
    parts = Path(path).parts
    if settings.exclude_hidden and any(part.startswith(".") for part in parts): return "hidden-path"
    if any(part in settings.excluded_folders for part in parts[:-1]): return "excluded-folder"
    if any(fnmatch(path, pattern) for pattern in settings.excluded_globs): return "excluded-glob"
    return None


def content_exclusion_reason(text: str, settings: Settings) -> str | None:
    if is_frontmatter_excluded(text, settings.frontmatter_false_keys): return "frontmatter-false"
    if contains_secret(text, settings.extra_secret_patterns): return "credential-content"
    return None


class Indexer:
    def __init__(self, settings: Settings, *, ollama=None, qdrant=None):
        self.settings = settings
        self.store = Store(settings.state)
        self.ollama = ollama or (OllamaClient(settings.ollama_url, settings.ollama_model, settings.timeout) if settings.ollama_url else None)
        self.qdrant = qdrant or (QdrantClient(settings.qdrant_url, settings.qdrant_collection, settings.vector_size, settings.timeout) if settings.qdrant_url else None)

    def close(self): self.store.close()

    def _delete_remote(self, point_ids: list[str], warnings: list[str]):
        if self.qdrant and point_ids:
            try: self.qdrant.delete(point_ids)
            except RemoteError: warnings.append("semantic delete unavailable; stale points remain safely postfiltered")

    def run(self) -> dict:
        if not self.settings.vault.is_dir(): raise ValueError("vault is not a directory")
        warnings: list[str] = []
        seen: set[str] = set()
        changed = excluded = unchanged = removed = 0
        for file in sorted(self.settings.vault.rglob("*.md")):
            path = file.relative_to(self.settings.vault).as_posix()
            seen.add(path)
            reason = path_exclusion_reason(path, self.settings)
            if reason:
                row = self.store.note(path)
                if not row or row["status"] != "excluded" or row["exclusion_reason"] != reason:
                    self._delete_remote(self.store.point_ids(path), warnings)
                    self.store.exclude(path, reason); excluded += 1
                else: unchanged += 1
                continue
            try:
                raw = file.read_bytes()
                text = raw.decode("utf-8")
            except (OSError, UnicodeError):
                reason = "unreadable"
                old = self.store.point_ids(path)
                self._delete_remote(old, warnings)
                self.store.exclude(path, reason); excluded += 1
                continue
            reason = content_exclusion_reason(text, self.settings)
            if reason:
                row = self.store.note(path)
                if not row or row["status"] != "excluded" or row["exclusion_reason"] != reason:
                    self._delete_remote(self.store.point_ids(path), warnings)
                    self.store.exclude(path, reason); excluded += 1
                else: unchanged += 1
                continue
            digest = source_sha(raw)
            row = self.store.note(path)
            if row and row["status"] == "active" and row["source_sha256"] == digest:
                if not (self.ollama and self.qdrant and self.store.needs_semantic(path)):
                    unchanged += 1; continue
            chunks = chunk_markdown(text, digest, path, max_lines=self.settings.max_lines, max_chars=self.settings.max_chars)
            old_points = self.store.point_ids(path)
            semantic_ready = False
            if self.ollama and self.qdrant and chunks:
                try:
                    vectors = self.ollama.embed([chunk["content"] for chunk in chunks])
                    if any(len(vector) != self.settings.vector_size for vector in vectors):
                        raise RemoteError("embedding vector size mismatch")
                    self.qdrant.ensure()
                    self.qdrant.upsert([{"id": c["point_id"], "vector": v, "payload": {"chunk_id": c["chunk_id"]}}
                                        for c, v in zip(chunks, vectors)])
                    semantic_ready = True
                except RemoteError:
                    warnings.append(f"semantic indexing unavailable for {path}; lexical index committed")
            self._delete_remote([point for point in old_points if point not in {c['point_id'] for c in chunks}], warnings)
            self.store.replace_note(path, digest, chunks, semantic_ready); changed += 1
        for path in sorted(self.store.paths() - seen):
            self._delete_remote(self.store.point_ids(path), warnings)
            self.store.delete(path); removed += 1
        return {"ok": True, "changed": changed, "excluded": excluded, "unchanged": unchanged,
                "removed": removed, "warnings": sorted(set(warnings)), **self.store.counts()}

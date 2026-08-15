"""Read-only vault indexing with durable semantic projection recovery."""
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


class IndexLockError(RuntimeError):
    """Another indexer owns the single-writer lock."""


def source_sha(source:str|bytes)->str:
    raw=source if isinstance(source,bytes) else source.encode();return hashlib.sha256(raw).hexdigest()


def path_exclusion_reason(path:str,settings:Settings)->str|None:
    parts=Path(path).parts
    if not any(fnmatch(path,pattern) for pattern in settings.include_globs):return "not-included"
    if settings.exclude_hidden and any(part.startswith(".") for part in parts):return "hidden-path"
    if any(part in settings.excluded_folders for part in parts[:-1]):return "excluded-folder"
    if any(fnmatch(path,pattern) for pattern in settings.excluded_globs):return "excluded-glob"
    return None


def content_exclusion_reason(text:str,settings:Settings)->str|None:
    if is_frontmatter_excluded(text,settings.frontmatter_false_keys):return "frontmatter-excluded"
    if contains_secret(text,settings.extra_secret_patterns):return "credential-content"
    return None


class Indexer:
    def __init__(self,settings:Settings,*,ollama=None,qdrant=None):
        self.settings=settings;self.store=Store(settings.state,settings=settings)
        self.ollama=ollama or (OllamaClient(settings.ollama_url,settings.ollama_model,settings.timeout) if settings.ollama_url else None)
        self.qdrant=qdrant or (QdrantClient(settings.qdrant_url,settings.qdrant_collection,settings.vector_size,settings.timeout) if settings.qdrant_url else None)
        self._lock_handle=None
    @property
    def lock_path(self)->Path:return self.settings.state.with_suffix(self.settings.state.suffix+".lock")
    def acquire_lock(self)->None:
        if self._lock_handle:return
        self.lock_path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        handle=self.lock_path.open("a+");os.chmod(self.lock_path,0o600)
        try:fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:handle.close();raise IndexLockError("knowledge index is already running") from exc
        self._lock_handle=handle
    def release_lock(self)->None:
        if self._lock_handle:
            fcntl.flock(self._lock_handle.fileno(),fcntl.LOCK_UN);self._lock_handle.close();self._lock_handle=None
    def close(self):self.release_lock();self.store.close()

    def _project_pending(self,warnings:list[str])->None:
        pending=self.store.pending()
        if not pending:return
        if not (self.ollama and self.qdrant):
            warnings.append("semantic projection unavailable; deterministic pending work retained");return
        try:
            self.qdrant.ensure(self.settings.compatibility_signature())
            vectors=self.ollama.embed([row["content"] for row in pending])
            if len(vectors)!=len(pending) or any(len(v)!=self.settings.vector_size for v in vectors):raise RemoteError("embedding vector size mismatch")
            points=[]
            for row,vector in zip(pending,vectors):
                payload={"corpus_id":self.settings.corpus_id,"schema_version":self.settings.schema_version,
                         "chunk_id":row["chunk_id"],"content_sha256":row["desired_sha256"],
                         "embedding_model":self.settings.ollama_model}
                points.append({"id":row["point_id"],"vector":vector,"payload":payload})
            # Network publication precedes applied-state commit. A crash here retries the idempotent UUID upsert.
            self.qdrant.upsert(points)
            for row in pending:self.store.mark_projection(row["chunk_id"],True)
        except RemoteError as exc:
            for row in pending:self.store.mark_projection(row["chunk_id"],False,type(exc).__name__)
            warnings.append("semantic projection unavailable; deterministic pending work retained")

    def _apply_tombstones(self,warnings:list[str])->None:
        ids=self.store.pending_tombstones()
        if not ids:return
        if not self.qdrant:warnings.append("semantic deletion unavailable; tombstones retained");return
        try:self.qdrant.delete(ids);self.store.mark_tombstones(ids)
        except RemoteError:warnings.append("semantic deletion unavailable; tombstones retained")

    def _reconcile_remote(self,warnings:list[str])->None:
        if not self.qdrant:warnings.append("full reconciliation unavailable without Qdrant");return
        try:
            self.qdrant.ensure(self.settings.compatibility_signature())
            orphans=sorted(set(self.qdrant.list_ids(self.settings.corpus_id))-self.store.active_ids())
            if orphans:self.qdrant.delete(orphans)
        except RemoteError:warnings.append("full reconciliation unavailable; local postfilter remains authoritative")

    def run(self,*,full_reconcile:bool=False,dry_run:bool=False)->dict:
        self.acquire_lock()
        try:
            if not self.settings.vault.is_dir():raise ValueError("vault is not a directory")
            warnings:list[str]=[];seen:set[str]=set();changed=excluded=unchanged=removed=0
            candidates=sorted(self.settings.vault.rglob("*.md"))
            for file in candidates:
                try:path=file.relative_to(self.settings.vault).as_posix()
                except ValueError:continue
                seen.add(path);reason=path_exclusion_reason(path,self.settings)
                if not reason:
                    try:
                        if file.is_symlink() or not file.is_file() or not file.resolve().is_relative_to(self.settings.vault.resolve()):reason="unsafe-path"
                        elif file.stat().st_size>self.settings.maximum_note_bytes:reason="oversized"
                    except OSError:reason="unreadable"
                if reason:
                    row=self.store.note(path)
                    if not row or row["status"]!="excluded" or row["exclusion_reason"]!=reason:
                        if not dry_run:self.store.exclude(path,reason)
                        excluded+=1
                    else:unchanged+=1
                    continue
                try:raw=file.read_bytes();text=raw.decode("utf-8")
                except (OSError,UnicodeError):reason="unreadable"
                else:reason=content_exclusion_reason(text,self.settings)
                if reason:
                    row=self.store.note(path)
                    if not row or row["status"]!="excluded" or row["exclusion_reason"]!=reason:
                        if not dry_run:self.store.exclude(path,reason)
                        excluded+=1
                    else:unchanged+=1
                    continue
                digest=source_sha(raw);row=self.store.note(path)
                if row and row["status"]=="active" and row["source_sha256"]==digest:
                    unchanged+=1;continue
                chunks=chunk_markdown(text,digest,path,max_lines=self.settings.max_lines,max_chars=self.settings.max_chars,
                                      corpus_id=self.settings.corpus_id)
                stat=file.stat()
                if not dry_run:self.store.replace_note(path,digest,chunks,mtime_ns=stat.st_mtime_ns,size_bytes=stat.st_size)
                changed+=1
            for path in sorted(self.store.paths()-seen):
                if not dry_run:self.store.delete(path)
                removed+=1
            if not dry_run:
                self._project_pending(warnings);self._apply_tombstones(warnings)
                if full_reconcile:self._reconcile_remote(warnings)
            result={"ok":True,"changed":changed,"excluded":excluded,"unchanged":unchanged,"removed":removed,
                    "warnings":sorted(set(warnings)),"dry_run":dry_run,**self.store.status()}
            return result
        finally:self.release_lock()

"""Freshness-checked deterministic lexical retrieval and read-only fallback."""
from __future__ import annotations

import json
import re
import sqlite3

from .chunker import chunk_markdown
from .config import Settings, validate_relative_prefix
from .indexer import content_exclusion_reason, path_exclusion_reason, source_sha
from .store import CompatibilityError, Store
from .vault_io import TrustedVault, VaultPolicyError


def _fresh(row: dict, vault: TrustedVault, settings: Settings) -> bool:
    path = row["note_path"]
    if path_exclusion_reason(path, settings): return False
    try: raw, _ = vault.read(path, settings.maximum_note_bytes); text = raw.decode("utf-8")
    except (OSError, UnicodeError): return False
    return not content_exclusion_reason(text, settings) and source_sha(raw) == row["source_sha256"]


def _source_drift(settings: Settings, store: Store) -> tuple[int | None, bool]:
    live: dict[str, str] = {}
    with TrustedVault(settings.vault) as vault:
        paths = vault.markdown_paths(settings.freshness_max_files + 1)
        if len(paths) > settings.freshness_max_files: return None, False
        for path in paths:
            if path_exclusion_reason(path, settings): continue
            try: raw, _ = vault.read(path, settings.maximum_note_bytes); text = raw.decode("utf-8")
            except (VaultPolicyError, UnicodeError): continue
            except OSError: return None, False
            if not content_exclusion_reason(text, settings): live[path] = source_sha(raw)
    indexed = {row["path"]: row["source_sha256"] for row in store.conn.execute("SELECT path,source_sha256 FROM notes WHERE status='active'")}
    return len(set(live) ^ set(indexed)) + sum(live[path] != indexed[path] for path in set(live) & set(indexed)), True


def status_with_freshness(settings: Settings) -> dict:
    store = Store(settings.state, settings=settings, read_only=True, immutable=True)
    try:
        status = store.status(); drift, complete = _source_drift(settings, store)
        stale = bool(not complete or drift)
        return {**status, "source_drift_count": drift, "source_inventory_complete": complete,
                "stale": stale, "current": not stale, "compatibility": "current"}
    finally: store.close()


def _result(row: dict, rank: int, score: float) -> dict:
    heading = json.loads(row["heading_path"]) if isinstance(row.get("heading_path"), str) else row.get("heading_path", [])
    suffix = ("#" + heading[-1]) if heading else ""
    return {"rank": rank, "chunk_id": row["chunk_id"], "title": row["title"], "path": row["note_path"],
            "heading_path": heading, "heading": heading, "line_start": row["start_line"], "line_end": row["end_line"],
            "start_line": row["start_line"], "end_line": row["end_line"],
            "citation": f"{row['note_path']}:L{row['start_line']}-L{row['end_line']}",
            "obsidian_link": f"[[{row['note_path'].removesuffix('.md')}{suffix}]]", "snippet": row["snippet"],
            "untrusted_source": True, "retrieval_type": "lexical", "modes": ["lexical"], "score": score,
            "scores": {"bm25": score}}


def _filesystem_fallback(settings: Settings, question: str, limit: int, path_prefix: str | None, reason: str) -> dict:
    terms = [item.casefold() for item in re.findall(r"[\w-]+", question, flags=re.UNICODE)]
    ranked = []; scanned = 0
    with TrustedVault(settings.vault) as vault:
        for path in vault.markdown_paths(settings.fallback_max_files):
            if scanned >= settings.fallback_max_files: break
            if path_prefix and not (path == path_prefix or path.startswith(path_prefix + "/")): continue
            if path_exclusion_reason(path, settings): continue
            try: raw, _ = vault.read(path, settings.maximum_note_bytes); scanned += 1; text = raw.decode("utf-8")
            except (OSError, UnicodeError): continue
            if content_exclusion_reason(text, settings): continue
            for chunk in chunk_markdown(text, source_sha(raw), path, max_lines=settings.max_lines, max_chars=settings.max_chars,
                                        overlap_lines=settings.overlap_lines, corpus_id=settings.corpus_id,
                                        compatibility_signature=settings.compatibility_signature()):
                normalized = chunk["content"].casefold(); hits = sum(normalized.count(term) for term in terms)
                if hits:
                    row = {"chunk_id": chunk["chunk_id"], "note_path": path, "start_line": chunk["start_line"],
                           "end_line": chunk["end_line"], "title": chunk["title"], "heading_path": chunk["heading_path"],
                           "snippet": chunk["snippet"]}
                    ranked.append((-hits, path, chunk["start_line"], chunk["chunk_id"], row, float(hits)))
    results = [_result(item[4], rank, item[5]) for rank, item in enumerate(sorted(ranked)[:limit], 1)]
    return {"ok": True, "schema_version": "1.0", "query": question, "mode": "lexical", "fallback": True,
            "warnings": [reason], "index": {"generated_at": None, "age_seconds": None, "stale": True,
            "current": False, "compatibility": "unavailable", "source_drift_count": None},
            "results": results, "passages_are_untrusted": True}


def search(settings: Settings, question: str, *, limit: int = 5, path_prefix: str | None = None) -> dict:
    if not isinstance(question, str) or not question.strip(): raise ValueError("query must not be empty")
    if len(question) > 512: raise ValueError("query must not exceed 512 characters")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20: raise ValueError("limit must be between 1 and 20")
    prefix = validate_relative_prefix(path_prefix)
    store = None
    try:
        store = Store(settings.state, settings=settings, read_only=True, immutable=True)
        with TrustedVault(settings.vault) as trusted:
            rows = [row for row in store.lexical(question, limit * 4, prefix) if _fresh(row, trusted, settings)][:limit]
        results = [_result(row, rank, round(-float(row["lexical_score"]), 8)) for rank, row in enumerate(rows, 1)]
        info = store.status(); drift, complete = _source_drift(settings, store)
        stale = bool(not complete or drift)
        info.update({"source_drift_count": drift, "source_inventory_complete": complete,
                     "stale": stale, "current": not stale, "compatibility": "current"})
        return {"ok": True, "schema_version": "1.0", "query": question, "mode": "lexical", "fallback": False,
                "warnings": [], "index": info, "results": results, "passages_are_untrusted": True}
    except (OSError, sqlite3.Error, CompatibilityError) as exc:
        if store is not None:
            store.close(); store = None
        return _filesystem_fallback(settings, question, limit, prefix, f"SQLite unavailable ({type(exc).__name__}); bounded read-only fallback used")
    finally:
        if store is not None: store.close()

"""Freshness-checked deterministic lexical retrieval and read-only fallback."""
from __future__ import annotations

import json
import math
import sqlite3

from .chunker import chunk_markdown
from .config import Settings, validate_relative_prefix
from .indexer import content_exclusion_reason, path_exclusion_reason, source_sha
from .lexical import lexical_hits, lexical_tokens
from .store import CompatibilityError, Store
from .vault_io import TrustedVault, VaultPolicyError


class DerivedStateCorruption(RuntimeError):
    """SQLite-derived content disagrees with the current authoritative source."""


class CandidateScanBound(RuntimeError):
    """The safe candidate validation bound prevented complete paging."""


def _chunks(text: str, raw: bytes, path: str, settings: Settings) -> list[dict]:
    return chunk_markdown(
        text, source_sha(raw), path, max_lines=settings.max_lines,
        max_chars=settings.max_chars, overlap_lines=settings.overlap_lines,
        corpus_id=settings.corpus_id,
        compatibility_signature=settings.compatibility_signature(),
    )


def _derived_row(row: dict, vault: TrustedVault, settings: Settings) -> dict | None:
    """Return an exact source-derived row, None if stale, or raise on corruption."""
    path = row.get("note_path")
    if not isinstance(path, str):
        raise DerivedStateCorruption("invalid indexed path")
    try:
        validate_relative_prefix(path)
    except ValueError as exc:
        raise DerivedStateCorruption("unsafe indexed path") from exc
    if path_exclusion_reason(path, settings):
        return None
    try:
        raw, _ = vault.read(path, settings.maximum_note_bytes)
        text = raw.decode("utf-8")
    except (OSError, UnicodeError):
        return None
    if content_exclusion_reason(text, settings):
        return None
    digest = source_sha(raw)
    stored_digest = row.get("source_sha256")
    if not isinstance(stored_digest, str):
        raise DerivedStateCorruption("invalid source digest")
    if digest != stored_digest:
        return None

    try:
        heading = json.loads(row["heading_path"])
    except (KeyError, TypeError, json.JSONDecodeError, UnicodeError) as exc:
        raise DerivedStateCorruption("invalid heading JSON") from exc
    if not isinstance(heading, list) or any(not isinstance(item, str) for item in heading):
        raise DerivedStateCorruption("invalid heading structure")
    chunk_id = row.get("chunk_id")
    if not isinstance(chunk_id, str):
        raise DerivedStateCorruption("invalid chunk id")
    derived = next((item for item in _chunks(text, raw, path, settings) if item["chunk_id"] == chunk_id), None)
    if derived is None:
        raise DerivedStateCorruption("chunk id is not derivable from source")

    normalized = " ".join(derived["content"].split())
    expected = {
        "note_path": path,
        "ordinal": derived["ordinal"],
        "title": derived["title"],
        "heading_path": heading,
        "start_line": derived["start_line"],
        "end_line": derived["end_line"],
        "content": derived["content"],
        "normalized_text": normalized,
        "snippet": derived["snippet"],
        "content_sha256": derived["content_sha256"],
        "source_sha256": digest,
        "fts_title": derived["title"],
        "fts_heading": " > ".join(derived["heading_path"]),
        "fts_path": path,
        "fts_content": normalized,
    }
    comparisons = {
        "ordinal": derived["ordinal"], "title": derived["title"],
        "start_line": derived["start_line"], "end_line": derived["end_line"],
        "content": derived["content"], "normalized_text": normalized,
        "snippet": derived["snippet"], "content_sha256": derived["content_sha256"],
        "fts_title": derived["title"], "fts_heading": " > ".join(derived["heading_path"]),
        "fts_path": path, "fts_content": normalized,
    }
    if heading != derived["heading_path"] or any(row.get(key) != value for key, value in comparisons.items()):
        raise DerivedStateCorruption("indexed fields disagree with source")
    try:
        score = float(row["lexical_score"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DerivedStateCorruption("invalid lexical score") from exc
    if not math.isfinite(score):
        raise DerivedStateCorruption("non-finite lexical score")
    return {"chunk_id": chunk_id, **expected, "lexical_score": score}


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
    indexed: dict[str, str] = {}
    for row in store.conn.execute("SELECT path,source_sha256 FROM notes WHERE status='active'"):
        path, digest = row["path"], row["source_sha256"]
        if not isinstance(path, str) or not isinstance(digest, str):
            raise DerivedStateCorruption("invalid indexed source inventory")
        try: validate_relative_prefix(path)
        except ValueError as exc: raise DerivedStateCorruption("unsafe indexed source inventory") from exc
        indexed[path] = digest
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
    heading = row.get("heading_path", [])
    if not isinstance(heading, list) or any(not isinstance(item, str) for item in heading):
        raise DerivedStateCorruption("result heading is malformed")
    suffix = ("#" + heading[-1]) if heading else ""
    return {"rank": rank, "chunk_id": row["chunk_id"], "title": row["title"], "path": row["note_path"],
            "heading_path": heading, "heading": heading, "line_start": row["start_line"], "line_end": row["end_line"],
            "start_line": row["start_line"], "end_line": row["end_line"],
            "citation": f"{row['note_path']}:L{row['start_line']}-L{row['end_line']}",
            "obsidian_link": f"[[{row['note_path'].removesuffix('.md')}{suffix}]]", "snippet": row["snippet"],
            "untrusted_source": True, "retrieval_type": "lexical", "modes": ["lexical"], "score": score,
            "scores": {"bm25": score}}


def _filesystem_fallback(settings: Settings, question: str, limit: int, path_prefix: str | None, reason: str) -> dict:
    terms = lexical_tokens(question)
    ranked = []; scanned = 0; bounded = False
    with TrustedVault(settings.vault) as vault:
        paths = vault.markdown_paths(settings.fallback_max_files + 1)
        bounded = len(paths) > settings.fallback_max_files
        for path in paths[:settings.fallback_max_files]:
            if path_prefix and not (path == path_prefix or path.startswith(path_prefix + "/")): continue
            if path_exclusion_reason(path, settings): continue
            try: raw, _ = vault.read(path, settings.maximum_note_bytes); scanned += 1; text = raw.decode("utf-8")
            except (OSError, UnicodeError): continue
            if content_exclusion_reason(text, settings): continue
            for chunk in _chunks(text, raw, path, settings):
                hits = lexical_hits(terms, chunk["title"], " > ".join(chunk["heading_path"]), path, chunk["content"])
                if hits:
                    row = {"chunk_id": chunk["chunk_id"], "note_path": path, "start_line": chunk["start_line"],
                           "end_line": chunk["end_line"], "title": chunk["title"], "heading_path": chunk["heading_path"],
                           "snippet": chunk["snippet"]}
                    ranked.append((-hits, path, chunk["start_line"], chunk["chunk_id"], row, float(hits)))
    results = [_result(item[4], rank, item[5]) for rank, item in enumerate(sorted(ranked)[:limit], 1)]
    warnings = [reason]
    if bounded:
        warnings.append("filesystem fallback file bound reached; results are truthful but may be incomplete")
    return {"ok": True, "schema_version": "1.0", "query": question, "mode": "lexical", "fallback": True,
            "warnings": warnings, "index": {"generated_at": None, "age_seconds": None, "stale": True,
            "current": False, "compatibility": "unavailable", "source_drift_count": None,
            "source_inventory_complete": not bounded},
            "results": results, "passages_are_untrusted": True}


def _fresh_candidates(store: Store, trusted: TrustedVault, settings: Settings, question: str,
                      limit: int, prefix: str | None) -> list[dict]:
    page_size = max(64, limit * 4)
    maximum_candidates = 10_000
    offset = 0
    output: list[dict] = []
    while offset < maximum_candidates and len(output) < limit:
        amount = min(page_size, maximum_candidates - offset)
        page = store.lexical(question, amount, prefix, offset=offset)
        for row in page:
            derived = _derived_row(row, trusted, settings)
            if derived is not None:
                output.append(derived)
                if len(output) == limit:
                    break
        offset += len(page)
        if len(page) < amount:
            return output
    if len(output) < limit:
        probe = store.lexical(question, 1, prefix, offset=offset)
        if probe:
            raise CandidateScanBound("SQLite candidate validation bound reached")
    return output


def search(settings: Settings, question: str, *, limit: int = 5, path_prefix: str | None = None) -> dict:
    if not isinstance(question, str) or not question.strip(): raise ValueError("query must not be empty")
    if len(question) > 512: raise ValueError("query must not exceed 512 characters")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20: raise ValueError("limit must be between 1 and 20")
    prefix = validate_relative_prefix(path_prefix)
    store = None
    try:
        store = Store(settings.state, settings=settings, read_only=True, immutable=True)
        store.assert_search_integrity()
        with TrustedVault(settings.vault) as trusted:
            rows = _fresh_candidates(store, trusted, settings, question, limit, prefix)
        results = [_result(row, rank, round(-float(row["lexical_score"]), 8)) for rank, row in enumerate(rows, 1)]
        info = store.status(); drift, complete = _source_drift(settings, store)
        stale = bool(not complete or drift)
        info.update({"source_drift_count": drift, "source_inventory_complete": complete,
                     "stale": stale, "current": not stale, "compatibility": "current"})
        return {"ok": True, "schema_version": "1.0", "query": question, "mode": "lexical", "fallback": False,
                "warnings": [], "index": info, "results": results, "passages_are_untrusted": True}
    except (OSError, sqlite3.Error, CompatibilityError, DerivedStateCorruption, CandidateScanBound) as exc:
        if store is not None:
            store.close(); store = None
        return _filesystem_fallback(settings, question, limit, prefix,
                                    f"SQLite unavailable or untrusted ({type(exc).__name__}); bounded read-only fallback used")
    finally:
        if store is not None: store.close()

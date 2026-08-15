"""Complete live-scan retrieval through a private ephemeral SQLite FTS5 corpus."""
from __future__ import annotations

from .config import Settings, validate_relative_prefix
from .corpus import MemoryFTS, scan


def _result(chunk: dict, rank: int, score: float) -> dict:
    heading = chunk["heading_path"]
    suffix = ("#" + heading[-1]) if heading else ""
    path = chunk["file_path"]
    start = chunk["start_line"]
    end = chunk["end_line"]
    return {
        "rank": rank,
        "chunk_id": chunk["chunk_id"],
        "title": chunk["title"],
        "path": path,
        "heading_path": heading,
        "heading": heading,
        "line_start": start,
        "line_end": end,
        "start_line": start,
        "end_line": end,
        "citation": f"{path}:L{start}-L{end}",
        "obsidian_link": f"[[{path.removesuffix('.md')}{suffix}]]",
        "snippet": chunk["snippet"],
        "untrusted_source": True,
        "retrieval_type": "lexical",
        "modes": ["lexical"],
        "score": score,
        "scores": {"bm25": score},
    }


def search(settings: Settings, question: str, *, limit: int = 5,
           path_prefix: str | None = None) -> dict:
    if not isinstance(question, str) or not question.strip():
        raise ValueError("query must not be empty")
    if len(question) > 512:
        raise ValueError("query must not exceed 512 characters")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")
    prefix = validate_relative_prefix(path_prefix)

    # A complete internally consistent point-in-time snapshot is scanned and
    # chunked before SQLite exists. The mutable authority may change afterward.
    corpus = scan(settings)
    with MemoryFTS(corpus) as database:
        rows = database.query(question, limit, prefix)
    results = [
        _result(chunk, rank, round(-score, 8))
        for rank, (chunk, score) in enumerate(rows, 1)
    ]
    return {
        "ok": True,
        "schema_version": "1.0",
        "query": question,
        "mode": "lexical",
        "fallback": False,
        "warnings": [],
        "index": {"ephemeral": True, "persistence": False, **corpus.status()},
        "results": results,
        "passages_are_untrusted": True,
    }

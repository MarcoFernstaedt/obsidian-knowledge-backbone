"""Freshness-checked deterministic hybrid retrieval."""
from __future__ import annotations

import json
from pathlib import Path

from .config import Settings
from .indexer import source_sha
from .remote import OllamaClient, QdrantClient, RemoteError
from .store import Store


def reciprocal_rank_fusion(lexical: list[dict], semantic: list[dict], limit: int, constant: int = 60) -> list[dict]:
    combined: dict[str, dict] = {}
    for mode, rows in (("lexical", lexical), ("semantic", semantic)):
        for rank, row in enumerate(rows, 1):
            key = row["chunk_id"]
            entry = combined.setdefault(key, {"row": row, "score": 0.0, "modes": set()})
            entry["score"] += 1.0 / (constant + rank)
            entry["modes"].add(mode)
    ordered = sorted(combined.values(), key=lambda item: (-item["score"], item["row"]["note_path"],
                                                         item["row"]["start_line"], item["row"]["chunk_id"]))[:limit]
    maximum = 2.0 / (constant + 1)
    output = []
    for item in ordered:
        row = dict(item["row"])
        row["score"] = round(item["score"] / maximum, 6)
        row["modes"] = sorted(item["modes"])
        output.append(row)
    return output


def _fresh(row: dict, vault: Path) -> bool:
    try: text = (vault / row["note_path"]).read_text(encoding="utf-8")
    except (OSError, UnicodeError): return False
    return source_sha(text) == row["source_sha256"]


def search(settings: Settings, question: str, *, limit: int = 5, offline: bool = False,
           ollama=None, qdrant=None) -> dict:
    if not question.strip(): raise ValueError("query must not be empty")
    if limit < 1 or limit > 50: raise ValueError("limit must be between 1 and 50")
    store = Store(settings.state, read_only=True)
    warnings: list[str] = []
    try:
        lexical = [row for row in store.lexical(question, limit * 4) if _fresh(row, settings.vault)]
        semantic: list[dict] = []
        if not offline and settings.ollama_url and settings.qdrant_url:
            embedder = ollama or OllamaClient(settings.ollama_url, settings.ollama_model, settings.timeout)
            vector_db = qdrant or QdrantClient(settings.qdrant_url, settings.qdrant_collection, settings.vector_size, settings.timeout)
            try:
                vector = embedder.embed([question])[0]
                points = vector_db.query(vector, limit * 4)
                identifiers = [str(point.get("payload", {}).get("chunk_id", "")) for point in points]
                allowed = store.active_semantic([value for value in identifiers if value])
                semantic = [allowed[value] for value in identifiers if value in allowed and _fresh(allowed[value], settings.vault)]
            except (RemoteError, IndexError):
                warnings.append("semantic retrieval unavailable; lexical-only results returned")
        fused = reciprocal_rank_fusion(lexical, semantic, limit)
        results = []
        for row in fused:
            heading = json.loads(row["heading_path"])
            results.append({"citation": f"{row['note_path']}:{row['start_line']}-{row['end_line']}",
                            "path": row["note_path"], "start_line": row["start_line"], "end_line": row["end_line"],
                            "title": row["title"], "heading": heading, "modes": row["modes"],
                            "score": row["score"], "snippet": row["snippet"]})
        return {"ok": True, "query": question, "results": results, "warnings": warnings,
                "degraded": bool(warnings) or offline or not (settings.ollama_url and settings.qdrant_url)}
    finally:
        store.close()

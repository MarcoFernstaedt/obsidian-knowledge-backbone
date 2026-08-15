"""Memory-bounded standard-library HTTP clients for Ollama and Qdrant."""
from __future__ import annotations

import json
import hashlib
import re
from collections.abc import Iterator
from urllib import error, parse, request

DEFAULT_RESPONSE_MAX_BYTES = 8 * 1024 * 1024
QDRANT_MAX_SCROLL_PAGES = 1024
QDRANT_MAX_SCROLL_POINTS = 100_000
QDRANT_MAX_SCROLL_BYTES = 64 * 1024 * 1024


class RemoteError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message); self.status = status


def _json_sized(method: str, url: str, payload: dict | None, timeout: float,
                maximum_bytes: int = DEFAULT_RESPONSE_MAX_BYTES) -> tuple[dict, int]:
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    req = request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read(maximum_bytes + 1)
    except error.HTTPError as exc: raise RemoteError("remote HTTP request failed", status=exc.code) from exc
    except (OSError, error.URLError) as exc: raise RemoteError(f"remote request failed: {type(exc).__name__}") from exc
    if len(raw) > maximum_bytes: raise RemoteError("remote response exceeds byte limit")
    try:
        value = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc: raise RemoteError("remote returned invalid JSON") from exc
    if not isinstance(value, dict): raise RemoteError("remote returned non-object JSON")
    return value, len(raw)


def _json(method: str, url: str, payload: dict | None, timeout: float,
          maximum_bytes: int = DEFAULT_RESPONSE_MAX_BYTES) -> dict:
    return _json_sized(method, url, payload, timeout, maximum_bytes)[0]


class OllamaClient:
    def __init__(self, url: str, model: str, timeout: float = 10,
                 response_max_bytes: int = DEFAULT_RESPONSE_MAX_BYTES):
        self.url, self.model, self.timeout = url.rstrip("/"), model, min(timeout, 30)
        self.response_max_bytes = response_max_bytes

    def embed(self, texts: list[str]) -> list[list[float]]:
        data = _json("POST", self.url + "/api/embed", {"model": self.model, "input": texts},
                     self.timeout, self.response_max_bytes)
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts): raise RemoteError("Ollama response missing embeddings")
        if any(not isinstance(v, list) or any(not isinstance(x, (int, float)) for x in v) for v in vectors):
            raise RemoteError("Ollama returned invalid vectors")
        return vectors


class QdrantClient:
    def __init__(self, url: str, collection: str, vector_size: int, timeout: float = 10,
                 response_max_bytes: int = DEFAULT_RESPONSE_MAX_BYTES):
        self.url, self.collection, self.vector_size, self.timeout = url.rstrip("/"), collection, vector_size, min(timeout, 30)
        self.response_max_bytes = response_max_bytes

    def collection_name(self, signature: str | None) -> str:
        """Map the documented logical base to a deterministic generation collection."""
        if signature is None:
            return self.collection
        suffix = signature.casefold() if re.fullmatch(r"[0-9a-fA-F]{64}", signature) else hashlib.sha256(signature.encode()).hexdigest()
        return f"{self.collection[:188]}__{suffix}"

    def endpoint(self, signature: str | None) -> str:
        return f"{self.url}/collections/{parse.quote(self.collection_name(signature), safe='')}"

    @staticmethod
    def _filter(corpus_id: str, signature: str | None = None) -> dict:
        must = [{"key": "corpus_id", "match": {"value": corpus_id}}]
        if signature is not None:
            must.append({"key": "compatibility_signature", "match": {"value": signature}})
        return {"must": must}

    def _scroll(self, corpus_id: str, signature: str | None, *, with_payload: bool,
                collection_signature: str | None = None) -> Iterator[dict]:
        offset = None; total_points = 0; total_bytes = 0
        for _ in range(QDRANT_MAX_SCROLL_PAGES):
            payload: dict = {"limit": 256, "with_payload": with_payload, "with_vector": False,
                             "filter": self._filter(corpus_id, signature)}
            if offset is not None: payload["offset"] = offset
            physical_signature = signature if collection_signature is None else collection_signature
            data, page_bytes = _json_sized("POST", self.endpoint(physical_signature) + "/points/scroll", payload,
                                           self.timeout, self.response_max_bytes)
            total_bytes += page_bytes
            result = data.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("points"), list):
                raise RemoteError("Qdrant scroll response invalid")
            rows = result["points"]
            total_points += len(rows)
            if total_points > QDRANT_MAX_SCROLL_POINTS or total_bytes > QDRANT_MAX_SCROLL_BYTES:
                raise RemoteError("Qdrant scroll aggregate limit exceeded")
            for row in rows:
                if not isinstance(row, dict) or "id" not in row:
                    raise RemoteError("Qdrant scroll point invalid")
                if with_payload and not isinstance(row.get("payload"), dict):
                    raise RemoteError("Qdrant scroll point payload invalid")
                yield row
            next_offset = result.get("next_page_offset")
            if next_offset is None: return
            if next_offset == offset:
                raise RemoteError("Qdrant scroll offset did not advance")
            offset = next_offset
        else: raise RemoteError("Qdrant scroll pagination limit exceeded")

    def ensure(self, corpus_id: str | None = None, signature: str | None = None,
               model_digest: str | None = None) -> None:
        try:
            data = _json("GET", self.endpoint(signature), None, self.timeout, self.response_max_bytes)
        except RemoteError as exc:
            if exc.status != 404: raise
            _json("PUT", self.endpoint(signature), {"vectors": {"size": self.vector_size, "distance": "Cosine"}},
                  self.timeout, self.response_max_bytes)
            return
        result = data.get("result")
        if not isinstance(result, dict): raise RemoteError("Qdrant collection metadata invalid")
        config = result.get("config"); params = config.get("params") if isinstance(config, dict) else None
        vectors = params.get("vectors") if isinstance(params, dict) else None
        size = vectors.get("size") if isinstance(vectors, dict) else None
        distance = vectors.get("distance") if isinstance(vectors, dict) else None
        if not isinstance(size, int) or isinstance(size, bool) or distance != "Cosine":
            raise RemoteError("Qdrant collection metadata invalid")
        if size != self.vector_size: raise RemoteError("Qdrant collection vector size mismatch")
        if corpus_id is None: return
        # Inspect every point for the corpus without a signature filter. Missing or mixed generations fail closed.
        for point in self._scroll(corpus_id, None, with_payload=True, collection_signature=signature):
            payload = point.get("payload") if isinstance(point.get("payload"), dict) else {}
            if payload.get("compatibility_signature") != signature or payload.get("model_digest") != model_digest:
                raise RemoteError("Qdrant corpus contains a missing or incompatible generation signature")

    def upsert(self, points: list[dict], signature: str) -> None:
        if points: _json("PUT", self.endpoint(signature) + "/points?wait=true", {"points": points}, self.timeout, self.response_max_bytes)

    def delete(self, point_ids: list[str], corpus_id: str | None = None,
               signature: str | None = None) -> None:
        if not point_ids: return
        selector: dict
        if corpus_id and signature:
            scoped = self._filter(corpus_id, signature)
            scoped["must"].append({"has_id": point_ids})
            selector = {"filter": scoped}
        else:
            selector = {"points": point_ids}
        _json("POST", self.endpoint(signature) + "/points/delete?wait=true", selector, self.timeout, self.response_max_bytes)

    def query(self, vector: list[float], limit: int, corpus_id: str | None = None,
              signature: str | None = None) -> list[dict]:
        payload: dict = {"query": vector, "limit": limit, "with_payload": True}
        if corpus_id: payload["filter"] = self._filter(corpus_id, signature)
        data = _json("POST", self.endpoint(signature) + "/points/query", payload, self.timeout, self.response_max_bytes)
        result = data.get("result")
        points = result.get("points") if isinstance(result, dict) else None
        if not isinstance(points, list): raise RemoteError("Qdrant query response invalid")
        for point in points:
            if not isinstance(point, dict) or not isinstance(point.get("payload"), dict):
                raise RemoteError("Qdrant query point invalid")
            if corpus_id and (point["payload"].get("corpus_id") != corpus_id or
                              point["payload"].get("compatibility_signature") != signature or
                              not isinstance(point["payload"].get("chunk_id"), str)):
                raise RemoteError("Qdrant query point metadata invalid")
        return points

    def list_ids(self, corpus_id: str, signature: str) -> list[str]:
        return [str(point["id"]) for point in self._scroll(corpus_id, signature, with_payload=False) if "id" in point]

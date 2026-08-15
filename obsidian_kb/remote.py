"""Memory-bounded standard-library HTTP clients for Ollama and Qdrant."""
from __future__ import annotations

import json
from urllib import error, parse, request

DEFAULT_RESPONSE_MAX_BYTES = 8 * 1024 * 1024


class RemoteError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message); self.status = status


def _json(method: str, url: str, payload: dict | None, timeout: float,
          maximum_bytes: int = DEFAULT_RESPONSE_MAX_BYTES) -> dict:
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
    return value


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

    @property
    def endpoint(self) -> str: return f"{self.url}/collections/{parse.quote(self.collection, safe='')}"

    @staticmethod
    def _filter(corpus_id: str, signature: str | None = None) -> dict:
        must = [{"key": "corpus_id", "match": {"value": corpus_id}}]
        if signature is not None:
            must.append({"key": "compatibility_signature", "match": {"value": signature}})
        return {"must": must}

    def _scroll(self, corpus_id: str, signature: str | None, *, with_payload: bool) -> list[dict]:
        points: list[dict] = []; offset = None
        for _ in range(10_000):
            payload: dict = {"limit": 256, "with_payload": with_payload, "with_vector": False,
                             "filter": self._filter(corpus_id, signature)}
            if offset is not None: payload["offset"] = offset
            result = _json("POST", self.endpoint + "/points/scroll", payload, self.timeout,
                           self.response_max_bytes).get("result", {})
            rows = result.get("points", []) if isinstance(result, dict) else []
            if not isinstance(rows, list): raise RemoteError("Qdrant scroll response invalid")
            points.extend(row for row in rows if isinstance(row, dict))
            offset = result.get("next_page_offset") if isinstance(result, dict) else None
            if offset is None: break
        else: raise RemoteError("Qdrant scroll pagination limit exceeded")
        return points

    def ensure(self, corpus_id: str | None = None, signature: str | None = None,
               model_digest: str | None = None) -> None:
        try:
            data = _json("GET", self.endpoint, None, self.timeout, self.response_max_bytes)
        except RemoteError as exc:
            if exc.status != 404: raise
            _json("PUT", self.endpoint, {"vectors": {"size": self.vector_size, "distance": "Cosine"}},
                  self.timeout, self.response_max_bytes)
            return
        result = data.get("result", {})
        vectors = result.get("config", {}).get("params", {}).get("vectors", {}) if isinstance(result, dict) else {}
        size = vectors.get("size") if isinstance(vectors, dict) else None
        if size is not None and size != self.vector_size: raise RemoteError("Qdrant collection vector size mismatch")
        if corpus_id is None: return
        # Inspect every point for the corpus without a signature filter. Missing or mixed generations fail closed.
        for point in self._scroll(corpus_id, None, with_payload=True):
            payload = point.get("payload") if isinstance(point.get("payload"), dict) else {}
            if payload.get("compatibility_signature") != signature or payload.get("model_digest") != model_digest:
                raise RemoteError("Qdrant corpus contains a missing or incompatible generation signature")

    def upsert(self, points: list[dict]) -> None:
        if points: _json("PUT", self.endpoint + "/points?wait=true", {"points": points}, self.timeout, self.response_max_bytes)

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
        _json("POST", self.endpoint + "/points/delete?wait=true", selector, self.timeout, self.response_max_bytes)

    def query(self, vector: list[float], limit: int, corpus_id: str | None = None,
              signature: str | None = None) -> list[dict]:
        payload: dict = {"query": vector, "limit": limit, "with_payload": True}
        if corpus_id: payload["filter"] = self._filter(corpus_id, signature)
        data = _json("POST", self.endpoint + "/points/query", payload, self.timeout, self.response_max_bytes)
        result = data.get("result", {}); points = result.get("points", result) if isinstance(result, dict) else result
        return points if isinstance(points, list) else []

    def list_ids(self, corpus_id: str, signature: str) -> list[str]:
        return [str(point["id"]) for point in self._scroll(corpus_id, signature, with_payload=False) if "id" in point]

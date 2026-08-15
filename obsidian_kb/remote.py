"""Minimal urllib clients for Ollama and Qdrant."""
from __future__ import annotations

import json
from urllib import request, error, parse


class RemoteError(RuntimeError):
    pass


def _json(method: str, url: str, payload: dict | None, timeout: float) -> dict:
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    req = request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
    except (OSError, error.URLError, error.HTTPError) as exc:
        raise RemoteError(f"remote request failed: {type(exc).__name__}") from exc
    try:
        return json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise RemoteError("remote returned invalid JSON") from exc


class OllamaClient:
    def __init__(self, url: str, model: str, timeout: float = 10):
        self.url, self.model, self.timeout = url.rstrip("/"), model, timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        data = _json("POST", self.url + "/api/embed", {"model": self.model, "input": texts}, self.timeout)
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise RemoteError("Ollama response missing embeddings")
        return vectors


class QdrantClient:
    def __init__(self, url: str, collection: str, vector_size: int, timeout: float = 10):
        self.url, self.collection, self.vector_size, self.timeout = url.rstrip("/"), collection, vector_size, timeout

    @property
    def endpoint(self) -> str:
        return f"{self.url}/collections/{parse.quote(self.collection, safe='')}"

    def ensure(self) -> None:
        try:
            _json("GET", self.endpoint, None, self.timeout)
        except RemoteError:
            _json("PUT", self.endpoint, {"vectors": {"size": self.vector_size, "distance": "Cosine"}}, self.timeout)

    def upsert(self, points: list[dict]) -> None:
        if points:
            _json("PUT", self.endpoint + "/points?wait=true", {"points": points}, self.timeout)

    def delete(self, point_ids: list[str]) -> None:
        if point_ids:
            _json("POST", self.endpoint + "/points/delete?wait=true", {"points": point_ids}, self.timeout)

    def query(self, vector: list[float], limit: int) -> list[dict]:
        data = _json("POST", self.endpoint + "/points/query", {"query": vector, "limit": limit, "with_payload": True}, self.timeout)
        result = data.get("result", {})
        points = result.get("points", result) if isinstance(result, dict) else result
        return points if isinstance(points, list) else []

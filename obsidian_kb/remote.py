"""Bounded standard-library HTTP clients for Ollama and Qdrant."""
from __future__ import annotations

import json
from urllib import error, parse, request


class RemoteError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message); self.status = status


def _json(method: str, url: str, payload: dict | None, timeout: float) -> dict:
    data=None if payload is None else json.dumps(payload,separators=(",", ":")).encode()
    req=request.Request(url,data=data,method=method,headers={"Content-Type":"application/json"})
    try:
        with request.urlopen(req,timeout=timeout) as response: raw=response.read()
    except error.HTTPError as exc: raise RemoteError("remote HTTP request failed",status=exc.code) from exc
    except (OSError,error.URLError) as exc: raise RemoteError(f"remote request failed: {type(exc).__name__}") from exc
    try:return json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:raise RemoteError("remote returned invalid JSON") from exc


class OllamaClient:
    def __init__(self,url:str,model:str,timeout:float=10):self.url,self.model,self.timeout=url.rstrip("/"),model,min(timeout,30)
    def embed(self,texts:list[str])->list[list[float]]:
        data=_json("POST",self.url+"/api/embed",{"model":self.model,"input":texts},self.timeout)
        vectors=data.get("embeddings")
        if not isinstance(vectors,list) or len(vectors)!=len(texts):raise RemoteError("Ollama response missing embeddings")
        if any(not isinstance(v,list) or any(not isinstance(x,(int,float)) for x in v) for v in vectors):raise RemoteError("Ollama returned invalid vectors")
        return vectors


class QdrantClient:
    def __init__(self,url:str,collection:str,vector_size:int,timeout:float=10):
        self.url,self.collection,self.vector_size,self.timeout=url.rstrip("/"),collection,vector_size,min(timeout,30)
    @property
    def endpoint(self)->str:return f"{self.url}/collections/{parse.quote(self.collection,safe='')}"
    def ensure(self,signature:str|None=None)->None:
        try:data=_json("GET",self.endpoint,None,self.timeout)
        except RemoteError as exc:
            if exc.status!=404:raise
            _json("PUT",self.endpoint,{"vectors":{"size":self.vector_size,"distance":"Cosine"}},self.timeout);return
        result=data.get("result",{})
        vectors=result.get("config",{}).get("params",{}).get("vectors",{}) if isinstance(result,dict) else {}
        size=vectors.get("size") if isinstance(vectors,dict) else None
        if size is not None and size!=self.vector_size:raise RemoteError("Qdrant collection vector size mismatch")
    def upsert(self,points:list[dict])->None:
        if points:_json("PUT",self.endpoint+"/points?wait=true",{"points":points},self.timeout)
    def delete(self,point_ids:list[str])->None:
        if point_ids:_json("POST",self.endpoint+"/points/delete?wait=true",{"points":point_ids},self.timeout)
    def query(self,vector:list[float],limit:int,corpus_id:str|None=None)->list[dict]:
        payload={"query":vector,"limit":limit,"with_payload":True}
        if corpus_id:payload["filter"]={"must":[{"key":"corpus_id","match":{"value":corpus_id}}]}
        data=_json("POST",self.endpoint+"/points/query",payload,self.timeout)
        result=data.get("result",{});points=result.get("points",result) if isinstance(result,dict) else result
        return points if isinstance(points,list) else []
    def list_ids(self,corpus_id:str)->list[str]:
        ids:list[str]=[]; offset=None
        for _ in range(10_000):
            payload={"limit":256,"with_payload":False,"with_vector":False,
                     "filter":{"must":[{"key":"corpus_id","match":{"value":corpus_id}}]}}
            if offset is not None:payload["offset"]=offset
            data=_json("POST",self.endpoint+"/points/scroll",payload,self.timeout).get("result",{})
            points=data.get("points",[]) if isinstance(data,dict) else []
            ids.extend(str(p["id"]) for p in points if isinstance(p,dict) and "id" in p)
            offset=data.get("next_page_offset") if isinstance(data,dict) else None
            if offset is None:break
        return ids

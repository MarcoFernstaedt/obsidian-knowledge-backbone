"""Freshness-checked deterministic hybrid retrieval and read-only fallback."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import sqlite3

from .chunker import chunk_markdown
from .config import Settings, validate_relative_prefix
from .indexer import content_exclusion_reason, path_exclusion_reason, source_sha
from .remote import OllamaClient, QdrantClient, RemoteError
from .store import CompatibilityError, Store
from .vault_io import TrustedVault, VaultPolicyError


def reciprocal_rank_fusion(lexical:list[dict],semantic:list[dict],limit:int,constant:int=60)->list[dict]:
    combined:dict[str,dict]={}
    for mode,rows,weight in (("lexical",lexical,0.40),("semantic",semantic,0.60)):
        for rank,row in enumerate(rows,1):
            entry=combined.setdefault(row["chunk_id"],{"row":row,"fusion":0.0,"lexical_rank":None,"semantic_rank":None})
            entry["fusion"]+=weight/(constant+rank);entry[mode+"_rank"]=rank
    maximum=1.0/(constant+1)
    ordered=sorted(combined.values(),key=lambda x:(-x["fusion"],min(v for v in (x["lexical_rank"],x["semantic_rank"]) if v is not None),
                                                         x["row"]["note_path"],x["row"]["start_line"],x["row"]["chunk_id"]))[:limit]
    output=[]
    for item in ordered:
        row=dict(item["row"]);row["fusion_score"]=round(item["fusion"]/maximum,6)
        row["score"]=row["fusion_score"];row["lexical_rank"]=item["lexical_rank"];row["semantic_rank"]=item["semantic_rank"]
        row["modes"]=[m for m in ("lexical","semantic") if item[m+"_rank"] is not None];output.append(row)
    return output


def _fresh(row:dict,vault:TrustedVault,settings:Settings)->bool:
    path=row["note_path"]
    if path_exclusion_reason(path,settings):return False
    try:
        raw,_=vault.read(path,settings.maximum_note_bytes)
        text=raw.decode("utf-8")
    except (OSError,UnicodeError):return False
    return not content_exclusion_reason(text,settings) and source_sha(raw)==row["source_sha256"]


def _index_info(store:Store)->dict:
    status=store.status();return {"generated_at":status["generated_at"],"age_seconds":status["age_seconds"],
                                  "stale":status["stale"],"pending_vectors":status["pending_vectors"],
                                  "pending_tombstones":status["pending_tombstones"],
                                  "compatibility_signature":status["compatibility_signature"]}


def _source_drift(settings:Settings,store:Store)->tuple[int | None, bool]:
    live:dict[str,str]={}
    with TrustedVault(settings.vault) as vault:
        paths = vault.markdown_paths(settings.freshness_max_files + 1)
        if len(paths) > settings.freshness_max_files:
            return None, False
        for path in paths:
            if path_exclusion_reason(path,settings):continue
            try:raw,_=vault.read(path,settings.maximum_note_bytes);text=raw.decode("utf-8")
            except (VaultPolicyError,UnicodeError):continue
            except OSError:return None, False
            if not content_exclusion_reason(text,settings):live[path]=source_sha(raw)
    indexed={row["path"]:row["source_sha256"] for row in store.conn.execute("SELECT path,source_sha256 FROM notes WHERE status='active'")}
    return len(set(live)^set(indexed))+sum(live[path]!=indexed[path] for path in set(live)&set(indexed)), True


def status_with_freshness(settings:Settings)->dict:
    store=Store(settings.state,settings=settings,read_only=True)
    try:
        status=store.status();drift,complete=_source_drift(settings,store)
        return {**status,"source_drift_count":drift,"source_inventory_complete":complete,
                "stale":bool(status["stale"] or not complete or drift)}
    finally:store.close()


def _result(row:dict,rank:int)->dict:
    heading=json.loads(row["heading_path"]) if isinstance(row.get("heading_path"),str) else row.get("heading_path",[])
    suffix=("#"+heading[-1]) if heading else ""
    return {"rank":rank,"chunk_id":row["chunk_id"],"title":row["title"],"path":row["note_path"],
            "heading_path":heading,"heading":heading,"line_start":row["start_line"],"line_end":row["end_line"],
            "start_line":row["start_line"],"end_line":row["end_line"],
            "citation":f"{row['note_path']}:L{row['start_line']}-L{row['end_line']}",
            "obsidian_link":f"[[{row['note_path'].removesuffix('.md')}{suffix}]]","snippet":row["snippet"],
            "untrusted_source":True,"retrieval_type":"both" if len(row["modes"])==2 else row["modes"][0],
            "modes":row["modes"],"score":row["fusion_score"],
            "scores":{"fusion":row["fusion_score"],"semantic_rank":row["semantic_rank"],"lexical_rank":row["lexical_rank"]}}


def _filesystem_fallback(settings:Settings,question:str,limit:int,path_prefix:str|None,reason:str)->dict:
    terms=[x.casefold() for x in re.findall(r"[\w-]+",question,flags=re.UNICODE)]
    ranked=[];scanned=0
    with TrustedVault(settings.vault) as vault:
        for path in vault.markdown_paths(settings.fallback_max_files):
            if scanned>=settings.fallback_max_files:break
            if path_prefix and not (path==path_prefix or path.startswith(path_prefix+"/")):continue
            if path_exclusion_reason(path,settings):continue
            try:raw,_=vault.read(path,settings.maximum_note_bytes);scanned+=1;text=raw.decode("utf-8")
            except (OSError,UnicodeError):continue
            if content_exclusion_reason(text,settings):continue
            for chunk in chunk_markdown(text,source_sha(raw),path,max_lines=settings.max_lines,max_chars=settings.max_chars,
                                        overlap_lines=settings.overlap_lines,corpus_id=settings.corpus_id):
                normalized=chunk["content"].casefold();hits=sum(normalized.count(term) for term in terms)
                if hits:
                    row={"chunk_id":chunk["chunk_id"],"note_path":path,"start_line":chunk["start_line"],"end_line":chunk["end_line"],
                         "title":chunk["title"],"heading_path":chunk["heading_path"],"snippet":chunk["snippet"],"modes":["fallback"],
                         "fusion_score":round(min(1.0,hits/max(1,len(terms))),6),"semantic_rank":None,"lexical_rank":None}
                    ranked.append((-hits,path,chunk["start_line"],chunk["chunk_id"],row))
    rows=[x[-1] for x in sorted(ranked)[:limit]]
    results=[]
    for rank,row in enumerate(rows,1):
        heading=row["heading_path"];suffix=("#"+heading[-1]) if heading else ""
        results.append({"rank":rank,"chunk_id":row["chunk_id"],"title":row["title"],"path":row["note_path"],
                        "heading_path":heading,"heading":heading,"line_start":row["start_line"],"line_end":row["end_line"],
                        "start_line":row["start_line"],"end_line":row["end_line"],
                        "citation":f"{row['note_path']}:L{row['start_line']}-L{row['end_line']}",
                        "obsidian_link":f"[[{row['note_path'].removesuffix('.md')}{suffix}]]","snippet":row["snippet"],
                        "untrusted_source":True,"retrieval_type":"fallback","modes":["fallback"],"score":row["fusion_score"],
                        "scores":{"fusion":row["fusion_score"],"semantic_rank":None,"lexical_rank":None}})
    return {"ok":True,"schema_version":"1.0","query":question,"mode":"filesystem-fallback",
            "degraded":True,"degraded_reasons":[reason],"warnings":[reason],
            "index":{"generated_at":None,"age_seconds":None,"stale":True,"pending_vectors":None,"pending_tombstones":None},
            "results":results,"passages_are_untrusted":True}


def search(settings:Settings,question:str,*,limit:int=5,offline:bool=False,path_prefix:str|None=None,ollama=None,qdrant=None)->dict:
    if not isinstance(question,str) or not question.strip():raise ValueError("query must not be empty")
    if len(question)>512:raise ValueError("query must not exceed 512 characters")
    if not isinstance(limit,int) or isinstance(limit,bool) or not 1<=limit<=20:raise ValueError("limit must be between 1 and 20")
    prefix=validate_relative_prefix(path_prefix)
    try:store=Store(settings.state,settings=settings,read_only=True)
    except (OSError,sqlite3.Error,CompatibilityError) as exc:
        return _filesystem_fallback(settings,question,limit,prefix,f"SQLite unavailable ({type(exc).__name__}); bounded filesystem fallback used")
    reasons=[]
    try:
        with TrustedVault(settings.vault) as trusted:
            lexical=[row for row in store.lexical(question,limit*4,prefix) if _fresh(row,trusted,settings)]
        semantic=[]
        if not offline and settings.ollama_url and settings.qdrant_url:
            embedder=ollama or OllamaClient(settings.ollama_url,settings.ollama_model,min(settings.timeout,5.0),settings.response_max_bytes)
            vector_db=qdrant or QdrantClient(settings.qdrant_url,settings.qdrant_collection,settings.vector_size,min(settings.timeout,5.0),settings.response_max_bytes)
            try:
                vector=embedder.embed([question])[0]
                if len(vector)!=settings.vector_size:raise RemoteError("query embedding vector size mismatch")
                points=vector_db.query(vector,limit*4,settings.corpus_id,settings.compatibility_signature())
                if not isinstance(points,list) or any(not isinstance(point,dict) or
                        not isinstance(point.get("payload"),dict) or
                        not isinstance(point["payload"].get("chunk_id"),str) for point in points):
                    raise RemoteError("Qdrant query rows invalid")
                identifiers=[str(p.get("payload",{}).get("chunk_id","")) for p in points]
                allowed=store.active_semantic([x for x in identifiers if x],prefix)
                with TrustedVault(settings.vault) as trusted:
                    semantic=[allowed[x] for x in identifiers if x in allowed and _fresh(allowed[x],trusted,settings)]
            except (RemoteError,IndexError):reasons.append("semantic retrieval unavailable; lexical-only results returned")
        else:reasons.append("semantic retrieval disabled or not configured; lexical-only results returned")
        rows=reciprocal_rank_fusion(lexical,semantic,limit);results=[_result(row,i) for i,row in enumerate(rows,1)]
        info=_index_info(store);drift,complete=_source_drift(settings,store);info["source_drift_count"]=drift
        info["source_inventory_complete"]=complete;info["stale"]=bool(info["stale"] or not complete or drift)
        if info["pending_vectors"]:reasons.append("semantic projection is pending")
        mode="hybrid" if semantic else "lexical"
        return {"ok":True,"schema_version":"1.0","query":question,"mode":mode,"degraded":bool(reasons),
                "degraded_reasons":reasons,"warnings":reasons,"index":info,"results":results,"passages_are_untrusted":True}
    finally:store.close()

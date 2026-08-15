"""Deterministic operator CLI for indexing, search, audit, and status."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

from .config import ConfigError, load_settings
from .indexer import Indexer, IndexLockError, source_sha
from .search import search
from .store import CompatibilityError, Store


def _settings(args):
    return load_settings(args.config or os.environ.get("OBSIDIAN_KB_CONFIG"),vault=getattr(args,"vault",None),state=getattr(args,"state",None))


def _emit(payload:dict,as_json:bool):
    if as_json:print(json.dumps(payload,indent=2,sort_keys=True))
    elif "results" in payload:
        for item in payload["results"]:
            heading=" > ".join(item["heading_path"]) or "(note body)"
            print(f"{item['citation']} | {item['title']} | {heading} | {item['retrieval_type']} | {item['score']:.6f}")
            print("UNTRUSTED QUOTED SOURCE: "+item["snippet"])
        for warning in payload.get("warnings",[]):print(f"Warning: {warning}",file=sys.stderr)
    else:
        for key,value in payload.items():
            if key!="ok":print(f"{key}: {value}")


def cmd_index(args):
    engine=Indexer(_settings(args))
    try:payload=engine.run(full_reconcile=args.full_reconcile,dry_run=args.dry_run)
    finally:engine.close()
    _emit(payload,args.json)
    return 4 if payload.get("pending_vectors") or payload.get("pending_tombstones") else 0


def cmd_query(args):
    payload=search(_settings(args),args.query,limit=args.limit,offline=args.offline,path_prefix=args.path_prefix)
    _emit(payload,args.json);return 0


def _audit(settings):
    store=Store(settings.state,settings=settings,read_only=True);stale=[]
    try:
        integrity=store.conn.execute("PRAGMA quick_check(1)").fetchone()[0]
        for row in store.conn.execute("SELECT path,source_sha256 FROM notes WHERE status='active'"):
            try:current=source_sha((settings.vault/row["path"]).read_bytes())
            except OSError:current=None
            if current!=row["source_sha256"]:stale.append(row["path"])
        status=store.status()
        return {"ok":integrity=="ok" and not stale,"integrity":integrity,"stale_paths":stale,**status}
    finally:store.close()


def cmd_audit(args):
    payload=_audit(_settings(args));_emit(payload,args.json);return 0 if payload["ok"] else 4


def cmd_status(args):
    settings=_settings(args)
    if not settings.state.is_file():
        payload={"ok":False,"error":"index not found","state":str(settings.state)};_emit(payload,args.json);return 3
    store=Store(settings.state,settings=settings,read_only=True)
    try:payload={"ok":True,"state":str(settings.state),**store.status()}
    finally:store.close()
    _emit(payload,args.json);return 0


def _common(child):
    child.add_argument("--config");child.add_argument("--vault");child.add_argument("--state");child.add_argument("--json",action="store_true")


def build_parser():
    parser=argparse.ArgumentParser(prog="imperator-knowledge",description="Privacy-bound Obsidian citation search")
    sub=parser.add_subparsers(dest="command",required=True)
    index=sub.add_parser("index");_common(index);index.add_argument("--dry-run",action="store_true");index.add_argument("--full-reconcile",action="store_true");index.set_defaults(handler=cmd_index)
    for name,handler in (("audit",cmd_audit),("status",cmd_status)):
        child=sub.add_parser(name);_common(child);child.set_defaults(handler=handler)
    for name in ("search","query"):
        query=sub.add_parser(name);query.add_argument("query");_common(query);query.add_argument("--limit","-k",type=int,default=5)
        query.add_argument("--path-prefix");query.add_argument("--offline",action="store_true");query.set_defaults(handler=cmd_query)
    return parser


def main(argv=None):
    try:
        args=build_parser().parse_args(argv);return args.handler(args)
    except (ConfigError,CompatibilityError,IndexLockError,ValueError,sqlite3.Error,OSError) as exc:
        print(json.dumps({"ok":False,"error":str(exc)}),file=sys.stderr);return 2


if __name__=="__main__":raise SystemExit(main())

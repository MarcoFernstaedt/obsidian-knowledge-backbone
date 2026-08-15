"""Argparse CLI for indexing, querying, auditing, and status."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .config import ConfigError, load_settings
from .indexer import Indexer, source_sha
from .search import search
from .store import Store


def _settings(args):
    return load_settings(args.config, vault=getattr(args, "vault", None), state=getattr(args, "state", None))


def _emit(payload: dict, as_json: bool):
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif "results" in payload:
        for result in payload["results"]:
            heading = " > ".join(result["heading"]) or "(note body)"
            print(f"{result['citation']} | {result['title']} | {heading} | {','.join(result['modes'])} | {result['score']:.6f}")
            print(result["snippet"])
        for warning in payload.get("warnings", []): print(f"Warning: {warning}", file=sys.stderr)
    else:
        for key, value in payload.items():
            if key != "ok": print(f"{key}: {value}")


def cmd_index(args):
    settings = _settings(args)
    engine = Indexer(settings)
    try: payload = engine.run()
    finally: engine.close()
    _emit(payload, args.json)
    return 0


def cmd_query(args):
    payload = search(_settings(args), args.query, limit=args.limit, offline=args.offline)
    _emit(payload, args.json)
    return 0


def _audit(settings):
    store = Store(settings.state, read_only=True)
    stale = []
    try:
        integrity = store.conn.execute("PRAGMA quick_check(1)").fetchone()[0]
        for row in store.conn.execute("SELECT path,source_sha256 FROM notes WHERE status='active'"):
            try: current = source_sha((settings.vault / row["path"]).read_text(encoding="utf-8"))
            except (OSError, UnicodeError): current = None
            if current != row["source_sha256"]: stale.append(row["path"])
        return {"ok": integrity == "ok" and not stale, "integrity": integrity, "stale_paths": stale, **store.counts()}
    finally: store.close()


def cmd_audit(args):
    payload = _audit(_settings(args)); _emit(payload, args.json)
    return 0 if payload["ok"] else 4


def cmd_status(args):
    settings = _settings(args)
    if not settings.state.is_file():
        payload = {"ok": False, "error": "index not found", "state": str(settings.state)}
        _emit(payload, args.json); return 3
    store = Store(settings.state, read_only=True)
    try: payload = {"ok": True, "state": str(settings.state), **store.counts()}
    finally: store.close()
    _emit(payload, args.json); return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="obsidian-kb", description="Privacy-bound Obsidian citation search")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler in (("index", cmd_index), ("audit", cmd_audit), ("status", cmd_status)):
        child = sub.add_parser(name)
        child.add_argument("--config")
        child.add_argument("--vault")
        child.add_argument("--state")
        child.add_argument("--json", action="store_true")
        child.set_defaults(handler=handler)
    query = sub.add_parser("query")
    query.add_argument("query")
    query.add_argument("--config"); query.add_argument("--vault"); query.add_argument("--state")
    query.add_argument("--limit", "-k", type=int, default=5)
    query.add_argument("--offline", action="store_true"); query.add_argument("--json", action="store_true")
    query.set_defaults(handler=cmd_query)
    return parser


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
        return args.handler(args)
    except (ConfigError, ValueError, sqlite3.Error, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Read-only CLI for ephemeral point-in-time audit, search, and status."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

from .config import ConfigError, load_settings
from .corpus import CorpusError, audit, live_status
from .rendering import sanitize_human
from .search import search

CONFIG_ENV = "OBSIDIAN_KB_CONFIG"


def _settings():
    path = os.environ.get(CONFIG_ENV)
    if not path:
        raise ConfigError(f"{CONFIG_ENV} is required")
    return load_settings(path, require_private=True)


def _emit(payload: dict, as_json: bool):
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif "results" in payload:
        for item in payload["results"]:
            heading = " > ".join(sanitize_human(value) for value in item["heading_path"]) or "(note body)"
            print(f"{sanitize_human(item['citation'])} | {sanitize_human(item['title'])} | {heading} | lexical | {item['score']:.8f}")
            print("UNTRUSTED QUOTED SOURCE: " + sanitize_human(item["snippet"]))
    else:
        for key, value in payload.items():
            if key != "ok":
                print(f"{sanitize_human(key)}: {sanitize_human(value)}")


def cmd_audit(args):
    payload = audit(_settings())
    _emit(payload, args.json)
    return 0 if payload["ok"] else 1


def cmd_query(args):
    payload = search(_settings(), args.query, limit=args.limit, path_prefix=args.path_prefix)
    _emit(payload, args.json)
    return 0


def cmd_status(args):
    payload = {"ok": True, "ephemeral": True, "persistence": False, **live_status(_settings())}
    _emit(payload, args.json)
    return 0


def _json(child):
    child.add_argument("--json", action="store_true")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="imperator-knowledge",
        description="Private read-only ephemeral Obsidian citation search",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    index = sub.add_parser("index", help="compatibility name for read-only ephemeral audit")
    _json(index)
    index.add_argument("--dry-run", action="store_true", help="accepted compatibility flag; audit is always read-only")
    index.set_defaults(handler=cmd_audit)
    audit_parser = sub.add_parser("audit", help="validate point-in-time eligibility/chunking and in-memory FTS")
    _json(audit_parser)
    audit_parser.set_defaults(handler=cmd_audit)
    status = sub.add_parser("status", help="scan and report path-free point-in-time corpus counts")
    _json(status)
    status.set_defaults(handler=cmd_status)
    for name in ("search", "query"):
        query = sub.add_parser(name)
        query.add_argument("query")
        _json(query)
        query.add_argument("--limit", "-k", type=int, default=5)
        query.add_argument("--path-prefix")
        query.set_defaults(handler=cmd_query)
    return parser


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
        return args.handler(args)
    except (ConfigError, ValueError) as exc:
        print(json.dumps({"ok": False, "error_class": type(exc).__name__}), file=sys.stderr)
        return 2
    except (CorpusError, sqlite3.Error, OSError) as exc:
        print(json.dumps({"ok": False, "error_class": type(exc).__name__}), file=sys.stderr)
        return 1


def imperator_search_main(argv=None):
    return main(["search", *list(sys.argv[1:] if argv is None else argv)])


def imperator_vault_index_main(argv=None):
    return main(["index", *list(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())

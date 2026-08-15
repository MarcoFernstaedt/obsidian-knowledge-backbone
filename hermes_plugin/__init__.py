"""Official read-only Hermes plugin contract."""
from __future__ import annotations

import json
import os
import shlex

from obsidian_kb.config import load_settings
from obsidian_kb.search import search

TOOL_SCHEMA = {
    "name": "obsidian_knowledge_search",
    "description": "Search the curated Obsidian index and return freshness-verified citations. Read-only.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Question or search terms."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            "offline": {"type": "boolean", "default": False},
            "config_path": {"type": "string", "description": "Optional operator-approved TOML config path."},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


def obsidian_knowledge_search(args: dict, **_kwargs) -> str:
    """Hermes tool handler. Always returns a JSON string and never writes the vault."""
    try:
        if not isinstance(args, dict): raise ValueError("arguments must be an object")
        query = args.get("query")
        if not isinstance(query, str) or not query.strip(): raise ValueError("query is required")
        limit = args.get("limit", 5)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            raise ValueError("limit must be an integer between 1 and 20")
        offline = args.get("offline", False)
        if not isinstance(offline, bool): raise ValueError("offline must be a boolean")
        config_path = args.get("config_path") or os.environ.get("OBSIDIAN_KB_CONFIG")
        if not config_path: raise ValueError("OBSIDIAN_KB_CONFIG or config_path is required")
        payload = search(load_settings(config_path), query, limit=limit, offline=offline)
    except Exception as exc:  # Plugin boundary must be JSON-shaped and redacted.
        payload = {"ok": False, "error": f"knowledge search failed: {type(exc).__name__}"}
    return json.dumps(payload, sort_keys=True)


def _knowledge_command(raw_args: str = "") -> str:
    try:
        words = shlex.split(raw_args or "")
    except ValueError:
        return "Usage: /knowledge <question>"
    if not words: return "Usage: /knowledge <question>"
    payload = json.loads(obsidian_knowledge_search({"query": " ".join(words), "limit": 5}))
    if not payload.get("ok"): return payload.get("error", "Knowledge search failed.")
    if not payload["results"]: return "No current indexed citations found."
    return "\n\n".join(f"{item['citation']} | {' > '.join(item['heading']) or '(note body)'}\n{item['snippet']}"
                         for item in payload["results"])


def register(ctx):
    ctx.register_tool(name="obsidian_knowledge_search", toolset="obsidian_knowledge",
                      schema=TOOL_SCHEMA, handler=obsidian_knowledge_search)
    ctx.register_command("knowledge", _knowledge_command,
                         description="Search the curated Obsidian index and return exact citations.")
    skill = __import__("pathlib").Path(__file__).parent / "skills" / "obsidian-knowledge-backbone" / "SKILL.md"
    if skill.exists(): ctx.register_skill("obsidian-knowledge-backbone", skill)

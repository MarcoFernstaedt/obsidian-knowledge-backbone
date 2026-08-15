"""Native Hermes directory-plugin wrapper for the packaged implementation."""
from __future__ import annotations

from .hermes_plugin import (
    CONFIG_ENV,
    SEARCH_SCHEMA,
    STATUS_SCHEMA,
    obsidian_knowledge_search,
    obsidian_knowledge_status,
    register,
)

__all__ = [
    "CONFIG_ENV",
    "SEARCH_SCHEMA",
    "STATUS_SCHEMA",
    "obsidian_knowledge_search",
    "obsidian_knowledge_status",
    "register",
]

"""Shared deterministic lexical tokenization for SQLite FTS and fallback."""
from __future__ import annotations

import unicodedata


def lexical_tokens(value: str) -> tuple[str, ...]:
    """Approximate unicode61 tokens with case/diacritic folding and no stemming."""
    folded = "".join(
        char for char in unicodedata.normalize("NFD", value.casefold())
        if unicodedata.category(char) != "Mn"
    )
    tokens: list[str] = []
    current: list[str] = []
    for char in folded:
        if char.isalnum():
            current.append(char)
        elif current:
            tokens.append("".join(current)); current = []
    if current:
        tokens.append("".join(current))
    return tuple(tokens)


def fts_expression(value: str) -> str | None:
    tokens = lexical_tokens(value)
    return " OR ".join(f'"{token}"' for token in tokens) if tokens else None


def lexical_hits(query_tokens: tuple[str, ...], *fields: str) -> int:
    searchable: list[str] = []
    for field in fields:
        searchable.extend(lexical_tokens(field))
    return sum(searchable.count(token) for token in query_tokens)

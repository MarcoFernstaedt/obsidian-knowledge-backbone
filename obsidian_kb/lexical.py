"""Deterministic Unicode tokenization and transparent exact-token FTS queries."""
from __future__ import annotations

import unicodedata

# Fixed, intentionally small English function-word set. Filtering only affects the
# query; source fields remain exact and fully indexed. An all-stop-word query is
# safely empty rather than expanded into an opaque broad search.
STOP_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is",
    "it", "of", "on", "or", "that", "the", "this", "to", "was", "were", "with",
})


def lexical_tokens(value: str, *, remove_stop_words: bool = False) -> tuple[str, ...]:
    """Case/diacritic-fold alphanumeric tokens with no stemming or prefix expansion."""
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
            token = "".join(current)
            if not remove_stop_words or token not in STOP_WORDS:
                tokens.append(token)
            current = []
    if current:
        token = "".join(current)
        if not remove_stop_words or token not in STOP_WORDS:
            tokens.append(token)
    return tuple(tokens)


def lexical_projection(value: str) -> str:
    """Canonical search projection shared by every indexed field."""
    return " ".join(lexical_tokens(value))


def fts_expression(value: str) -> str | None:
    # Exact quoted terms joined by OR are transparent, parameterized, and robust
    # for short natural-language queries. Duplicate terms do not alter FTS matching.
    tokens = tuple(dict.fromkeys(lexical_tokens(value, remove_stop_words=True)))
    return " OR ".join(f'"{token}"' for token in tokens) if tokens else None

"""Human-display escaping for untrusted note metadata and passages."""
from __future__ import annotations


def sanitize_human(value: object) -> str:
    """Render controls visibly so terminal and chat output cannot spoof structure."""
    text = str(value)
    return "".join(f"\\u{ord(char):04x}" if ord(char) < 32 or 127 <= ord(char) <= 159 else char
                   for char in text)
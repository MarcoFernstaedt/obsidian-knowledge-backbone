"""Human-display escaping for untrusted note metadata and passages."""
from __future__ import annotations

import unicodedata


def sanitize_human(value: object) -> str:
    """Render control/format/separator code points visibly to prevent spoofing."""
    text = str(value)
    return "".join(
        f"\\u{ord(char):04x}"
        if (ord(char) < 32 or 127 <= ord(char) <= 159 or
            unicodedata.category(char) in {"Cf", "Zl", "Zp"})
        else char
        for char in text
    )
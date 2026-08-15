"""Conservative, high-confidence note suppression."""
from __future__ import annotations

import re
from collections.abc import Iterable

PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----", re.I)
ASSIGNMENT = re.compile(
    r"(?im)^\s*(?:export\s+)?(?:api[_-]?key|secret(?:[_-]?(?:key|token))?|password|passwd|token|"
    r"aws_access_key_id|aws_secret_access_key|client_secret)\s*[:=]\s*([^\s#]+|['\"][^'\"]+['\"])")
KNOWN_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9_-]{24,}|AKIA[0-9A-Z]{16})\b")
PLACEHOLDER = re.compile(
    r"^(?:<[^>]+>|\$\{?\w+\}?|%\w+%|your[_ -]?(?:key|token|password|secret)(?:[_ -]here)?|"
    r"example|sample|placeholder|redacted|changeme|replace[_ -]?me|xxx+|\*+|none|null)$", re.I)


def contains_secret(text: str, extra_patterns: Iterable[str] = ()) -> bool:
    if PRIVATE_KEY.search(text) or KNOWN_TOKEN.search(text):
        return True
    for match in ASSIGNMENT.finditer(text):
        value = match.group(1).strip("'\"").rstrip(",;")
        if len(value) >= 6 and not PLACEHOLDER.fullmatch(value):
            return True
    for pattern in extra_patterns:
        try:
            if re.search(pattern, text, re.I):
                return True
        except re.error as exc:
            raise ValueError(f"invalid secret pattern {pattern!r}: {exc}") from exc
    return False

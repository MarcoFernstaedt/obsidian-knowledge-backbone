"""Conservative high-confidence credential suppression."""
from __future__ import annotations

from collections.abc import Iterable
import re

PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----", re.I)
# Provider shapes are deliberately anchored to realistic lengths/prefixes to avoid suppressing docs.
PROVIDER_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"xox[baprs]-[A-Za-z0-9-]{24,}|"                       # Slack
    r"glpat-[A-Za-z0-9_-]{20,}|"                           # GitLab
    r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{24,}|"           # Stripe
    r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}|"           # OpenAI
    r"gh[pousr]_[A-Za-z0-9]{30,}|"                         # GitHub
    r"AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|"                  # AWS long-lived/session access IDs
    r"AC[0-9a-f]{32}:[0-9a-f]{32}"                         # Twilio SID:auth token
    r")(?![A-Za-z0-9])", re.I)
CREDENTIAL_NAME = (
    r"(?:api[_-]?key|secret(?:[_-]?(?:key|token))?|password|passwd|token|auth[_-]?token|"
    r"access[_-]?token|refresh[_-]?token|client[_-]?secret|private[_-]?key|"
    r"aws[_-]?(?:access[_-]?key[_-]?id|secret[_-]?access[_-]?key|session[_-]?token)|"
    r"[a-z0-9]+(?:[_-](?:api[_-]?key|auth[_-]?token|access[_-]?token|secret|password|token)))"
)
# Supports shell/env, simple YAML, and JSON object assignments.
ASSIGNMENT = re.compile(
    rf"(?im)(?:^|[,{{])\s*(?:export\s+)?[\"']?{CREDENTIAL_NAME}[\"']?\s*[:=]\s*"
    r"(?P<value>\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|[^\s,#}]+)")
PLACEHOLDER = re.compile(
    r"^(?:<[^>]+>|\$\{?[A-Z_][A-Z0-9_]*\}?|\$[A-Z_][A-Z0-9_]*|%[A-Z_][A-Z0-9_]*%|"
    r"your[_ -]?(?:api[_ -]?key|key|token|password|secret)(?:[_ -]here)?|"
    r"example(?:[_-].*)?|sample(?:[_-].*)?|placeholder|redacted|changeme|replace[_ -]?me|"
    r"x{3,}|\*+|none|null)$", re.I)


def contains_secret(text: str, extra_patterns: Iterable[str] = ()) -> bool:
    if PRIVATE_KEY.search(text) or PROVIDER_TOKEN.search(text):
        return True
    for match in ASSIGNMENT.finditer(text):
        value = match.group("value").strip().strip("'\"").rstrip(",;")
        if len(value) >= 6 and not PLACEHOLDER.fullmatch(value):
            return True
    for pattern in extra_patterns:
        try:
            if re.search(pattern, text, re.I): return True
        except re.error as exc:
            raise ValueError(f"invalid secret pattern: {type(exc).__name__}") from exc
    return False

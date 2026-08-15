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
    r"|AIza[0-9A-Za-z_-]{35}"                              # Google API key
    r"|npm_[A-Za-z0-9]{36}"                                # npm granular access token
    r")(?![A-Za-z0-9])", re.I)
CREDENTIAL_NAME = (
    r"(?:api[_-]?key|secret(?:[_-]?(?:key|token))?|password|passwd|token|auth[_-]?token|"
    r"access[_-]?token|refresh[_-]?token|client[_-]?secret|private[_-]?key|"
    r"aws[_-]?(?:access[_-]?key[_-]?id|secret[_-]?access[_-]?key|session[_-]?token)|"
    r"[a-z0-9]+(?:[_-](?:api[_-]?key|auth[_-]?token|access[_-]?token|secret|password|token)))"
)
# Supports shell/env, simple YAML (including list mappings), and JSON object assignments.
ASSIGNMENT = re.compile(
    rf"(?im)(?:^[ \t]*(?:-[ \t]+)?|[,{{][ \t]*)"
    rf"(?:export[ \t]+)?[\"']?{CREDENTIAL_NAME}[\"']?[ \t]*[:=][ \t]*"
    r"(?P<value>\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|[^\s,#}]+)")
YAML_ASSIGNMENT = re.compile(
    r"^(?P<indent>[ \t]*)(?:-[ \t]+)?"
    r"(?P<key>\"(?:[^\"\\]|\\.)*\"|'(?:[^']|'')*'|[A-Za-z0-9_-]+)"
    r"[ \t]*:[ \t]*(?P<value>.*)$")
BLOCK_SCALAR = re.compile(r"^[|>](?:(?:[1-9][+-]?)|(?:[+-][1-9]?)?)?(?:[ \t]+#.*)?$")
PLACEHOLDER = re.compile(
    r"^(?:<[^>]+>|\$\{?[A-Z_][A-Z0-9_]*\}?|\$[A-Z_][A-Z0-9_]*|%[A-Z_][A-Z0-9_]*%|"
    r"your[_ -]?(?:api[_ -]?key|key|token|password|secret)(?:[_ -]here)?|"
    r"example(?:[_-].*)?|sample(?:[_-].*)?|placeholder|redacted|changeme|replace[_ -]?me|"
    r"\[redacted\]|x{3,}|\*+|none|null)$", re.I)
BEARER_JWT = re.compile(r"(?i)\bbearer\s+eyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}(?![A-Za-z0-9_-])")
DATABASE_URL = re.compile(
    r"(?i)\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis)://[^\s/:@]+:(?P<password>[^\s/@]+)@[^\s]+")


def _yaml_scalar(value: str) -> str | None:
    """Decode only scalar forms we can classify confidently; unknown means secret."""
    value = value.strip()
    if not value or value.startswith("#"):
        return ""
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            return None
        return value[1:-1].replace("''", "'")
    if value.startswith('"'):
        if len(value) < 2 or not value.endswith('"'):
            return None
        try:
            import json
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return None
        return decoded if isinstance(decoded, str) else None
    return value.split(" #", 1)[0].strip().rstrip(",;")


def _contains_yaml_credential(text: str) -> bool:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = YAML_ASSIGNMENT.match(line)
        if not match:
            continue
        key = _yaml_scalar(match.group("key"))
        if key is None or not re.fullmatch(CREDENTIAL_NAME, key, re.I):
            continue
        raw_value = match.group("value").strip()
        if BLOCK_SCALAR.fullmatch(raw_value):
            base_indent = len(match.group("indent").expandtabs(8))
            content: list[str] = []
            for following in lines[index + 1:]:
                if not following.strip():
                    continue
                indent = len(following) - len(following.lstrip(" \t"))
                if indent <= base_indent:
                    break
                content.append(following.strip())
            if not content or any(not PLACEHOLDER.fullmatch(item) for item in content):
                return True
            continue
        scalar = _yaml_scalar(raw_value)
        if scalar and not PLACEHOLDER.fullmatch(scalar):
            return True
        if scalar is None:
            return True
    return False


def contains_secret(text: str, extra_patterns: Iterable[str] = ()) -> bool:
    if (_contains_yaml_credential(text) or PRIVATE_KEY.search(text) or
            PROVIDER_TOKEN.search(text) or BEARER_JWT.search(text)):
        return True
    for match in DATABASE_URL.finditer(text):
        if not PLACEHOLDER.fullmatch(match.group("password")):
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

"""Heading-aware Markdown chunking with exact source line spans."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import json
import re
import uuid

from .config import normalize_control_key

HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")
FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")
APPLICATION_NAMESPACE = uuid.UUID("598f094b-a203-5a8f-8cca-81edc80aaed4")


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    file_path: str
    title: str
    heading_path: tuple[str, ...]
    start_line: int
    end_line: int
    content: str
    snippet: str
    source_sha256: str

    def as_dict(self) -> dict:
        value = asdict(self)
        value["heading_path"] = list(self.heading_path)
        return value


def _mapping_key(raw: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    if value.startswith('"'):
        try: decoded = json.loads(value)
        except (json.JSONDecodeError, UnicodeError): return None
        return decoded if isinstance(decoded, str) and decoded else None
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            return None
        inner = value[1:-1]
        index = 0
        while index < len(inner):
            if inner[index] == "'":
                if index + 1 >= len(inner) or inner[index + 1] != "'":
                    return None
                index += 2
            else:
                index += 1
        return inner.replace("''", "'") or None
    if value[:1] in "[{&*!|>" or any(char in value for char in "'\""):
        return None
    return value


def frontmatter(text: str, control_keys: tuple[str, ...] = ()) -> tuple[dict[str, str], int]:
    lines = text.splitlines()
    if not lines or lines[0].removeprefix("\ufeff").strip() != "---":
        return {}, 0
    bounded = 0
    for index in range(1, len(lines)):
        bounded += len(lines[index].encode("utf-8")) + 1
        if bounded > 65_536:
            return {"__malformed__": "true"}, 0
        if lines[index].strip() == "---":
            values: dict[str, str] = {}
            controls = {normalize_control_key(key) for key in control_keys} | {"imperator_retrieval"}
            previous_control = False
            for line in lines[1:index]:
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                indent = len(line) - len(line.lstrip(" \t"))
                candidate = line.lstrip(" \t")
                list_item = candidate.startswith("-") and len(candidate) > 1 and candidate[1].isspace()
                if list_item:
                    candidate = candidate[1:].lstrip(" \t")
                if ":" not in candidate:
                    if previous_control:
                        return {"__malformed__": "true"}, 0
                    continue
                key, value = candidate.split(":", 1)
                decoded_key = _mapping_key(key)
                normalized_key = normalize_control_key(decoded_key) if decoded_key else ""
                if not normalized_key:
                    return {"__malformed__": "true"}, 0
                is_control = normalized_key in controls
                if is_control and (indent or list_item or normalized_key in values):
                    # A retrieval control is authoritative only as one unique top-level scalar.
                    # Seeing it anywhere nested/indented/list-valued is ambiguous and excludes.
                    return {"__malformed__": "true"}, 0
                previous_control = is_control
                if not is_control:
                    continue
                raw_value = value.strip()
                # Retrieval controls are deliberately limited to plain/quoted scalars.
                if raw_value.startswith(("'", '"')):
                    quote = raw_value[0]; closing = raw_value.find(quote, 1)
                    if closing < 1 or raw_value[closing + 1:].strip()[:1] not in {"", "#"}:
                        return {"__malformed__": "true"}, 0
                    raw_value = raw_value[:closing + 1]
                else:
                    raw_value = raw_value.split(" #", 1)[0].rstrip()
                if not raw_value or raw_value[:1] in "[{&*!|>" or ":" in raw_value:
                    return {"__malformed__": "true"}, 0
                scalar = raw_value.casefold().strip("'\"")
                allowed = ({"exclude", "include"} if normalized_key == "imperator_retrieval" else
                           {"true", "false", "yes", "no", "on", "off", "1", "0"})
                if scalar not in allowed:
                    return {"__malformed__": "true"}, 0
                values[normalized_key] = scalar
            return values, index + 1
    return {"__malformed__": "true"}, 0


def is_frontmatter_excluded(text: str, keys: tuple[str, ...]) -> bool:
    values, _ = frontmatter(text, keys)
    if values.get("__malformed__") == "true":
        return True
    return (values.get("imperator_retrieval") == "exclude" or
            any(values.get(normalize_control_key(key)) in {"false", "no", "0", "off"}
                for key in keys))


def _bounded(parts: list[tuple[int, str]], max_lines: int, max_chars: int, overlap_lines: int):
    expanded: list[tuple[int, str]] = []
    for number, line in parts:
        expanded.extend((number, line[i:i + max_chars]) for i in range(0, len(line), max_chars))
        if not line: expanded.append((number, ""))
    start = 0
    while start < len(expanded):
        end = start
        size = 0
        while end < len(expanded) and end - start < max_lines:
            extra = len(expanded[end][1]) + (1 if end > start else 0)
            if end > start and size + extra > max_chars: break
            size += extra; end += 1
        if end == start: end += 1
        # Prefer the latest complete paragraph boundary when more data remains.
        if end < len(expanded):
            blanks = [i for i in range(start + 1, end) if not expanded[i][1].strip()]
            if blanks: end = blanks[-1] + 1
        part = expanded[start:end]
        yield part
        if end >= len(expanded): break
        overlap = min(overlap_lines, max(0, len(part) - 1))
        next_start = end - overlap
        # Overlap must not make the next piece exceed configured character bounds.
        while overlap and sum(len(x[1]) for x in expanded[next_start:min(len(expanded), next_start + max_lines)]) + max_lines > max_chars:
            overlap -= 1; next_start = end - overlap
        start = max(start + 1, next_start)


def chunk_markdown(text: str, source_sha256: str, file_path: str, *, max_lines: int = 60,
                   max_chars: int = 6000, corpus_id: str = "curated-obsidian", overlap_lines: int = 2,
                   compatibility_signature: str = "") -> list[dict]:
    lines = text.splitlines()
    _, body_offset = frontmatter(text)
    stack: list[str] = []
    sections: list[tuple[tuple[str, ...], list[tuple[int, str]]]] = []
    current_path: tuple[str, ...] = ()
    current: list[tuple[int, str]] = []
    title = ""
    fence_marker: str | None = None
    fence_length = 0
    for index in range(body_offset, len(lines)):
        line = lines[index]
        fence = FENCE.match(line)
        if fence_marker is None:
            match = HEADING.match(line)
            if fence:
                fence_marker, fence_length = fence.group(1)[0], len(fence.group(1))
        else:
            match = None
            if fence and fence.group(1)[0] == fence_marker and len(fence.group(1)) >= fence_length and not fence.group(2).strip():
                fence_marker, fence_length = None, 0
        if match:
            if current:
                sections.append((current_path, current))
            level, heading = len(match.group(1)), match.group(2).strip()
            stack[level - 1:] = [heading]
            current_path = tuple(stack)
            title = title or heading
            current = [(index + 1, line)]
        else:
            current.append((index + 1, line))
    if current:
        sections.append((current_path, current))
    chunks: list[dict] = []
    ordinal = 0
    for heading_path, section in sections:
        if not any(line.strip() and not HEADING.match(line) for _, line in section):
            continue
        for part in _bounded(section, max_lines, max_chars, min(2, max(0, overlap_lines))):
            content = "\n".join(line for _, line in part).strip()
            if not content:
                continue
            start, end = part[0][0], part[-1][0]
            content_digest = hashlib.sha256(" ".join(content.split()).encode()).hexdigest()
            chunk_id = str(uuid.uuid5(APPLICATION_NAMESPACE,
                                      f"{corpus_id}\0{compatibility_signature}\0{file_path}\0{ordinal}"))
            value = Chunk(chunk_id, file_path, title or file_path.rsplit("/", 1)[-1].removesuffix(".md"),
                          heading_path, start, end, content, content[:320], source_sha256).as_dict()
            value["ordinal"] = ordinal
            value["content_sha256"] = content_digest
            chunks.append(value)
            ordinal += 1
    return chunks

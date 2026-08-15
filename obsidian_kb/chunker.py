"""Heading-aware Markdown chunking with exact source line spans."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import re
import uuid

HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")
FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")
APPLICATION_NAMESPACE = uuid.UUID("598f094b-a203-5a8f-8cca-81edc80aaed4")


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    point_id: str
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


def frontmatter(text: str) -> tuple[dict[str, str], int]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, 0
    bounded = 0
    for index in range(1, len(lines)):
        bounded += len(lines[index].encode("utf-8")) + 1
        if bounded > 65_536:
            return {"__malformed__": "true"}, 0
        if lines[index].strip() == "---":
            values: dict[str, str] = {}
            for line in lines[1:index]:
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                if ":" not in line:
                    return {"__malformed__": "true"}, 0
                key, value = line.split(":", 1)
                if not key.strip() or key.strip().lower() in values:
                    return {"__malformed__": "true"}, 0
                values[key.strip().lower()] = value.strip().lower().strip("'\"")
            return values, index + 1
    return {"__malformed__": "true"}, 0


def is_frontmatter_excluded(text: str, keys: tuple[str, ...]) -> bool:
    values, _ = frontmatter(text)
    if values.get("__malformed__") == "true":
        return True
    return (values.get("imperator_retrieval") == "exclude" or
            any(values.get(key.lower()) in {"false", "no", "0", "off"} for key in keys))


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
                   max_chars: int = 6000, corpus_id: str = "curated-obsidian", overlap_lines: int = 2) -> list[dict]:
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
            chunk_id = str(uuid.uuid5(APPLICATION_NAMESPACE, f"{corpus_id}\0{file_path}\0{ordinal}"))
            value = Chunk(chunk_id, chunk_id, file_path, title or file_path.rsplit("/", 1)[-1].removesuffix(".md"),
                          heading_path, start, end, content, content[:320], source_sha256).as_dict()
            value["ordinal"] = ordinal
            value["content_sha256"] = content_digest
            chunks.append(value)
            ordinal += 1
    return chunks

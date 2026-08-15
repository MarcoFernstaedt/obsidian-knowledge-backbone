"""Heading-aware Markdown chunking with exact source line spans."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import re
import uuid

HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")
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


def _bounded(parts: list[tuple[int, str]], max_lines: int, max_chars: int):
    current: list[tuple[int, str]] = []
    size = 0
    for line_number, line in parts:
        segments = [line[i:i + max_chars] for i in range(0, len(line), max_chars)] or [""]
        for segment in segments:
            extra = len(segment) + (1 if current else 0)
            if current and (len(current) >= max_lines or size + extra > max_chars):
                yield current
                current, size = [], 0
            current.append((line_number, segment))
            size += len(segment) + (1 if len(current) > 1 else 0)
    if current:
        yield current


def chunk_markdown(text: str, source_sha256: str, file_path: str, *, max_lines: int = 60,
                   max_chars: int = 6000, corpus_id: str = "curated-obsidian") -> list[dict]:
    lines = text.splitlines()
    _, body_offset = frontmatter(text)
    stack: list[str] = []
    sections: list[tuple[tuple[str, ...], list[tuple[int, str]]]] = []
    current_path: tuple[str, ...] = ()
    current: list[tuple[int, str]] = []
    title = ""
    fenced = False
    for index in range(body_offset, len(lines)):
        line = lines[index]
        match = None if fenced else HEADING.match(line)
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
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
        for part in _bounded(section, max_lines, max_chars):
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

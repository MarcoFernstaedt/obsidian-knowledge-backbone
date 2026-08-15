import re
from typing import List, Dict, Any, Optional, Tuple

CHUNK_MIN_LINES = 3
CHUNK_MAX_LINES = 60

class MarkdownChunk:
    def __init__(self, heading_path: List[str], start_line: int, end_line: int, lines: List[str]):
        self.heading_path = list(heading_path)
        self.start_line = start_line
        self.end_line = end_line
        self.lines = list(lines)
        self.content = "".join(lines).strip()

    def as_dict(self, file_sha256: str, file_path: str) -> Dict[str, Any]:
        return {
            "heading_path": self.heading_path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "content": self.content,
            "file_sha256": file_sha256,
            "file_path": file_path,
            "snippet": self.content[:240],
        }

def parse_frontmatter(frontmatter_str: str) -> Dict[str, Any]:
    result = {}
    for line in frontmatter_str.splitlines():
        if ':' not in line:
            continue
        k, v = line.split(':', 1)
        result[k.strip().lower()] = v.strip().lower()
    return result

def heading_chunks_md(lines: List[str]) -> List[Tuple[List[str], int, int]]:
    headings = []
    cur_path = []
    starts = [0]
    chunks = []
    line_heads = []
    # Hierarchical heading path stack
    for i, line in enumerate(lines):
        hit = re.match(r'^(#{1,6}) (.*)', line)
        if hit:
            while len(cur_path) >= len(hit.group(1)):
                cur_path.pop()
            cur_path.append(hit.group(2).strip())
            headings.append((list(cur_path), i))
            line_heads.append(i)
    # Build [start,end) spans between headings
    for idx, (path, start) in enumerate(headings):
        if idx + 1 < len(headings):
            end = headings[idx + 1][1]
        else:
            end = len(lines)
        chunks.append((path, start, end))
    # Prepend non-heading content at start if any
    if line_heads and line_heads[0] > 0:
        chunks = [([], 0, line_heads[0])] + chunks
    if not headings:
        chunks = [([], 0, len(lines))]
    return chunks

def chunk_markdown(
    md_text: str,
    file_sha256: str,
    file_path: str,
    respect_frontmatter: bool = True,
    chunk_min_lines: int = CHUNK_MIN_LINES,
    chunk_max_lines: int = CHUNK_MAX_LINES,
) -> List[Dict[str, Any]]:
    """Chunk Markdown into heading-aware units, tracking line spans and exclusions."""
    lines = md_text.splitlines()
    # Frontmatter exclusion
    exclude = False
    if respect_frontmatter and lines and lines[0].strip() == '---':
        fm_end = 1
        while fm_end < len(lines) and lines[fm_end].strip() != '---':
            fm_end += 1
        frontmatter_text = '\n'.join(lines[1:fm_end])
        fm = parse_frontmatter(frontmatter_text)
        exclude = fm.get('semantic_index', 'true') == 'false' or fm.get('index', 'true') == 'false'
        if exclude:
            return []
        body_lines = lines[fm_end+1:]
    else:
        body_lines = lines
    # Chunk by headings
    chunks = []
    for head_path, start, end in heading_chunks_md(body_lines):
        chunk_lines = body_lines[start:end]
        # Further split oversized chunks
        i = 0
        while i < len(chunk_lines):
            sub_end = min(i+chunk_max_lines, len(chunk_lines))
            sub_lines = chunk_lines[i:sub_end]
            if len(sub_lines) >= chunk_min_lines:
                chunks.append(MarkdownChunk(head_path, start+i+1, start+sub_end, sub_lines).as_dict(
                    file_sha256, file_path
                ))
            i = sub_end
    return chunks

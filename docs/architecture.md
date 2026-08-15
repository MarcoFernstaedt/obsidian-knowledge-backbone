# Architecture

## Authority and privacy

The curated Markdown vault is the content authority and is never written. SQLite is the only derived ledger and retrieval index. Every returned candidate is revalidated against current exact source bytes and the complete eligibility policy. Excluded notes store only relative path and reason. Passages are explicitly untrusted quoted source data.

## Index flow and crash recovery

1. Validate the fixed private config. At use time, traverse every state-directory ancestor from `/` with descriptor-relative `O_NOFOLLOW`, reject symlink/non-directory ancestors, prove the opened state directory is outside the current vault, and retain its device/inode-bound descriptor.
2. Acquire a private nonblocking single-writer lock through that descriptor. SQLite and its WAL/SHM names use the same stable `/proc/self/fd` directory and fail closed where that facility is unavailable. Revalidate configured-path identity and vault containment at operation boundaries.
3. Classify path exclusions before content reads. Traverse with descriptor-relative no-follow opens from one trusted root and reject symlinks, non-regular entries, races, oversized files, malformed/excluded frontmatter, and credentials.
4. Chunk allowed Markdown by headings with exact inclusive source spans and deterministic corpus/signature/path/ordinal UUIDv5 identities.
5. Commit the complete scan's notes, chunks, FTS rows, removals, generation, and timestamp in one SQLite transaction. Any unknown read or invariant failure rolls the whole scan back.

SQLite uses WAL, foreign keys, explicit transactions, and a busy timeout. Metadata binds local schema `3`, corpus, all chunk parameters, maximum source size, and a fingerprint of every content-affecting exclusion rule. Drift requires a fresh side-by-side index.

## Retrieval flow

Queries are 1–512 characters and limits are 1–20. Relative `path_prefix` filters are normalized, traversal-safe, and parameterized. FTS5 uses shared non-stemming Unicode lexical tokens plus BM25 with deterministic score, path, line, and chunk-ID ordering. Candidates are paged through a 10,000-row safety bound. Before publication, each result is re-read and re-chunked through the trusted vault descriptor; all citation, content, digest, identity, and FTS projection fields must exactly equal the current source-derived chunk. Corruption or safety-bound exhaustion invokes fallback.

If SQLite is missing, corrupt, or incompatible, retrieval scans at most the configured filesystem bound in memory and applies the same path, frontmatter, source-size, symlink, race, and credential policy. It writes no state. Status performs a separately bounded complete inventory: overflow or unknown reads report incomplete/stale rather than false-current.

## Interfaces

- CLI: `imperator-knowledge` index, search/query, status, and audit, plus compatibility entry points.
- Hermes plugin: exactly `obsidian_knowledge_search`, `obsidian_knowledge_status`, and `/notesearch`.
- Configuration is fixed by `OBSIDIAN_KB_CONFIG`; no caller path or execution-mode override exists.
- Search reports lexical mode only. Status reports local age, drift, current/stale, compatibility, active/excluded notes, and chunks without note paths.
- All runtime paths are network-free.

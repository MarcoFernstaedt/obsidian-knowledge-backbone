# Architecture

## Authority

The curated Markdown vault is the sole content authority and is never written. Each operation constructs one complete, bounded, internally consistent point-in-time corpus. Passages remain untrusted quoted source data.

## Live corpus flow

1. Validate the fixed private config. Unsupported and unknown sections fail closed.
2. Bind the approved root device/inode during configuration. Reopen it by traversing every absolute path component from `/` with descriptor-relative no-follow directory operations, reject identity drift, then enumerate/read descendants through the held root descriptor.
3. Lazily enumerate under the global file bound before sorting only the bounded collection. Apply path rules before content reads, then source-size, UTF-8, bounded frontmatter, explicit opt-out, and credential controls.
4. Abort the whole operation on unknown/transient traversal or read errors. Deterministic policy exclusions count safely. Abort as incomplete on file, chunk, or total-byte overflow.
5. Compare descriptor-bound recursive inventories before and after scanning, then re-read and SHA-256 verify every regular Markdown source whose bytes were consulted, including content-excluded and invalid-UTF-8 notes, and compare a final inventory. Any observed insertion, removal, replacement, eligibility change, or content change aborts.
6. Chunk every eligible snapshot note by headings with exact inclusive source spans and deterministic corpus/policy/path/ordinal UUIDv5 identities.
7. Only after the complete corpus exists, open SQLite with the literal `:memory:` database name, create FTS5, insert all chunks, query, and close it.

No partial corpus is queryable. A mutable filesystem can change after any final check, so the interface makes no impossible perpetual-currency promise. Success means the citations derive from a complete internally consistent scan and reports `snapshot_consistent=true`, `source_inventory_complete=true`, `current=false`, and `freshness=point-in-time`; authoritative notes may change after the scan.

## Retrieval

Queries are 1–512 characters and limits are 1–20. Relative `path_prefix` filters are traversal-safe and parameterized. Query tokens use deterministic case/diacritic folding, no stemming, no prefix matching, and a fixed small stop-word set. Remaining exact tokens are quoted and OR-combined. There is no hidden query expansion.

FTS5 uses `unicode61 remove_diacritics 2`. BM25 field weights are title 8, heading 5, path 3, and content 1. Ordering is score, path, then insertion row. Source-derived chunks directly supply title, heading hierarchy, path, exact line span, snippet, citation, and Obsidian link.

## Interfaces

- CLI: search/query, status, audit, and compatibility `index`; `index` is a read-only audit.
- Compatibility console entry points: `imperator-search` and `imperator-vault-index`.
- Hermes plugin: exactly `obsidian_knowledge_search`, `obsidian_knowledge_status`, and `/notesearch`.
- Configuration: fixed `OBSIDIAN_KB_CONFIG`; no caller override.
- Status: eligible/excluded notes, chunks, scan duration, source-inventory completeness, snapshot consistency, `current=false`, `freshness=point-in-time`, and `compatibility=ephemeral-live`; no paths/content.
- Runtime: no network use and no filesystem writes.

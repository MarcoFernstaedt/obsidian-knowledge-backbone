# Architecture

## Authority and privacy

The Markdown vault is the content authority and is never written. SQLite is the sole desired-state, retry, and lexical-index authority. Qdrant is a disposable semantic projection; remote points cannot authorize content. Every returned SQLite candidate is revalidated against the current exact source bytes and eligibility policy.

Excluded notes store only relative path and reason. Qdrant payloads contain exactly `corpus_id`, `schema_version`, `chunk_id`, `content_sha256`, and `embedding_model`; they never contain note text, title, path, heading, snippet, or frontmatter. Retrieved passages are explicitly untrusted quoted source data.

## Index flow and crash recovery

1. Acquire the private nonblocking index lock.
2. Classify path exclusions before reading content, then reject symlinks, outside-root resolution, oversized files, malformed/excluded frontmatter, and credential canaries.
3. Chunk allowed Markdown by heading with exact inclusive source spans and corpus/path/ordinal UUIDv5 identities.
4. Atomically commit notes, chunks, FTS rows, pending semantic projections, and tombstones to SQLite.
5. Publish pending vectors to the side-by-side Qdrant collection, then mark each projection applied locally. A crash after network publication retries the same UUID upsert idempotently.
6. Apply durable tombstones. Optional full reconciliation compares all active local IDs with remote IDs filtered to the corpus and removes remote orphans.

SQLite uses WAL, foreign keys, explicit transactions, and a busy timeout. Metadata binds schema, corpus, collection, embedding model/digest, vector size, chunker version, and policy fingerprint. A mismatch fails closed and requires a fresh side-by-side index/collection, preventing mixed vector generations.

## Retrieval flow

Queries are 1–512 characters and limits are 1–20. Relative `path_prefix` filters are parameterized and traversal-safe. FTS5 and semantic retrieval each request four times the result limit. Semantic candidates must map to active, applied, current SQLite rows. Fusion uses weighted reciprocal-rank fusion (`k=60`, semantic `0.60`, lexical `0.40`) with deterministic best-component-rank, path, line, and chunk-ID tie breaks.

Remote failures return lexical results immediately. Missing, corrupt, or incompatible SQLite invokes a bounded, in-memory, read-only filesystem lexical fallback that uses the same path, frontmatter, size, symlink, and secret policy. Status and search output expose generated time, age, pending vectors, pending tombstones, and stale state without exposing note metadata.

## Interfaces

- CLI: `imperator-knowledge search`, `index`, `status`, and `audit`, plus packaged compatibility entry points.
- Hermes plugin: exactly `obsidian_knowledge_search`, `obsidian_knowledge_status`, and `/notesearch`.
- Plugin configuration is fixed by `OBSIDIAN_KB_CONFIG`; callers cannot supply config paths or force offline policy.
- Registration performs no network access. Search network calls have a five-second per-request ceiling before lexical degradation.

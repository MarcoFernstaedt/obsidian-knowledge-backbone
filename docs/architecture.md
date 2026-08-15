# Architecture

## Authority model

The Markdown vault is the content authority. SQLite is the sole index authority. Qdrant is a disposable semantic projection. A remote point has no authority unless its `chunk_id` maps to an active, `semantic_ready` SQLite chunk whose source hash still matches the current file.

## Index flow

1. Enumerate Markdown paths under the vault without writing to it.
2. Classify hidden, folder, glob, frontmatter, unreadable, and credential exclusions.
3. Record excluded entries as relative path and reason only.
4. Hash allowed source bytes and skip unchanged active notes.
5. Produce deterministic heading-aware chunks with hierarchical headings and exact one-indexed lines.
6. Optionally embed and upsert minimal Qdrant points.
7. Transactionally replace note metadata, chunks, and FTS rows.
8. Reconcile missing paths and attempt deletion of obsolete remote points.

A remote outage leaves `semantic_ready` false while lexical state remains valid. A failed remote delete is safe because semantic query results are postfiltered.

## Query flow

Lexical candidates come from FTS5. Optional semantic candidates come from an Ollama query vector and Qdrant. Both lists are checked against authoritative active rows and current source SHA-256 values. Reciprocal-rank fusion uses fixed rank constant 60 and deterministic path, line, and chunk-ID tie breaks. Scores are normalized against the maximum two-list first-rank score.

## SQLite tables

- `notes`: active source SHA or excluded path and reason, enforced by a check constraint.
- `chunks`: citation metadata, source hash, point ID, content, snippet, and semantic readiness.
- `chunks_fts`: FTS5 projection joined by chunk ID.
- `metadata`: schema version.

All note replacement, exclusion, deletion, and FTS changes use SQLite transactions with foreign keys enabled.

# Architecture: Obsidian Knowledge Backbone

## Visual Diagram

[ASCII Illustration]

      ┌─────────────┐    index enumerator   ┌─────────────┐
      │Obsidian Dir │ ────────────────▶    │Chunker      │
      └─────────────┘                     └─────┬───────┘
                                              │ heading-path, lines, digest, snippet
                                              ▼
                                    ┌────────────────────┐
                                    │  Exclusions/Privacy│ ←───── config, frontmatter, secret patterns
                                    └─────┬──────────────┘
                                          │
                                          ▼
       ┌──────────────┐     ┌─────────────┴─────────────┐
       │ SQLite (FTS5)│ ◀──▶│Hybrid Engine (RRF)        │◀──▶ Qdrant Embeddings via Ollama
       └──────────────┘     └───────────────────────────┘
                                       │
                                       ▼
                           ┌────────────────────────────┐
                           │  obsidian-kb CLI           │
                           └──────────────┬─────────────┘
                                          │
                                          ▼
                           ┌────────────────────────────┐
                           │  Hermes Plugin / SLASH CMD │
                           └────────────────────────────┘

## Screen Reader Equivalent

Process flow:
1. The Obsidian vault directory is enumerated for Markdown files, skipping configured or hidden folders and globs.
2. Each file is chunked by Markdown heading, recording hierarchy, heading path, and exact line numbers.
3. Each chunk is checked for exclusions:
   - Folder/glob exclusion (user config)
   - Frontmatter exclusion (semantic_index/index: false)
   - Secret pattern match (e.g., private keys)
4. Allowed chunks are indexed in both SQLite (FTS5) and Qdrant (semantic index via Ollama embed API).
5. Only currently-verified, included state is available for retrieval—removal or changes are honored atomically.
6. The CLI supports index, query, audit, and status. Query deterministically fuses results from Qdrant and SQLite, always states retrieval mode/source, and privacy filters all output.
7. The Hermes plugin exposes a read-only `/knowledge` search command with JSON citation output and never exposes index/admin mutation or content.

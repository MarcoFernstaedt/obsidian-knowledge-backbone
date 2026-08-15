# Obsidian Knowledge Backbone (obsidian-kb)

A production-grade, privacy-focused, heading-aware Markdown knowledge indexing and search engine for Obsidian vaults. Supports deterministic hybrid semantic (Qdrant) and lexical (SQLite FTS5) search with privacy fail-closed exclusion, high-quality citations, incremental reindexing, and Hermès plugin integration.

- ⚡ **Privacy fail-closed**: Never indexes secrets or excluded content, never leaks raw note data, never logs secret values.
- 🔎 **Precise citations**: Every result states exact vault-relative path, line range, heading path, snippet, and retrieval mode.
- 🧑‍💻 **Hybrid ranking**: Deterministic reciprocal rank fusion from Qdrant+SQLite, reliable offline fallback.
- 🛡 **Incremental & rollback safe**: Handles file moves/deletes, atomic state, and operator-approved rollbacks.
- 🛠 **Hermes plugin**: Provides `/knowledge` slash-command and structured citations—never exposes index/admin mutation or secret information.

See `docs/architecture.md` for detailed architecture, privacy, and operator/CI guidelines. MIT Licensed. No private vault data or secrets are stored or exposed.

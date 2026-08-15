# Obsidian Knowledge Backbone

Obsidian Knowledge Backbone is a read-only, privacy-bound search index for curated Markdown vaults. It combines SQLite FTS5 lexical retrieval with optional Ollama embeddings and Qdrant vectors, then returns freshness-verified citations to the original note lines. The Python 3.11+ runtime uses only the standard library.

## Architecture

```text
Read-only Obsidian vault
          |
          v
Exclusion and credential gate
          |
          v
Heading-aware bounded chunks
          |
          +-----------------------+
          |                       |
          v                       v
Authoritative SQLite          Ollama embeddings
metadata and FTS5                 |
          |                       v
          |                 Qdrant projection
          |                       |
          +-----------+-----------+
                      v
       Active-row and source-SHA postfilter
                      |
                      v
       Deterministic reciprocal-rank fusion
                      |
                      v
 CLI and read-only Hermes citation surfaces
```

Screen-reader equivalent: the read-only vault passes through exclusion and credential checks, then heading-aware chunking. SQLite stores authoritative metadata and FTS5 text. Ollama and Qdrant form an optional, rebuildable semantic projection. Query results from both paths must pass active-row and current source-hash checks before deterministic rank fusion. The CLI and Hermes plugin expose only verified citations and snippets.

## Privacy and correctness model

- The vault is opened only for reading. State is written only to the configured SQLite path and optional Qdrant collection.
- Hidden paths (enabled by default), configured folders and globs, frontmatter false values, private keys, known token shapes, and high-confidence credential assignments are excluded.
- Excluded-note records contain only path and reason. They contain no source hash, excerpt, or content.
- SQLite is authoritative. Qdrant payloads contain only `chunk_id`; stale remote points cannot authorize a result.
- Every candidate is checked against the current note SHA-256. Changed or missing source suppresses results until reindexing.
- Remote failures degrade to lexical retrieval. They do not bypass filtering.

## Install

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
cp config.example.toml config.toml
```

Edit `config.toml`. Keep the state path outside the vault. Set `OBSIDIAN_KB_CONFIG=/absolute/path/config.toml` for the Hermes plugin.

## CLI

```bash
obsidian-kb index --config config.toml --json
obsidian-kb query --config config.toml --offline --json "deployment rollback"
obsidian-kb audit --config config.toml --json
obsidian-kb status --config config.toml --json
```

Exit codes are `0` for success, `2` for invalid configuration or operational input, `3` for a missing index, and `4` for a failed audit. Human output includes `path:start-end`, title, heading hierarchy, retrieval modes, score, and snippet. JSON is stable for automation.

Indexing is incremental. Changed notes replace their chunks transactionally. Deleted, moved, newly excluded, and changed notes are reconciled. Best-effort Qdrant deletion is attempted; query postfiltering remains the security boundary if it fails.

## Exclusion control

A note is excluded when a configured frontmatter key is false:

```yaml
---
knowledge_index: false
---
```

Folder and glob rules use vault-relative POSIX paths. Hidden path components are excluded when `exclusions.hidden` is true; this secure default should be disabled only for intentionally curated hidden notes. Placeholder documentation such as `api_key = ${API_KEY}` remains indexable; real credential assignments suppress the complete note. Use custom regular expressions sparingly because matches fail closed.

## Hermes plugin

`hermes_plugin/plugin.yaml` declares the plugin. `hermes_plugin.register(ctx)` registers:

- tool `obsidian_knowledge_search`, returning a JSON string;
- slash command `/knowledge`;
- bundled skill `obsidian-knowledge-backbone`.

The plugin is read-only and requires `OBSIDIAN_KB_CONFIG` or an explicit approved `config_path`. Install or activate it through Hermes' normal plugin workflow; a new session or gateway restart is required after activation. This repository does not modify any Hermes profile or runtime configuration.

## Migration and rollback

The database is entirely derived state; vault files are never migrated. No vault backup is required to adopt or remove this index.

1. Configure a new SQLite path and, if desired, a new Qdrant collection name.
2. Run `index`, then `audit`, then an offline `query` before enabling consumers.
3. Point the operator config at the verified state path.
4. To roll back, point the config at the previous verified SQLite path and collection. If no previous derived index was retained, disable the plugin and rebuild from the unchanged vault into a fresh state path.
5. Remove an obsolete SQLite file or Qdrant collection only as a separate explicit operator action. The application never deletes state directories or vault files.

## Verification

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
python3 -m compileall -q obsidian_kb hermes_plugin tests
python3 -m build
```

Tests use synthetic temporary vaults and mocked HTTP only. CI covers Python 3.11 and 3.13.

## Limitations

- The frontmatter reader intentionally supports simple top-level `key: value` booleans, not full YAML.
- FTS5 must be enabled in the host SQLite build.
- Secret detection prioritizes high-confidence suppression; operators must still exclude sensitive folders and review configuration.
- No live Qdrant or Ollama deployment is performed by this repository's tests.
- Snippets are retrieval evidence, not a substitute for reading the exact cited original lines before making consequential claims.

See `SECURITY.md` for reporting and operational boundaries and `docs/architecture.md` for data-flow details.

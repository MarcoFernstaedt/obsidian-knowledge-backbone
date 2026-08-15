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
- Hidden paths (enabled by default), configured folders and globs, frontmatter false values, private keys, common Slack/GitLab/Stripe/Twilio/OpenAI/AWS token shapes, and non-placeholder shell/JSON/YAML credential assignments are excluded.
- Every note read uses descriptor-relative `O_NOFOLLOW` traversal from one trusted vault-root descriptor. Systems without the required descriptor APIs fail closed.
- Excluded-note records contain only path and reason. They contain no source hash, excerpt, or content.
- One complete vault scan, including FTS changes, commits as one SQLite transaction. A failed scan leaves the previous complete generation visible. Qdrant work starts only after that commit and remains pending for retry on failure.
- SQLite is authoritative and uses WAL plus durable pending/tombstone ledgers. Qdrant payloads include corpus, index schema 2, model digest, and compatibility signature; every query/list/delete is generation-filtered and a corpus containing missing or mismatched signatures is rejected before publication or reconciliation.
- Every candidate is checked against the current note SHA-256. Changed or missing source suppresses results until reindexing.
- Remote failures degrade to lexical retrieval. They do not bypass filtering.

## Install

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
cp config.example.toml config.toml
```

Edit `config.toml`. Keep the state path outside the vault. For the Hermes plugin and refresh wrapper, the fixed `OBSIDIAN_KB_CONFIG` target must be an absolute, current-user-owned regular non-symlink file with no group/other permission bits (`chmod 600 config.toml`).

## CLI

```bash
imperator-knowledge index --config config.toml --full-reconcile --json
imperator-knowledge search --config config.toml --path-prefix Runbooks --json "deployment rollback"
imperator-knowledge audit --config config.toml --json
imperator-knowledge status --config config.toml --json
```

Compatibility executables accept their historical direct syntax: `imperator-search QUERY [flags]` injects `search`, while `imperator-vault-index [flags]` injects `index`. `imperator-knowledge` continues to require a subcommand.

Exit codes are `0` for success (including a valid degraded lexical search), `2` for usage or invalid configuration, `4` for a locally committed index with pending semantic/tombstone work or a stale audit, and `1` for fatal corruption or invariant failure. Human output marks passages untrusted and includes `path:Lstart-Lend`, title, heading hierarchy, retrieval mode, score, and snippet. Queries are capped at 512 characters, limits at 20, and path prefixes must be relative vault paths.

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

- tools `obsidian_knowledge_search` and `obsidian_knowledge_status`, both returning JSON strings;
- slash command `/notesearch`;
- bundled skill `obsidian-knowledge-backbone`.

The plugin is read-only and reads one fixed `OBSIDIAN_KB_CONFIG`. Callers cannot supply a config path or offline override. Registration performs no network access. Returned passages are untrusted quoted source material, never instructions.

Supported installation paths (run only after local review):

```bash
# Local/pip entry-point install: use the same Python environment that owns `hermes`.
python -m pip install /absolute/path/to/obsidian-knowledge-backbone
hermes plugins list
hermes plugins enable obsidian-retrieval

# Git install through Hermes (replace with the reviewed owner/repository).
hermes plugins install OWNER/REPOSITORY --no-enable
hermes plugins list
hermes plugins enable obsidian-retrieval
```

For source-directory development, Hermes also supports a trusted plugin directory under `~/.hermes/plugins/`; do not copy only `hermes_plugin/` without also installing this package, because it imports `obsidian_kb`. Restart the CLI session or gateway after activation. Roll back plugin activation with `hermes plugins disable obsidian-retrieval`; remove only with `hermes plugins remove obsidian-retrieval` after rollback is accepted. This repository does not perform those profile/runtime actions.

## Migration and rollback

The database is entirely derived state; vault files are never migrated. No vault backup is required to adopt or remove this index.

1. Configure a new SQLite path and the side-by-side `imperator_obsidian_chunks_v2` collection. The TOML configuration schema is integer `1`; the SQLite/Qdrant index schema is integer `2`; public search results independently use string schema `"1.0"`.
2. Run `index`, then `audit`, then an offline `query` before enabling consumers.
3. Point the operator config at the verified state path.
4. Rollback does **not** depend on an automatically created database backup. Disable the new plugin/refresh and restore the unchanged legacy command implementation against the unchanged legacy `imperator_obsidian_notes` collection retained outside this repository.
5. Preserve the v2 SQLite database and `imperator_obsidian_chunks_v2` for diagnosis. Do not delete either old or new collection during rollback. Removal is a separate explicit operator action after the retention window.

## Verification

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
python3 -m compileall -q obsidian_kb hermes_plugin tests
python3 -m build
```

Tests use synthetic temporary vaults and mocked HTTP only. CI covers Python 3.11 and 3.13. Production live acceptance (complete private vault, Ollama/Qdrant parity, private query set, scheduled run, channel round trips, leakage inspection, and rollback rehearsal) is explicitly parent/operator-owned and remains pending until recorded against the deployed exact candidate.

For scheduling after manual acceptance, set absolute `OBSIDIAN_KB_BIN` and fixed private `OBSIDIAN_KB_CONFIG`, then invoke `scripts/imperator_obsidian_retrieval_refresh.sh` from exactly one no-agent scheduler. The wrapper validates config ownership/mode before creating state, uses private directories, `flock`, a bounded timeout, and logs only safe counts plus an exit/error class—never raw CLI errors, paths, queries, or note content. This repository does not install or activate a schedule.

Build front doors are exactly pinned to `build==1.5.0` and `setuptools==80.9.0`, and GitHub Actions remain full-SHA pinned. PyPI wheel integrity and transitive tooling used to obtain those exact versions remain an operator/CI image trust boundary; this zero-runtime-dependency project does not claim a hash-locked offline build.

## Limitations

- The frontmatter reader intentionally supports simple top-level `key: value` booleans, not full YAML.
- FTS5 must be enabled in the host SQLite build.
- Secret detection prioritizes high-confidence suppression; operators must still exclude sensitive folders and review configuration.
- No live Qdrant or Ollama deployment is performed by this repository's tests.
- Snippets are retrieval evidence, not a substitute for reading the exact cited original lines before making consequential claims.

See `SECURITY.md` for reporting and operational boundaries and `docs/architecture.md` for data-flow details.

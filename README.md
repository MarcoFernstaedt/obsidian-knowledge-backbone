# Obsidian Knowledge Backbone

A private, network-free citation index for curated Markdown vaults. It reads the vault without writing it, atomically publishes a local SQLite FTS5 index, deterministically ranks lexical matches, and returns current exact path/line citations. The Python 3.11+ runtime has no third-party dependencies.

## Architecture

```text
Read-only Obsidian vault
          |
          v
Privacy and eligibility gate
          |
          v
Heading-aware bounded chunks
          |
          v
Atomic SQLite ledger + FTS5
          |
          v
Deterministic BM25 ranking and source-SHA freshness check
          |
          v
CLI and two read-only Hermes tools with exact citations
```

## Privacy and correctness

- Descriptor-relative, no-follow vault traversal rejects symlinks, non-regular entries, races, oversized files, malformed/excluded frontmatter, and credential canaries. Device, inode, size, mtime, and ctime are validated across every read.
- Configuration rejects the SQLite database and lock at or beneath the vault, including symlink-resolved and nonexistent descendants.
- Hidden paths, configured folders/globs, false retrieval controls, and credential-bearing notes are excluded. Excluded rows store only relative path and a reason.
- A complete scan commits notes, chunks, FTS rows, removals, and metadata in one SQLite transaction. A failed scan leaves the previous complete generation visible.
- Compatibility binds the corpus, schema, all chunk limits, source-size bound, and content policy. Incompatible databases are never queried.
- Every indexed candidate is re-read through the trusted vault descriptor. A missing or changed source SHA suppresses it until refresh.
- Missing, corrupt, or incompatible SQLite uses a bounded in-memory filesystem lexical fallback under the same privacy, path, size, and freshness controls. Fallback never creates or modifies state.
- Returned snippets are explicitly untrusted quoted source data, never instructions.

## Install and configure

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
cp config.example.toml /absolute/private/config.toml
chmod 600 /absolute/private/config.toml
export OBSIDIAN_KB_CONFIG=/absolute/private/config.toml
```

The fixed config must be a current-user-owned regular non-symlink file with no group/other permission bits. Its state path must be outside the vault.

## CLI

```bash
imperator-knowledge index --json
imperator-knowledge search --path-prefix Runbooks --json "deployment rollback"
imperator-knowledge audit --json
imperator-knowledge status --json
```

All commands use only `OBSIDIAN_KB_CONFIG`; callers cannot override config or execution mode. Queries are 1–512 characters, limits are 1–20, and path prefixes are relative traversal-safe POSIX vault paths. Index success exits `0`; usage/config errors exit `2`; corruption and invariant failures exit `1`. Compatibility entry points `imperator-search` and `imperator-vault-index` remain available.

## Hermes plugin

The plugin registers exactly:

- `obsidian_knowledge_search`
- `obsidian_knowledge_status`
- `/notesearch`

It reads only the fixed private config. Status returns index age, source drift, current/stale and compatibility state, plus active/excluded note and chunk counts; it returns no note paths or content. Search returns lexical-only results with `path:Lstart-Lend`, Obsidian links, heading hierarchy, and untrusted snippets.

Install only after local review using the Python environment that owns Hermes, then enable `obsidian-retrieval` through the normal Hermes plugin command. This repository does not modify profiles, services, schedules, vaults, or live state.

## Refresh wrapper

Set absolute `OBSIDIAN_KB_BIN` and fixed private `OBSIDIAN_KB_CONFIG`, then invoke `scripts/imperator_obsidian_retrieval_refresh.sh` from one private scheduler. It validates ownership/mode, locks, applies a bounded timeout, and logs only safe counts and exit classes—never queries, paths, source text, or raw errors.

## Migration and rollback

The v2 package uses local index schema `3`; older database generations are intentionally incompatible. Build a new side-by-side state path, run `index`, `audit`, and representative private searches, then change the private config only after acceptance. Roll back by disabling the plugin/refresh and restoring the previous config. State is derived and may be retained for diagnosis or deleted as a separate explicit operator action; vault files are never migrated.

## Verification

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
PYTHONPYCACHEPREFIX=/tmp/obsidian-kb-pycache python3 -m compileall -q obsidian_kb hermes_plugin tests
bash -n scripts/imperator_obsidian_retrieval_refresh.sh
python3 -m build
```

CI covers Python 3.11 and 3.13, an isolated wheel install, and every console entry-point smoke. Live private-vault, scheduled-run, channel, leakage, and rollback acceptance remains parent/operator-owned.

## Limitations

- FTS5 must be enabled in the host SQLite build.
- The bounded frontmatter reader is intentionally not a general YAML loader; malformed retrieval controls fail closed.
- Secret detection is defense in depth. Keep sensitive folders excluded and credentials outside Markdown.
- Exact cited original lines should be re-read before consequential claims.

See `SECURITY.md` and `docs/architecture.md`.

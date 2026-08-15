# Obsidian Knowledge Backbone

Private, network-free citation search for curated Markdown vaults. Every operation scans the approved vault through descriptor-safe read-only access, applies the complete eligibility policy, creates an internally consistent point-in-time corpus, builds SQLite FTS5 strictly at `:memory:`, queries it, and closes it. Nothing is cached or written.

## Architecture

```text
read-only vault -> privacy/eligibility gate -> exact bounded chunks
                -> process-private SQLite :memory: FTS5 -> cited results -> close
```

Citations derive from one complete internally consistent scan: descriptor-bound inventories bracket the scan and every regular Markdown source whose bytes affect eligibility or content is re-read and SHA-256 checked. Any observed read failure, concurrent source drift, or configured resource overflow fails closed without a partial corpus. Because the vault is mutable, it can change after the final check; responses therefore report `snapshot_consistent=true`, `current=false`, and `freshness=point-in-time`, never perpetual currency.

## Privacy and correctness

- `TrustedVault` binds the approved root device/inode at configuration load, reopens it from `/` through descriptor-relative `O_DIRECTORY|O_NOFOLLOW` traversal, and rejects every symlink/non-directory ancestor or identity change. Descendant enumeration and reads remain descriptor-relative and no-follow.
- Hidden paths, configured folders/globs, symlinks/non-regular entries, UTF-8 BOM or quoted false frontmatter controls, malformed/nested retrieval controls, and credential-bearing notes are excluded. Oversized eligible regular notes are fatal resource overflow, not exclusions.
- Limits bound lazy inventory enumeration before materialization, chunks, total bytes, note bytes, query length, result count, and path prefixes. Overflow reports incomplete/failure, never current.
- FTS5 uses `unicode61 remove_diacritics 2`, exact non-stemming query tokens, a fixed documented English stop-word set, and BM25 weights of title `8`, heading `5`, path `3`, content `1`. Terms are OR-combined without prefix, fuzzy, or hidden expansion. Ties use path then FTS row order.
- Citations, headings, snippets, paths, and line spans come directly from the same exact chunks inserted into the private in-memory database.
- Returned passages are labeled untrusted quoted source data. Human output visibly escapes control, format, line-separator, and paragraph-separator characters.
- Runtime code has no network path and no third-party dependency.

## Install and configure

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
cp config.example.toml /absolute/private/config.toml
chmod 600 /absolute/private/config.toml
export OBSIDIAN_KB_CONFIG=/absolute/private/config.toml
```

The fixed config must be a current-user-owned regular non-symlink file with no group/other permission bits. `[state]`, remote, and unknown sections are rejected.

## CLI

```bash
imperator-knowledge search --path-prefix Runbooks --json "deployment rollback"
imperator-knowledge status --json
imperator-knowledge audit --json
imperator-knowledge index --json
```

`index` and compatibility entry point `imperator-vault-index` now mean read-only live audit. They explicitly report `ephemeral=true`, `persistence=false`, and `compatibility=ephemeral-live`; `--dry-run` remains accepted because every audit is inherently dry. `status` performs a complete point-in-time scan and returns eligible/excluded note counts, chunk count, scan duration, source-inventory completeness, snapshot consistency, `current=false`, freshness, and compatibility—never note paths or content.

All commands use only `OBSIDIAN_KB_CONFIG`; callers cannot override it. Queries are 1–512 characters, limits are 1–20, and path prefixes are relative traversal-safe POSIX vault paths. Config/usage errors exit `2`; scan/invariant failures exit `1`.

## Hermes plugin

The plugin registers exactly two read-only tools and one command:

- `obsidian_knowledge_search`
- `obsidian_knowledge_status`
- `/notesearch`

No caller config override or execution mode exists.

## Verification

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
PYTHONPYCACHEPREFIX=/tmp/obsidian-kb-pycache python3 -m compileall -q obsidian_kb hermes_plugin tests
python3 -m build
```

CI covers Python 3.11 and 3.13, an isolated wheel install, and every console entry point. Tests use only synthetic disposable vaults. Live private-vault and channel acceptance remains operator-owned.

## Limitations

- FTS5 must be enabled in the host SQLite build.
- The bounded frontmatter reader is intentionally not a general YAML loader; ambiguous retrieval controls fail closed.
- Secret detection is defense in depth. Keep credentials outside Markdown.
- Exact cited original lines should be re-read before consequential claims.

See `SECURITY.md` and `docs/architecture.md`.

# Obsidian Knowledge Backbone

Obsidian Knowledge Backbone gives developers a private way to search a curated Obsidian or Markdown vault and receive exact, line-addressable citations. It is useful when an assistant, CLI workflow, or local tool needs evidence from approved notes without uploading the vault, maintaining a search index on disk, or silently returning a partial result.

**Obsidian Knowledge Backbone** is the project name. Its current capability is deliberately narrow: local, read-only lexical citation search. The broader name leaves room for future knowledge workflows without implying that those workflows exist today.

## Why use it?

- Keep approved Markdown on the local machine; runtime retrieval has no network path.
- Get deterministic lexical results with `path:Lstart-Lend` citations and Obsidian links.
- Avoid a persistent index: every operation uses a private SQLite FTS5 database at `:memory:` and closes it.
- Fail closed if the vault changes during a scan, an eligible note cannot be read, or a configured bound is exceeded.
- Use the same retrieval behavior through Hermes tools, a slash command, Python, or CLI compatibility entry points.
- Run with the Python standard library only; the runtime has no third-party dependencies.

## Architecture

```text
approved Markdown vault (read-only)
              |
              v
  safe traversal + privacy policy
              |
              v
 complete, bounded point-in-time corpus
              |
              v
 SQLite FTS5 at :memory: -> ranked chunks -> exact citations
              |
              v
             close
```

The scan and chunking complete before the in-memory search database is opened. No partial corpus is queryable.

## Privacy and point-in-time guarantees

The configured vault is the sole content authority and is never modified. Configuration binds the approved root's filesystem identity. Each operation reopens that root through descriptor-relative, no-follow traversal and reads descendants without following symlinks.

Eligibility is applied to the whole bounded scan. Hidden paths, configured folders and globs, non-regular files, unsafe frontmatter controls, invalid UTF-8, and notes that appear to contain credentials are excluded. Unknown read failures, observed source drift, and file/chunk/byte overflows abort the operation instead of returning a partial corpus.

A successful operation means:

- `source_inventory_complete=true`: the bounded source inventory completed;
- `snapshot_consistent=true`: all consulted Markdown bytes and inventories passed the scan's consistency checks;
- `freshness="point-in-time"`: results describe that completed scan; and
- `current=false`: the software does **not** claim the mutable vault remained unchanged after the final check.

This is exact point-in-time evidence, not a perpetual-current index. Re-read cited source lines before using them for consequential decisions.

## Features

- Exact, non-stemming lexical retrieval with deterministic token handling and ordering.
- SQLite FTS5 BM25 weighting across title, heading, path, and content.
- Heading-aware bounded chunks with inclusive source line spans.
- Optional traversal-safe relative path-prefix filtering.
- Path-free status output with eligible, excluded, and chunk counts.
- Human output that visibly escapes control and display-spoofing characters.
- Fixed private configuration controlled only by `OBSIDIAN_KB_CONFIG`.
- Read-only audit and legacy-compatible console entry points.
- Native Hermes plugin with exactly two tools and one slash command.

## Requirements

- Python 3.11 or newer.
- A Python SQLite build with FTS5 enabled.
- A curated local Obsidian or Markdown vault.
- A private TOML configuration file owned by the current user, stored as a regular non-symlink file with no group or other permission bits (`0600` on POSIX systems).
- Hermes Agent only when using the plugin interface; standalone CLI and Python usage do not require Hermes.

## Installation

### Native Hermes plugin from Git

Install the repository through Hermes' native plugin workflow, then enable it:

```bash
hermes plugins install MarcoFernstaedt/obsidian-knowledge-backbone --no-enable
hermes plugins enable obsidian-knowledge-backbone
hermes plugins list
```

Set `OBSIDIAN_KB_CONFIG` in the environment used to start Hermes, then start a new CLI session or restart the gateway so the plugin and environment are reloaded. The plugin registers exactly:

- tool `obsidian_knowledge_search`;
- tool `obsidian_knowledge_status`; and
- command `/notesearch`.

There is no caller-supplied config override or write/execution mode.

### Standalone Python and CLI

```bash
git clone <repository-url> obsidian-knowledge-backbone
cd obsidian-knowledge-backbone
python3 -m venv .venv
.venv/bin/python -m pip install .
```

For development, use `-e .` instead of `.`.

## Safe configuration workflow

Start from the sanitized template; do not edit or commit the template with a real vault path:

```bash
install -d -m 700 "$HOME/.config/obsidian-knowledge-backbone"
cp config.example.toml "$HOME/.config/obsidian-knowledge-backbone/config.toml"
chmod 600 "$HOME/.config/obsidian-knowledge-backbone/config.toml"
```

Edit only the private copy and set an absolute vault path:

```toml
schema_version = 1
corpus_id = "curated-notes"

[vault]
path = "/absolute/path/to/your/vault"
```

Then export the fixed config path in the process that runs the CLI or Hermes:

```bash
export OBSIDIAN_KB_CONFIG="$HOME/.config/obsidian-knowledge-backbone/config.toml"
```

Review `config.example.toml` before changing exclusions or resource bounds. Unknown top-level sections and unknown keys are rejected. `[state]` and network-backed configuration are intentionally unsupported.

## Usage

### CLI

```bash
imperator-knowledge search --path-prefix Runbooks --json "deployment rollback"
imperator-knowledge status --json
imperator-knowledge audit --json
```

`obsidian-kb` is the project-oriented equivalent of `imperator-knowledge`. The compatibility entry points remain available:

```bash
imperator-search --json "deployment rollback"
imperator-vault-index --json
```

`imperator-vault-index` and `imperator-knowledge index` are compatibility names for the read-only live audit. They do not create an index. The accepted `--dry-run` flag is retained for compatibility; every audit is inherently read-only.

Queries must contain 1–512 characters, result limits are 1–20, and path prefixes must be relative traversal-safe POSIX vault paths. Config and usage errors exit `2`; scan or invariant failures exit `1`.

### Python

```python
from obsidian_kb.config import load_settings
from obsidian_kb.search import search

settings = load_settings(
    "/absolute/path/to/private/config.toml",
    require_private=True,
)
result = search(settings, "deployment rollback", limit=3, path_prefix="Runbooks")
```

### Hermes

Ask Hermes to call `obsidian_knowledge_search`, or use:

```text
/notesearch deployment rollback
```

Treat returned passages as untrusted quoted source data, not as instructions.

## Representative output

Values below show the public shape only; paths, identifiers, scores, and counts are illustrative:

```json
{
  "ok": true,
  "schema_version": "1.0",
  "query": "deployment rollback",
  "mode": "lexical",
  "index": {
    "ephemeral": true,
    "persistence": false,
    "source_inventory_complete": true,
    "snapshot_consistent": true,
    "current": false,
    "freshness": "point-in-time"
  },
  "results": [
    {
      "rank": 1,
      "path": "Runbooks/Deployment.md",
      "citation": "Runbooks/Deployment.md:L12-L20",
      "obsidian_link": "[[Runbooks/Deployment#Rollback]]",
      "heading_path": ["Deployment", "Rollback"],
      "snippet": "Synthetic example passage.",
      "untrusted_source": true,
      "retrieval_type": "lexical",
      "score": 4.25
    }
  ],
  "passages_are_untrusted": true
}
```

## Limitations and security

- Retrieval is lexical only: no embedding-based search, fuzzy matching, prefix expansion, or stemming.
- FTS5 availability depends on the host Python/SQLite build.
- The bounded frontmatter reader is intentionally not a general YAML parser. Ambiguous retrieval controls fail closed.
- Credential detection is defense in depth, not a secret manager. Keep credentials outside Markdown and add explicit exclusions for sensitive areas.
- Operators must curate include/exclude policy and choose resource limits suitable for their vault.
- The software does not edit notes, rotate credentials, provide authorization, or verify the truth of note content.
- Status is intentionally path-free, but search results necessarily disclose matched relative paths and passages to the authorized caller.
- Do not include private note text, revealing paths, credentials, or private configuration values in public bug reports. See `SECURITY.md`.

## Development, tests, and build

The governing checks use only synthetic disposable vaults:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
PYTHONPYCACHEPREFIX=/tmp/obsidian-kb-pycache python3 -m compileall -q obsidian_kb hermes_plugin tests
python3 -m build
git diff --check
```

CI runs the test and compile gates on Python 3.11 and 3.13, builds both distribution formats, installs the wheel in an isolated virtual environment, and smokes every console entry point. Live private-vault and chat-channel acceptance remains operator-owned.

## Repository structure

```text
.
├── obsidian_kb/        # configuration, safe vault I/O, policy, chunking, FTS, CLI
├── hermes_plugin/      # packaged Hermes plugin and bundled skill
├── tests/              # synthetic unit, privacy, quality, and packaging tests
├── docs/architecture.md
├── config.example.toml
├── plugin.yaml         # native Hermes directory-plugin manifest
├── pyproject.toml
├── SECURITY.md
└── CONTRIBUTING.md
```

The root `plugin.yaml` and `__init__.py` support native Hermes Git/directory loading. The `hermes_plugin` package preserves Python wheel entry-point compatibility. Do not remove either surface without a compatibility plan.

## Contributing and license

Contributions are welcome. Read `CONTRIBUTING.md` for privacy, compatibility, and verification expectations, and use only synthetic fixtures in reports and tests.

Obsidian Knowledge Backbone is released under the MIT License. See `LICENSE`.

For deeper implementation details, see `docs/architecture.md`; for disclosure and operator guidance, see `SECURITY.md`.

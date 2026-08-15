# Installation and first run

This guide covers the supported standalone and Hermes Agent installation paths for Obsidian Knowledge Backbone. Runtime retrieval is local and read-only. The package is installed from Git; this project does not claim a PyPI publication.

## Before you install

You need:

- Python 3.11 or newer;
- Git;
- a Python SQLite build with FTS5 enabled;
- a curated local Obsidian or Markdown vault; and
- Hermes Agent only if you want the native plugin.

The security boundary depends on POSIX ownership, file modes, descriptor-relative traversal, and no-follow filesystem operations. Linux, macOS, and Windows through WSL are the supported operating environments. Native Windows Python is not supported.

## Verify Python and FTS5

Run this with the Python interpreter that will own the installation:

```bash
python3 --version
python3 - <<'PY'
import sqlite3

connection = sqlite3.connect(":memory:")
try:
    connection.execute("CREATE VIRTUAL TABLE fts_probe USING fts5(body)")
    print("FTS5 available")
finally:
    connection.close()
PY
```

Continue only when the second command prints `FTS5 available`. If virtual-table creation raises `sqlite3.OperationalError`, use a Python distribution built with SQLite FTS5.

## Standalone installation on Linux or macOS

Clone the public repository and install it into a project-local virtual environment:

```bash
git clone https://github.com/MarcoFernstaedt/obsidian-knowledge-backbone.git
cd obsidian-knowledge-backbone
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/obsidian-kb --help
```

For local development, replace the install command with:

```bash
.venv/bin/python -m pip install -e .
```

For a direct Git dependency without a checkout:

```bash
python3 -m pip install "git+https://github.com/MarcoFernstaedt/obsidian-knowledge-backbone.git@main"
```

Use a full reviewed commit in place of `main` for reproducible environments.

## Windows PowerShell and WSL

Run PowerShell as an administrator to install WSL if it is not already available:

```powershell
wsl --install
```

Restart Windows if requested. Open the installed Linux distribution from the Start menu, or enter it from PowerShell:

```powershell
wsl
```

All remaining commands run inside the WSL shell, not native PowerShell:

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv

git clone https://github.com/MarcoFernstaedt/obsidian-knowledge-backbone.git
cd obsidian-knowledge-backbone
python3 -m venv .venv
.venv/bin/python -m pip install .
```

Keep the config and, preferably, the vault inside the Linux filesystem rather than a Windows-mounted directory so POSIX ownership and mode checks remain meaningful. If the vault must live under `/mnt/c`, verify the config still lives in the WSL filesystem with mode `0600`, and test the complete scan before relying on it.

Native Windows Python is intentionally not presented as an installation choice. PowerShell ACL commands are not equivalent to the POSIX security invariants enforced by the implementation.

## Create the private configuration

From the repository checkout, create a private copy of the sanitized template:

```bash
umask 077
mkdir -p "$HOME/.config/obsidian-knowledge-backbone"
cp config.example.toml "$HOME/.config/obsidian-knowledge-backbone/config.toml"
chmod 600 "$HOME/.config/obsidian-knowledge-backbone/config.toml"
```

Edit only that private copy. At minimum, set a corpus label and an absolute vault path:

```toml
schema_version = 1
corpus_id = "curated-notes"

[vault]
path = "/absolute/path/to/your/vault"
```

Do not commit the private copy or add a real path to `config.example.toml`.

The loader requires the config to be:

- owned by the current user;
- a regular file, not a symlink; and
- inaccessible to group and other users (`0600` on POSIX systems).

Check the effective mode on Linux:

```bash
stat -c '%a %U %n' "$HOME/.config/obsidian-knowledge-backbone/config.toml"
```

On macOS, use:

```bash
stat -f '%Lp %Su %N' "$HOME/.config/obsidian-knowledge-backbone/config.toml"
```

The expected mode is `600` and the owner should be the current user.

## Understand the configuration policy

The complete template includes:

- `include_globs` for eligible Markdown paths;
- folder and glob exclusions;
- hidden-path policy;
- frontmatter keys whose false value suppresses retrieval;
- additional operator-defined credential patterns;
- chunk line, character, overlap, and note-size bounds; and
- scan-wide file, chunk, and byte limits.

Unknown sections and keys fail closed. An empty list for a built-in list setting falls back to the implementation default; copy and adjust the explicit template values rather than assuming an empty list disables a policy.

Resource bounds are safety limits, not pagination. Exceeding one aborts the complete operation rather than returning a partial corpus.

## First standalone commands

Export the fixed config path in every shell that invokes the CLI:

```bash
export OBSIDIAN_KB_CONFIG="$HOME/.config/obsidian-knowledge-backbone/config.toml"
```

Start with path-free status, then search with exact terms that appear in an eligible note:

```bash
.venv/bin/obsidian-kb status
.venv/bin/obsidian-kb search --limit 3 "deployment rollback"
```

For machine-readable output:

```bash
.venv/bin/obsidian-kb status --json
.venv/bin/obsidian-kb search --limit 3 --json "deployment rollback"
```

For a deeper read-only validation that also constructs and closes FTS5:

```bash
.venv/bin/obsidian-kb audit --json
```

A successful scan reports `source_inventory_complete=true`, `snapshot_consistent=true`, `freshness="point-in-time"`, and `current=false`. The last value is deliberate: the mutable vault may change after the final consistency check.

## Hermes and gateway setup

### Install the plugin from Git

Hermes' plugin installer accepts a Git URL or `owner/repo` shorthand. Install and enable this repository with:

```bash
hermes plugins install MarcoFernstaedt/obsidian-knowledge-backbone --enable
hermes plugins list
```

To review it before enabling:

```bash
hermes plugins install MarcoFernstaedt/obsidian-knowledge-backbone --no-enable
hermes plugins enable obsidian-knowledge-backbone
```

The native manifest exposes exactly two tools and one command:

- `obsidian_knowledge_search`;
- `obsidian_knowledge_status`; and
- `/notesearch`.

### Provide the config to Hermes CLI

Export the variable before starting Hermes:

```bash
export OBSIDIAN_KB_CONFIG="$HOME/.config/obsidian-knowledge-backbone/config.toml"
hermes
```

Start a new Hermes session after enabling the plugin. Tool exposure is established at session start.

### Provide the config to Hermes Gateway

The gateway must receive the variable from its own startup environment; exporting it in an unrelated interactive shell is insufficient.

Hermes can print the environment-file location used by the active profile:

```bash
hermes config env-path
```

Edit that private environment file and add an absolute path without shell quoting or `~` expansion:

```dotenv
OBSIDIAN_KB_CONFIG=/absolute/path/to/private/config.toml
```

Keep the environment file private, then restart and inspect the gateway through Hermes:

```bash
chmod 600 "$(hermes config env-path)"
hermes gateway restart
hermes gateway status
```

If your gateway is launched by another service manager or container, configure `OBSIDIAN_KB_CONFIG` in that manager instead. Do not hard-code another person's home directory, vault path, or service layout. Restart only after the environment and config permissions are correct.

For current Hermes platform and service behavior, use the [Hermes Agent documentation](https://hermes-agent.nousresearch.com/docs/). The plugin-install syntax above is also discoverable from the installed runtime with `hermes plugins install --help`.

### First Hermes checks

Ask Hermes to call `obsidian_knowledge_status`. Then run a synthetic or non-sensitive search:

```text
/notesearch deployment rollback
```

The command labels note passages as untrusted quoted source. A model or operator must not treat note text as executable instructions.

## Upgrade and removal

Update the native plugin from its configured Git source:

```bash
hermes plugins update obsidian-knowledge-backbone
```

Start a new CLI session or restart the gateway after an update.

Disable without deleting:

```bash
hermes plugins disable obsidian-knowledge-backbone
```

Remove the native plugin:

```bash
hermes plugins remove obsidian-knowledge-backbone
```

For a standalone checkout, update deliberately, review upstream changes, and reinstall:

```bash
git fetch origin
# Review the target commit before integrating it.
.venv/bin/python -m pip install --force-reinstall .
```

No uninstall step deletes or edits the vault. Remove the private config separately only when you no longer need it.

## Troubleshooting

### FTS5 is unavailable

Symptom: `sqlite3.OperationalError` mentions `fts5` during the probe or audit.

Resolution: install a Python distribution whose bundled or linked SQLite enables FTS5. Verify with the exact probe at the top of this guide before retrying the application.

### The config is rejected

Check all of the following:

```bash
printf '%s\n' "$OBSIDIAN_KB_CONFIG"
test -f "$OBSIDIAN_KB_CONFIG" && test ! -L "$OBSIDIAN_KB_CONFIG"
chmod 600 "$OBSIDIAN_KB_CONFIG"
```

Confirm that the file belongs to the current user, uses valid TOML, contains `schema_version = 1`, names only supported sections and keys, and points to an accessible directory.

### Status or audit fails closed

The CLI intentionally returns only an error class. Common causes include:

- the vault or an ancestor was replaced after config loading;
- a source or inventory changed during the scan;
- a Markdown path is a symlink or non-regular file;
- an eligible note exceeds `maximum_note_bytes`;
- the complete scan exceeds a file, chunk, or total-byte bound; or
- a source cannot be read consistently.

Stop active vault synchronization briefly for diagnosis, confirm permissions, inspect policy and bounds, then rerun `status` and `audit`. Do not weaken limits until you understand the vault size and exposure.

### Search returns no passages

Use exact words present in a note. Search does not stem words, expand prefixes, or perform fuzzy matching. Then check:

- `include_globs` includes the path;
- no folder or glob exclusion matches it;
- hidden-path policy is expected;
- frontmatter does not opt out;
- the note is valid UTF-8;
- the note does not match credential controls; and
- `--path-prefix`, if supplied, is the intended relative POSIX path.

### Hermes does not show the tools

Run:

```bash
hermes plugins list
hermes gateway status
```

Confirm the plugin is enabled and `OBSIDIAN_KB_CONFIG` exists in the environment that actually starts Hermes. Start a new CLI session or restart the gateway after changing plugin or environment state.

### Safe support requests

Never attach a real config, note, vault listing, home path, token, or search response containing private text. Reproduce with a disposable synthetic vault and follow [the security policy](../SECURITY.md) for private vulnerability reports.

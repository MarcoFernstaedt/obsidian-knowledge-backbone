# Security Policy

## Supported version

Security fixes target the current `main` branch and latest tagged release.

## Data boundary

The vault is read-only. Configuration binds the approved root device/inode. A complete search/status/audit reopens every absolute root component through descriptor-relative no-follow directory operations, compares the configured identity, and traverses descendants the same way. Symlinks, non-regular files, changed-during-read sources, scan-wide inventory drift, and configured limits fail closed.

Eligible exact chunks are inserted only into a process-private SQLite FTS5 database opened as `:memory:`. The connection is closed before results return. Runtime retrieval creates no filesystem artifact and opens no network connection.

Hidden/configured paths, false or malformed/nested retrieval controls, private keys, known token formats, and sensitive assignments (including YAML list mappings, quoted scalars, YAML single-quote escaping, and block scalars) are suppressed unless confidently classified as documentation placeholders. This is defense in depth, not a replacement for vault curation. Unknown/transient read failures and file/chunk/byte overflow abort the complete operation; an oversized eligible regular note is never reported as a safe exclusion, and no partial result is returned.

Successful results are complete and internally consistent for the scan, not perpetually current. The mutable vault may change after the final check; status and search therefore report point-in-time freshness and always set `current=false`.

Hermes errors disclose exception class rather than private paths or content. Status is path-free. Search passages are untrusted quoted source data, and human rendering escapes Unicode display controls and separators.

## Reporting

Use GitHub's private vulnerability reporting workflow:

1. Open this repository's **Security** tab on GitHub.
2. Select **Report a vulnerability**.
3. Submit a minimal synthetic reproducer and the affected version.

Do not open a public issue containing private note text, revealing paths, credentials, or private configuration values. If private vulnerability reporting is temporarily unavailable, do not disclose sensitive details publicly; wait for the private reporting route to be restored.

## Operator response

If a secret-bearing note may have been retrieved:

1. Disable the plugin or remove its config environment variable.
2. Add a folder, glob, frontmatter, or secret-pattern exclusion.
3. Rotate any credential that may have been exposed.
4. Run the read-only audit and a representative search before re-enabling.

There is no retrieval artifact to locate or delete. The software never edits or deletes vault content and never performs automatic credential rotation.

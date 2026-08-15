# Security Policy

## Supported version

Security fixes target the current `main` branch and latest tagged release.

## Data boundary

The vault is read-only. A complete search/status/audit opens one trusted vault root and traverses descendants with descriptor-relative no-follow operations. Symlinks, non-regular files, changed-during-read sources, and configured limits fail closed.

Eligible exact chunks are inserted only into a process-private SQLite FTS5 database opened as `:memory:`. The connection is closed before results return. Runtime retrieval creates no filesystem artifact and opens no network connection.

Hidden/configured paths, false or malformed retrieval controls, private keys, known token formats, and high-confidence credential assignments are suppressed. This is defense in depth, not a replacement for vault curation. Unknown/transient read failures and file/chunk/byte overflow abort the complete operation; no partial result is returned.

Hermes errors disclose exception class rather than private paths or content. Status is path-free. Search passages are untrusted quoted source data, and human rendering escapes Unicode display controls and separators.

## Reporting

Do not open a public issue containing private note text, revealing paths, credentials, or private configuration values. Report privately with a minimal synthetic reproducer.

## Operator response

If a secret-bearing note may have been retrieved:

1. Disable the plugin or remove its config environment variable.
2. Add a folder, glob, frontmatter, or secret-pattern exclusion.
3. Rotate any credential that may have been exposed.
4. Run the read-only audit and a representative search before re-enabling.

There is no retrieval artifact to locate or delete. The software never edits or deletes vault content and never performs automatic credential rotation.

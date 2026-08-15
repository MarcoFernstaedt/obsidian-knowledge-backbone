# Security Policy

## Supported version

Security fixes target the current `main` branch and latest tagged release.

## Data boundary

The vault is read-only. State ancestors are opened from the filesystem root with descriptor-relative `O_NOFOLLOW`; symlink/non-directory ancestors are rejected. SQLite, WAL, SHM, and lock operations remain bound to one trusted state-directory descriptor outside the live vault, with device/inode and containment checks repeated at operation boundaries. Linux `/proc/self/fd` support is required and other hosts fail closed. Keep the private config and derived database readable only by the operator account.

The index suppresses hidden paths, configured exclusions, false frontmatter controls, private keys, known token formats, and high-confidence credential assignments. This is defense in depth, not a replacement for vault curation. Keep credentials in a secret store, not Markdown.

Excluded-note metadata is limited to relative path and reason. SQLite is untrusted derived state: every result is re-read and re-derived from the exact source before citation. Hermes errors disclose exception class rather than private paths or content. Search snippets are untrusted quoted source data, and human rendering escapes Unicode display controls and separators. The runtime performs no network communication.

## Reporting

Do not open a public issue containing private note text, index databases, revealing paths, credentials, or private configuration values. Report privately with a minimal synthetic reproducer.

## Operator response

If an indexed secret is suspected:

1. Disable the plugin or remove its config environment variable.
2. Add a folder, glob, frontmatter, or secret-pattern exclusion.
3. Refresh into a new outside-vault state path and run `audit`.
4. Replace the active config only after verification.
5. Delete the old SQLite state as a separate explicit action.
6. Rotate any credential that may have been exposed.

The software never edits or deletes vault content and never performs automatic credential rotation.

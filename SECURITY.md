# Security Policy

## Supported version

Security fixes target the current `main` branch and latest tagged release.

## Data boundary

The vault is read-only. Configure SQLite state outside the vault and restrict its filesystem permissions to the operator account. Qdrant is a derived projection and must not be exposed publicly. Bind Ollama and Qdrant to trusted interfaces and use network controls appropriate to the host.

The index suppresses hidden paths, configured exclusions, frontmatter false notes, private keys, known token formats, and high-confidence credential assignments. This is defense in depth, not a replacement for vault curation. Keep credentials in a secret store, not Markdown.

Excluded-note metadata is intentionally limited to relative path and reason. Qdrant payloads contain only corpus/schema/model and deterministic chunk/digest metadata—never note text, titles, paths, headings, snippets, or frontmatter. Errors returned by the Hermes boundary disclose exception type, not private paths or content.

## Reporting

Do not open a public issue containing private note text, index databases, paths that reveal private subjects, credentials, or service URLs. Report a vulnerability privately to the repository owner with a minimal synthetic reproducer.

## Operator response

If an indexed secret is suspected:

1. Disable the plugin or remove its config environment variable.
2. Add a folder, glob, frontmatter, or secret-pattern exclusion.
3. Reindex and run `audit` against a new state path.
4. Replace the active state only after verification.
5. Delete the old SQLite state and Qdrant collection as explicit operator actions.
6. Rotate any credential that may have been exposed.

The software never edits or deletes vault content and never performs automatic credential rotation.

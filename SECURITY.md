# SECURITY.md

## Data and Privacy

This repository and all produced artifacts are designed with fail-closed privacy for 
Marco's Obsidian vault knowledge base. The following guarantees are enforced:

- No raw vault content is copied into this repository, test data, or logs.
- All secret, credential, or excluded files/patterns are omitted from indexing.
- Source chunks in indices only ever contain allowed, curated content; their lineage is recorded by SHA-256 digest and line-range within the vault.
- Plugin and CLI never leak uncurated, private, or secret information—citing only deterministic, current, explicitly approved note fragments.
- All operator and test logs exclude indexed content or secret values, and all audit mechanisms are local-only.

Review `README.md` and `docs/architecture.md` for a complete privacy and exclusion policy. Never expose or interpret non-indexed parts of vault data.

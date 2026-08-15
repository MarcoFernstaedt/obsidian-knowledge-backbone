# Contributing

Thank you for helping improve Obsidian Knowledge Backbone. Contributions should preserve its local, read-only, fail-closed privacy model.

## Before opening a change

- Use synthetic Markdown fixtures only. Never commit a real vault, private path, config, database, token, or copied note content.
- Keep retrieval deterministic and dependency-free unless a dependency proposal is discussed first.
- Preserve the public compatibility surfaces: the documented console entry points and exactly two Hermes tools plus one command.
- Keep changes focused. Behavior changes should include regression coverage; documentation should match the executable interfaces.

## Development workflow

Obsidian Knowledge Backbone requires Python 3.11 or newer.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
PYTHONPYCACHEPREFIX=/tmp/obsidian-kb-pycache .venv/bin/python -m compileall -q obsidian_kb hermes_plugin tests
git diff --check
```

To verify release packaging when the `build` package is available:

```bash
.venv/bin/python -m build
```

Before submitting, review all tracked and untracked files, confirm the test run is warning-free, and make sure examples contain no personal or operational data.

## Security reports

Do not put private note text, revealing paths, credentials, or private configuration values in a public issue. Follow `SECURITY.md` and use a minimal synthetic reproducer.

## License

By contributing, you agree that your contribution is licensed under the repository's MIT License.

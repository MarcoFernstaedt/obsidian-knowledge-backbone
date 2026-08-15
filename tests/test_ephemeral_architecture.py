import contextlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.request

import hermes_plugin
from obsidian_kb.cli import main
from obsidian_kb.config import ConfigError, Settings, load_settings
from obsidian_kb.corpus import CorpusLimitError, CorpusScanError, audit, live_status
from obsidian_kb.search import search
from obsidian_kb.vault_io import TrustedVault


def snapshot(root: Path) -> dict[str, tuple[str, int, bytes]]:
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = ("symlink", path.lstat().st_mode, os.readlink(path).encode())
        elif path.is_dir():
            result[relative] = ("dir", path.stat().st_mode, b"")
        elif path.is_file():
            result[relative] = ("file", path.stat().st_mode, path.read_bytes())
    return result


class EphemeralArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.settings = Settings(self.vault)

    def tearDown(self):
        self.tmp.cleanup()

    def config(self, extra: str = "") -> Path:
        path = self.root / "config.toml"
        path.write_text(f'schema_version = 1\n[vault]\npath = "{self.vault}"\n{extra}')
        path.chmod(0o600)
        return path

    def test_search_builds_only_memory_database_and_writes_nothing(self):
        (self.vault / "note.md").write_text("# Deployment rollback\nrollback the release safely\n")
        config = self.config()
        before = snapshot(self.root)
        real_connect = sqlite3.connect
        opened = []

        def connect(database, *args, **kwargs):
            self.assertEqual(database, ":memory:")
            connection = real_connect(database, *args, **kwargs)
            opened.append(connection)
            return connection

        with patch("obsidian_kb.corpus.sqlite3.connect", connect), \
                patch.object(socket, "socket", side_effect=AssertionError("network forbidden")), \
                patch.object(urllib.request, "urlopen", side_effect=AssertionError("network forbidden")), \
                patch.dict(os.environ, {"OBSIDIAN_KB_CONFIG": str(config)}, clear=True):
            result = search(self.settings, "deployment rollback")
            status = live_status(self.settings)
            report = audit(self.settings)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["index", "--json"]), 0)
        self.assertEqual(snapshot(self.root), before)
        self.assertTrue(opened)
        for connection in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
        self.assertEqual(result["results"][0]["citation"], "note.md:L1-L2")
        self.assertEqual(status["compatibility"], "ephemeral-live")
        self.assertTrue(status["source_inventory_complete"])
        self.assertTrue(report["ephemeral"])
        self.assertFalse(report["persistence"])
        self.assertIn("ephemeral-live", output.getvalue())

    def test_config_rejects_state_remote_and_unknown_sections(self):
        for extra in (
            '[state]\nsqlite_path = "/tmp/state.sqlite3"\n',
            '[remote]\nendpoint = "forbidden"\n',
            '[mystery]\nvalue = 1\n',
        ):
            with self.subTest(extra=extra):
                with self.assertRaises(ConfigError):
                    load_settings(self.config(extra), require_private=True)

    def test_status_is_live_complete_and_path_free(self):
        (self.vault / "active.md").write_text("# Active\nneedle\n")
        (self.vault / "off.md").write_text("\ufeff---\n\"index\": false\n---\n# Private\nprivacy sentinel\n")
        status = live_status(self.settings)
        self.assertEqual(status["eligible_notes"], 1)
        self.assertEqual(status["excluded_notes"], 1)
        self.assertEqual(status["chunks"], 1)
        self.assertTrue(status["current"])
        self.assertTrue(status["source_inventory_complete"])
        self.assertGreaterEqual(status["scan_duration_ms"], 0)
        rendered = json.dumps(status)
        self.assertNotIn("active.md", rendered)
        self.assertNotIn(str(self.vault), rendered)

    def test_unknown_read_failure_and_resource_overflow_fail_closed(self):
        (self.vault / "a.md").write_text("# A\nneedle\n")
        with patch.object(TrustedVault, "read", side_effect=OSError("transient")):
            with self.assertRaises(CorpusScanError):
                search(self.settings, "needle")
        bounded = Settings(self.vault, maximum_files=1)
        (self.vault / "b.md").write_text("# B\nneedle\n")
        with self.assertRaises(CorpusLimitError):
            search(bounded, "needle")
        failed = audit(bounded)
        self.assertFalse(failed["ok"])
        self.assertFalse(failed["source_inventory_complete"])
        self.assertFalse(failed["current"])

    def test_privacy_controls_credentials_prefix_and_bounds(self):
        (self.vault / "Allowed").mkdir()
        (self.vault / "Other").mkdir()
        (self.vault / "Allowed/good.md").write_text("# Good\nneedle\n")
        (self.vault / "Other/good.md").write_text("# Other\nneedle\n")
        (self.vault / "bom.md").write_text("\ufeff---\nindex: false\n---\n# Private\nprivacy needle\n")
        (self.vault / "malformed.md").write_text("---\nindex: [false]\n---\n# Private\nprivacy needle\n")
        (self.vault / "secret.md").write_text("# Secret\napi_key='s3cr3t-value-987'\nneedle\n")
        result = search(self.settings, "needle", path_prefix="Allowed")
        self.assertEqual([item["path"] for item in result["results"]], ["Allowed/good.md"])
        self.assertNotIn("privacy", json.dumps(search(self.settings, "needle")))
        for query, limit, prefix in (("", 5, None), ("x" * 513, 5, None), ("x", 21, None),
                                     ("x", 5, "../x"), ("x", 5, "/abs"), ("x", 5, "A\\B")):
            with self.assertRaises(ValueError):
                search(self.settings, query, limit=limit, path_prefix=prefix)

    def test_unicode_stopwords_weighting_and_ties_are_deterministic(self):
        (self.vault / "content.md").write_text("# Generic\ncafe resume rollback rollback rollback\n")
        (self.vault / "title.md").write_text("# Café résumé rollback\nbrief mention\n")
        (self.vault / "a.md").write_text("# Tie\nunique token\n")
        (self.vault / "b.md").write_text("# Tie\nunique token\n")
        (self.vault / "unicode.md").write_text("# Straße İstanbul\nnaïve coöperate\n")
        ranked = search(self.settings, "the café résumé rollback")
        self.assertEqual(ranked["results"][0]["path"], "title.md")
        first = search(self.settings, "unique token")
        second = search(self.settings, "unique token")
        self.assertEqual(first["results"], second["results"])
        self.assertEqual([item["path"] for item in first["results"]], ["a.md", "b.md"])
        self.assertEqual(search(self.settings, "the and of")["results"], [])
        self.assertEqual(search(self.settings, "running")["results"], [])
        self.assertEqual(search(self.settings, "STRASSE")["results"][0]["path"], "unicode.md")
        self.assertEqual(search(self.settings, "istanbul naive cooperate")["results"][0]["path"], "unicode.md")

    def test_malformed_controls_symlinks_and_all_resource_caps_fail_closed(self):
        malformed = (
            "---\nindex: [false]\n---\n# Private\nneedle\n",
            "---\nindex:\n  nested: false\n---\n# Private\nneedle\n",
            "---\nindex: true\nindex: false\n---\n# Private\nneedle\n",
            "---\n'index: false\n---\n# Private\nneedle\n",
            "---\nindex: false\n# unterminated\nneedle\n",
        )
        for number, text in enumerate(malformed):
            (self.vault / f"malformed-{number}.md").write_text(text)
        outside = self.root / "outside.md"
        outside.write_text("# Outside\nneedle\n")
        (self.vault / "linked.md").symlink_to(outside)
        self.assertEqual(search(self.settings, "needle")["results"], [])

        (self.vault / "large.md").write_text("# Large\n" + "x" * 100)
        with self.assertRaises(CorpusLimitError):
            search(Settings(self.vault, maximum_total_bytes=10), "large")
        with self.assertRaises(CorpusLimitError):
            search(Settings(self.vault, max_lines=1, max_chars=64, maximum_chunks=1), "large")

    def test_shipped_surfaces_retire_mutable_and_remote_architecture(self):
        root = Path(__file__).parents[1]
        shipped = [root / "obsidian_kb", root / "hermes_plugin", root / "docs",
                   root / "README.md", root / "SECURITY.md", root / "config.example.toml",
                   root / "pyproject.toml", root / "MANIFEST.in", root / ".github"]
        forbidden = ("state_io", "trustedstatedirectory", "journal_mode", "-wal", "-shm",
                     "ollama", "qdrant", "semantic", "vector", "http://", "https://",
                     "urllib", "imperator_obsidian_retrieval_refresh")
        hits = []
        for item in shipped:
            paths = [item] if item.is_file() else list(item.rglob("*"))
            for path in paths:
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                text = path.read_text(errors="ignore").casefold()
                for token in forbidden:
                    if token in text:
                        hits.append(f"{path.relative_to(root)}:{token}")
        self.assertEqual(hits, [])

    def test_exact_two_plugin_tools_one_command_and_fixed_config(self):
        class Context:
            def __init__(self):
                self.tools, self.commands, self.skills = [], [], []
            def register_tool(self, **kwargs): self.tools.append(kwargs)
            def register_command(self, *args, **kwargs): self.commands.append((args, kwargs))
            def register_skill(self, *args): self.skills.append(args)

        context = Context()
        hermes_plugin.register(context)
        self.assertEqual([tool["name"] for tool in context.tools],
                         ["obsidian_knowledge_search", "obsidian_knowledge_status"])
        self.assertEqual([command[0][0] for command in context.commands], ["notesearch"])
        self.assertFalse(json.loads(hermes_plugin.obsidian_knowledge_search({"query": "x", "config_path": "x"}))["ok"])

    def test_repository_quality_fixture_reaches_acceptance_threshold(self):
        fixture_path = Path(__file__).parent / "fixtures" / "retrieval_quality.json"
        fixture = json.loads(fixture_path.read_text())
        for relative, content in fixture["notes"].items():
            path = self.vault / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        hits = 0
        for query, expected in fixture["queries"]:
            paths = [item["path"] for item in search(self.settings, query, limit=5)["results"]]
            hits += expected in paths
        self.assertGreaterEqual(hits, 17, f"quality acceptance was {hits}/20 expected top-5")

    def test_acceptance_quality_and_local_performance_fixture(self):
        for note in range(100):
            lines = [f"# Note {note}"]
            for chunk in range(15):
                lines.extend((f"## Topic {chunk}", f"ordinary material {note} {chunk}"))
            (self.vault / f"note-{note:03}.md").write_text("\n".join(lines))
        (self.vault / "note-042.md").write_text("# Incident rollback guide\n## Database recovery\nrestore database after failed deployment rollback\n")
        queries = {
            "failed deployment database recovery": "note-042.md",
            "incident rollback": "note-042.md",
            "restore database": "note-042.md",
        }
        durations = []
        for query, expected in queries.items():
            started = time.perf_counter()
            result = search(self.settings, query, limit=5)
            durations.append(time.perf_counter() - started)
            self.assertIn(expected, [item["path"] for item in result["results"]])
        self.assertLess(sorted(durations)[len(durations) // 2], 1.0)


if __name__ == "__main__":
    unittest.main()

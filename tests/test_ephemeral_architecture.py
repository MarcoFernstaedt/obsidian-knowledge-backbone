import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import sys
import tempfile
import time
import types
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


def simple_manifest(path: Path) -> dict[str, object]:
    """Parse the scalar/list subset used by the dependency-free test manifest."""
    manifest: dict[str, object] = {}
    current_list: list[str] | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if raw_line.startswith("  - ") and current_list is not None:
            current_list.append(raw_line[4:])
            continue
        key, separator, value = raw_line.partition(":")
        if not separator:
            raise AssertionError(f"invalid manifest line: {raw_line!r}")
        if value.strip():
            manifest[key] = value.strip()
            current_list = None
        else:
            current_list = []
            manifest[key] = current_list
    return manifest


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
            plugin_status = json.loads(hermes_plugin.obsidian_knowledge_status({}))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["index", "--json"]), 0)
        self.assertEqual(snapshot(self.root), before)
        self.assertTrue(opened)
        for connection in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
        self.assertEqual(result["results"][0]["citation"], "note.md:L1-L2")
        for point_in_time in (result["index"], status, report, plugin_status["index"]):
            self.assertFalse(point_in_time["current"])
            self.assertTrue(point_in_time["snapshot_consistent"])
            self.assertEqual(point_in_time["freshness"], "point-in-time")
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
        self.assertFalse(status["current"])
        self.assertTrue(status["snapshot_consistent"])
        self.assertEqual(status["freshness"], "point-in-time")
        self.assertTrue(status["source_inventory_complete"])
        self.assertGreaterEqual(status["scan_duration_ms"], 0)
        rendered = json.dumps(status)
        self.assertNotIn("active.md", rendered)
        self.assertNotIn(str(self.vault), rendered)

    def test_search_never_claims_perpetual_currency_after_final_inventory(self):
        note = self.vault / "race.md"
        note.write_text("# Race\noldcanary\n")
        original = TrustedVault.inventory
        calls = 0

        def inventory(bound, maximum_entries):
            nonlocal calls
            result = original(bound, maximum_entries)
            calls += 1
            if calls == 3:
                note.unlink()
            return result

        with patch.object(TrustedVault, "inventory", inventory):
            result = search(self.settings, "oldcanary")
        self.assertEqual(result["results"][0]["path"], "race.md")
        self.assertFalse(result["index"]["current"])
        self.assertTrue(result["index"]["snapshot_consistent"])
        self.assertEqual(result["index"]["freshness"], "point-in-time")

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

    def test_oversized_regular_note_is_fatal_not_excluded_or_current(self):
        (self.vault / "oversized.md").write_text("# Oversized\n" + "x" * 128)
        bounded = Settings(self.vault, maximum_note_bytes=32)
        with self.assertRaises(CorpusLimitError):
            search(bounded, "oversized")
        failed = audit(bounded)
        self.assertEqual(failed["error_class"], "CorpusLimitError")
        self.assertFalse(failed["source_inventory_complete"])
        self.assertFalse(failed["current"])
        self.assertEqual(failed["excluded_notes"], 0)
        rendered = json.dumps(failed)
        self.assertNotIn("oversized.md", rendered)
        self.assertNotIn(str(self.vault), rendered)
        self.assertNotIn("x" * 16, rendered)

    def test_control_key_projection_nested_controls_and_yaml_secret_lists_fail_closed(self):
        settings = Settings(self.vault, frontmatter_false_keys=("KNOWLEDGE_SS", "index"))
        notes = {
            "unicode.md": "---\nknowledge_ss: false\n---\n# Private\nunicodecanary\n",
            "nested.md": "---\nmeta:\n  index: false\n---\n# Private\nnestedcanary\n",
            "list-secret.md": "# Private\n- api_key: hunter2-secret\nlistcanary\n",
            "indented-secret.md": "# Private\n  - token: hunter2-secret\nindentcanary\n",
            "quoted-secret.md": "# Private\n- 'API_KEY': 'abc''synthetic-secret-canary'\nquotedcanary\n",
            "block-secret.md": "# Private\n- TOKEN: |\n    synthetic-secret-canary\nblockcanary\n",
        }
        for name, text in notes.items():
            (self.vault / name).write_text(text)
        for canary in ("unicodecanary", "nestedcanary", "listcanary", "indentcanary",
                       "quotedcanary", "blockcanary"):
            self.assertEqual(search(settings, canary)["results"], [])

        (self.vault / "ordinary-metadata.md").write_text(
            "---\nmeta:\n  owner: docs\ntags:\n  - safe\n---\n# Public\nordinarycanary\n"
        )
        self.assertEqual(search(settings, "ordinarycanary")["results"][0]["path"],
                         "ordinary-metadata.md")

    def test_control_key_configuration_rejects_ambiguous_or_dangerous_names(self):
        for keys in (("index", "INDEX"), ("imperator_retrieval",), ("bad:key",), ("bad\nkey",),
                     ("index", "іndex"), ("éxclude",), ("_index",), ("x" * 65,)):
            with self.subTest(keys=keys), self.assertRaises(ConfigError):
                Settings(self.vault, frontmatter_false_keys=keys)

    def test_private_config_rejects_same_inode_parse_aba_and_permission_drift(self):
        config = self.config()
        foreign = self.root / "foreign"
        foreign.mkdir()
        import obsidian_kb.config as config_module
        real_load = config_module.tomllib.load

        def mutate_after_parse(handle):
            loaded = real_load(handle)
            inode = config.stat().st_ino
            config.write_text(f'schema_version = 1\n[vault]\npath = "{foreign}"\n')
            config.chmod(0o644)
            self.assertEqual(config.stat().st_ino, inode)
            return loaded

        with patch("obsidian_kb.config.tomllib.load", mutate_after_parse):
            with self.assertRaises(ConfigError):
                load_settings(config, require_private=True)
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o644)

    def test_bound_vault_root_rejects_replaced_symlink_ancestor(self):
        approved = self.root / "approved"
        approved.mkdir()
        bound_vault = approved / "vault"
        bound_vault.mkdir()
        (bound_vault / "safe.md").write_text("# Safe\nsafecanary\n")
        settings = Settings(bound_vault)
        displaced = self.root / "displaced"
        approved.rename(displaced)
        foreign = self.root / "foreign"
        (foreign / "vault").mkdir(parents=True)
        (foreign / "vault/foreign.md").write_text("# Foreign\nforeigncanary\n")
        approved.symlink_to(foreign, target_is_directory=True)
        with self.assertRaises(CorpusScanError):
            search(settings, "foreigncanary")

    def test_scan_aborts_on_exact_post_inventory_insertion(self):
        (self.vault / "first.md").write_text("# First\nfirstcanary\n")
        original = TrustedVault.inventory
        calls = 0

        def inventory(bound, maximum_entries):
            nonlocal calls
            result = original(bound, maximum_entries)
            calls += 1
            if calls == 1:
                (self.vault / "late.md").write_text("# Late\nlatecanary\n")
            return result

        with patch.object(TrustedVault, "inventory", inventory):
            with self.assertRaises(CorpusScanError):
                search(self.settings, "firstcanary")

    def test_scan_aborts_on_exact_post_read_mutation(self):
        note = self.vault / "race.md"
        note.write_text("# Race\noldcanary\n")
        original = TrustedVault.read
        reads = 0

        def read(bound, relative_path, maximum_bytes):
            nonlocal reads
            result = original(bound, relative_path, maximum_bytes)
            reads += 1
            if reads == 1:
                note.write_text("# Race\nnewcanary\n")
            return result

        with patch.object(TrustedVault, "read", read):
            with self.assertRaises(CorpusScanError):
                search(self.settings, "oldcanary")

    def test_scan_sha_revalidates_content_excluded_and_invalid_utf8_sources(self):
        excluded = self.vault / "excluded.md"
        excluded.write_text("---\nindex: false\n---\n# Private\noldcanary\n")
        original_inventory = TrustedVault.inventory
        frozen = None
        import obsidian_kb.corpus as corpus_module
        original_exclusion = corpus_module.content_exclusion_reason

        def inventory(bound, maximum_entries):
            nonlocal frozen
            current = original_inventory(bound, maximum_entries)
            if frozen is None:
                frozen = current
            return frozen

        def exclusion(text, settings):
            reason = original_exclusion(text, settings)
            excluded.write_text("---\nindex: true \n---\n# Private\nnewcanary\n")
            return reason

        with patch.object(TrustedVault, "inventory", inventory), \
                patch("obsidian_kb.corpus.content_exclusion_reason", exclusion):
            with self.assertRaises(CorpusScanError):
                search(self.settings, "oldcanary")

        invalid = self.vault / "invalid.md"
        invalid.write_bytes(b"# Invalid\n\xffoldcanary\n")
        original_read = TrustedVault.read
        frozen = None

        def read(bound, relative_path, maximum_bytes):
            result = original_read(bound, relative_path, maximum_bytes)
            if relative_path == "invalid.md":
                invalid.write_bytes(b"# Invalid\n\xffnewcanary\n")
            return result

        with patch.object(TrustedVault, "inventory", inventory), \
                patch.object(TrustedVault, "read", read):
            with self.assertRaises(CorpusScanError):
                search(self.settings, "oldcanary")

    def test_inventory_limit_stops_scandir_before_unbounded_materialization(self):
        for number in range(100):
            (self.vault / f"entry-{number:03}.txt").write_text("x")
        import obsidian_kb.vault_io as vault_io
        original_scandir = vault_io.os.scandir
        consumed = 0

        class CountingScandir:
            def __init__(self, iterator): self.iterator = iterator
            def __iter__(self): return self
            def __next__(self):
                nonlocal consumed
                item = next(self.iterator)
                consumed += 1
                return item
            def close(self): self.iterator.close()
            def __enter__(self): return self
            def __exit__(self, *_args): self.close()

        def scandir(path):
            return CountingScandir(original_scandir(path))

        with patch("obsidian_kb.vault_io.os.scandir", scandir):
            with TrustedVault(self.vault) as vault:
                with self.assertRaises(Exception) as caught:
                    vault.inventory(1)
        self.assertEqual(type(caught.exception).__name__, "VaultInventoryOverflow")
        self.assertEqual(consumed, 2)

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

    def test_root_plugin_package_loads_with_matching_manifest(self):
        root = Path(__file__).parents[1]
        root_manifest = root / "plugin.yaml"
        nested_manifest = root / "hermes_plugin" / "plugin.yaml"
        self.assertTrue(root_manifest.is_file())
        self.assertEqual(root_manifest.read_bytes(), nested_manifest.read_bytes())
        self.assertEqual(simple_manifest(root_manifest), {
            "name": "obsidian-knowledge-backbone",
            "version": "3.0.0",
            "description": "Read-only cited retrieval over an approved Obsidian corpus.",
            "author": "Marco Fernstaedt",
            "provides_tools": ["obsidian_knowledge_search", "obsidian_knowledge_status"],
            "provides_commands": ["notesearch"],
        })

        parent_name = "hermes_plugins_test"
        module_name = f"{parent_name}.obsidian_knowledge_backbone"
        parent = types.ModuleType(parent_name)
        parent.__path__ = []
        parent.__package__ = parent_name
        sys.modules[parent_name] = parent
        original_path = list(sys.path)
        hidden_modules = {
            name: module for name, module in tuple(sys.modules.items())
            if name == "obsidian_kb" or name.startswith("obsidian_kb.")
        }
        for hidden_name in hidden_modules:
            sys.modules.pop(hidden_name)
        sys.path[:] = [
            entry for entry in sys.path
            if Path(entry or os.getcwd()).resolve() != root.resolve()
        ]
        try:
            spec = importlib.util.spec_from_file_location(
                module_name,
                root / "__init__.py",
                submodule_search_locations=[str(root)],
            )
            if spec is None or spec.loader is None:
                self.fail("Hermes-style module specification could not be created")
            module = importlib.util.module_from_spec(spec)
            module.__package__ = module_name
            module.__path__ = [str(root)]
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            class Context:
                def __init__(self):
                    self.tools, self.commands, self.skills = [], [], []
                def register_tool(self, **kwargs): self.tools.append(kwargs)
                def register_command(self, *args, **kwargs): self.commands.append((args, kwargs))
                def register_skill(self, *args): self.skills.append(args)

            context = Context()
            module.register(context)
            self.assertEqual([tool["name"] for tool in context.tools],
                             ["obsidian_knowledge_search", "obsidian_knowledge_status"])
            self.assertEqual([command[0][0] for command in context.commands], ["notesearch"])
        finally:
            sys.path[:] = original_path
            for loaded_name in tuple(sys.modules):
                if loaded_name == parent_name or loaded_name.startswith(f"{parent_name}."):
                    sys.modules.pop(loaded_name, None)
                elif loaded_name == "obsidian_kb" or loaded_name.startswith("obsidian_kb."):
                    sys.modules.pop(loaded_name, None)
            sys.modules.update(hidden_modules)

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

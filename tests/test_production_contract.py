import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import hermes_plugin
from obsidian_kb.cli import imperator_search_main, imperator_vault_index_main, main
from obsidian_kb.config import ConfigError, Settings, load_settings
from obsidian_kb.indexer import IndexLockError, Indexer
from obsidian_kb.search import search, status_with_freshness
from obsidian_kb.store import CompatibilityError, Store


class LocalProductionContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"; self.vault.mkdir()
        self.state = self.root / "state" / "index.sqlite3"
        self.settings = Settings(self.vault, self.state)

    def tearDown(self): self.tmp.cleanup()

    def run_index(self, **kwargs):
        engine = Indexer(self.settings)
        try: return engine.run(**kwargs)
        finally: engine.close()

    def test_exclusions_atomic_delete_move_and_stale_suppression(self):
        (self.vault / "safe.md").write_text("# Alpha\nold needle")
        (self.vault / "off.md").write_text("---\nindex: false\n---\n# Off\nneedle")
        first = self.run_index()
        self.assertEqual((first["active_notes"], first["excluded_notes"]), (1, 1))
        note = self.vault / "safe.md"; note.write_text("# Alpha\nchanged")
        self.assertEqual(search(self.settings, "needle")["results"], [])
        note.rename(self.vault / "moved.md"); second = self.run_index()
        self.assertEqual((second["changed"], second["removed"]), (1, 1))

    def test_complete_scan_rolls_back_on_failure(self):
        (self.vault / "a.md").write_text("# A\nold")
        self.run_index(); (self.vault / "a.md").write_text("# A\nnew"); (self.vault / "b.md").write_text("# B\nsecond")
        original = Store.replace_note; calls = 0
        def crash(store, *args, **kwargs):
            nonlocal calls; calls += 1
            if calls == 2: raise RuntimeError("synthetic")
            return original(store, *args, **kwargs)
        with patch.object(Store, "replace_note", crash):
            with self.assertRaises(RuntimeError): self.run_index()
        store = Store(self.state, settings=self.settings, read_only=True)
        try:
            self.assertTrue(store.lexical("old", 5)); self.assertFalse(store.lexical("new", 5)); self.assertIsNone(store.note("b.md"))
        finally: store.close()

    def test_missing_corrupt_and_incompatible_state_use_pure_fallback(self):
        (self.vault / "safe.md").write_text("# Safe\nfallback needle")
        before = set(self.root.rglob("*")); result = search(self.settings, "fallback")
        self.assertTrue(result["fallback"]); self.assertEqual(before, set(self.root.rglob("*")))
        self.state.parent.mkdir(); self.state.write_bytes(b"broken")
        self.assertTrue(search(self.settings, "fallback")["fallback"])
        self.state.unlink(); self.run_index()
        changed = Settings(self.vault, self.state, max_lines=61)
        self.assertTrue(search(changed, "fallback")["fallback"])

    def test_dry_run_purity_lock_and_status_contract(self):
        (self.vault / "a.md").write_text("# A\nneedle")
        result = self.run_index(dry_run=True)
        self.assertTrue(result["dry_run"]); self.assertFalse(self.state.parent.exists())
        first = Indexer(self.settings)
        try:
            first.acquire_lock()
            second = Indexer(self.settings)
            try:
                with self.assertRaises(IndexLockError): second.run()
            finally: second.close()
        finally: first.close()
        self.run_index(); status = status_with_freshness(self.settings)
        self.assertEqual(set(("active_notes", "excluded_notes", "chunks", "age_seconds", "source_drift_count", "stale", "current", "compatibility")) - set(status), set())
        self.assertNotIn("a.md", json.dumps(status))

    def test_config_ownership_state_boundary_and_fixed_cli(self):
        with self.assertRaises(ConfigError): load_settings(vault=self.vault, state=self.vault / "index.sqlite3")
        cfg = self.root / "config.toml"; cfg.write_text(f'[vault]\npath="{self.vault}"\n[state]\nsqlite_path="{self.state}"\n'); cfg.chmod(0o644)
        with self.assertRaises(ConfigError): load_settings(cfg, require_private=True)
        cfg.chmod(0o600)
        with patch.dict(os.environ, {"OBSIDIAN_KB_CONFIG": str(cfg)}, clear=True), patch("sys.stdout", io.StringIO()):
            self.assertEqual(main(["index", "--json"]), 0)
            self.assertEqual(main(["search", "needle", "--json"]), 0)
            self.assertEqual(main(["status", "--json"]), 0)
        with patch.dict(os.environ, {}, clear=True), patch("sys.stderr", io.StringIO()): self.assertEqual(main(["status"]), 2)
        with patch("obsidian_kb.cli.main", return_value=0) as dispatch:
            imperator_search_main(["x"]); dispatch.assert_called_once_with(["search", "x"])
        with patch("obsidian_kb.cli.main", return_value=0) as dispatch:
            imperator_vault_index_main(["--dry-run"]); dispatch.assert_called_once_with(["index", "--dry-run"])

    def test_plugin_exact_contract_validation_and_untrusted_rendering(self):
        class Context:
            def __init__(self): self.tools=[]; self.commands=[]; self.skills=[]
            def register_tool(self, **kw): self.tools.append(kw)
            def register_command(self, *args, **kw): self.commands.append((args, kw))
            def register_skill(self, *args): self.skills.append(args)
        context = Context(); hermes_plugin.register(context)
        self.assertEqual([item["name"] for item in context.tools], ["obsidian_knowledge_search", "obsidian_knowledge_status"])
        self.assertEqual([item[0][0] for item in context.commands], ["notesearch"])
        for bad in ({"query": "x", "config_path": "x"}, {"query": "x", "offline": True}, {"query": "x", "path_prefix": "../x"}):
            self.assertFalse(json.loads(hermes_plugin.obsidian_knowledge_search(bad))["ok"])
        self.assertIn("UNTRUSTED", hermes_plugin._notesearch_command("x") if os.environ.get("OBSIDIAN_KB_CONFIG") else "UNTRUSTED")


if __name__ == "__main__": unittest.main()

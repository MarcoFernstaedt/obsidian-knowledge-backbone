import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import hermes_plugin
from obsidian_kb import remote
from obsidian_kb.chunker import chunk_markdown, frontmatter, is_frontmatter_excluded
from obsidian_kb.cli import _emit
from obsidian_kb.config import ConfigError, Settings, load_settings
from obsidian_kb.indexer import Indexer
from obsidian_kb.privacy import contains_secret
from obsidian_kb.remote import QdrantClient, RemoteError
from obsidian_kb.search import search, status_with_freshness
from obsidian_kb.store import CompatibilityError, Store
from obsidian_kb.vault_io import TrustedVault


class Response:
    def __init__(self, value):
        self.raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def read(self, amount=-1): return self.raw if amount < 0 else self.raw[:amount]


class FinalCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.state = self.root / "state" / "index.sqlite3"
        self.settings = Settings(self.vault, self.state, vector_size=2)

    def tearDown(self): self.tmp.cleanup()

    def test_state_and_lock_realpaths_must_be_outside_vault(self):
        for state in (self.vault, self.vault / "derived" / "index.sqlite3"):
            with self.subTest(kind="direct"):
                with self.assertRaises(ConfigError): load_settings(vault=self.vault, state=state)
        outside = self.root / "outside"
        outside.mkdir()
        redirected = outside / "redirected"
        redirected.symlink_to(self.vault, target_is_directory=True)
        with self.assertRaises(ConfigError):
            load_settings(vault=self.vault, state=redirected / "new" / "index.sqlite3")
        self.assertFalse((self.vault / "new").exists())

    def test_compatibility_binds_chunking_and_content_policy(self):
        base = Settings(self.vault, self.state)
        variants = [
            Settings(self.vault, self.state, max_lines=base.max_lines + 1),
            Settings(self.vault, self.state, max_chars=base.max_chars + 1),
            Settings(self.vault, self.state, overlap_lines=1),
            Settings(self.vault, self.state, maximum_note_bytes=base.maximum_note_bytes + 1),
            Settings(self.vault, self.state, include_globs=("Notes/**",)),
        ]
        self.assertTrue(all(item.compatibility_signature() != base.compatibility_signature() for item in variants))
        store = Store(self.state, settings=base)
        store.close()
        with self.assertRaises(CompatibilityError):
            Store(self.state, settings=variants[0], read_only=True)
        first = chunk_markdown("# H\nbody", "a" * 64, "n.md", compatibility_signature="a" * 64)
        second = chunk_markdown("# H\nbody", "a" * 64, "n.md", compatibility_signature="b" * 64)
        self.assertNotEqual(first[0]["point_id"], second[0]["point_id"])

    def test_valid_nested_and_sequence_frontmatter_is_not_excluded(self):
        text = "---\ntags:\n- alpha\n- beta\naliases: [one, two]\nmetadata:\n  owner: team\nindex: true # explicit opt-in\n---\n# Topic\nneedle"
        values, offset = frontmatter(text)
        self.assertGreater(offset, 0)
        self.assertNotIn("__malformed__", values)
        self.assertFalse(is_frontmatter_excluded(text, ("index",)))
        chunks = chunk_markdown(text, "a" * 64, "note.md")
        self.assertEqual(chunks[0]["start_line"], 10)

    def test_retrieval_control_fields_remain_fail_closed(self):
        for metadata in ("index: false", "imperator_retrieval: exclude", "index:\n  - false", "index: [false]", "index: maybe: false", "index: perhaps"):
            text = f"---\n{metadata}\n---\n# Topic\nneedle"
            self.assertTrue(is_frontmatter_excluded(text, ("index",)))

    def test_transient_read_failure_rolls_back_complete_generation(self):
        a = self.vault / "a.md"; b = self.vault / "b.md"
        a.write_text("# A\nold alpha"); b.write_text("# B\nold beta")
        engine = Indexer(self.settings)
        try: engine.run()
        finally: engine.close()
        a.write_text("# A\nnew alpha")
        original = TrustedVault.read
        def fail_second(vault, path, maximum):
            if path == "b.md": raise OSError("synthetic transient input/output failure")
            return original(vault, path, maximum)
        with patch.object(TrustedVault, "read", fail_second):
            engine = Indexer(self.settings)
            try:
                with self.assertRaises(OSError): engine.run()
            finally: engine.close()
        store = Store(self.state, settings=self.settings, read_only=True)
        try:
            self.assertTrue(store.lexical("old", 10))
            self.assertFalse(store.lexical("new", 10))
            self.assertEqual(store.note("b.md")["status"], "active")
        finally: store.close()

    def test_qdrant_missing_metadata_and_malformed_rows_fail_closed(self):
        q = QdrantClient("http://q", "logical", 2)
        with patch("obsidian_kb.remote.request.urlopen", return_value=Response({})):
            with self.assertRaises(RemoteError): q.ensure("corpus", "a" * 64, "digest")
        with patch("obsidian_kb.remote.request.urlopen", return_value=Response({"result": {"points": [7]}})):
            with self.assertRaises(RemoteError): q.query([1.0, 0.0], 2, "corpus", "a" * 64)

    def test_malformed_injected_qdrant_row_degrades_to_lexical(self):
        (self.vault / "a.md").write_text("# A\nneedle")
        engine = Indexer(self.settings)
        try: engine.run()
        finally: engine.close()
        configured = Settings(self.vault, self.state, vector_size=2, ollama_url="http://o", qdrant_url="http://q")
        class Embedder:
            def embed(self, _texts): return [[1.0, 0.0]]
        class Malformed:
            def query(self, *_args): return [7]
        result = search(configured, "needle", ollama=Embedder(), qdrant=Malformed())
        self.assertEqual(result["mode"], "lexical")
        self.assertTrue(result["degraded"])

    def test_qdrant_scroll_has_strict_total_page_bound(self):
        q = QdrantClient("http://q", "logical", 2)
        page = {"result": {"points": [{"id": "x"}], "next_page_offset": "again"}}
        with patch.object(remote, "QDRANT_MAX_SCROLL_PAGES", 2, create=True), \
             patch("obsidian_kb.remote.request.urlopen", return_value=Response(page)):
            with self.assertRaises(RemoteError): q.list_ids("corpus", "a" * 64)

    def test_physical_collections_are_isolated_by_signature(self):
        q = QdrantClient("http://q", "imperator_obsidian_chunks_v2", 2)
        one = q.collection_name("a" * 64)
        two = q.collection_name("b" * 64)
        self.assertNotEqual(one, two)
        self.assertTrue(one.startswith("imperator_obsidian_chunks_v2"))

    def test_query_ollama_uses_configured_response_limit(self):
        (self.vault / "a.md").write_text("# A\nneedle")
        engine = Indexer(self.settings)
        try: engine.run()
        finally: engine.close()
        configured = Settings(self.vault, self.state, vector_size=2, ollama_url="http://o", qdrant_url="http://q", response_max_bytes=64)
        with patch("obsidian_kb.search.OllamaClient") as client:
            client.return_value.embed.side_effect = RemoteError("down")
            search(configured, "needle")
        self.assertEqual(client.call_args.args[3], 64)

    def test_freshness_inventory_overflow_is_unknown_and_stale(self):
        (self.vault / "a.md").write_text("# A\nneedle")
        engine = Indexer(self.settings)
        try: engine.run()
        finally: engine.close()
        (self.vault / "b.md").write_text("# B\nnew")
        bounded = Settings(self.vault, self.state, vector_size=2, freshness_max_files=1)
        payload = status_with_freshness(bounded)
        self.assertTrue(payload["stale"])
        self.assertFalse(payload["source_inventory_complete"])
        self.assertIsNone(payload["source_drift_count"])

    def test_additional_high_confidence_credentials_and_redacted_placeholder(self):
        canaries = [
            "AIza" + "A" * 35,
            "npm_" + "B" * 36,
            "Bearer " + "eyJ" + "A" * 20 + "." + "eyJ" + "B" * 20 + "." + "C" * 24,
            "postgresql://service:" + "p" * 16 + "@db.invalid/app",
        ]
        self.assertTrue(all(contains_secret(value) for value in canaries))
        self.assertFalse(contains_secret("OPENAI_API_KEY=[REDACTED]"))
        for index, value in enumerate(canaries):
            (self.vault / f"secret-{index}.md").write_text("# Synthetic\nneedle " + value)
        class NoSecretOllama:
            def __init__(self): self.called = False
            def embed(self, _texts): self.called = True; return []
        class NoSecretQdrant:
            def __init__(self): self.called = False
            def ensure(self, *_args): self.called = True
            def upsert(self, *_args): self.called = True
            def delete(self, *_args): self.called = True
        ollama, qdrant = NoSecretOllama(), NoSecretQdrant()
        engine = Indexer(self.settings, ollama=ollama, qdrant=qdrant)
        try: result = engine.run()
        finally: engine.close()
        self.assertEqual(result["excluded_notes"], len(canaries))
        self.assertEqual(result["chunks"], 0)
        self.assertFalse(ollama.called)
        self.assertFalse(qdrant.called)
        self.assertEqual(search(self.settings, "needle", offline=True)["results"], [])

    def test_ctime_detects_same_inode_same_size_mtime_restore(self):
        note = self.vault / "note.md"
        note.write_bytes(b"safe")
        original_read = os.read
        changed = False
        def mutate_then_read(fd, amount):
            nonlocal changed
            if not changed:
                changed = True
                before = note.stat()
                time.sleep(0.002)
                note.write_bytes(b"evil")
                os.utime(note, ns=(before.st_atime_ns, before.st_mtime_ns))
            return original_read(fd, amount)
        with TrustedVault(self.vault) as trusted, patch("obsidian_kb.vault_io.os.read", mutate_then_read):
            with self.assertRaises(OSError): trusted.read("note.md", 100)

    def test_human_rendering_sanitizes_controls_but_json_escapes_normally(self):
        item = {"citation": "bad\x1b[2J.md:L1-L1", "title": "title\nspoof", "heading_path": ["head\x85next"],
                "retrieval_type": "lexical", "score": 1.0, "snippet": "body\rspoof"}
        output = io.StringIO()
        with patch("sys.stdout", output): _emit({"ok": True, "results": [item]}, False)
        rendered = output.getvalue()
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("title\nspoof", rendered)
        self.assertNotIn("body\rspoof", rendered)
        with patch.object(hermes_plugin, "obsidian_knowledge_search", return_value=json.dumps({"ok": True, "results": [item]})):
            slash = hermes_plugin._notesearch_command("needle")
        self.assertNotIn("\x1b", slash)
        self.assertNotIn("body\rspoof", slash)
        self.assertIn("\\u001b", json.dumps(item))


if __name__ == "__main__": unittest.main()

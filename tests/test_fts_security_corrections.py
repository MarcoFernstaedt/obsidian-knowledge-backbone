import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from obsidian_kb.chunker import is_frontmatter_excluded
from obsidian_kb.cli import main
from obsidian_kb.config import Settings, load_settings
from obsidian_kb.indexer import Indexer
from obsidian_kb.rendering import sanitize_human
from obsidian_kb.search import search


class FtsSecurityCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.state = self.root / "private" / "index.sqlite3"
        self.settings = Settings(self.vault, self.state)

    def tearDown(self):
        self.tmp.cleanup()

    def index(self, settings=None, **kwargs):
        engine = Indexer(settings or self.settings)
        try:
            return engine.run(**kwargs)
        finally:
            engine.close()

    def test_state_parent_symlink_swap_before_lock_fails_closed(self):
        configured = self.root / "mutable" / "derived" / "index.sqlite3"
        settings = load_settings(vault=self.vault, state=configured)
        (self.root / "mutable").symlink_to(self.vault, target_is_directory=True)
        with self.assertRaises(OSError):
            self.index(settings)
        self.assertFalse((self.vault / "derived").exists())

    def test_state_parent_swap_after_lock_is_rejected_before_sqlite_open(self):
        (self.root / "private").mkdir()
        engine = Indexer(self.settings)
        try:
            engine.acquire_lock()
            (self.root / "private").rename(self.root / "original-private")
            (self.root / "private").symlink_to(self.vault, target_is_directory=True)
            with self.assertRaises(OSError):
                engine.run()
        finally:
            engine.close()
        self.assertFalse((self.vault / "index.sqlite3").exists())
        self.assertFalse((self.vault / "index.sqlite3-wal").exists())
        self.assertFalse((self.vault / "index.sqlite3-shm").exists())

    def test_dry_run_drift_is_stale_with_and_without_database_and_is_pure(self):
        note = self.vault / "note.md"
        note.write_text("# Topic\nold\n")
        missing = self.index(dry_run=True)
        self.assertEqual(missing["changed"], 1)
        self.assertTrue(missing["stale"])
        self.assertFalse(missing["current"])
        self.assertFalse(self.state.exists())

        self.index()
        before = self.state.read_bytes()
        note.write_text("# Topic\nchanged\n")
        existing = self.index(dry_run=True)
        self.assertEqual(existing["changed"], 1)
        self.assertTrue(existing["stale"])
        self.assertFalse(existing["current"])
        self.assertEqual(self.state.read_bytes(), before)

    def test_corrupt_derived_fields_and_json_use_exact_filesystem_fallback(self):
        note = self.vault / "a.md"
        note.write_text("# Trusted\nreal needle passage\n")
        self.index()
        conn = sqlite3.connect(self.state)
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("UPDATE chunks SET start_line=999,end_line=999,snippet='FORGED',title='Fake'")
            conn.commit()
        finally:
            conn.close()
        result = search(self.settings, "needle")
        self.assertTrue(result["fallback"])
        self.assertEqual(result["results"][0]["citation"], "a.md:L1-L2")
        self.assertEqual(result["results"][0]["snippet"], "# Trusted\nreal needle passage")
        self.assertNotIn("FORGED", json.dumps(result))

        self.index()
        conn = sqlite3.connect(self.state)
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("UPDATE chunks SET heading_path='not-json'")
            conn.commit()
        finally:
            conn.close()
        result = search(self.settings, "needle")
        self.assertTrue(result["fallback"])
        self.assertEqual(result["results"][0]["heading_path"], ["Trusted"])

        for assignment in (
            "chunk_id='forged-id'",
            "note_path='forged.md'",
            "source_sha256='forged-digest'",
            "ordinal=99",
            "content='FORGED CONTENT'",
            "normalized_text='FORGED NORMALIZED'",
            "content_sha256='forged-content-digest'",
        ):
            self.state.unlink()
            self.index()
            conn = sqlite3.connect(self.state)
            try:
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.execute(f"UPDATE chunks SET {assignment}")
                conn.commit()
            finally:
                conn.close()
            probe = search(self.settings, "needle")
            self.assertTrue(probe["fallback"], assignment)
            self.assertNotIn("FORGED", json.dumps(probe).upper(), assignment)

        config = self.root / "config.toml"
        config.write_text(f'[vault]\npath="{self.vault}"\n[state]\nsqlite_path="{self.state}"\n')
        config.chmod(0o600)
        output, error = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"OBSIDIAN_KB_CONFIG": str(config)}, clear=True), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = main(["search", "needle", "--json"])
        self.assertEqual(code, 0, error.getvalue())

    def test_indexed_and_fallback_token_semantics_do_not_stem(self):
        (self.vault / "run.md").write_text("# Run\nrun only\n")
        (self.vault / "running.md").write_text("# Running\nrunning only\n")
        self.index()
        indexed = search(self.settings, "running")
        indexed_paths = [item["path"] for item in indexed["results"]]
        self.state.unlink()
        fallback = search(self.settings, "running")
        fallback_paths = [item["path"] for item in fallback["results"]]
        self.assertEqual(indexed_paths, fallback_paths)
        self.assertEqual(indexed_paths, ["running.md"])

    def test_fresh_result_is_not_starved_by_stale_higher_ranked_rows(self):
        for number in range(9):
            (self.vault / f"stale-{number}.md").write_text("# Needle\n" + "needle " * 40)
        (self.vault / "fresh.md").write_text("# Other\na single needle\n")
        self.index()
        for number in range(9):
            (self.vault / f"stale-{number}.md").write_text("# Changed\nno match now\n")
        result = search(self.settings, "needle", limit=1)
        self.assertEqual([item["path"] for item in result["results"]], ["fresh.md"])

    def test_bom_and_quoted_control_keys_exclude_end_to_end(self):
        probes = {
            "bom.md": "\ufeff---\nindex: false\n---\n# Private\nprivacy sentinel needle\n",
            "double.md": '---\n"index": false\n---\n# Private\nprivacy sentinel needle\n',
            "single.md": "---\n'knowledge_index': false\n---\n# Private\nprivacy sentinel needle\n",
        }
        for name, text in probes.items():
            self.assertTrue(is_frontmatter_excluded(text, ("index", "knowledge_index")), name)
            (self.vault / name).write_text(text)
        status = self.index()
        self.assertEqual(status["excluded_notes"], 3)
        self.assertEqual(status["chunks"], 0)
        self.assertEqual(search(self.settings, "needle")["results"], [])

    def test_malformed_or_non_scalar_control_values_fail_closed(self):
        probes = (
            "---\nindex: [false]\n---\nbody",
            "---\nindex:\n  nested: false\n---\nbody",
            '---\n"index: false\n---\nbody',
            "---\n'index': {value: false}\n---\nbody",
        )
        self.assertTrue(all(is_frontmatter_excluded(text, ("index",)) for text in probes))

    def test_all_human_output_escapes_unicode_display_controls_and_separators(self):
        controls = ("\u202e", "\u2066", "\u2069", "\u200b", "\u200d", "\ufeff", "\u2028", "\u2029")
        rendered = sanitize_human("A" + "".join(controls) + "Z")
        for char in controls:
            self.assertNotIn(char, rendered)
            self.assertIn(f"\\u{ord(char):04x}", rendered)


if __name__ == "__main__":
    unittest.main()

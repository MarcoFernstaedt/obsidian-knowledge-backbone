import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from obsidian_kb.chunker import chunk_markdown, frontmatter, is_frontmatter_excluded
from obsidian_kb.config import ConfigError, Settings, load_settings
from obsidian_kb.indexer import Indexer
from obsidian_kb.privacy import contains_secret
from obsidian_kb.search import status_with_freshness
from obsidian_kb.store import CompatibilityError, Store
from obsidian_kb.vault_io import TrustedVault


class FinalCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"; self.vault.mkdir()
        self.state = self.root / "state" / "index.sqlite3"
        self.settings = Settings(self.vault, self.state)
    def tearDown(self): self.tmp.cleanup()

    def test_state_realpaths_and_compatibility_policy(self):
        for state in (self.vault, self.vault / "new" / "index.sqlite3"):
            with self.assertRaises(ConfigError): load_settings(vault=self.vault, state=state)
        outside = self.root / "outside"; outside.mkdir(); redirected = outside / "redirected"; redirected.symlink_to(self.vault, target_is_directory=True)
        with self.assertRaises(ConfigError): load_settings(vault=self.vault, state=redirected / "new" / "index.sqlite3")
        base = Settings(self.vault, self.state); changed = Settings(self.vault, self.state, max_chars=1300)
        self.assertNotEqual(base.compatibility_signature(), changed.compatibility_signature())
        Store(self.state, settings=base).close()
        with self.assertRaises(CompatibilityError): Store(self.state, settings=changed, read_only=True)

    def test_bounded_frontmatter_and_exact_chunk_lines(self):
        text = "---\ntags:\n- alpha\nindex: true\n---\n# Topic\nneedle"
        values, offset = frontmatter(text); self.assertGreater(offset, 0); self.assertNotIn("__malformed__", values)
        self.assertFalse(is_frontmatter_excluded(text, ("index",)))
        chunks = chunk_markdown(text, "a" * 64, "note.md")
        self.assertEqual((chunks[0]["start_line"], chunks[0]["end_line"]), (6, 7))
        oversized = "---\n" + "x: " + "a" * 66000 + "\n---\nbody"
        self.assertTrue(is_frontmatter_excluded(oversized, ("index",)))

    def test_credential_canaries_excluded_from_database(self):
        canaries = ["AIza" + "A" * 35, "npm_" + "B" * 36,
                    "Bearer eyJ" + "A" * 20 + ".eyJ" + "B" * 20 + "." + "C" * 24,
                    "postgresql://service:" + "p" * 16 + "@db.invalid/app"]
        self.assertTrue(all(contains_secret(value) for value in canaries))
        for index, value in enumerate(canaries): (self.vault / f"secret-{index}.md").write_text("# Secret\n" + value)
        engine = Indexer(self.settings)
        try: result = engine.run()
        finally: engine.close()
        self.assertEqual(result["excluded_notes"], len(canaries)); self.assertEqual(result["chunks"], 0)

    def test_descriptor_rejects_symlink_and_ctime_race(self):
        note = self.vault / "note.md"; outside = self.root / "outside.md"; note.write_text("safe"); outside.write_text("outside")
        with TrustedVault(self.vault) as trusted:
            note.unlink(); note.symlink_to(outside)
            with self.assertRaises(OSError): trusted.read("note.md", 100)
        note.unlink(); note.write_bytes(b"safe"); original_read = os.read; changed = False
        def mutate(fd, amount):
            nonlocal changed
            if not changed:
                changed = True; before = note.stat(); time.sleep(0.002); note.write_bytes(b"evil-longer"); os.utime(note, ns=(before.st_atime_ns, before.st_mtime_ns))
            return original_read(fd, amount)
        with TrustedVault(self.vault) as trusted, patch("obsidian_kb.vault_io.os.read", mutate):
            with self.assertRaises(OSError): trusted.read("note.md", 100)

    def test_inventory_overflow_is_unknown_and_stale(self):
        (self.vault / "a.md").write_text("# A\nneedle")
        engine = Indexer(self.settings)
        try: engine.run()
        finally: engine.close()
        (self.vault / "b.md").write_text("# B\nnew")
        status = status_with_freshness(Settings(self.vault, self.state, freshness_max_files=1))
        self.assertTrue(status["stale"]); self.assertFalse(status["current"])
        self.assertFalse(status["source_inventory_complete"]); self.assertIsNone(status["source_drift_count"])


if __name__ == "__main__": unittest.main()

from pathlib import Path
import tempfile
import unittest

from obsidian_kb.config import Settings
from obsidian_kb.indexer import Indexer
from obsidian_kb.search import search
from obsidian_kb.store import Store


class IndexSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = Path(self.tmp.name)
        self.vault, self.state = root / "vault", root / "state.sqlite3"; self.vault.mkdir()
        self.settings = Settings(self.vault, self.state, excluded_folders=("Templates",), excluded_globs=("Private/**",))
    def tearDown(self): self.tmp.cleanup()
    def run_index(self):
        engine = Indexer(self.settings)
        try: return engine.run()
        finally: engine.close()

    def test_folder_glob_frontmatter_and_hidden_exclusions(self):
        for folder, name in (("Templates", "a.md"), ("Private", "b.md")):
            (self.vault / folder).mkdir(); (self.vault / folder / name).write_text("# T\nbody")
        (self.vault / ".hidden.md").write_text("# T\nbody")
        (self.vault / "off.md").write_text("---\nindex: false\n---\n# T\nbody")
        result = self.run_index(); self.assertEqual(result["excluded_notes"], 4)
        store = Store(self.state, read_only=True)
        try:
            rows = list(store.conn.execute("SELECT source_sha256,exclusion_reason FROM notes"))
            self.assertTrue(all(row[0] is None and row[1] for row in rows))
        finally: store.close()

    def test_path_prefix_and_bounds(self):
        (self.vault / "Allowed").mkdir(); (self.vault / "Other").mkdir()
        (self.vault / "Allowed/a.md").write_text("# A\nneedle")
        (self.vault / "Other/b.md").write_text("# B\nneedle")
        self.run_index()
        result = search(self.settings, "needle", limit=20, path_prefix="Allowed")
        self.assertEqual([item["path"] for item in result["results"]], ["Allowed/a.md"])
        for query, limit, prefix in (("x" * 513, 5, None), ("x", 21, None), ("x", 5, "/abs"),
                                     ("x", 5, "../x"), ("x", 5, "A\\B")):
            with self.assertRaises(ValueError): search(self.settings, query, limit=limit, path_prefix=prefix)

    def test_deterministic_bm25_quality_and_tiebreak(self):
        (self.vault / "z.md").write_text("# Generic\nalpha beta")
        (self.vault / "best.md").write_text("# Alpha guide\nalpha alpha alpha beta")
        (self.vault / "a.md").write_text("# Tie\nunique")
        (self.vault / "b.md").write_text("# Tie\nunique")
        self.run_index()
        ranked = search(self.settings, "alpha beta", limit=2)
        self.assertEqual(ranked["results"][0]["path"], "best.md")
        first = search(self.settings, "unique", limit=2)
        second = search(self.settings, "unique", limit=2)
        self.assertEqual(first, second)
        self.assertEqual([item["path"] for item in first["results"]], ["a.md", "b.md"])

    def test_hidden_exclusion_is_configurable(self):
        (self.vault / ".visible.md").write_text("# Alpha\nunique needle")
        settings = Settings(self.vault, self.state, exclude_hidden=False)
        engine = Indexer(settings)
        try: engine.run()
        finally: engine.close()
        self.assertEqual(search(settings, "needle")["results"][0]["path"], ".visible.md")


if __name__ == "__main__": unittest.main()

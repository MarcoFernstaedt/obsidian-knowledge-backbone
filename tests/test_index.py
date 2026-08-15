from pathlib import Path
import tempfile
import unittest

from obsidian_kb.config import Settings
from obsidian_kb.indexer import Indexer
from obsidian_kb.search import reciprocal_rank_fusion, search
from obsidian_kb.store import Store


class FakeOllama:
    def embed(self, texts): return [[1.0, 0.0] for _ in texts]


class FakeQdrant:
    def __init__(self): self.points = {}; self.deleted = []
    def ensure(self): pass
    def upsert(self, points): self.points.update({p["id"]: p for p in points})
    def delete(self, ids): self.deleted.extend(ids); [self.points.pop(i, None) for i in ids]
    def query(self, vector, limit): return list(self.points.values())[:limit]


class IndexSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.vault, self.state = root / "vault", root / "state.sqlite3"
        self.vault.mkdir()
        self.settings = Settings(self.vault, self.state, excluded_folders=("Templates",),
                                 excluded_globs=("Private/**",), vector_size=2)

    def tearDown(self): self.tmp.cleanup()

    def run_index(self, **kwargs):
        indexer = Indexer(self.settings, **kwargs)
        try: return indexer.run()
        finally: indexer.close()

    def test_folder_glob_frontmatter_and_hidden_exclusions_record_path_reason_only(self):
        (self.vault / "Templates").mkdir(); (self.vault / "Templates" / "a.md").write_text("# T\nbody")
        (self.vault / "Private").mkdir(); (self.vault / "Private" / "b.md").write_text("# T\nbody")
        (self.vault / ".hidden.md").write_text("# T\nbody")
        (self.vault / "off.md").write_text("---\nindex: false\n---\n# T\nbody")
        result = self.run_index()
        self.assertEqual(result["excluded_notes"], 4)
        store = Store(self.state, read_only=True)
        try:
            rows = list(store.conn.execute("SELECT path,source_sha256,exclusion_reason FROM notes"))
            self.assertTrue(all(row[1] is None and row[2] for row in rows))
        finally: store.close()

    def test_delete_move_and_new_exclusion_reconcile(self):
        source = self.vault / "old.md"; source.write_text("# Alpha\nneedle body")
        self.run_index(); source.rename(self.vault / "new.md")
        result = self.run_index()
        self.assertEqual((result["changed"], result["removed"]), (1, 1))
        (self.vault / "new.md").write_text("---\nindex: false\n---\n# Alpha\nneedle body")
        self.run_index()
        store = Store(self.state, read_only=True)
        try:
            self.assertEqual(store.note("new.md")["status"], "excluded")
            self.assertFalse(store.lexical("needle", 5))
        finally: store.close()

    def test_stale_source_is_suppressed(self):
        note = self.vault / "a.md"; note.write_text("# Alpha\nunique needle")
        self.run_index(); note.write_text("# Alpha\nchanged without indexing")
        result = search(self.settings, "needle", offline=True)
        self.assertEqual(result["results"], [])

    def test_source_hash_uses_exact_bytes(self):
        note = self.vault / "a.md"; note.write_bytes(b"# Alpha\r\nunique needle\r\n")
        self.run_index(); note.write_bytes(b"# Alpha\nunique needle\n")
        self.assertEqual(search(self.settings, "needle", offline=True)["results"], [])

    def test_hidden_exclusion_is_configurable(self):
        (self.vault / ".visible.md").write_text("# Alpha\nunique needle")
        settings = Settings(self.vault, self.state, exclude_hidden=False)
        indexer = Indexer(settings)
        try: indexer.run()
        finally: indexer.close()
        self.assertEqual(search(settings, "needle", offline=True)["results"][0]["path"], ".visible.md")

    def test_semantic_postfilter_rejects_stale_remote_point(self):
        note = self.vault / "a.md"; note.write_text("# Alpha\nunique needle")
        remote = FakeQdrant(); self.run_index(ollama=FakeOllama(), qdrant=remote)
        valid_id = next(iter(remote.points))
        remote.points["stale"] = {"id": "stale", "payload": {"chunk_id": "missing"}}
        settings = Settings(self.vault, self.state, ollama_url="http://o", qdrant_url="http://q", vector_size=2)
        result = search(settings, "unmatched-semantic", ollama=FakeOllama(), qdrant=remote)
        self.assertEqual([item["path"] for item in result["results"]], ["a.md"])
        self.assertIn("semantic", result["results"][0]["modes"])
        self.assertTrue(valid_id)

    def test_remote_failure_degrades_to_lexical(self):
        class Down:
            def embed(self, texts):
                from obsidian_kb.remote import RemoteError
                raise RemoteError("down")
        note = self.vault / "a.md"; note.write_text("# Alpha\nunique needle")
        self.run_index()
        settings = Settings(self.vault, self.state, ollama_url="http://o", qdrant_url="http://q", vector_size=2)
        result = search(settings, "needle", ollama=Down(), qdrant=FakeQdrant())
        self.assertEqual(result["results"][0]["modes"], ["lexical"])
        self.assertTrue(result["degraded"])

    def test_rrf_is_deterministic_with_ties(self):
        a = {"chunk_id": "a", "note_path": "b.md", "start_line": 1}
        b = {"chunk_id": "b", "note_path": "a.md", "start_line": 1}
        first = reciprocal_rank_fusion([a, b], [b, a], 2)
        self.assertEqual([row["chunk_id"] for row in first], ["b", "a"])
        self.assertEqual(first, reciprocal_rank_fusion([a, b], [b, a], 2))


if __name__ == "__main__": unittest.main()

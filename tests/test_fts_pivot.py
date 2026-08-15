import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import hermes_plugin
from obsidian_kb.config import Settings
from obsidian_kb.indexer import Indexer
from obsidian_kb.search import search, status_with_freshness


class FtsPivotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.state = self.root / "state" / "index.sqlite3"
        self.settings = Settings(self.vault, self.state)

    def tearDown(self):
        self.tmp.cleanup()

    def index(self, **kwargs):
        engine = Indexer(self.settings)
        try:
            return engine.run(**kwargs)
        finally:
            engine.close()

    def test_ranked_lexical_results_have_exact_citations_and_links(self):
        (self.vault / "z.md").write_text("# Operations\nrollback deployment rollback\n")
        (self.vault / "a.md").write_text("# Operations\ndeployment only\n")
        self.index()
        first = search(self.settings, "rollback deployment", limit=2)
        second = search(self.settings, "rollback deployment", limit=2)
        self.assertEqual(first, second)
        self.assertEqual(first["mode"], "lexical")
        self.assertEqual(first["results"][0]["path"], "z.md")
        self.assertEqual(first["results"][0]["citation"], "z.md:L1-L2")
        self.assertEqual(first["results"][0]["obsidian_link"], "[[z#Operations]]")
        self.assertEqual(first["results"][0]["retrieval_type"], "lexical")
        self.assertEqual(first["results"][0]["modes"], ["lexical"])
        self.assertTrue(first["passages_are_untrusted"])

    def test_runtime_is_network_free(self):
        (self.vault / "note.md").write_text("# Topic\nneedle\n")
        with patch.object(socket, "socket", side_effect=AssertionError("network forbidden")):
            self.index()
            self.assertEqual(search(self.settings, "needle")["results"][0]["path"], "note.md")
            self.assertTrue(status_with_freshness(self.settings)["current"])

    def test_shipped_surfaces_have_no_retired_or_network_terms(self):
        root = Path(__file__).parents[1]
        shipped = [root / "obsidian_kb", root / "hermes_plugin", root / "docs",
                   root / "README.md", root / "SECURITY.md", root / "config.example.toml",
                   root / "pyproject.toml", root / "MANIFEST.in", root / "scripts", root / ".github"]
        forbidden = ("ollama", "qdrant", "semantic", "embedding", "vector", "hybrid",
                     "reciprocal rank", "rrf", "pending projection", "pending_vector",
                     "tombstone", "http://", "https://", "urllib")
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

    def test_plugin_fixed_config_and_status_are_path_free(self):
        (self.vault / "note.md").write_text("# Topic\nneedle\n")
        self.index()
        config = self.root / "config.toml"
        config.write_text(f'[vault]\npath="{self.vault}"\n[state]\nsqlite_path="{self.state}"\n')
        config.chmod(0o600)
        with patch.dict(os.environ, {"OBSIDIAN_KB_CONFIG": str(config)}, clear=True):
            result = json.loads(hermes_plugin.obsidian_knowledge_search({"query": "needle"}))
            status = json.loads(hermes_plugin.obsidian_knowledge_status({}))
        self.assertTrue(result["ok"])
        self.assertTrue(status["ok"])
        self.assertNotIn(str(self.vault), json.dumps(status))
        self.assertNotIn("note.md", json.dumps(status))


if __name__ == "__main__":
    unittest.main()

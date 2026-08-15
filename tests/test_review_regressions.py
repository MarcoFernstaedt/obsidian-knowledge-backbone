import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import hermes_plugin
from obsidian_kb.chunker import chunk_markdown
from obsidian_kb.cli import imperator_search_main, imperator_vault_index_main, main
from obsidian_kb.config import ConfigError, Settings, load_settings
from obsidian_kb.indexer import Indexer
from obsidian_kb.privacy import contains_secret
from obsidian_kb.remote import OllamaClient, QdrantClient, RemoteError
from obsidian_kb.search import search, status_with_freshness
from obsidian_kb.store import Store
from obsidian_kb.vault_io import TrustedVault


class FakeOllama:
    def __init__(self): self.calls = []
    def embed(self, texts): self.calls.append(list(texts)); return [[1.0, 0.0] for _ in texts]


class FakeQdrant:
    def __init__(self): self.points = {}; self.deleted = []; self.signatures = []; self.upsert_batches = []
    def ensure(self, corpus, signature, model_digest): self.signatures.append((corpus, signature, model_digest))
    def upsert(self, points, signature): self.upsert_batches.append(list(points)); self.points.update({p["id"]: p for p in points})
    def delete(self, ids, corpus=None, signature=None): self.deleted.extend(ids)
    def query(self, vector, limit, corpus, signature): return list(self.points.values())[:limit]
    def list_ids(self, corpus, signature): return list(self.points)


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"; self.vault.mkdir()
        self.state = self.root / "state" / "index.sqlite3"
        self.settings = Settings(self.vault, self.state, vector_size=2,
                                 ollama_url="http://ollama", qdrant_url="http://qdrant",
                                 embedding_batch_size=2)
    def tearDown(self): self.tmp.cleanup()

    def test_provider_tokens_and_generic_assignments_fail_closed_but_placeholders_pass(self):
        secrets = [
            "xoxb-123456789012-123456789012-abcdefghijklmnopqrstuvwxyzABCD",
            "glpat-abcdefghijklmnopqrst",
            "sk_live_abcdefghijklmnopqrstuvwxyz123456",
            "AC0123456789abcdef0123456789abcdef:0123456789abcdef0123456789abcdef",
            "TWILIO_AUTH_TOKEN=0123456789abcdef0123456789abcdef",
            "OPENAI_API_KEY=abcdefghijklmnopqrstuvwxyz123456",
            "AWS_SESSION_TOKEN=abcdefghijklmnopqrstuvwxyz1234567890+/=",
            '"password": "a-real-password-value"',
            "gitlab_token: abcdefghijklmnopqrstuvwxyz123456",
        ]
        for value in secrets: self.assertTrue(contains_secret(value), value)
        for value in ("TWILIO_AUTH_TOKEN=${TWILIO_AUTH_TOKEN}", '"password": "<password>"',
                      "openai_api_key: REDACTED", "sk_live_example", "xoxb-your-token"):
            self.assertFalse(contains_secret(value), value)

    def test_true_canary_never_reaches_any_index_or_remote_or_results(self):
        canary = "xoxb-123456789012-123456789012-abcdefghijklmnopqrstuvwxyzABCD"
        (self.vault / "secret.md").write_text("# Secret\nneedle " + canary)
        ollama, qdrant = FakeOllama(), FakeQdrant(); engine = Indexer(self.settings, ollama=ollama, qdrant=qdrant)
        try: result = engine.run()
        finally: engine.close()
        db = sqlite3.connect(self.state)
        try:
            for table in ("notes", "chunks", "chunks_fts"):
                self.assertNotIn(canary, "\n".join(str(r) for r in db.execute(f"SELECT * FROM {table}")))
        finally: db.close()
        self.assertNotIn(canary, json.dumps(result)); self.assertEqual(ollama.calls, []); self.assertEqual(qdrant.points, {})
        self.assertNotIn(canary, json.dumps(search(self.settings, "needle", offline=True)))

    def test_entire_scan_is_one_transaction_on_mid_scan_crash(self):
        (self.vault / "a.md").write_text("# A\nold generation")
        engine = Indexer(self.settings)
        try: engine.run()
        finally: engine.close()
        (self.vault / "a.md").write_text("# A\nnew generation")
        (self.vault / "b.md").write_text("# B\nsecond")
        original = Store.replace_note; calls = 0
        def crash(store, *args, **kwargs):
            nonlocal calls; calls += 1
            if calls == 2: raise RuntimeError("synthetic crash")
            return original(store, *args, **kwargs)
        with patch.object(Store, "replace_note", crash):
            engine = Indexer(self.settings)
            try:
                with self.assertRaises(RuntimeError): engine.run()
            finally: engine.close()
        store = Store(self.state, settings=self.settings, read_only=True)
        try:
            self.assertTrue(store.lexical("old", 5)); self.assertFalse(store.lexical("new", 5)); self.assertIsNone(store.note("b.md"))
        finally: store.close()

    def test_dry_run_from_absent_state_creates_nothing(self):
        (self.vault / "a.md").write_text("# A\nneedle")
        self.assertFalse(self.state.parent.exists())
        engine = Indexer(self.settings)
        try: result = engine.run(dry_run=True)
        finally: engine.close()
        self.assertTrue(result["dry_run"]); self.assertFalse(self.state.parent.exists())

    def test_descriptor_reads_reject_symlink_swaps(self):
        note = self.vault / "note.md"; note.write_text("safe")
        outside = self.root / "outside.md"; outside.write_text("secret outside")
        with TrustedVault(self.vault) as trusted:
            note.unlink(); note.symlink_to(outside)
            with self.assertRaises(OSError): trusted.read("note.md", 1024)

    def test_index_toctou_swap_cannot_reach_ollama_or_fts(self):
        note = self.vault / "note.md"; note.write_text("# Safe\nneedle")
        outside = self.root / "outside.md"; canary = "xoxb-123456789012-123456789012-abcdefghijklmnopqrstuvwxyzABCD"
        outside.write_text("# Outside\n" + canary)
        original = TrustedVault.read; swapped = False
        def swap_then_read(trusted, path, maximum):
            nonlocal swapped
            if not swapped:
                swapped = True; note.unlink(); note.symlink_to(outside)
            return original(trusted, path, maximum)
        ollama, qdrant = FakeOllama(), FakeQdrant()
        with patch.object(TrustedVault, "read", swap_then_read):
            engine = Indexer(self.settings, ollama=ollama, qdrant=qdrant)
            try: engine.run()
            finally: engine.close()
        self.assertEqual(ollama.calls, []); self.assertEqual(qdrant.points, {})
        db = sqlite3.connect(self.state)
        try: self.assertNotIn(canary, "".join(str(row) for row in db.execute("SELECT * FROM chunks_fts")))
        finally: db.close()

    def test_config_is_private_owned_regular_nonsymlink_at_plugin_boundary(self):
        cfg = self.root / "config.toml"
        cfg.write_text(f'[vault]\npath="{self.vault}"\n[state]\nsqlite_path="{self.state}"\n')
        cfg.chmod(0o644)
        with self.assertRaises(ConfigError): load_settings(cfg, require_private=True)
        with patch.dict(os.environ, {"OBSIDIAN_KB_CONFIG": str(cfg)}):
            self.assertFalse(json.loads(hermes_plugin.obsidian_knowledge_status({}))["ok"])
        cfg.chmod(0o600); link = self.root / "link.toml"; link.symlink_to(cfg)
        with self.assertRaises(ConfigError): load_settings(link, require_private=True)

    def test_fences_track_marker_and_minimum_closing_length(self):
        text = "# Real\n````python\n~~~\n# not heading\n```\n# still code\n````\n## Child\nbody"
        chunks = chunk_markdown(text, "a" * 64, "n.md")
        self.assertEqual([c["heading_path"] for c in chunks], [["Real"], ["Real", "Child"]])
        self.assertIn("# still code", chunks[0]["content"])

    def test_paragraph_first_split_and_at_most_two_line_overlap(self):
        text = "# H\na one\na two\n\nb one\nb two\n\nc one\nc two"
        chunks = chunk_markdown(text, "a" * 64, "n.md", max_lines=5, max_chars=64, overlap_lines=2)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertLessEqual(chunks[1]["start_line"], chunks[0]["end_line"] + 1)
        self.assertLessEqual(chunks[0]["end_line"] - chunks[1]["start_line"] + 1, 2)

    def test_embedding_and_upsert_batches_are_bounded(self):
        for i in range(5): (self.vault / f"{i}.md").write_text(f"# N{i}\nneedle {i}")
        ollama, qdrant = FakeOllama(), FakeQdrant(); engine = Indexer(self.settings, ollama=ollama, qdrant=qdrant)
        try: engine.run()
        finally: engine.close()
        self.assertTrue(ollama.calls); self.assertTrue(all(len(batch) <= 2 for batch in ollama.calls))
        self.assertTrue(qdrant.upsert_batches); self.assertTrue(all(len(batch) <= 2 for batch in qdrant.upsert_batches))

    def test_mixed_remote_generation_blocks_upsert_and_destructive_reconcile(self):
        class Mixed(FakeQdrant):
            def ensure(self, corpus, signature, model_digest): raise RemoteError("mixed signature")
        (self.vault / "a.md").write_text("# A\nneedle")
        remote = Mixed(); engine = Indexer(self.settings, ollama=FakeOllama(), qdrant=remote)
        try: result = engine.run(full_reconcile=True)
        finally: engine.close()
        self.assertEqual(remote.points, {}); self.assertEqual(remote.deleted, [])
        self.assertEqual(result["pending_vectors"], 1)

    def test_source_drift_marks_status_stale_without_paths(self):
        note = self.vault / "a.md"; note.write_text("# A\nneedle")
        engine = Indexer(self.settings)
        try: engine.run()
        finally: engine.close()
        note.write_text("# A\nmodified")
        (self.vault / "new.md").write_text("# New\nnew")
        payload = status_with_freshness(self.settings)
        self.assertTrue(payload["stale"]); self.assertEqual(payload["source_drift_count"], 2)
        self.assertNotIn("a.md", json.dumps(payload)); self.assertNotIn("new.md", json.dumps(payload))

    def test_direct_compatibility_entry_points_inject_subcommands(self):
        with patch("obsidian_kb.cli.main", return_value=0) as dispatch:
            self.assertEqual(imperator_search_main(["needle", "--limit", "2"]), 0)
            dispatch.assert_called_once_with(["search", "needle", "--limit", "2"])
        with patch("obsidian_kb.cli.main", return_value=0) as dispatch:
            self.assertEqual(imperator_vault_index_main(["--dry-run"]), 0)
            dispatch.assert_called_once_with(["index", "--dry-run"])

    def test_exit_codes_distinguish_config_pending_and_fatal(self):
        with patch("sys.stderr", io.StringIO()): self.assertEqual(main(["status"]), 2)
        with patch("obsidian_kb.cli.build_parser", side_effect=sqlite3.DatabaseError("corrupt")), patch("sys.stderr", io.StringIO()):
            self.assertEqual(main([]), 1)


class BoundedResponse:
    def __init__(self, payload): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, amount=-1): return self.payload if amount < 0 else self.payload[:amount]


class QdrantCompatibilityTests(unittest.TestCase):
    @patch("obsidian_kb.remote.request.urlopen")
    def test_http_response_limit_and_signature_filters(self, urlopen):
        urlopen.return_value = BoundedResponse(b"{" + b"x" * (8 * 1024 * 1024) + b"}")
        with self.assertRaises(RemoteError): OllamaClient("http://o", "m").embed(["x"])
        urlopen.reset_mock()
        urlopen.side_effect = [
            BoundedResponse(json.dumps({"result":{"config":{"params":{"vectors":{"size":2,"distance":"Cosine"}}}}}).encode()),
            BoundedResponse(json.dumps({"result":{"points":[{"id":"x","payload":{"corpus_id":"c"}}],"next_page_offset":None}}).encode()),
        ]
        q = QdrantClient("http://q", "collection", 2)
        with self.assertRaises(RemoteError): q.ensure("c", "sig", "digest")
        body = json.loads(urlopen.call_args_list[1].args[0].data)
        self.assertEqual(body["filter"]["must"][0]["key"], "corpus_id")


if __name__ == "__main__": unittest.main()

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import hermes_plugin
from obsidian_kb.config import Settings
from obsidian_kb.indexer import IndexLockError, Indexer
from obsidian_kb.search import reciprocal_rank_fusion, search
from obsidian_kb.store import CompatibilityError, Store


class FakeOllama:
    def __init__(self, fail=False): self.fail = fail; self.seen = []
    def embed(self, texts):
        from obsidian_kb.remote import RemoteError
        self.seen.extend(texts)
        if self.fail: raise RemoteError("offline")
        return [[1.0, 0.0] for _ in texts]


class FakeQdrant:
    def __init__(self, fail=False): self.fail = fail; self.points = {}; self.deleted = []
    def ensure(self, signature=None):
        from obsidian_kb.remote import RemoteError
        if self.fail: raise RemoteError("offline")
    def upsert(self, points):
        from obsidian_kb.remote import RemoteError
        if self.fail: raise RemoteError("offline")
        self.points.update({p["id"]: p for p in points})
    def delete(self, ids):
        from obsidian_kb.remote import RemoteError
        if self.fail: raise RemoteError("offline")
        self.deleted.extend(ids); [self.points.pop(i, None) for i in ids]
    def query(self, vector, limit, corpus_id=None): return list(self.points.values())[:limit]
    def list_ids(self, corpus_id): return list(self.points)


class ProductionContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.vault = root / "vault"; self.vault.mkdir()
        self.state = root / "state.sqlite3"
        self.settings = Settings(self.vault, self.state, vector_size=2,
                                 ollama_url="http://ollama", qdrant_url="http://qdrant")
    def tearDown(self): self.tmp.cleanup()

    def run_index(self, **kwargs):
        run_kwargs = kwargs.pop("run_kwargs", {})
        engine = Indexer(self.settings, **kwargs)
        try: return engine.run(**run_kwargs)
        finally: engine.close()

    def test_plugin_manifest_and_registration_are_exact_and_network_free(self):
        manifest = (Path(hermes_plugin.__file__).parent / "plugin.yaml").read_text()
        self.assertIn("obsidian_knowledge_search", manifest)
        self.assertIn("obsidian_knowledge_status", manifest)
        self.assertIn("notesearch", manifest)
        class Ctx:
            def __init__(self): self.tools=[]; self.commands=[]; self.skills=[]
            def register_tool(self, **kw): self.tools.append(kw)
            def register_command(self, *args, **kw): self.commands.append((args,kw))
            def register_skill(self,*args): self.skills.append(args)
        with patch("obsidian_kb.remote.request.urlopen", side_effect=AssertionError("network")):
            ctx=Ctx(); hermes_plugin.register(ctx)
        self.assertEqual([x["name"] for x in ctx.tools], ["obsidian_knowledge_search", "obsidian_knowledge_status"])
        self.assertEqual([x[0][0] for x in ctx.commands], ["notesearch"])
        schema = ctx.tools[0]["schema"]
        self.assertNotIn("config_path", schema["parameters"]["properties"])
        self.assertNotIn("offline", schema["parameters"]["properties"])
        self.assertIn("untrusted", schema["description"].lower())
        self.assertEqual(schema["parameters"]["properties"]["limit"]["maximum"], 20)

    def test_plugin_rejects_unknown_arguments_and_uses_only_fixed_environment_config(self):
        for bad in ({"query":"x", "config_path":"/tmp/x"}, {"query":"x", "offline":True},
                    {"query":"x", "path_prefix":"../private"}):
            payload=json.loads(hermes_plugin.obsidian_knowledge_search(bad))
            self.assertFalse(payload["ok"])
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(json.loads(hermes_plugin.obsidian_knowledge_status({}))["ok"])

    def test_query_and_limit_bounds_and_relative_prefix(self):
        (self.vault / "Allowed").mkdir(); (self.vault / "Other").mkdir()
        (self.vault / "Allowed/a.md").write_text("# A\nneedle")
        (self.vault / "Other/b.md").write_text("# B\nneedle")
        self.run_index(ollama=FakeOllama(), qdrant=FakeQdrant())
        result=search(self.settings, "needle", limit=20, path_prefix="Allowed")
        self.assertEqual([r["path"] for r in result["results"]], ["Allowed/a.md"])
        for query, limit, prefix in (("x"*513,5,None),("x",21,None),("x",5,"/abs"),("x",5,"../x"),("x",5,"A\\B")):
            with self.assertRaises(ValueError): search(self.settings, query, limit=limit, path_prefix=prefix)

    def test_weighted_rrf_and_tiebreak_include_component_rank(self):
        a={"chunk_id":"a","note_path":"z.md","start_line":1}
        b={"chunk_id":"b","note_path":"a.md","start_line":1}
        rows=reciprocal_rank_fusion([a,b],[b,a],2)
        self.assertEqual([r["chunk_id"] for r in rows],["b","a"])
        self.assertGreater(rows[0]["fusion_score"], rows[1]["fusion_score"])
        self.assertEqual(rows[0]["semantic_rank"],1)
        self.assertEqual(rows[0]["lexical_rank"],2)

    def test_missing_and_corrupt_sqlite_use_read_only_filesystem_fallback_with_secret_policy(self):
        (self.vault / "safe.md").write_text("# Safe\nfallback needle")
        canary="sk-" + "A"*30
        (self.vault / "secret.md").write_text("# Secret\nfallback needle " + canary)
        before=set(self.vault.rglob("*"))
        result=search(self.settings,"fallback needle",offline=True)
        self.assertEqual(result["mode"],"filesystem-fallback")
        self.assertEqual([x["path"] for x in result["results"]],["safe.md"])
        self.state.write_bytes(b"not sqlite")
        result=search(self.settings,"fallback",offline=True)
        self.assertEqual(result["mode"],"filesystem-fallback")
        self.assertNotIn(canary,json.dumps(result))
        self.assertEqual(before,set(self.vault.rglob("*")))

    def test_malformed_frontmatter_and_secret_canary_never_reach_remotes(self):
        (self.vault / "bad.md").write_text("---\nnot yaml\n---\n# Bad\nneedle")
        canary = "sk-" + "Z" * 30
        (self.vault / "secret.md").write_text("# Secret\nneedle " + canary)
        ollama, remote = FakeOllama(), FakeQdrant()
        result = self.run_index(ollama=ollama, qdrant=remote)
        self.assertEqual(result["excluded_notes"], 2)
        self.assertEqual(ollama.seen, [])
        self.assertEqual(remote.points, {})
        self.assertNotIn(canary, json.dumps(result))

    def test_refresh_wrapper_is_private_locked_bounded_and_absolute_binary_gated(self):
        wrapper = (Path(__file__).parents[1] / "scripts" / "imperator_obsidian_retrieval_refresh.sh").read_text()
        for token in ("umask 077", "flock -n", "timeout --signal=TERM", "OBSIDIAN_KB_BIN", "OBSIDIAN_KB_CONFIG"):
            self.assertIn(token, wrapper)
        self.assertIn('[[ "$OBSIDIAN_KB_BIN" = /*', wrapper)

    def test_wal_busy_timeout_metadata_signature_and_status_fields(self):
        store=Store(self.state, settings=self.settings)
        try:
            self.assertEqual(store.conn.execute("PRAGMA journal_mode").fetchone()[0],"wal")
            self.assertGreaterEqual(store.conn.execute("PRAGMA busy_timeout").fetchone()[0],1000)
            self.assertTrue(store.metadata("compatibility_signature"))
            status=store.status()
            self.assertEqual(set(("generated_at","age_seconds","pending_vectors","pending_tombstones","stale"))-set(status),set())
        finally: store.close()
        changed=Settings(self.vault,self.state,vector_size=3,ollama_url="http://ollama",qdrant_url="http://qdrant")
        with self.assertRaises(CompatibilityError): Store(self.state,settings=changed,read_only=True)

    def test_pending_semantic_recovers_idempotently_and_payload_is_content_free(self):
        (self.vault / "a.md").write_text("# Alpha\nprivate prose needle")
        down=FakeQdrant(fail=True)
        first=self.run_index(ollama=FakeOllama(),qdrant=down)
        self.assertEqual(first["pending_vectors"],1)
        remote=FakeQdrant(); second=self.run_index(ollama=FakeOllama(),qdrant=remote)
        self.assertEqual(second["pending_vectors"],0)
        self.assertEqual(len(remote.points),1)
        payload=next(iter(remote.points.values()))["payload"]
        self.assertEqual(set(payload),{"corpus_id","schema_version","chunk_id","content_sha256","embedding_model"})
        self.assertNotIn("private prose",json.dumps(payload))
        third=self.run_index(ollama=FakeOllama(),qdrant=remote)
        self.assertEqual(third["changed"],0)

    def test_tombstone_retry_and_full_remote_orphan_reconciliation(self):
        note=self.vault / "a.md"; note.write_text("# A\nneedle")
        remote=FakeQdrant(); self.run_index(ollama=FakeOllama(),qdrant=remote)
        old_id=next(iter(remote.points)); note.unlink()
        remote.fail=True; result=self.run_index(ollama=FakeOllama(),qdrant=remote)
        self.assertEqual(result["pending_tombstones"],1)
        remote.fail=False; remote.points["orphan"]={"id":"orphan","payload":{"corpus_id":self.settings.corpus_id}}
        result=self.run_index(ollama=FakeOllama(),qdrant=remote,run_kwargs={"full_reconcile":True})
        self.assertEqual(result["pending_tombstones"],0)
        self.assertIn(old_id,remote.deleted); self.assertIn("orphan",remote.deleted)

    def test_recreated_chunk_cancels_pending_tombstone_before_projection(self):
        note = self.vault / "a.md"; note.write_text("# A\nneedle")
        remote = FakeQdrant(); self.run_index(ollama=FakeOllama(), qdrant=remote)
        chunk_id = next(iter(remote.points))
        note.unlink(); remote.fail = True
        self.run_index(ollama=FakeOllama(), qdrant=remote)
        note.write_text("# A\nneedle restored"); remote.fail = False
        result = self.run_index(ollama=FakeOllama(), qdrant=remote)
        self.assertEqual(result["pending_tombstones"], 0)
        self.assertIn(chunk_id, remote.points)

    def test_index_lock_is_exclusive(self):
        first=Indexer(self.settings,ollama=FakeOllama(),qdrant=FakeQdrant())
        try:
            first.acquire_lock()
            second=Indexer(self.settings,ollama=FakeOllama(),qdrant=FakeQdrant())
            try:
                with self.assertRaises(IndexLockError): second.run()
            finally: second.close()
        finally: first.release_lock(); first.close()


if __name__ == "__main__": unittest.main()

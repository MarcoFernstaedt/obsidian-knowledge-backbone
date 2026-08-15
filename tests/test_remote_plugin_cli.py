import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from obsidian_kb.cli import main
from obsidian_kb.config import ConfigError, load_settings
from obsidian_kb.remote import OllamaClient, QdrantClient
import hermes_plugin


class Response:
    def __init__(self, payload): self.payload = json.dumps(payload).encode()
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return self.payload


class RemoteTests(unittest.TestCase):
    @patch("obsidian_kb.remote.request.urlopen")
    def test_ollama_and_qdrant_http_contracts_have_minimal_payload(self, urlopen):
        urlopen.side_effect = [Response({"embeddings": [[1.0, 2.0]]}), Response({}),
                               Response({}), Response({"result": {"points": []}})]
        self.assertEqual(OllamaClient("http://ollama", "model").embed(["text"]), [[1.0, 2.0]])
        qdrant = QdrantClient("http://qdrant", "collection", 2)
        qdrant.ensure()
        qdrant.upsert([{"id": "id", "vector": [1, 2], "payload": {"chunk_id": "id"}}])
        self.assertEqual(qdrant.query([1, 2], 3), [])
        bodies = [json.loads(call.args[0].data) for call in urlopen.call_args_list if call.args[0].data]
        self.assertEqual(bodies[1], {"points": [{"id": "id", "vector": [1, 2], "payload": {"chunk_id": "id"}}]})


class PluginTests(unittest.TestCase):
    def test_registration_schema_and_handler(self):
        class Context:
            def __init__(self): self.tools = []; self.commands = []; self.skills = []
            def register_tool(self, **kw): self.tools.append(kw)
            def register_command(self, *args, **kw): self.commands.append((args, kw))
            def register_skill(self, *args): self.skills.append(args)
        ctx = Context(); hermes_plugin.register(ctx)
        self.assertEqual([tool["name"] for tool in ctx.tools], ["obsidian_knowledge_search", "obsidian_knowledge_status"])
        self.assertEqual(ctx.tools[0]["schema"]["parameters"]["additionalProperties"], False)
        self.assertEqual(ctx.commands[0][0][0], "notesearch")
        self.assertEqual(ctx.skills[0][0], "obsidian-knowledge-backbone")

    def test_handler_returns_json_and_redacts_exception(self):
        with patch.dict(os.environ, {"OBSIDIAN_KB_CONFIG": "x"}), patch.object(hermes_plugin, "load_settings", side_effect=RuntimeError("private path")):
            payload = json.loads(hermes_plugin.obsidian_knowledge_search({"query": "x"}))
        self.assertEqual(payload, {"ok": False, "error": "knowledge search failed: RuntimeError"})

    def test_handler_success(self):
        expected = {"ok": True, "results": []}
        with patch.dict(os.environ, {"OBSIDIAN_KB_CONFIG": "x"}), patch.object(hermes_plugin, "load_settings", return_value=object()), \
             patch.object(hermes_plugin, "search", return_value=expected):
            payload = json.loads(hermes_plugin.obsidian_knowledge_search({"query": "what"}))
        self.assertEqual(payload, expected)


class ConfigCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"; self.vault.mkdir()
        self.state = self.root / "state.sqlite3"
        self.config = self.root / "config.toml"
        self.config.write_text(f'[vault]\npath="{self.vault}"\n[state]\nsqlite_path="{self.state}"\n')

    def tearDown(self): self.tmp.cleanup()

    def test_malformed_and_unknown_config_rejected(self):
        bad = self.root / "bad.toml"; bad.write_text("[vault\n")
        with self.assertRaises(ConfigError): load_settings(bad)
        bad.write_text(f'[vault]\npath="{self.vault}"\n[state]\nsqlite_path="{self.state}"\n[unknown]\nx=1\n')
        with self.assertRaises(ConfigError): load_settings(bad)

    def test_cli_index_query_audit_status_json_smoke(self):
        (self.vault / "note.md").write_text("# Topic\nsearchable phrase")
        for argv in (["index", "--config", str(self.config), "--json"],
                     ["query", "searchable", "--config", str(self.config), "--offline", "--json"],
                     ["audit", "--config", str(self.config), "--json"],
                     ["status", "--config", str(self.config), "--json"]):
            output = io.StringIO()
            with patch("sys.stdout", output): code = main(argv)
            self.assertEqual(code, 4 if argv[0] == "index" else 0, (argv, output.getvalue()))
            self.assertTrue(json.loads(output.getvalue())["ok"])
        query_output = io.StringIO()
        with patch("sys.stdout", query_output): main(["query", "searchable", "--config", str(self.config), "--offline", "--json"])
        item = json.loads(query_output.getvalue())["results"][0]
        self.assertEqual(item["citation"], "note.md:L1-L2")

    def test_status_missing_index_has_meaningful_exit(self):
        with patch("sys.stdout", io.StringIO()): code = main(["status", "--config", str(self.config), "--json"])
        self.assertEqual(code, 3)


if __name__ == "__main__": unittest.main()

import hashlib
import unittest


from obsidian_kb.chunker import chunk_markdown, is_frontmatter_excluded
from obsidian_kb.privacy import contains_secret


class ChunkingTests(unittest.TestCase):
    def test_frontmatter_is_not_prose_and_lines_are_exact(self):
        text = "---\ntags: [safe]\n---\n# Root\nintro\n## Child\nchild text\n"
        chunks = chunk_markdown(text, hashlib.sha256(text.encode()).hexdigest(), "note.md")
        self.assertEqual(chunks[0]["start_line"], 4)
        self.assertEqual(chunks[0]["end_line"], 5)
        self.assertEqual(chunks[1]["heading_path"], ["Root", "Child"])
        self.assertEqual(chunks[1]["start_line"], 6)
        self.assertNotIn("tags", " ".join(c["content"] for c in chunks))
        self.assertRegex(chunks[0]["chunk_id"], r"^[0-9a-f-]{36}$")

    def test_bounded_splits_preserve_source_spans(self):
        text = "# T\n" + "\n".join(f"line {i}" for i in range(8))
        chunks = chunk_markdown(text, "a" * 64, "n.md", max_lines=3, max_chars=64)
        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(c["end_line"] - c["start_line"] + 1 <= 3 for c in chunks))
        self.assertEqual(chunks[0]["start_line"], 1)
        self.assertEqual(chunks[-1]["end_line"], 9)

    def test_false_frontmatter_keys(self):
        self.assertTrue(is_frontmatter_excluded("---\nknowledge_index: false\n---\nbody", ("knowledge_index",)))
        self.assertFalse(is_frontmatter_excluded("---\nknowledge_index: true\n---\nbody", ("knowledge_index",)))


class PrivacyTests(unittest.TestCase):
    def test_private_keys_and_real_assignments_suppressed(self):
        self.assertTrue(contains_secret("-----BEGIN PRIVATE KEY-----\nabc"))
        self.assertTrue(contains_secret("api_key = 's3cr3t-value-987'"))
        self.assertTrue(contains_secret("aws_secret_access_key=hunter2"))

    def test_placeholders_and_documentation_allowed(self):
        for text in ("API key configuration guide", "api_key = ${API_KEY}", "password: <your-password>",
                     "token=REDACTED", "Use the word secret in documentation"):
            self.assertFalse(contains_secret(text), text)


if __name__ == "__main__": unittest.main()

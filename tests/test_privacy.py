import unittest

from obsidian_kb.privacy import contains_secret


class AdditionalPrivacyTests(unittest.TestCase):
    def test_known_token_shapes(self):
        self.assertTrue(contains_secret("token=ghp_abcdefghijklmnopqrstuvwxyz1234567890"))

    def test_benign_security_discussion(self):
        self.assertFalse(contains_secret("Rotate passwords and API keys regularly."))

    def test_yaml_quoted_sequence_assignments_and_block_scalars_fail_closed(self):
        credential_notes = (
            "- 'API_KEY': 'abc''synthetic-secret-canary'",
            '- "TOKEN": "synthetic-secret-canary"',
            "  - TOKEN: |\n      synthetic-secret-canary\n",
            "- CLIENT_SECRET: >-\n    synthetic-secret-canary\n",
        )
        for text in credential_notes:
            with self.subTest(text=text):
                self.assertTrue(contains_secret(text))

    def test_yaml_sensitive_placeholders_remain_documentable(self):
        for text in ("- API_KEY: ${API_KEY}", "- 'TOKEN': '<your-token>'", "PASSWORD: REDACTED"):
            with self.subTest(text=text):
                self.assertFalse(contains_secret(text))


if __name__ == "__main__": unittest.main()

import unittest

from obsidian_kb.privacy import contains_secret


class AdditionalPrivacyTests(unittest.TestCase):
    def test_known_token_shapes(self):
        self.assertTrue(contains_secret("token=ghp_abcdefghijklmnopqrstuvwxyz1234567890"))

    def test_benign_security_discussion(self):
        self.assertFalse(contains_secret("Rotate passwords and API keys regularly."))


if __name__ == "__main__": unittest.main()

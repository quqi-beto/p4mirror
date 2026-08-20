"""Regression tests for ``core.github_auth``.

Covers ``GitHubAppTokenProvider`` credential validation (App ID +
installation ID + private key all required) — the actual token minting is
a live GitHub API call and is not exercised here.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.github_auth import GitHubAppTokenProvider, GitHubTokenError


class TokenProviderConfigTest(unittest.TestCase):
    """Per-test temp dir so Windows never keeps a file handle locked."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_key(self, name: str = "app.private-key.pem") -> Path:
        path = self.dir / name
        path.write_text("fake-key", encoding="utf-8")
        return path

    def test_provider_stores_credentials(self):
        key = self._make_key()
        provider = GitHubAppTokenProvider(
            app_id="111",
            installation_id="222",
            private_key_path=key,
        )
        self.assertEqual(provider._app_id, "111")
        self.assertEqual(provider._installation_id, "222")
        self.assertEqual(provider._private_key_path, key)

    def test_missing_private_key_raises(self):
        with self.assertRaises(GitHubTokenError):
            GitHubAppTokenProvider(
                app_id="111",
                installation_id="222",
                private_key_path=self.dir / "does_not_exist.pem",
            )

    def test_missing_installation_id_raises(self):
        key = self._make_key()
        with self.assertRaises(GitHubTokenError):
            GitHubAppTokenProvider(
                app_id="111",
                installation_id="",
                private_key_path=key,
            )

    def test_missing_app_id_raises(self):
        key = self._make_key()
        with self.assertRaises(GitHubTokenError):
            GitHubAppTokenProvider(
                app_id="",
                installation_id="222",
                private_key_path=key,
            )


if __name__ == "__main__":
    unittest.main()

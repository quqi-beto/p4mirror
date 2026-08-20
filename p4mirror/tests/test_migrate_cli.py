"""Regression tests for the ``migrate.py`` credential fail-fast check.

A run with **no** GitHub credential source must exit immediately with a
clear message instead of failing later at an authenticated git/GitHub call.
A run with any valid source (private key, --github-token, GH_TOKEN, or
GITHUB_TOKEN) must proceed past the check.
"""

from __future__ import annotations

import io
import sys
import unittest
from unittest import mock

import migrate


class FailFastNoCredentialTest(unittest.TestCase):
    """migrate.py main() must fail fast when no credential source is set."""

    def _run_main(self, argv: list[str]) -> tuple[int | None, str]:
        """Call ``migrate.main()`` capturing its exit code and stderr."""
        stderr = io.StringIO()
        with mock.patch.object(sys, "argv", argv), mock.patch(
            "sys.stderr", stderr
        ), mock.patch(
            "migrate.load_repository_config", return_value=object()
        ):
            try:
                migrate.main()
            except SystemExit as exc:
                return exc.code, stderr.getvalue()
        self.fail("migrate.main() did not exit")

    def test_migrate_no_credentials_fails_fast(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            code, err = self._run_main(["migrate.py", "migrate"])
        self.assertEqual(code, 1)
        self.assertIn("no GitHub credential source configured", err)

    def test_init_no_credentials_fails_fast(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            code, err = self._run_main(["migrate.py", "init"])
        self.assertEqual(code, 1)
        self.assertIn("no GitHub credential source configured", err)

    def test_missing_private_key_without_ids_still_fails(self):
        # A private key alone (without app/installation IDs) is a config
        # error, not "no credential source".
        with mock.patch.dict(
            "os.environ", {"GITHUB_PRIVATE_KEY_PATH": "C:/nope.pem"}, clear=True
        ):
            code, err = self._run_main(["migrate.py", "migrate"])
        self.assertEqual(code, 1)
        self.assertIn("requires --app-id and --installation-id", err)


class CredentialAcceptedTest(unittest.TestCase):
    """A run with any valid credential source proceeds past the check."""

    @staticmethod
    def _run_main(argv: list[str]) -> None:
        with mock.patch.object(sys, "argv", argv), mock.patch(
            "migrate.load_repository_config", return_value=object()
        ), mock.patch("migrate.load_user_mapping", return_value={}), mock.patch(
            "migrate.run_migration"
        ) as run_migration:
            migrate.main()
            return run_migration

    def test_gh_token_env_accepted(self):
        with mock.patch.dict("os.environ", {"GH_TOKEN": "ghs_x"}, clear=True):
            run_migration = self._run_main(["migrate.py", "migrate"])
        run_migration.assert_called_once()

    def test_github_token_env_accepted(self):
        with mock.patch.dict("os.environ", {"GITHUB_TOKEN": "ghs_y"}, clear=True):
            run_migration = self._run_main(["migrate.py", "migrate"])
        run_migration.assert_called_once()

    def test_github_token_flag_accepted(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            run_migration = self._run_main(
                ["migrate.py", "migrate", "--github-token", "ghs_z"]
            )
        run_migration.assert_called_once()


if __name__ == "__main__":
    unittest.main()

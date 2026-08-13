"""Regression tests for ``P4Client._parse_describe``.

The parser previously used ``//\\S+`` for the depot-path group, which silently
dropped any file whose path contained spaces (e.g. "Scanned Items Grid
Library.txt") from the changelist.  An empty file list made ``_match_affected_paths``
return ``[]`` and the changelist was skipped with a misleading "already covered"
message.  These tests pin the fixed behaviour.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from core.p4_client import P4Client


def _make_client() -> P4Client:
    return P4Client(
        p4_port="ssl:server:1666",
        p4_user="user",
        p4_client="client",
        p4_repository="repo",
        workspace_root=Path("."),
    )


class ParseDescribeTest(unittest.TestCase):
    def _parse(self, text: str):
        return _make_client()._parse_describe(text, cl_id=1234)

    def test_plain_path(self):
        text = (
            "Change 1234 by user@client on 2026/08/13 10:00:00\n\n"
            "\tFix the thing\n\n"
            "Affected files ...\n\n"
            "... //QA/robot_3/projects/foo.bat#2 edit\n"
        )
        cl = self._parse(text)
        self.assertEqual(len(cl.files), 1)
        self.assertEqual(cl.files[0].path, "//QA/robot_3/projects/foo.bat")
        self.assertEqual(cl.files[0].rev, 2)
        self.assertEqual(cl.files[0].action, "edit")

    def test_path_with_spaces(self):
        text = (
            "Change 1234 by user@client on 2026/08/13 10:00:00\n\n"
            "\tFix the thing\n\n"
            "Affected files ...\n\n"
            "... //QA/robot_3/projects/webclient-main/implementation/resources/"
            "libraries/Scanned Items Grid Library.txt#3 edit\n"
        )
        cl = self._parse(text)
        self.assertEqual(len(cl.files), 1)
        self.assertEqual(
            cl.files[0].path,
            "//QA/robot_3/projects/webclient-main/implementation/resources/"
            "libraries/Scanned Items Grid Library.txt",
        )
        self.assertEqual(cl.files[0].rev, 3)
        self.assertEqual(cl.files[0].action, "edit")

    def test_multiple_files_mixed_spaces(self):
        text = (
            "Change 1234 by user@client on 2026/08/13 10:00:00\n\n"
            "\tMixed\n\n"
            "Affected files ...\n\n"
            "... //QA/a/b/plain.bat#1 add\n"
            "... //QA/a/b/Some File With Spaces.txt#2 edit\n"
            "... //QA/a/b/plain2.bat#3 delete\n"
        )
        cl = self._parse(text)
        self.assertEqual(len(cl.files), 3)
        self.assertEqual(
            [f.path for f in cl.files],
            [
                "//QA/a/b/plain.bat",
                "//QA/a/b/Some File With Spaces.txt",
                "//QA/a/b/plain2.bat",
            ],
        )
        self.assertEqual([f.rev for f in cl.files], [1, 2, 3])
        self.assertEqual([f.action for f in cl.files], ["add", "edit", "delete"])

    def test_integrate_line_with_from_suffix(self):
        text = (
            "Change 1234 by user@client on 2026/08/13 10:00:00\n\n"
            "\tIntegrate\n\n"
            "Affected files ...\n\n"
            "... //QA/target/My File.txt#3 integrate (from //depot/source/Other.txt#2)\n"
        )
        cl = self._parse(text)
        self.assertEqual(len(cl.files), 1)
        self.assertEqual(cl.files[0].path, "//QA/target/My File.txt")
        self.assertEqual(cl.files[0].rev, 3)
        self.assertEqual(cl.files[0].action, "integrate")


if __name__ == "__main__":
    unittest.main()

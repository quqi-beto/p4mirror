"""Wrapper around the Perforce CLI (``p4.exe``).

Executes ``p4`` commands, parses their text output, and returns
structured Python objects.
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from core.changelist import ChangedFile, Changelist


class P4Error(Exception):
    """Raised when a ``p4`` command exits with a non-zero status."""


# Regex to parse a single ``p4 changes`` output line.
#   Change 58321 on 2026/06/18 by john@workspace 'Fixed login timeout'
_RE_CHANGE = re.compile(
    r"^Change\s+(?P<cl>\d+)\s+"
    r"on\s+(?P<date>\S+)\s+"
    r"by\s+(?P<user>\S+)@\S+\s+"
    r"'(?P<desc>.*)'$",
    re.MULTILINE,
)


class P4Client:
    """Thin wrapper around the ``p4`` command-line tool.

    Parameters
    ----------
    p4_port : str
        Perforce server address (``host:port``).
    p4_user : str
        Perforce username.
    p4_client : str
        Perforce workspace (client) name.
    workspace_root : str or Path
        Local filesystem root where the Perforce workspace is synced.
        Used as the working directory for all ``p4`` commands.
    p4_executable : str or Path
        Path to ``p4.exe``.  Defaults to ``"p4"`` (expect it on ``PATH``).
    """

    def __init__(
        self,
        p4_port: str,
        p4_user: str,
        p4_client: str,
        p4_repository: str,
        workspace_root: str | Path,
        p4_executable: str | Path = "p4",
    ) -> None:
        self._p4 = p4_executable
        self._port = p4_port
        self._user = p4_user
        self._client = p4_client
        self._repository = p4_repository
        self._root = Path(workspace_root)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_command(self, *args: str) -> str:
        """Execute a raw ``p4`` command and return its stdout.

        Parameters
        ----------
        *args
            Command arguments passed directly to ``p4``
            (e.g. ``"changes", "-s", "submitted", "//depot/..."``).

        Raises
        ------
        P4Error
            If ``p4`` exits with a non-zero status.
        """
        cmd = [
            str(self._p4),
            "-p", self._port,
            "-u", self._user,
            "-c", self._client,
            *args,
        ]
        result = subprocess.run(
            cmd,
            cwd=str(self._root),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise P4Error(
                f"p4 {' '.join(args)!r} failed (exit {result.returncode}):\n"
                f"{result.stderr.strip()}"
            )
        return result.stdout

    # -- Changelist discovery ----------------------------------------------

    def get_changelists(
        self,
        after_cl: int,
        depot_paths: list[str],
    ) -> list[int]:
        """Return sorted changelist numbers that affect *depot_paths* and
        are newer than *after_cl*.

        Results are sorted oldest-first.
        """
        seen: set[int] = set()

        for path in depot_paths:
            stdout = self.run_command(
                "changes", "-s", "submitted", f"{path}@>{after_cl}",
            )
            # Normalise Windows \r\n line endings so $ anchor works reliably
            stdout = stdout.replace("\r\n", "\n")
            for match in _RE_CHANGE.finditer(stdout):
                seen.add(int(match.group("cl")))

        return sorted(seen)

    # -- Changelist details ------------------------------------------------

    def get_changelist_details(self, cl_id: int) -> Changelist:
        """Fetch full metadata and file list for a single changelist.

        Uses ``p4 describe -s`` for metadata (omits diff content) and
        ``p4 describe`` for the file list.
        """
        stdout = self.run_command("describe", "-s", str(cl_id))
        return self._parse_describe(stdout, cl_id)

    # -- Sync workspace to a specific changelist --------------------------

    def sync(self, client_name: str, cl_id: int) -> None:
        """Sync the entire Perforce workspace to a specific changelist.

        This ensures the workspace reflects exactly the state of that
        single changelist across all mapped depot paths.
        """
        self.run_command("-c", client_name, "sync", f"//...@{cl_id}")

    def sync_path(self, depot_path: str, cl_id: int) -> None:
        """Sync a single *depot_path* to a specific changelist.

        Only files under *depot_path* are updated; other paths in the
        workspace remain at their current synced revision.  This is used
        for per-gitPath migration so that each path can progress
        independently.

        Parameters
        ----------
        client_name : str
            Perforce client (workspace) name.
        depot_path : str
            Depot path to sync (e.g. ``"//RFB/AppA/..."``).
        cl_id : int
            Changelist number to sync to.
        """
        result = self.run_command("sync", f"{depot_path}@{cl_id}")
        print(f"Sync result for {depot_path}@{cl_id}:\n{result}")

    # -- Client workspace management ---------------------------------------

    def ensure_client_workspace(
        self,
        view_mappings: list[str],
        description: str = "Created by P4Mirror",
    ) -> None:
        """Create or update the Perforce client workspace spec.

        Builds a complete client workspace specification from the provided
        parameters and submits it via ``p4 client -i``.  This is idempotent
        — if the client already exists it will be updated to match the
        desired spec.

        Parameters
        ----------
        workspace_root : str
            Absolute local filesystem path for the workspace ``Root:`` field.
        view_mappings : list of (depot_path, client_path) tuples
            Each tuple maps a Perforce depot path (e.g. ``//RFB/AppA/...``)
            to a client-relative path (e.g. ``AppA``).  These become the
            ``View:`` lines.
        description : str
            Optional description text for the client spec.

        Raises
        ------
        P4Error
            If the ``p4 client -i`` command fails.
        """
        spec_lines = [
            f"Client:\t{self._client}",
            "",
            f"Owner:\t{self._user}",
            "",
            "Description:",
            f"\t{description}",
            "",
            f"Root:\t{self.get_workspace_root()}",
            "",
            "Options:\tnoallwrite clobber nocompress unlocked nomodtime normdir",
            "",
            "LineEnd:\tlocal",
            "",
            "View:",
        ]
        
        for depot_name in view_mappings:
            spec_lines.append(
                f"\t//{depot_name}/... //{self._client}/{depot_name}/..."
            )

        spec_lines.append("")  # trailing newline

        spec = "\n".join(spec_lines)

        cmd = [
            str(self._p4),
            "-p", self._port,
            "-u", self._user,
            "client", "-i",
        ]
        result = subprocess.run(
            cmd,
            input=spec,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise P4Error(
                f"Failed to create/update client workspace "
                f"{self._client!r}:\n{result.stderr.strip()}"
            )

    # -- User info ---------------------------------------------------------

    # -- Remove repository name from the workspace_root to get the workspace root
    def get_workspace_root(self) -> Path:
        """Return the root of the Perforce workspace.

        This is derived from the configured workspace_root by removing
        the repository name, which is appended to the workspace_root
        during initialization.
        """
        return Path(self._root).parent

    def get_user_email(self, p4_user: str) -> str:
        """Fetch the email address of a Perforce user via ``p4 user -o``."""
        stdout = self.run_command("user", "-o", p4_user)
        for line in stdout.splitlines():
            if line.startswith("Email:"):
                return line.split(":", 1)[1].strip()
        return ""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_describe(self, text: str, cl_id: int) -> Changelist:
        """Parse ``p4 describe -s`` output into a :class:`Changelist`.

        Expected format::

            Change 58321 by john@workspace on 2026/06/18 09:32:14

                    Fixed login timeout.

            Affected files ...

            ... //depot/path/file.cpp#3 edit

        """
        lines = text.splitlines()

        # --- First line: metadata ---
        if not lines:
            raise P4Error(f"Empty output from p4 describe for CL {cl_id}")

        meta_match = re.match(
            r"^Change\s+(?P<cl>\d+)\s+by\s+(?P<user>\S+)@\S+\s+"
            r"on\s+(?P<date>\S+)\s+(?P<time>\S+)",
            lines[0],
        )
        if not meta_match:
            raise P4Error(
                f"Unexpected format from p4 describe for CL {cl_id}:\n{text}"
            )

        author = meta_match.group("user")
        raw_dt = f"{meta_match.group('date')} {meta_match.group('time')}"
        try:
            timestamp = datetime.strptime(raw_dt, "%Y/%m/%d %H:%M:%S")
        except ValueError:
            # Fallback: just use the date portion
            timestamp = datetime.strptime(meta_match.group("date"), "%Y/%m/%d")

        # --- Description: indented lines after the first blank line ---
        description = ""
        in_desc = False
        desc_lines: list[str] = []
        for line in lines[1:]:
            stripped = line.strip()
            if not in_desc and stripped == "":
                in_desc = True
                continue
            if in_desc:
                if stripped.startswith("Affected") or stripped.startswith("..."):
                    break
                if stripped:
                    desc_lines.append(stripped)

        description = "\n".join(desc_lines)

        # --- Files: lines starting with "... //" ---
        files: list[ChangedFile] = []
        for line in lines:
            stripped = line.strip()
            file_match = re.match(
                r"^\.\.\.\s+(?P<path>//\S+)#\d+\s+(?P<action>\S+)",
                stripped,
            )
            if file_match:
                files.append(
                    ChangedFile(
                        path=file_match.group("path"),
                        action=file_match.group("action"),
                        depot_file=file_match.group("path"),
                    )
                )

        return Changelist(
            cl_id=cl_id,
            author=author,
            timestamp=timestamp,
            description=description,
            files=files,
        )

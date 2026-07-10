"""Wrapper around the Git CLI for P4Mirror.

Handles staging, committing (with metadata), pushing, pulling, and
GitHub token-based authentication.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Mapping


# Regex for git-p4 changelist marker in commit messages.
# Format: [git-p4: depot-paths = "//REPOSITORY/": change = 280791]
_RE_GIT_P4 = re.compile(
    r"\[git-p4:\s+depot-paths\s*=\s*\"(?P<depot_paths>[^\"]+)\":\s+change\s*=\s*(?P<cl>\d+)\]"
)


class GitError(Exception):
    """Raised when a ``git`` command exits with a non-zero status."""


class GitHubAPIError(Exception):
    """Raised when the GitHub API returns a non-2xx status or the request fails."""


def _parse_repo_full_name(github_url: str) -> str:
    """Extract ``{owner}/{repo}`` from a GitHub URL.

    Examples
    --------
    >>> _parse_repo_full_name("https://github.com/company/ApplicationA.git")
    'company/ApplicationA'
    >>> _parse_repo_full_name("https://github.com/company/ApplicationA")
    'company/ApplicationA'
    """
    url = github_url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    return urllib.parse.urlparse(url).path.strip("/")


class GitClient:
    """High-level Git operations for migration.

    Parameters
    ----------
    workspace_root : str or Path
        Root of the local Git repository.
    default_branch : str
        Branch to push/pull against (e.g. ``"main"``).
    """

    def __init__(
        self,
        workspace_root: str | Path,
        default_branch: str = "main",
    ) -> None:
        self._root = Path(workspace_root)
        self._branch = default_branch

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def force_sync_to_remote(self) -> None:
        """Force local branch, index, and working tree to match the remote.

        Runs the following commands inside the repository:

        #. ``git fetch origin`` — downloads latest remote refs.
        #. ``git reset --hard origin/{branch}`` — resets the branch pointer
           and **discards all staged and unstaged local changes**.
        #. ``git clean -fd`` — removes untracked files and directories.

        After this call the working tree is an exact copy of the remote
        branch.  **Uncommitted local changes are permanently lost.**
        """
        self._run("fetch", "origin")
        self._run("reset", "--hard", f"origin/{self._branch}")
        self._run("clean", "-fd")

    def stage_all(self) -> None:
        """Stage all changes (adds, modifications, deletes) via ``git add -A``."""
        self._run("add", "-A")

    def commit(
        self,
        author_name: str,
        author_email: str,
        timestamp: datetime,
        message: str,
    ) -> None:
        """Create a Git commit with preserved Perforce metadata.

        Parameters
        ----------
        author_name : str
            Git author name (mapped from Perforce user).
        author_email : str
            Git author email (mapped from Perforce user).
        timestamp : datetime
            Original Perforce changelist timestamp.
        message : str
            Commit message (Perforce changelist description).
        """
        env = self._git_env(author_name, author_email, timestamp)
        self._run("commit", f"--message={message}", extra_env=env)

    def push(self) -> None:
        """Push commits to ``origin/{branch}``."""
        self._run("push", "origin", self._branch)

    # -- GitHub token authentication --------------------------------------

    def configure_github_auth(self, token: str, remote_url: str | None = None) -> None:
        """Configure the origin remote to use a GitHub token for authentication.

        Rewrites the remote URL so that all subsequent Git operations
        (fetch, pull, push) authenticate with the given token.

        For GitHub Apps the token is embedded as:
        ``https://x-access-token:{token}@{host}/{owner}/{repo}``

        .. note::

           Only ``https://`` URLs are rewritten — ``git@`` (SSH) and local
           ``file://`` paths are left untouched since they do not support
           token-based authentication.

        Parameters
        ----------
        token : str
            GitHub token (JWT for a GitHub App, or a personal access token).
        remote_url : str or None
            The full remote URL. If ``None``, reads it from Git
            (``git remote get-url origin``).
        """
        if remote_url is None:
            remote_url = self._run("remote", "get-url", "origin").strip()

        # Only inject token into HTTPS URLs; SSH and local paths are
        # left as-is since they don't support token auth.
        if remote_url.startswith("https://"):
            scheme, rest = remote_url.split("://", 1)
            auth_url = f"{scheme}://x-access-token:{token}@{rest}"
            self._run("remote", "set-url", "origin", auth_url)
        # else: SSH (git@...) or local path → no change needed

    # -- Git history scan (state fallback) --------------------------------

    def scan_last_p4_cl(
        self,
        git_paths: list[str],
        github_token: str,
        repo_full_name: str,
    ) -> dict[str, int]:
        """Scan GitHub commit history for the latest P4 changelist per gitPath.

        Uses the GitHub Commits API (``GET /repos/{owner}/{repo}/commits``)
        with a ``path`` filter so only commits that actually touched files
        under *git_paths* are examined.  For each path up to 30 commits are
        fetched; the first one carrying a ``[git-p4: ... change = N]`` marker
        is used.

        Returns a dict mapping each gitPath to its latest P4 changelist
        found in the commit history.  Paths with no git-p4 marker are
        omitted from the result.

        Parameters
        ----------
        git_paths : list[str]
            Git paths (directories) to filter commits by.
        github_token : str
            GitHub API token (PAT or installation token).
        repo_full_name : str
            Repository full name, e.g. ``"company/ApplicationA"``.

        Raises
        ------
        GitHubAPIError
            If the API request fails or returns a non-2xx status.
        """
        result: dict[str, int] = {}
        headers = {
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github+json",
        }

        for gp in git_paths:
            params = urllib.parse.urlencode({
                "path": gp,
                "sha": self._branch,
                "per_page": "30",
            })
            url = f"https://api.github.com/repos/{repo_full_name}/commits?{params}"

            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data: list[dict] = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                raise GitHubAPIError(
                    f"GitHub API returned {exc.code} for path {gp!r}: {body}"
                ) from exc
            except (urllib.error.URLError, OSError) as exc:
                raise GitHubAPIError(
                    f"GitHub API request failed for path {gp!r}: {exc}"
                ) from exc

            # Iterate newest-first; pick the first commit with a git-p4 marker
            for item in data:
                commit_msg = item.get("commit", {}).get("message", "")
                m = _RE_GIT_P4.search(commit_msg)
                if m:
                    cl = int(m.group("cl"))
                    print(f"Found git-p4 marker for path {gp!r}: CL {cl}")
                    result[gp] = cl
                    break  # newest match for this path

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run(self, *args: str, extra_env: Mapping[str, str] | None = None) -> str:
        """Execute a ``git`` command inside the workspace root.

        Parameters
        ----------
        *args
            Git subcommand and arguments (e.g. ``"add", "-A"``).
        extra_env
            Optional environment variables to set for this invocation.

        Raises
        ------
        GitError
            If the command exits with a non-zero status.
        """
        cmd = ["git", *args]

        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)

        result = subprocess.run(
            cmd,
            cwd=str(self._root),
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        if result.returncode != 0:
            raise GitError(
                f"git {' '.join(args)!r} failed (exit {result.returncode}):\n"
                f"{result.stderr.strip()}"
            )
        return result.stdout

    @staticmethod
    def _git_env(
        author_name: str,
        author_email: str,
        timestamp: datetime,
    ) -> dict[str, str]:
        """Build environment variables for a metadata-preserving commit.

        Both ``GIT_AUTHOR_*`` and ``GIT_COMMITTER_*`` are set to the
        same values so that ``git log`` shows the original Perforce
        author and date.
        """
        date_str = timestamp.strftime("%Y-%m-%dT%H:%M:%S")
        return {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_AUTHOR_DATE": date_str,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
            "GIT_COMMITTER_DATE": date_str,
        }

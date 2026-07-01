"""Workspace operations for P4Mirror.

Validates the local workspace directory, initialises the Git repository,
configures sparse checkout, and provides cleanup helpers.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from config import RepositoryConfig


class WorkspaceError(Exception):
    """Raised when a workspace operation fails."""


def validate_workspace(config: RepositoryConfig) -> Path:
    """Check that the configured workspace root exists and is a directory.

    Returns
    -------
    Path
        The resolved workspace root path.

    Raises
    ------
    WorkspaceError
        If the path does not exist or is not a directory.
    """
    root = Path(config.workspace_root).resolve()
    if not root.exists():
        raise WorkspaceError(
            f"Workspace root does not exist: {root}"
        )
    if not root.is_dir():
        raise WorkspaceError(
            f"Workspace root is not a directory: {root}"
        )
    return root


def ensure_workspace(config: RepositoryConfig) -> Path:
    """Ensure the workspace root directory exists, creating it if needed.

    Creates the full directory path if it does not already exist.
    This is used during ``p4mirror init`` to bootstrap a fresh workspace.

    Returns
    -------
    Path
        The resolved workspace root path.

    Raises
    ------
    WorkspaceError
        If the path exists but is not a directory, or cannot be created.
    """
    root = Path(config.workspace_root).resolve()
    if root.exists():
        if not root.is_dir():
            raise WorkspaceError(
                f"Workspace root is not a directory: {root}"
            )
        return root

    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkspaceError(
            f"Cannot create workspace directory {root}: {exc}"
        ) from exc
    return root


def init_git_repo(workspace_root: Path, github_url: str) -> None:
    """Initialise a Git repository if one does not already exist.

    If ``.git`` is missing, runs ``git init`` and adds *github_url*
    as the ``origin`` remote.
    """
    git_dir = workspace_root / ".git"
    if git_dir.exists():
        return  # already a repository

    _run_git(workspace_root, "init")
    _run_git(workspace_root, "remote", "add", "origin", github_url)


def setup_sparse_checkout(
    workspace_root: Path,
    git_paths: list[str],
) -> None:
    """Enable Git sparse checkout and set the cone patterns.

    Parameters
    ----------
    workspace_root : Path
        Root of the local Git repository.
    git_paths : list of str
        Relative Git paths to include in the sparse checkout
        (e.g. ``["AppA", "AppC"]``).
    """
    _run_git(workspace_root, "sparse-checkout", "init", "--cone")
    for git_path in git_paths:
        _run_git(workspace_root, "sparse-checkout", "add", git_path)


def clean_workspace(workspace_root: Path) -> None:
    """Remove untracked files and directories from the workspace.

    Runs ``git clean -fd`` to ensure no stale files remain between
    changelist syncs.
    """
    _run_git(workspace_root, "clean", "-fd")


def detect_modified_files(workspace_root: Path) -> list[str]:
    """Return a list of files with uncommitted changes.

    Uses ``git status --porcelain``.
    """
    stdout = _run_git(workspace_root, "status", "--porcelain")
    return [line.strip() for line in stdout.splitlines() if line.strip()]


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _run_git(workspace_root: Path, *args: str) -> str:
    """Run a ``git`` command inside *workspace_root*.

    Raises
    ------
    WorkspaceError
        If the command exits with a non-zero status.
    """
    result = subprocess.run(
        ["git", *args],
        cwd=str(workspace_root),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise WorkspaceError(
            f"git {' '.join(args)!r} failed (exit {result.returncode}):\n"
            f"{result.stderr.strip()}"
        )
    return result.stdout

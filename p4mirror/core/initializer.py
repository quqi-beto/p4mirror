"""One-time workspace initialisation for P4Mirror.

Bootstraps a fresh workspace by cloning the GitHub repository (with sparse
checkout for the mapped paths), scanning git-p4 markers to find the last
Perforce changelist already in GitHub, and writing the initial state file.

After ``p4mirror init`` completes, subsequent ``p4mirror migrate`` runs
work incrementally from the discovered baseline changelist.
"""

from __future__ import annotations

import os
from pathlib import Path

from config import RepositoryConfig
from core.git_client import GitClient, GitError, GitHubAPIError, _parse_repo_full_name
from core.logger import P4MirrorLogger
from core.p4_client import P4Client, P4Error
from core.state_manager import StateManager, StateError
from core.workspace import (
    WorkspaceError,
    ensure_workspace,
    init_git_repo,
    setup_sparse_checkout,
)


class InitError(Exception):
    """Raised when workspace initialisation fails."""


def run_init(
    config: RepositoryConfig,
    *,
    github_token: str | None = None,
    log_dir: str | Path = "logs",
    state_dir: str | Path = "state",
) -> None:
    """Execute one-time workspace initialisation.

    Parameters
    ----------
    config : RepositoryConfig
        Repository migration configuration.
    github_token : str or None
        GitHub token (JWT or PAT) for authenticated Git operations.
        Falls back to the ``GITHUB_TOKEN`` environment variable if not
        provided.
    log_dir : str or Path
        Directory for log files.
    state_dir : str or Path
        Directory for the state file.

    Raises
    ------
    InitError
        On any fatal error during initialisation.
    """
    if github_token is None:
        github_token = os.environ.get("GITHUB_TOKEN")
    logger = P4MirrorLogger(log_dir=log_dir)
    logger.start()

    errors: list[str] = []

    try:
        _run_init_impl(
            config=config,
            logger=logger,
            state_dir=state_dir,
            github_token=github_token,
            errors=errors,
        )
    except InitError:
        # Already logged inside the implementation.
        pass
    except Exception as exc:
        msg = f"Unexpected error: {exc}"
        logger.error(msg)
        errors.append(msg)
    finally:
        logger.close_summary(
            changelists=0,
            commits=0,
            push_ok=(len(errors) == 0),
            errors=errors or None,
        )

    if errors:
        raise InitError("Initialisation failed — see log for details.")


# ------------------------------------------------------------------
# Internal implementation
# ------------------------------------------------------------------


def _run_init_impl(
    config: RepositoryConfig,
    logger: P4MirrorLogger,
    state_dir: str | Path,
    github_token: str | None,
    errors: list[str],
) -> None:
    """Internal init logic — extracted for clean error handling."""

    git_paths = [m.git_path for m in config.path_mappings]

    # -- 1. Ensure workspace directory exists ----------------------------
    logger.info("Ensuring workspace directory ...")
    try:
        workspace_root = ensure_workspace(config)
    except WorkspaceError as exc:
        logger.error(str(exc))
        errors.append(str(exc))
        raise InitError() from exc

    # -- 1b. Ensure P4 client workspace exists --------------------------
    logger.info(
        f"Ensuring P4 client workspace '{config.p4_client}' ..."
    )
    p4 = P4Client(
        p4_port=config.p4_port,
        p4_user=config.p4_user,
        p4_client=config.p4_client,
        p4_repository=config.repository_name
    )
    view_mappings = config.view_mappings
    try:
        p4.ensure_client_workspace(
            workspace_root=config.workspace_root,
            view_mappings=view_mappings,
        )
    except P4Error as exc:
        logger.error(str(exc))
        errors.append(str(exc))
        raise InitError() from exc

    # -- 2. Initialise Git repo -----------------------------------------
    logger.info("Initialising Git repository ...")
    try:
        init_git_repo(workspace_root, config.github_url, github_token)
    except WorkspaceError as exc:
        logger.error(str(exc))
        errors.append(str(exc))
        raise InitError() from exc

    # -- 3. Configure sparse checkout ------------------------------------
    if config.sparse_checkout:
        logger.info(f"Setting up sparse checkout for: {git_paths}")
        try:
            setup_sparse_checkout(workspace_root, git_paths)
        except WorkspaceError as exc:
            logger.error(str(exc))
            errors.append(str(exc))
            raise InitError() from exc

    # -- 4. Scan git-p4 markers for baseline CL -------------------------
    logger.info("Scanning Git history for last Perforce changelist ...")
    git = GitClient(
        workspace_root=workspace_root,
        default_branch=config.default_branch,
    )
    try:
        repo_full = _parse_repo_full_name(config.github_url)
        scanned_cl = git.scan_last_p4_cl(
            git_paths,
            github_token=github_token,
            repo_full_name=repo_full,
        )
    except (GitError, GitHubAPIError) as exc:
        logger.error(str(exc))
        errors.append(str(exc))
        raise InitError() from exc

    if scanned_cl is None:
        msg = (
            "No git-p4 markers found in the repository history. "
            "Cannot determine the baseline Perforce changelist. "
            f"If you know the initial CL, set it manually in "
            f"state/state_{config.repository_name}.json "
            "and run 'p4mirror migrate'."
        )
        logger.error(msg)
        errors.append(msg)
        raise InitError() from None

    logger.info(f"Baseline changelist determined: {scanned_cl}")

    # -- 5. Write state file --------------------------------------------
    logger.info("Writing state file ...")
    try:
        state_mgr = StateManager(
            repository_name=config.repository_name,
            state_dir=state_dir,
        )
        state_mgr.write(
            scanned_cl,
            repository=config.repository_name,
            branch=config.default_branch,
        )
    except StateError as exc:
        logger.error(str(exc))
        errors.append(str(exc))
        raise InitError() from exc

    logger.info("Initialisation complete.")

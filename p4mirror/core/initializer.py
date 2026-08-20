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
from core.github_auth import GitHubAppTokenProvider, GitHubTokenError
from core.logger import P4MirrorLogger
from core.p4_client import P4Client, P4Error
from core.state_manager import PathState, State, StateManager, StateError
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
    token_provider: GitHubAppTokenProvider | None = None,
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
        provided.  Ignored when *token_provider* is set.
    token_provider : GitHubAppTokenProvider or None
        When set, a **fresh** installation token is minted for this run
        (a cached/stale token is never reused).
    log_dir : str or Path
        Directory for log files.
    state_dir : str or Path
        Directory for the state file.

    Raises
    ------
    InitError
        On any fatal error during initialisation.
    """
    logger = P4MirrorLogger(log_dir=log_dir)
    logger.start()

    errors: list[str] = []

    try:
        if token_provider is not None:
            try:
                github_token = token_provider.get_token()
            except GitHubTokenError as exc:
                msg = f"Failed to generate a fresh GitHub token: {exc}"
                logger.error(msg)
                errors.append(msg)
                raise InitError() from exc
        elif github_token is None:
            github_token = os.environ.get("GITHUB_TOKEN")

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

    git_paths = [m.git_path for m in config.path_mappings if m.git_path]
    # Root dynamic mappings use git_path "" (repo-root mirror) — excluded
    # from the GitHub history scan (no meaningful path filter); their
    # baseline comes from baseline_cl seeding below.
    # Dynamic mappings are never materialized (no-checkout commits), so they
    # are excluded from sparse checkout to avoid wasting local storage.
    sparse_paths = [m.git_path for m in config.path_mappings if not m.dynamic]

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
        p4_repository=config.repository_name,
        workspace_root=config.workspace_root,
    )
    view_mappings = config.view_mappings
    try:
        p4.ensure_client_workspace(
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
        logger.info(f"Setting up sparse checkout for: {sparse_paths}")
        try:
            setup_sparse_checkout(workspace_root, sparse_paths)
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
        path_cls = git.scan_last_p4_cl(
            git_paths,
            github_token=github_token,
            repo_full_name=repo_full,
        )
    except (GitError, GitHubAPIError) as exc:
        logger.error(str(exc))
        errors.append(str(exc))
        raise InitError() from exc

    # Ensure every configured path has a baseline.  Paths without a git-p4
    # marker default to 0 (the first migration discovers all CLs), except
    # for dynamic mappings which may seed a configured ``baseline_cl`` so a
    # depot can be adopted without backfilling its entire history.
    #
    # This runs even when *no* markers were found: an empty scan must not
    # abort init before the configured ``baseline_cl`` gets a chance to
    # seed state (e.g. a fresh GitHub repo, or adopting a dynamic depot
    # with ``baseline_cl`` set).
    state_paths: dict[str, PathState] = {}
    for m in config.path_mappings:
        gp = m.git_path
        cl = path_cls.get(gp, 0)
        if cl == 0 and m.dynamic and m.baseline_cl > 0:
            cl = m.baseline_cl
            logger.info(
                f"  {gp}: no git-p4 marker; using configured baseline_cl {cl}"
            )
        state_paths[gp] = PathState(last_migrated_cl=cl)
        if cl:
            logger.info(f"  {gp}: baseline CL {cl}")
        else:
            logger.info(f"  {gp}: no git-p4 marker found, will start from CL 0")

    if not path_cls:
        # No git-p4 markers found anywhere in the repository history.  This
        # is expected and safe for:
        #   * a brand-new/empty GitHub repository (nothing migrated yet), or
        #   * dynamic mappings being adopted via ``baseline_cl``, or
        #     deliberately from CL 0 ("start from the beginning").
        # It is only fatal when a *static* mapping has no baseline AND the
        # remote already contains commits: without a marker we cannot tell
        # how much history is already in GitHub, so backfilling from CL 0
        # would create duplicate commits.
        if not git.is_branch_empty() and not all(
            m.dynamic for m in config.path_mappings
        ):
            msg = (
                "No git-p4 markers found in the repository history and the "
                "repository already contains commits, so the baseline "
                "Perforce changelist cannot be determined safely. "
                "If you know the initial CL, set it manually in "
                f"state/state_{config.repository_name}.json "
                "and run 'p4mirror migrate'."
            )
            logger.error(msg)
            errors.append(msg)
            raise InitError() from None

        logger.warning(
            "No git-p4 markers found in the repository history. "
            "Starting each path from CL 0 or its configured baseline_cl; "
            "the first migration will backfill from there."
        )

    # -- 5. Write state file --------------------------------------------
    logger.info("Writing state file ...")
    try:
        state_mgr = StateManager(
            repository_name=config.repository_name,
            state_dir=state_dir,
        )
        state_mgr.write(State(
            paths=state_paths,
            repository=config.repository_name,
            branch=config.default_branch,
        ))
    except StateError as exc:
        logger.error(str(exc))
        errors.append(str(exc))
        raise InitError() from exc

    # -- 6. Sync each P4 path to its baseline CL ------------------------
    # Dynamic mappings are skipped — their files are never synced to the
    # local workspace (no-checkout commits fetch content via p4 print).
    logger.info("Syncing P4 workspace to baseline changelists ...")
    path_baselines: dict[str, int] = {}
    for mapping in config.path_mappings:
        if mapping.dynamic:
            continue
        baseline_cl = state_paths.get(mapping.git_path, PathState(last_migrated_cl=0)).last_migrated_cl
        if baseline_cl > 0:
            path_baselines[mapping.p4_path] = baseline_cl
    if path_baselines:
        try:
            p4.sync_paths_to_baseline(path_baselines)
            logger.info(f"P4 workspace synced to baselines: {path_baselines}")
        except P4Error as exc:
            logger.error(f"P4 baseline sync failed: {exc}")
            errors.append(str(exc))
            raise InitError() from exc
    else:
        logger.info("  No baselines to sync (all CLs are 0).")

    logger.info("Initialisation complete.")

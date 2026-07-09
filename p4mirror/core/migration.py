"""Core migration business logic.

Orchestrates the end-to-end flow: discover changelists, sync Perforce
workspace, stage in Git, commit with preserved metadata, push, and
update state.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from config import RepositoryConfig, UserMapping
from core.changelist import Changelist
from core.git_client import GitClient, GitError, GitHubAPIError, _parse_repo_full_name
from core.logger import P4MirrorLogger
from core.p4_client import P4Client, P4Error
from core.state_manager import StateManager, StateError
from core.workspace import (
    WorkspaceError,
    clean_workspace,
    init_git_repo,
    setup_sparse_checkout,
    validate_workspace,
)


class MigrationError(Exception):
    """Raised when the migration process must stop."""


def run_migration(
    config: RepositoryConfig,
    user_mapping: dict[str, UserMapping],
    *,
    github_token: str | None = None,
    build_number: int | None = None,
    log_dir: str | Path = "logs",
    state_dir: str | Path = "state",
) -> None:
    """Execute one full migration cycle.

    Parameters
    ----------
    config : RepositoryConfig
        Repository migration configuration.
    user_mapping : dict[str, UserMapping]
        Perforce username → Git author mapping.
    build_number : int or None
        Optional Jenkins build number for logging.
    log_dir : str or Path
        Directory for log files.
    state_dir : str or Path
        Directory for the state file.

    Raises
    ------
    MigrationError
        On any fatal error during the migration process.
    """
    if github_token is None:
        github_token = os.environ.get("GITHUB_TOKEN")

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------
    logger = P4MirrorLogger(log_dir=log_dir, build_number=build_number)
    logger.start()

    errors: list[str] = []
    changelists_processed = 0
    commits_created = 0

    try:
        changelists_processed, commits_created = _run_migration_impl(
            config=config,
            user_mapping=user_mapping,
            logger=logger,
            state_dir=state_dir,
            github_token=github_token,
            errors=errors,
        )
    except MigrationError:
        # Already logged inside the implementation; just finalise summary.
        pass
    except Exception as exc:
        msg = f"Unexpected error: {exc}"
        logger.error(msg)
        errors.append(msg)
    finally:
        logger.close_summary(
            changelists=changelists_processed,
            commits=commits_created,
            push_ok=(len(errors) == 0),
            errors=errors or None,
        )

    if errors:
        sys.exit(1)


def _run_migration_impl(
    config: RepositoryConfig,
    user_mapping: dict[str, UserMapping],
    logger: P4MirrorLogger,
    state_dir: str | Path,
    github_token: str | None,
    errors: list[str],
) -> tuple[int, int]:
    """Internal migration logic — extracted for clean error handling.

    Returns
    -------
    tuple of (changelists_processed, commits_created)
    """

    # -- 1. Validate workspace -------------------------------------------
    logger.info("Validating workspace ...")
    try:
        workspace_root = validate_workspace(config)
    except WorkspaceError as exc:
        logger.error(str(exc))
        errors.append(str(exc))
        raise MigrationError() from exc

    # -- 2. Initialise / verify Git repo ---------------------------------
    logger.info("Initialising Git repository (if needed) ...")
    try:
        init_git_repo(workspace_root, config.github_url, github_token)
    except WorkspaceError as exc:
        logger.error(str(exc))
        errors.append(str(exc))
        raise MigrationError() from exc

    # -- 3. Sparse checkout (optional) -----------------------------------
    if config.sparse_checkout:
        git_paths = [m.git_path for m in config.path_mappings]
        logger.info(f"Setting up sparse checkout for: {git_paths}")
        try:
            setup_sparse_checkout(workspace_root, git_paths)
        except WorkspaceError as exc:
            logger.error(str(exc))
            errors.append(str(exc))
            raise MigrationError() from exc

    # -- 4. Initialise clients -------------------------------------------
    p4 = P4Client(
        p4_port=config.p4_port,
        p4_user=config.p4_user,
        p4_client=config.p4_client,
        p4_repository=config.repository_name
    )
    git = GitClient(
        workspace_root=workspace_root,
        default_branch=config.default_branch,
    )
    if github_token:
        logger.info("Configuring GitHub authentication ...")
        try:
            git.configure_github_auth(github_token, config.github_url)
        except GitError as exc:
            logger.error(str(exc))
            errors.append(str(exc))
            raise MigrationError() from exc

    # -- 4b. Validate P4 client workspace exists -------------------------
    logger.info(f"Verifying P4 client workspace '{config.p4_client}' ...")
    try:
        p4.run_command("client", "-o", config.p4_client)
    except P4Error:
        msg = (
            f"P4 client workspace '{config.p4_client}' does not exist. "
            f"Run 'p4mirror init --config {Path.cwd() / 'config/repository.json'}' "
            f"first to create it."
        )
        logger.error(msg)
        errors.append(msg)
        raise MigrationError() from None

    # -- 5. Read migration state (with Git history fallback) -------------
    logger.info("Reading migration state ...")
    state_mgr = StateManager(
        repository_name=config.repository_name,
        state_dir=state_dir,
    )
    try:
        state = state_mgr.read()
        last_cl = state.last_migrated_cl
        logger.info(f"Last migrated changelist: {last_cl}")
    except StateError as exc:
        logger.info(f"State file not found: {exc}")
        logger.info("Falling back to scanning Git history for last P4 changelist ...")
        git_paths = [m.git_path for m in config.path_mappings]
        try:
            repo_full = _parse_repo_full_name(config.github_url)
            scanned_cl = git.scan_last_p4_cl(
                git_paths,
                github_token=github_token,
                repo_full_name=repo_full,
            )
            if scanned_cl is not None:
                last_cl = scanned_cl
                logger.info(f"Found last P4 changelist from Git history: {last_cl}")
                # Persist for future runs so the fallback is only needed once
                state_mgr.write(
                    scanned_cl,
                    repository=config.repository_name,
                    branch=config.default_branch,
                )
            else:
                raise MigrationError(
                    "No previous P4 changelist found in Git history "
                    "and no state file available."
                )
        except GitError as scan_err:
            logger.error(f"Git history scan failed: {scan_err}")
            errors.append(str(scan_err))
            raise MigrationError() from scan_err

    # -- 6. Fetch / pull latest Git changes ------------------------------
    logger.info("Fetching and pulling latest Git changes ...")
    try:
        git.fetch()
        git.pull_ff_only()
        git.checkout_branch()
    except GitError as exc:
        logger.error(str(exc))
        errors.append(str(exc))
        raise MigrationError() from exc

    # -- 7. Discover new changelists -------------------------------------
    depot_paths = [m.p4_path for m in config.path_mappings]
    logger.info(f"Querying Perforce for changes after CL {last_cl} ...")
    try:
        cl_ids = p4.get_changelists(after_cl=last_cl, depot_paths=depot_paths)
    except P4Error as exc:
        logger.error(str(exc))
        errors.append(str(exc))
        raise MigrationError() from exc

    if not cl_ids:
        logger.info("No new changelists to migrate.")
        return (0, 0)

    logger.info(f"Found {len(cl_ids)} new changelist(s): {cl_ids}")

    # -- 8. Process each changelist (oldest first) -----------------------
    for cl_id in cl_ids:
        changelists_processed += 1
        logger.info(f"Processing changelist {cl_id} ({changelists_processed}/{len(cl_ids)}) ...")

        try:
            _process_one_changelist(
                p4=p4,
                git=git,
                config=config,
                user_mapping=user_mapping,
                cl_id=cl_id,
                workspace_root=workspace_root,
                logger=logger,
            )
            commits_created += 1
        except (P4Error, GitError, WorkspaceError) as exc:
            msg = f"Failed at changelist {cl_id}: {exc}"
            logger.error(msg)
            errors.append(msg)
            # Stop immediately — do not update state
            raise MigrationError() from exc

    # -- 9. Push all commits ---------------------------------------------
    logger.info("Pushing commits to GitHub ...")
    try:
        git.push()
        logger.info("Push succeeded.")
    except GitError as exc:
        logger.error(f"Push failed: {exc}")
        errors.append(str(exc))
        raise MigrationError() from exc

    # -- 10. Update state ------------------------------------------------
    final_cl = cl_ids[-1]
    logger.info(f"Updating state to CL {final_cl} ...")
    try:
        state_mgr.write(
            final_cl,
            repository=config.repository_name,
            branch=config.default_branch,
        )
    except StateError as exc:
        logger.error(f"Failed to update state: {exc}")
        errors.append(str(exc))
        raise MigrationError() from exc

    logger.info(
        f"Migration complete: {changelists_processed} changelist(s) "
        f"processed, {commits_created} commit(s) created."
    )

    return (changelists_processed, commits_created)


# ------------------------------------------------------------------
# Single-changelist processing
# ------------------------------------------------------------------


def _process_one_changelist(
    p4: P4Client,
    git: GitClient,
    config: RepositoryConfig,
    user_mapping: dict[str, UserMapping],
    cl_id: int,
    workspace_root: Path,
    logger: P4MirrorLogger,
) -> None:
    """Fetch, sync, stage, and commit a single Perforce changelist."""

    # --- a. Fetch changelist metadata ---
    logger.info(f"  Fetching details for CL {cl_id} ...")
    cl: Changelist = p4.get_changelist_details(cl_id)

    # --- b. Sync workspace to this changelist ---
    logger.info(f"  Syncing workspace to CL {cl_id} ...")
    p4.sync(config.p4_client, cl_id)

    # --- c. Clean untracked files ---
    clean_workspace(workspace_root)

    # --- d. Stage all changes in Git ---
    logger.info(f"  Staging changes ...")
    git.stage_all()

    # --- e. Map Perforce user to Git author ---
    if cl.author in user_mapping:
        author_name = user_mapping[cl.author].name
        author_email = user_mapping[cl.author].email
    else:
        # Fallback: use the Perforce user name and try to fetch email
        author_name = cl.author
        try:
            author_email = p4.get_user_email(cl.author)
        except P4Error:
            author_email = f"{cl.author}@unknown"
        logger.info(
            f"  Warning: no mapping for Perforce user '{cl.author}'; "
            f"using fallback {author_name} <{author_email}>"
        )

    # --- f. Build commit message ---
    message = _build_commit_message(cl)

    # --- g. Create Git commit ---
    logger.info(f"  Creating commit: {author_name} <{author_email}> @ {cl.timestamp}")
    git.commit(
        author_name=author_name,
        author_email=author_email,
        timestamp=cl.timestamp,
        message=message,
    )
    logger.info(f"  Commit created for CL {cl_id}.")


def _build_commit_message(cl: Changelist) -> str:
    """Build the Git commit message from a changelist.

    Appends the Perforce changelist reference in brackets.
    """
    desc = cl.description.strip()
    if desc:
        return f"{desc}\n\n[P4 CL {cl.cl_id}]"
    return f"[P4 CL {cl.cl_id}]"

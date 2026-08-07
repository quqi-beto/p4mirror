"""Core migration business logic.

Orchestrates the end-to-end flow: discover changelists, sync Perforce
workspace, stage in Git, commit with preserved metadata, push, and
update state.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

from config import PathMapping, RepositoryConfig, UserMapping
from core.changelist import ChangedFile, Changelist
from core.git_client import GitClient, GitError, GitHubAPIError, _parse_repo_full_name
from core.logger import P4MirrorLogger
from core.p4_client import P4Client, P4Error
from core.state_manager import PathState, State, StateManager, StateError
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
    # Dynamic mappings are excluded: their files are never materialized in
    # the working tree (no-checkout commits), so adding them to the sparse
    # cone would only waste local storage.
    if config.sparse_checkout:
        git_paths = [m.git_path for m in config.path_mappings if not m.dynamic]
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
        p4_repository=config.repository_name,
        workspace_root=workspace_root,
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
        path_baselines = {gp: ps.last_migrated_cl for gp, ps in state.paths.items()}
        logger.info(f"Per-path baselines: {path_baselines}")
    except StateError as exc:
        logger.info(f"State file not found: {exc}")
        logger.info("Falling back to scanning Git history for last P4 changelist ...")
        git_paths = [m.git_path for m in config.path_mappings]
        try:
            repo_full = _parse_repo_full_name(config.github_url)
            path_cls = git.scan_last_p4_cl(
                git_paths,
                github_token=github_token,
                repo_full_name=repo_full,
            )
            if path_cls:
                logger.info(f"Found per-path baselines from Git history: {path_cls}")
                # Ensure every configured path has a baseline (fallback to 0
                # if a path has no git-p4 marker — it will discover all CLs
                # from scratch on the first migration run).
                state_paths: dict[str, PathState] = {}
                for m in config.path_mappings:
                    cl = path_cls.get(m.git_path, 0)
                    if cl == 0 and m.dynamic and m.baseline_cl > 0:
                        cl = m.baseline_cl
                    state_paths[m.git_path] = PathState(last_migrated_cl=cl)
                state = State(
                    paths=state_paths,
                    repository=config.repository_name,
                    branch=config.default_branch,
                )
                state_mgr.write(state)
            else:
                raise MigrationError(
                    "No previous P4 changelist found in Git history "
                    "and no state file available."
                )
        except GitError as scan_err:
            logger.error(f"Git history scan failed: {scan_err}")
            errors.append(str(scan_err))
            raise MigrationError() from scan_err

    # -- 6. Force-sync local state to remote ----------------------------
    logger.info(
        f"Force-syncing local workspace to remote origin/{config.default_branch} ...",
    )
    try:
        git.force_sync_to_remote()
    except GitError as exc:
        logger.error(str(exc))
        errors.append(str(exc))
        raise MigrationError() from exc

    # -- 7. Discover new changelists (per-path baselines) ----------------
    all_cl_ids: set[int] = set()
    for mapping in config.path_mappings:
        path_baseline = state.get_path_cl(mapping.git_path)
        # Fall back to the configured baseline_cl for dynamic mappings with
        # no state entry yet (e.g. after renaming git_path) so we never
        # accidentally re-migrate the whole depot history from CL 0.
        if path_baseline is None and mapping.dynamic and mapping.baseline_cl > 0:
            path_baseline = mapping.baseline_cl
        path_baseline = path_baseline or 0
        logger.info(
            f"Querying {mapping.git_path or '(root)'} ({mapping.p4_path}) "
            f"for changes after CL {path_baseline} ..."
        )
        try:
            cls = p4.get_changelists(
                after_cl=path_baseline,
                depot_paths=[mapping.p4_path],
            )
        except P4Error as exc:
            logger.error(str(exc))
            errors.append(str(exc))
            raise MigrationError() from exc

        if cls:
            logger.info(f"  {mapping.git_path}: {len(cls)} new CL(s) — {cls}")
        else:
            logger.info(f"  {mapping.git_path}: no new changelists found.")
        all_cl_ids.update(cls)

    cl_ids = sorted(all_cl_ids)
    if not cl_ids:
        logger.info("No new changelists to migrate.")
        return (0, 0)

    logger.info(f"Found {len(cl_ids)} new changelist(s) in total: {cl_ids}")

    # -- 8. Process each changelist (oldest first) -----------------------
    highest_per_path: dict[str, int] = {}

    commits_created = 0
    changelists_processed = 0
    for cl_id in cl_ids:
        changelists_processed += 1
        logger.info(f"Processing changelist {changelists_processed}/{len(cl_ids)} — CL {cl_id} ...")

        try:
            # 8a. Fetch CL details FIRST (before syncing) so we know
            #     which gitPaths are affected.
            cl: Changelist = p4.get_changelist_details(cl_id)

            # 8b. Match changed files to gitPaths
            affected_git_paths = _match_affected_paths(
                cl.files, config.path_mappings,
            )
            logger.info(f"  Affected gitPaths: {affected_git_paths}")

            # 8c. Determine which depot paths actually need syncing
            #     (skip paths whose baseline already covers this CL).
            depot_paths_to_sync: list[str] = []
            dynamic_mappings: list[PathMapping] = []
            for git_path in affected_git_paths:
                path_baseline = state.get_path_cl(git_path) or 0
                if cl_id <= path_baseline:
                    logger.info(
                        f"  {git_path} already covered at CL {path_baseline}; skipping"
                    )
                    continue
                for m in config.path_mappings:
                    if m.git_path == git_path:
                        if m.dynamic:
                            dynamic_mappings.append(m)
                        else:
                            depot_paths_to_sync.append(m.p4_path)
                        break

            if not depot_paths_to_sync and not dynamic_mappings:
                logger.info(f"  CL {cl_id} already covered for all affected paths; skipping")
                continue

            # 8d. Sync non-dynamic paths (working tree) + no-checkout stage
            #     dynamic paths (never touch the working tree), then create
            #     a single Git commit for this changelist.
            _process_one_changelist(
                p4=p4,
                git=git,
                config=config,
                user_mapping=user_mapping,
                cl=cl,
                depot_paths_to_sync=depot_paths_to_sync,
                dynamic_mappings=dynamic_mappings,
                logger=logger,
            )
            commits_created += 1

            # 8e. Track per-path progress
            for git_path in affected_git_paths:
                current = highest_per_path.get(git_path, 0)
                if cl_id > current:
                    highest_per_path[git_path] = cl_id

        except (P4Error, GitError, WorkspaceError) as exc:
            msg = f"Failed at changelist {cl_id}: {exc}"
            logger.error(msg)
            errors.append(msg)
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

    # -- 10. Update state (per-path) ------------------------------------
    logger.info(f"Updating per-path state: {highest_per_path}")
    try:
        for git_path, last_cl in highest_per_path.items():
            state.set_path_cl(git_path, last_cl)
        state_mgr.write(state)
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
    cl: Changelist,
    depot_paths_to_sync: list[str],
    dynamic_mappings: list[PathMapping],
    logger: P4MirrorLogger,
) -> None:
    """Sync only the affected depot paths, stage, and commit a single CL.

    Parameters
    ----------
    cl : Changelist
        Pre-fetched changelist metadata (caller fetches details *before*
        calling this function so it can determine which paths to sync).
    depot_paths_to_sync : list[str]
        Depot paths affected by this CL that actually need syncing
        (already filtered against per-path baselines).  Non-dynamic only.
    dynamic_mappings : list of PathMapping
        Affected dynamic mappings for this CL; their files are staged
        no-checkout (never written to the working tree).
    """

    # --- a. Sync only the affected (non-dynamic) depot paths ---
    for dp in depot_paths_to_sync:
        logger.info(f"  Syncing {dp} to CL {cl.cl_id} ...")
        p4.sync_path(dp, cl.cl_id)

    # --- c. Map Perforce user to Git author ---
    if cl.author in user_mapping:
        author_name = user_mapping[cl.author].name
        author_email = user_mapping[cl.author].email
    else:
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
    message = _build_commit_message(cl, config.repository_name)
    print(f"  Commit message:\n{message}")

    # --- e. Stage all changes in Git (non-dynamic working tree) ---
    logger.info(f"  Staging changes ...")
    git.stage_all()

    # --- b. No-checkout stage dynamic depot files ---
    # Runs after stage_all so `git add -A` never sees the dynamic entries
    # (they have no working-tree file and are marked skip-worktree).
    for mapping in dynamic_mappings:
        _stage_dynamic_changelist(
            p4=p4,
            git=git,
            cl=cl,
            mapping=mapping,
            logger=logger,
        )

    # --- f. Create Git commit ---
    logger.info(f"  Creating commit: {author_name} <{author_email}> @ {cl.timestamp}")
    git.commit(
        author_name=author_name,
        author_email=author_email,
        timestamp=cl.timestamp,
        message=message,
    )
    logger.info(f"  Commit created for CL {cl.cl_id}.")


def _stage_dynamic_changelist(
    p4: P4Client,
    git: GitClient,
    cl: Changelist,
    mapping: PathMapping,
    logger: P4MirrorLogger,
) -> None:
    """Stage a changelist's files under a *dynamic* mapping without writing
    them to the local workspace (no-checkout commit).

    For every file in *cl.files* that falls under ``mapping.p4_path``:

    - the relative Git path is derived by stripping the mapping's depot
      prefix and prepending ``mapping.git_path``, so the Git tree mirrors
      the P4 layout (e.g. ``//REPOSITORY/org/apache/.../x.jar`` →
      ``REPOSITORY/org/apache/.../x.jar``);
    - ``add``/``edit``/``integrate``/``move/add`` files are fetched via
      ``p4 print`` into a temp file, hashed into the Git object database,
      and staged with ``git update-index --cacheinfo`` (exec bit preserved);
    - ``delete`` files are removed from the index;
    - every staged entry is marked ``skip-worktree`` so Git never
      materializes it on ``checkout``/``reset``.
    """
    prefix = mapping.p4_path[:-4]  # strip trailing "/..."
    git_base = mapping.git_path.rstrip("/")
    delete_actions = {"delete", "move/delete", "purge"}

    files = [f for f in cl.files if f.path.startswith(prefix)]
    if not files:
        logger.info(
            f"    [dynamic] {mapping.git_path}: no files under {mapping.p4_path}"
        )
        return

    logger.info(
        f"    [dynamic] {mapping.git_path}: staging {len(files)} file(s) no-checkout"
    )
    # Use the OS temp dir (OUTSIDE the git working tree) so `git add -A`
    # never sees these blob files — even if a per-file unlink fails or a
    # file stays locked on Windows, leftover blobs cannot break staging.
    temp_dir = Path(tempfile.mkdtemp(prefix="p4mirror_blob_"))
    try:
        staged = 0
        removed = 0
        for i, f in enumerate(files):
            # ``prefix`` ends without the trailing "/" (p4_path[:-4] strips
            # "/..."), so ``rel`` starts with the "/" separator — drop it to
            # avoid a double slash in the joined Git path ("NDPro//Dev/...").
            rel = f.path[len(prefix):].lstrip("/")  # e.g. "org/apache/.../x.jar"
            if git_base:
                git_path = f"{git_base}/{rel}" if rel else git_base
            else:
                # Root mirror (git_path was "/" or empty): files land at
                # the repository root, preserving the P4-relative structure.
                git_path = rel

            if f.action in delete_actions:
                logger.info(f"    [dynamic] delete  {git_path}")
                try:
                    git.update_index_remove(git_path)
                except GitError:
                    logger.info(
                        f"    [dynamic] {git_path} not in index; skipping remove"
                    )
                removed += 1
                continue

            logger.info(f"    [dynamic] {f.action:8s} {git_path}")
            temp_path = temp_dir / f"p4mirror_blob_{cl.cl_id}_{i}.tmp"
            try:
                p4.print_file(f.path, f.rev, temp_path)
                blob = git.hash_object_from_file(temp_path)
                mode = p4.fstat_mode(f.path, f.rev)
                git.update_index_add(git_path, blob, mode)
                git.mark_skip_worktree(git_path)
            finally:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            staged += 1

        logger.info(
            f"    [dynamic] {mapping.git_path}: staged {staged}, removed {removed}"
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _match_affected_paths(
    files: list[ChangedFile],
    path_mappings: list[PathMapping],
) -> list[str]:
    """Determine which gitPaths are affected by a changelist based on its files.

    Each file's depot path is checked against the ``p4_path`` prefix of
    every mapping.  Returns a sorted list of unique gitPath names.
    """
    affected: set[str] = set()
    for f in files:
        for mapping in path_mappings:
            # p4_path is like "//RFB/AppA/..." — strip trailing "/..."
            prefix = mapping.p4_path[:-4]
            if f.path.startswith(prefix):
                affected.add(mapping.git_path)
                break
    return sorted(affected)


def _build_commit_message(cl: Changelist, repository) -> str:
    """Build the Git commit message from a changelist.

    Appends the Perforce changelist reference in brackets.
    """
    desc = cl.description.strip()
    if desc:
        return f"{desc}\n\nP4MIRROR:\n[git-p4: depot-paths = \"//{repository}/\": change = {cl.cl_id}]"
    return f"P4MIRROR:\n[git-p4: change = {cl.cl_id}]"
"""P4Mirror — Incremental Perforce to GitHub Migration Framework.

Entry point. Supports two subcommands:

- ``p4mirror init``      — One-time workspace initialisation (clone + baseline)
- ``p4mirror migrate``   — Incremental migration (existing behaviour)

Usage
-----
    uv run python migrate.py init
    uv run python migrate.py init --config config/my_repo.json

    uv run python migrate.py migrate
    uv run python migrate.py migrate --config config/my_repo.json --build-number 42

For backward compatibility, running ``python migrate.py`` without a subcommand
defaults to the ``migrate`` command.
"""

from __future__ import annotations

import argparse
import os
import sys

from config import ConfigError, load_repository_config, load_user_mapping
from core.github_auth import GitHubAppTokenProvider, GitHubTokenError
from core.migration import run_migration


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="P4Mirror — Incremental Perforce to GitHub Migration",
    )
    subparsers = parser.add_subparsers(dest="command")

    # -- init subcommand --------------------------------------------------
    p_init = subparsers.add_parser(
        "init",
        help="One-time workspace initialisation (clone + discover baseline)",
    )
    p_init.add_argument(
        "--config",
        default="config/repository.json",
        help="Path to repository configuration JSON (default: %(default)s)",
    )
    p_init.add_argument(
        "--users",
        default="config/users.json",
        help=(
            "Path to Perforce-to-Git user mapping JSON "
            "(default: %(default)s; not used by init)"
        ),
    )
    p_init.add_argument(
        "--github-token",
        default=None,
        help=(
            "GitHub token (JWT or PAT) for authenticated Git operations. "
            "Falls back to GITHUB_TOKEN env var. Ignored when --private-key "
            "is provided (a fresh token is minted instead)."
        ),
    )
    p_init.add_argument(
        "--private-key",
        default=None,
        help=(
            "Path to the GitHub App private key PEM (Jenkins secret file). "
            "Falls back to the GITHUB_PRIVATE_KEY_PATH env var. When "
            "provided, a FRESH installation token is generated for every run "
            "from the GitHub App ID / installation ID "
            "(--app-id / --installation-id) — cached/stale tokens are never "
            "reused."
        ),
    )
    p_init.add_argument(
        "--app-id",
        default=None,
        help=(
            "GitHub App ID (numeric). Falls back to the GITHUB_APP_ID env "
            "var. Required together with --private-key to mint a fresh "
            "installation token."
        ),
    )
    p_init.add_argument(
        "--installation-id",
        default=None,
        help=(
            "GitHub App installation ID (numeric). Falls back to the "
            "GITHUB_INSTALLATION_ID env var. Required together with "
            "--private-key to mint a fresh installation token."
        ),
    )

    # -- migrate subcommand -----------------------------------------------
    p_migrate = subparsers.add_parser(
        "migrate",
        help="Incremental migration (existing behaviour)",
    )
    p_migrate.add_argument(
        "--config",
        default="config/repository.json",
        help="Path to repository configuration JSON (default: %(default)s)",
    )
    p_migrate.add_argument(
        "--users",
        default="config/users.json",
        help="Path to Perforce-to-Git user mapping JSON (default: %(default)s)",
    )
    p_migrate.add_argument(
        "--build-number",
        type=int,
        default=None,
        help="Jenkins build number (optional, logged if provided)",
    )
    p_migrate.add_argument(
        "--max-cls",
        type=int,
        default=0,
        help=(
            "Maximum number of changelists to process in this run "
            "(0 = unlimited, the default). Use to migrate in batches, e.g. "
            "--max-cls 5 today and run again tomorrow for the rest."
        ),
    )
    p_migrate.add_argument(
        "--github-token",
        default=None,
        help=(
            "GitHub token (JWT or PAT) for authenticated Git operations. "
            "Falls back to GITHUB_TOKEN env var. Ignored when --private-key "
            "is provided (a fresh token is minted instead)."
        ),
    )
    p_migrate.add_argument(
        "--private-key",
        default=None,
        help=(
            "Path to the GitHub App private key PEM (Jenkins secret file). "
            "Falls back to the GITHUB_PRIVATE_KEY_PATH env var. When "
            "provided, a FRESH installation token is generated for every run "
            "from the GitHub App ID / installation ID "
            "(--app-id / --installation-id), and again right before the "
            "final push — cached/stale tokens are never reused."
        ),
    )
    p_migrate.add_argument(
        "--app-id",
        default=None,
        help=(
            "GitHub App ID (numeric). Falls back to the GITHUB_APP_ID env "
            "var. Required together with --private-key to mint a fresh "
            "installation token."
        ),
    )
    p_migrate.add_argument(
        "--installation-id",
        default=None,
        help=(
            "GitHub App installation ID (numeric). Falls back to the "
            "GITHUB_INSTALLATION_ID env var. Required together with "
            "--private-key to mint a fresh installation token."
        ),
    )

    # -- Handle backward compatibility ------------------------------------
    # If no subcommand is given, default to "migrate" so that existing
    # invocations (e.g. "python migrate.py --config ...") continue to work.
    if len(sys.argv) == 1:
        # python migrate.py  →  default to migrate with no extra args
        parsed = p_migrate.parse_args([])
        parsed.command = "migrate"
        return parsed

    if sys.argv[1] not in ("init", "migrate"):
        # python migrate.py --config x --build-number y
        # → parse directly with the migrate subparser
        parsed = p_migrate.parse_args(sys.argv[1:])
        parsed.command = "migrate"
        return parsed

    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    # -- Validate optional flags -----------------------------------------
    if getattr(args, "max_cls", 0) < 0:
        print(
            "Configuration error: --max-cls must be >= 0 (0 = unlimited).",
            file=sys.stderr,
        )
        sys.exit(1)

    # -- Load config (shared) --------------------------------------------
    try:
        config = load_repository_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    # -- Resolve GitHub App credentials (CLI arg > env var, like GH_TOKEN) --
    # The private key (Jenkins secret file) takes precedence as the trigger:
    # when present, the app mints a FRESH installation token on every run
    # (and again before the final push) instead of reusing a cached token.
    app_id = args.app_id or os.environ.get("GITHUB_APP_ID")
    installation_id = args.installation_id or os.environ.get(
        "GITHUB_INSTALLATION_ID"
    )
    private_key_path = args.private_key or os.environ.get(
        "GITHUB_PRIVATE_KEY_PATH"
    )

    token_provider = None
    if private_key_path:
        if not app_id or not installation_id:
            print(
                "Configuration error: --private-key requires --app-id and "
                "--installation-id (or the GITHUB_APP_ID / "
                "GITHUB_INSTALLATION_ID env vars) to mint a fresh token.",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            token_provider = GitHubAppTokenProvider(
                app_id=app_id,
                installation_id=installation_id,
                private_key_path=private_key_path,
            )
        except GitHubTokenError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            sys.exit(1)
        print(
            "GitHub token provider: fresh installation token will be minted "
            f"(app {app_id}, installation {installation_id}) using "
            f"private key {private_key_path} for this run.",
            file=sys.stderr,
        )

    # Backward compatibility: --github-token > GH_TOKEN > GITHUB_TOKEN env
    # vars (only used when no --private-key was given).
    github_token = (
        args.github_token
        or os.environ.get("GH_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
    )

    # -- Fail fast if no GitHub credential source is configured ----------
    # Without a private key or a token, the first authenticated git fetch /
    # push or GitHub API call would fail deep inside the run with a confusing
    # auth error.  Surface a clear message now instead.
    if token_provider is None and not github_token:
        print(
            "Configuration error: no GitHub credential source configured. "
            "Provide --private-key (with --app-id / --installation-id) to "
            "mint a fresh token, or pass --github-token, or set the "
            "GH_TOKEN / GITHUB_TOKEN environment variable.",
            file=sys.stderr,
        )
        sys.exit(1)

    # -- Route to the appropriate command ---------------------------------
    if args.command == "init":
        from core.initializer import run_init

        run_init(
            config=config,
            github_token=github_token,
            token_provider=token_provider,
        )
    else:
        # Users mapping is only needed for migration
        try:
            user_mapping = load_user_mapping(args.users)
        except ConfigError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            sys.exit(1)

        run_migration(
            config=config,
            user_mapping=user_mapping,
            github_token=github_token,
            token_provider=token_provider,
            build_number=args.build_number,
            max_cls=args.max_cls,
        )


if __name__ == "__main__":
    main()

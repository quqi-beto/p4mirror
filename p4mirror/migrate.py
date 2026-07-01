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
import sys

from config import ConfigError, load_repository_config, load_user_mapping
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

    # -- Load configuration -----------------------------------------------
    try:
        config = load_repository_config(args.config)
        user_mapping = load_user_mapping(args.users)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    # -- Route to the appropriate command ---------------------------------
    if args.command == "init":
        from core.initializer import run_init

        run_init(config=config)
    else:
        run_migration(
            config=config,
            user_mapping=user_mapping,
            build_number=args.build_number,
        )


if __name__ == "__main__":
    main()

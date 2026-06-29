"""P4Mirror — Incremental Perforce to GitHub Migration Framework.

Entry point. Loads configuration and coordinates the migration.

Usage
-----
    uv run python migrate.py
    uv run python migrate.py --config config/my_repo.json --build-number 42
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
    parser.add_argument(
        "--config",
        default="config/repository.json",
        help="Path to repository configuration JSON (default: %(default)s)",
    )
    parser.add_argument(
        "--users",
        default="config/users.json",
        help="Path to Perforce-to-Git user mapping JSON (default: %(default)s)",
    )
    parser.add_argument(
        "--build-number",
        type=int,
        default=None,
        help="Jenkins build number (optional, logged if provided)",
    )
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

    # -- Run migration ----------------------------------------------------
    run_migration(
        config=config,
        user_mapping=user_mapping,
        build_number=args.build_number,
    )


if __name__ == "__main__":
    main()

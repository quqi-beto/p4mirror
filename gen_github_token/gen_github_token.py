"""Generate a GitHub App installation access token (ghs_...) for Git operations.

Usage
-----
    pip install -r requirements.txt

    python gen_github_token.py \\
        --app-id 123456 \\
        --private-key app.private-key.pem \\
        --installation-id 789012

Or via environment variables:

    set GITHUB_APP_ID=123456
    set GITHUB_PRIVATE_KEY_PATH=app.private-key.pem
    set GITHUB_INSTALLATION_ID=789012
    python gen_github_token.py

The output is just the raw ``ghs_...`` token, suitable for:

    set GITHUB_TOKEN=ghs_xxxxxxxxxxxx
    uv run python p4mirror/migrate.py init
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import jwt
import requests


# ── GitHub API ──────────────────────────────────────────────────────────
_GITHUB_API = "https://api.github.com"


# ── Helpers ─────────────────────────────────────────────────────────────

def _load_private_key(path: str) -> bytes:
    """Read a PEM-encoded private key from *path*."""
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        print(f"Error: private key file not found: {path}", file=sys.stderr)
        sys.exit(1)
    except PermissionError:
        print(f"Error: permission denied reading: {path}", file=sys.stderr)
        sys.exit(1)


def _generate_jwt(app_id: str, private_key: bytes) -> str:
    """Create a signed JWT for the GitHub App.

    The JWT uses RS256 and is valid for 10 minutes (the maximum allowed
    by GitHub for App authentication).
    """
    now = int(time.time())
    payload = {
        "iss": app_id,          # issuer = App ID
        "iat": now,             # issued at
        "exp": now + 600,       # expires in 10 minutes
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


def _get_installation_token(jwt_token: str, installation_id: str) -> str:
    """Exchange the App JWT for an installation access token.

    POST /app/installations/{installation_id}/access_tokens
    https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app
    """
    url = f"{_GITHUB_API}/app/installations/{installation_id}/access_tokens"
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github+json",
    }

    resp = requests.post(url, headers=headers)

    if resp.status_code == 201:
        return resp.json()["token"]

    # Try to extract a meaningful error message
    try:
        detail = resp.json()
    except Exception:
        detail = resp.text

    print(
        f"Error: GitHub API returned {resp.status_code}\n{detail}",
        file=sys.stderr,
    )
    sys.exit(1)


# ── CLI ─────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a GitHub App installation access token (ghs_...)",
    )
    parser.add_argument(
        "--app-id",
        default=None,
        help=(
            "GitHub App ID (numeric). Falls back to GITHUB_APP_ID env var."
        ),
    )
    parser.add_argument(
        "--private-key",
        default=None,
        help=(
            "Path to the GitHub App private key PEM file. "
            "Falls back to GITHUB_PRIVATE_KEY_PATH env var."
        ),
    )
    parser.add_argument(
        "--installation-id",
        default=None,
        help=(
            "GitHub App installation ID (numeric). "
            "Falls back to GITHUB_INSTALLATION_ID env var."
        ),
    )
    return parser.parse_args()


def _resolve_arg(
    cli_value: str | None,
    env_var: str,
    flag_name: str,
) -> str:
    """Return *cli_value* or the environment variable, or exit."""
    value = cli_value or os.environ.get(env_var)
    if value:
        return value
    print(
        f"Error: --{flag_name} is required "
        f"(or set the {env_var} environment variable).",
        file=sys.stderr,
    )
    sys.exit(1)


# ── Main ────────────────────────────────────────────────────────────────

def main() -> None:
    args = _parse_args()

    app_id = _resolve_arg(args.app_id, "GITHUB_APP_ID", "app-id")
    private_key_path = _resolve_arg(
        args.private_key, "GITHUB_PRIVATE_KEY_PATH", "private-key",
    )
    installation_id = _resolve_arg(
        args.installation_id, "GITHUB_INSTALLATION_ID", "installation-id",
    )

    private_key = _load_private_key(private_key_path)

    print(f"Generating JWT for App ID {app_id} ...", file=sys.stderr)
    jwt_token = _generate_jwt(app_id, private_key)

    print(
        f"Exchanging JWT for installation {installation_id} token ...",
        file=sys.stderr,
    )
    token = _get_installation_token(jwt_token, installation_id)

    # Print ONLY the token to stdout (for piping / capturing)
    print(token)


if __name__ == "__main__":
    main()

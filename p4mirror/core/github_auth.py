"""Fresh GitHub App installation token generation for P4Mirror.

P4Mirror never reuses a cached/stale GitHub token.  Instead it mints a
**fresh** installation access token (``ghs_...``) on demand:

* once at the start of every ``init`` / ``migrate`` run, and
* again immediately before the final ``git push`` — a migration that
  processes many changelists can easily outlive the 1-hour lifetime of
  an installation token, so the token used at the start of the run may
  already be invalid by push time.

Credentials are supplied by the caller (typically the Jenkins job):

* the GitHub App **App ID** and **installation ID** are passed as CLI
  arguments (``--app-id`` / ``--installation-id``), and
* the GitHub App **private key** PEM file is kept in Jenkins credentials
  as a *secret file* and handed to the app via the ``--private-key``
  CLI flag.

Usage
-----
    provider = GitHubAppTokenProvider(
        app_id="123456",
        installation_id="789012",
        private_key_path=r"C:\\jenkins\\app.private-key.pem",
    )
    token = provider.get_token()   # fresh ghs_... token on every call
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

import jwt

try:
    import certifi as _certifi

    _SSL_CONTEXT = ssl.create_default_context(cafile=_certifi.where())  # type: ignore[attr-defined,unused-ignore]
except ImportError:
    _SSL_CONTEXT = ssl.create_default_context()


_GITHUB_API = "https://api.github.com"


class GitHubTokenError(Exception):
    """Raised when a GitHub App token cannot be generated."""


# ---------------------------------------------------------------------------
# Token provider
# ---------------------------------------------------------------------------


def _generate_jwt(app_id: str, private_key: bytes) -> str:
    """Create a signed RS256 JWT for the GitHub App.

    The JWT is valid for 10 minutes (the maximum GitHub allows for App
    authentication) and is only used to mint the installation token.
    """
    now = int(time.time())
    payload = {
        "iss": app_id,
        "iat": now,
        "exp": now + 600,
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


def _get_installation_token(jwt_token: str, installation_id: str) -> str:
    """Exchange the App JWT for an installation access token.

    ``POST /app/installations/{installation_id}/access_tokens``
    https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app
    """
    url = f"{_GITHUB_API}/app/installations/{installation_id}/access_tokens"
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "p4mirror",
    }
    req = urllib.request.Request(url, data=b"", headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=30, context=_SSL_CONTEXT) as resp:
            body: dict = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GitHubTokenError(
            f"GitHub API returned {exc.code} while minting installation "
            f"token: {detail}"
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise GitHubTokenError(
            f"GitHub API request failed while minting installation token: {exc}"
        ) from exc

    token = body.get("token")
    if not token:
        raise GitHubTokenError(
            f"GitHub API response did not contain a token: {body}"
        )
    return token


class GitHubAppTokenProvider:
    """Mints a fresh GitHub App installation token on every call.

    Parameters
    ----------
    app_id : str
        GitHub App ID (numeric), e.g. ``"123456"``.
    installation_id : str
        GitHub App installation ID (numeric), e.g. ``"789012"``.
    private_key_path : str or Path
        Path to the GitHub App private key PEM file (Jenkins secret file).

    Raises
    ------
    GitHubTokenError
        If the App ID / installation ID are missing or the private key file
        does not exist.
    """

    def __init__(
        self,
        app_id: str,
        installation_id: str,
        private_key_path: str | Path,
    ) -> None:
        self._app_id = app_id
        self._installation_id = installation_id
        self._private_key_path = Path(private_key_path)

        if not app_id or not installation_id:
            raise GitHubTokenError(
                "GitHub App ID and installation ID are required to mint a token"
            )
        if not self._private_key_path.exists():
            raise GitHubTokenError(
                f"GitHub App private key not found: {self._private_key_path}"
            )

    # -- Public API ------------------------------------------------------

    def get_token(self) -> str:
        """Return a fresh ``ghs_...`` installation token (never cached).

        Raises
        ------
        GitHubTokenError
            If the GitHub API rejects the request.
        """
        private_key = self._private_key_path.read_bytes()
        jwt_token = _generate_jwt(self._app_id, private_key)
        return _get_installation_token(jwt_token, self._installation_id)

"""Configuration loading and validation for P4Mirror."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    """Raised when configuration is invalid or cannot be loaded."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PathMapping:
    """A single Perforce-to-Git path mapping."""

    p4_path: str
    git_path: str


@dataclass(frozen=True)
class RepositoryConfig:
    """Complete repository migration configuration."""

    repository_name: str
    p4_port: str
    p4_user: str
    p4_client: str
    workspace_root: str
    github_url: str
    default_branch: str
    sparse_checkout: bool = False
    path_mappings: list[PathMapping] = field(default_factory=list)


@dataclass(frozen=True)
class UserMapping:
    """Mapping from a Perforce username to a Git author identity."""

    name: str
    email: str


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def _required(data: dict[str, Any], key: str, label: str = "") -> Any:
    """Return *data[key]* or raise :exc:`ConfigError`."""
    if key not in data:
        raise ConfigError(
            f"Missing required field {key!r}"
            + (f" in {label}" if label else "")
        )
    return data[key]


def _validate_depot_path(value: str) -> str:
    """Check that a depot path roughly looks like ``//depot/...``."""
    if not value.startswith("//"):
        raise ConfigError(f"Depot path must start with '//': {value!r}")
    if not value.endswith("/..."):
        raise ConfigError(
            f"Depot path must end with '/...': {value!r}"
        )
    return value


def load_repository_config(path: str | Path) -> RepositoryConfig:
    """Load and validate a :class:`RepositoryConfig` from a JSON file.

    Raises
    ------
    ConfigError
        If the file is missing, malformed, or fails validation.
    """
    path = Path(path)

    if not path.exists():
        raise ConfigError(f"Repository config file not found: {path}")

    try:
        with path.open(encoding="utf-8") as fh:
            data: dict[str, Any] = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc

    # --- Required top-level fields ----------------------------------------
    kwargs: dict[str, Any] = {
        "repository_name": _required(data, "repository_name", str(path)),
        "p4_port": _required(data, "p4_port", str(path)),
        "p4_user": _required(data, "p4_user", str(path)),
        "p4_client": _required(data, "p4_client", str(path)),
        "workspace_root": _required(data, "workspace_root", str(path)),
        "github_url": _required(data, "github_url", str(path)),
        "default_branch": _required(data, "default_branch", str(path)),
    }

    # --- Optional sparse_checkout -----------------------------------------
    kwargs["sparse_checkout"] = data.get("sparse_checkout", False)

    # --- Path mappings ----------------------------------------------------
    raw_mappings = data.get("path_mappings", [])
    if not raw_mappings:
        raise ConfigError(
            f"'path_mappings' is empty or missing in {path}. "
            "At least one Perforce-to-Git path mapping is required."
        )

    mappings: list[PathMapping] = []
    for i, entry in enumerate(raw_mappings):
        p4_path = _required(entry, "p4_path", f"path_mappings[{i}]")
        git_path = _required(entry, "git_path", f"path_mappings[{i}]")
        _validate_depot_path(p4_path)
        if not git_path.strip():
            raise ConfigError(
                f"git_path is empty in path_mappings[{i}] of {path}"
            )
        mappings.append(PathMapping(p4_path=p4_path, git_path=git_path))

    kwargs["path_mappings"] = mappings

    return RepositoryConfig(**kwargs)


def load_user_mapping(path: str | Path) -> dict[str, UserMapping]:
    """Load Perforce-to-Git author mappings from a JSON file.

    Returns a dict keyed by Perforce username.
    """
    path = Path(path)

    if not path.exists():
        raise ConfigError(f"User mapping file not found: {path}")

    try:
        with path.open(encoding="utf-8") as fh:
            data: dict[str, Any] = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc

    result: dict[str, UserMapping] = {}
    for username, value in data.items():
        if not isinstance(value, dict):
            raise ConfigError(
                f"Invalid entry for user {username!r} in {path}: "
                f"expected an object with 'name' and 'email'"
            )
        name = _required(value, "name", f"user {username!r}")
        email = _required(value, "email", f"user {username!r}")
        result[username] = UserMapping(name=name, email=email)

    return result

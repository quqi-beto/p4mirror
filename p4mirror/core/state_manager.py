"""Reads and writes P4Mirror migration state.

State is stored per-repository so that multiple repositories can be
migrated independently.  Each repository gets its own file under the
state directory (e.g. ``state/state_ApplicationA.json``).

Within a repository, each ``gitPath`` (path mapping) tracks its own
*last_migrated_cl* so that perforce-to-git paths can progress
independently.  Legacy state files that only store a single
``last_migrated_cl`` are auto-converted on read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StateError(Exception):
    """Raised when the state file is missing, empty, or contains an invalid
    changelist number."""


@dataclass
class PathState:
    """Migration state for a single gitPath within a repository."""

    last_migrated_cl: int
    """The latest Perforce changelist migrated for this gitPath."""


@dataclass
class State:
    """Deserialized migration state with per-gitPath tracking.

    Parameters
    ----------
    paths : dict[str, PathState]
        Mapping of gitPath name → per-path state.
    repository : str
        Repository name (from config).
    branch : str
        Git branch being synced to.
    last_run : str
        ISO-formatted timestamp of the last write.
    """

    paths: dict[str, PathState] = field(default_factory=dict)
    repository: str = ""
    branch: str = ""
    last_run: str = ""

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    @property
    def last_migrated_cl(self) -> int:
        """Return the maximum *last_migrated_cl* across all paths.

        This property exists for backward compatibility so that code
        which reads a single global CL continues to work.  For per-path
        granularity use :meth:`get_path_cl` or the *paths* dict directly.
        """
        if not self.paths:
            return 0
        return max(ps.last_migrated_cl for ps in self.paths.values())

    def get_path_cl(self, git_path: str) -> int | None:
        """Return the *last_migrated_cl* for a specific gitPath.

        Returns ``None`` if *git_path* has no entry yet.
        """
        ps = self.paths.get(git_path)
        return ps.last_migrated_cl if ps is not None else None

    def set_path_cl(self, git_path: str, value: int) -> None:
        """Set (or overwrite) the *last_migrated_cl* for a gitPath."""
        self.paths[git_path] = PathState(last_migrated_cl=value)


class StateManager:
    """Manages persistence of migration state.

    State is stored per-repository so that multiple repositories can be
    migrated independently without clobbering each other's checkpoint.

    Parameters
    ----------
    repository_name : str
        Repository name used to derive the state file name
        (e.g. ``state/state_ApplicationA.json``).
    state_dir : str or Path
        Directory where per-repository state files are stored.
    """

    _LEGACY_KEY = "last_migrated_cl"
    _PATHS_KEY = "paths"

    def __init__(
        self,
        repository_name: str,
        state_dir: str | Path = "state",
    ) -> None:
        self._path = Path(state_dir) / f"state_{repository_name}.json"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def read(self) -> State:
        """Load and return the current state.

        Legacy files containing a single ``last_migrated_cl`` field are
        auto-converted: the value is stored under an ``"*"`` path key
        so that consumers can continue to use ``state.last_migrated_cl``.

        Raises
        ------
        StateError
            If the file is missing, empty, or a required changelist value
            is not a valid positive integer.
        """
        if not self._path.exists():
            raise StateError(
                f"State file not found: {self._path}. "
                "A valid state file with 'last_migrated_cl' is required."
            )

        try:
            text = self._path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise StateError(f"Cannot read state file {self._path}: {exc}") from exc

        if not text:
            raise StateError(
                f"State file is empty: {self._path}. "
                "A valid 'last_migrated_cl' value is required."
            )

        try:
            data: dict[str, Any] = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StateError(
                f"State file is invalid JSON: {self._path}: {exc}"
            ) from exc

        repository = data.get("repository", "")
        branch = data.get("branch", "")
        last_run = data.get("last_run", "")

        # --- Detect / convert legacy format --------------------------------
        if self._PATHS_KEY in data:
            # New format — read per-path state
            raw_paths: dict[str, Any] = data[self._PATHS_KEY]
            paths: dict[str, PathState] = {}
            for gp, ps_data in raw_paths.items():
                cl = ps_data.get("last_migrated_cl") if isinstance(ps_data, dict) else ps_data
                self._validate_cl(cl, f"paths.{gp}.last_migrated_cl")
                paths[gp] = PathState(last_migrated_cl=int(cl))
            return State(
                paths=paths,
                repository=repository,
                branch=branch,
                last_run=last_run,
            )
        else:
            # Legacy format — single last_migrated_cl
            last_cl = data.get(self._LEGACY_KEY)
            if last_cl is None:
                raise StateError(
                    f"Neither '{self._PATHS_KEY}' nor '{self._LEGACY_KEY}' "
                    f"found in {self._path}."
                )
            self._validate_cl(last_cl, self._LEGACY_KEY)
            return State(
                paths={"*": PathState(last_migrated_cl=int(last_cl))},
                repository=repository,
                branch=branch,
                last_run=last_run,
            )

    def write(self, state: State) -> None:
        """Persist *state* to disk.

        Parameters
        ----------
        state : State
            The full state object to write (per-path CLs, metadata, etc.).
        """
        raw_paths: dict[str, dict[str, int]] = {}
        for gp, ps in state.paths.items():
            self._validate_cl(ps.last_migrated_cl, f"paths.{gp}.last_migrated_cl")
            raw_paths[gp] = {"last_migrated_cl": ps.last_migrated_cl}

        payload: dict[str, Any] = {
            self._PATHS_KEY: raw_paths,
            "last_run": datetime.now(timezone.utc).isoformat(),
        }
        if state.repository:
            payload["repository"] = state.repository
        if state.branch:
            payload["branch"] = state.branch

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_cl(value: object, label: str = "last_migrated_cl") -> None:
        """Ensure *value* is a positive integer.

        Parameters
        ----------
        value : object
            The value to validate.
        label : str
            Human-readable label used in error messages.
        """
        if not isinstance(value, int) or value <= 0:
            raise StateError(
                f"'{label}' must be a positive integer, got {value!r}"
            )

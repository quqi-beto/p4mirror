"""Reads and writes P4Mirror migration state.

State is stored per-repository so that multiple repositories can be
migrated independently.  Each repository gets its own file under the
state directory (e.g. ``state/state_ApplicationA.json``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StateError(Exception):
    """Raised when the state file is missing, empty, or contains an invalid
    changelist number."""


@dataclass
class State:
    """Deserialized migration state."""

    last_migrated_cl: int
    repository: str = ""
    branch: str = ""
    last_run: str = ""


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

        Raises
        ------
        StateError
            If the file is missing, empty, or ``last_migrated_cl`` is
            not a valid positive integer.
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

        last_cl = data.get("last_migrated_cl")
        if last_cl is None:
            raise StateError(
                f"'last_migrated_cl' is missing in {self._path}."
            )
        self._validate_cl(last_cl)

        return State(
            last_migrated_cl=int(last_cl),
            repository=data.get("repository", ""),
            branch=data.get("branch", ""),
            last_run=data.get("last_run", ""),
        )

    def write(self, last_migrated_cl: int, **extra: str) -> None:
        """Persist a new *last_migrated_cl* to the state file.

        Parameters
        ----------
        last_migrated_cl : int
            The latest successfully migrated Perforce changelist.
        **extra
            Additional key/value pairs to store (e.g. repository, branch).
        """
        self._validate_cl(last_migrated_cl)

        state: dict[str, Any] = {
            "last_migrated_cl": last_migrated_cl,
            "last_run": datetime.now(timezone.utc).isoformat(),
        }
        state.update(extra)

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_cl(value: object) -> None:
        """Ensure *value* is a positive integer."""
        if not isinstance(value, int) or value <= 0:
            raise StateError(
                f"'last_migrated_cl' must be a positive integer, got {value!r}"
            )

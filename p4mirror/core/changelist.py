"""Data model for a Perforce changelist.

:class:`Changelist` represents a single Perforce changelist with its
metadata and the list of files that were added, modified, or deleted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ChangedFile:
    """A single file touched by a changelist."""

    path: str
    """Depot path of the file (e.g. ``//RFB/AppA/main.cpp``)."""

    action: str
    """Perforce action: ``add``, ``edit``, ``delete``, ``move/add``, etc."""

    depot_file: str = ""
    """Full depot file syntax (may include revision)."""


@dataclass
class Changelist:
    """A single Perforce changelist."""

    cl_id: int
    """Perforce changelist number."""

    author: str
    """Perforce username of the author."""

    timestamp: datetime
    """When the changelist was submitted."""

    description: str
    """Changelist description / commit message."""

    files: list[ChangedFile] = field(default_factory=list)
    """Files affected by this changelist."""

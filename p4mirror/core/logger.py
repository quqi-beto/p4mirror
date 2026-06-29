"""Central logging module for P4Mirror.

Creates a timestamped log file per run and mirrors output to the console.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import IO


class P4MirrorLogger:
    """Logs migration progress to a timestamped file and the console.

    Parameters
    ----------
    log_dir : str or Path
        Directory where log files are written.
    build_number : int or None
        Jenkins build number (or any run identifier).
    """

    def __init__(
        self,
        log_dir: str | Path = "logs",
        build_number: int | None = None,
    ) -> None:
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)

        self._build_number = build_number
        self._start_time = datetime.now(timezone.utc)

        timestamp = self._start_time.strftime("%Y%m%d_%H%M%S")
        self._log_path = self._log_dir / f"{timestamp}.log"
        self._file: IO[str] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open the log file and write the run header."""
        self._file = self._log_path.open("w", encoding="utf-8")
        self._write_line("=" * 60)
        self._write_line("P4Mirror Migration Run")
        self._write_line(f"Start time:       {self._start_time.isoformat()}")
        if self._build_number is not None:
            self._write_line(f"Build number:     {self._build_number}")
        self._write_line("=" * 60)

    def info(self, message: str) -> None:
        """Log an informational message."""
        self._write_line(f"[INFO]  {message}")

    def error(self, message: str) -> None:
        """Log an error message."""
        self._write_line(f"[ERROR] {message}")

    def close_summary(
        self,
        changelists: int = 0,
        commits: int = 0,
        push_ok: bool = False,
        errors: list[str] | None = None,
    ) -> None:
        """Write the final summary and close the log file."""
        end_time = datetime.now(timezone.utc)
        duration = (end_time - self._start_time).total_seconds()

        self._write_line("=" * 60)
        self._write_line("Summary")
        self._write_line(f"End time:         {end_time.isoformat()}")
        self._write_line(f"Duration (s):     {duration:.1f}")
        self._write_line(f"Changelists:      {changelists}")
        self._write_line(f"Commits created:  {commits}")
        self._write_line(f"Push status:      {'OK' if push_ok else 'FAILED'}")

        if errors:
            self._write_line("Errors:")
            for err in errors:
                self._write_line(f"  - {err}")

        self._write_line("=" * 60)
        self._close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_line(self, text: str) -> None:
        """Write *text* to the log file (if open) and to stdout."""
        print(text)
        if self._file is not None and not self._file.closed:
            self._file.write(text + "\n")
            self._file.flush()

    def _close(self) -> None:
        if self._file is not None and not self._file.closed:
            self._file.close()
            self._file = None

    @property
    def log_path(self) -> Path:
        """Path to the current log file."""
        return self._log_path

    def __del__(self) -> None:
        self._close()

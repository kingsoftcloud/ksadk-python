"""Session backend exception types."""

from __future__ import annotations


class SessionBackendUnavailable(RuntimeError):
    """Raised when the configured session backend cannot be reached."""


class CheckpointScanRestartRequired(RuntimeError):
    """Raised when a checkpoint request must restart against one backend epoch."""

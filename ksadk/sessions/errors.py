"""Session backend exception types."""

from __future__ import annotations


class SessionBackendUnavailable(RuntimeError):
    """Raised when the configured session backend cannot be reached."""


class CheckpointScanRestartRequired(RuntimeError):
    """Raised when a checkpoint request must restart against one backend epoch."""


class HistoryProjectionBudgetExceeded(RuntimeError):
    """A complete invocation cannot fit within one bounded history projection."""


class HistoryReloadRequired(RuntimeError):
    """The supplied history cursor cannot be continued in this source epoch."""


class CheckpointLookupTimeout(RuntimeError):
    """Checkpoint lookup exceeded its operation deadline before execution."""

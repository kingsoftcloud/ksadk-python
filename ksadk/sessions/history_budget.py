"""Request-local byte allowance for bounded runtime history reads.

Only finite metadata/history HTTP actions install this context. Full Agent
execution, recovery replay, and SSE intentionally have no projection cap.
PostgreSQL uses it to check candidate JSON byte size before fetching bodies.
"""

from __future__ import annotations

from contextvars import ContextVar

from ksadk.sessions.errors import HistoryProjectionBudgetExceeded

MAX_HISTORY_READ_BYTES = 16 * 1024 * 1024


class HistoryReadBudget:
    def __init__(self, maximum: int = MAX_HISTORY_READ_BYTES) -> None:
        self.maximum = maximum
        self.used = 0

    @property
    def remaining(self) -> int:
        return max(0, self.maximum - self.used)

    def account(self, size: int) -> None:
        if size > self.remaining:
            raise HistoryProjectionBudgetExceeded(
                "History invocation exceeds 16 MiB of event JSON candidates"
            )
        self.used += size


CURRENT_HISTORY_READ_BUDGET: ContextVar[HistoryReadBudget | None] = ContextVar(
    "ksadk_history_read_budget", default=None,
)

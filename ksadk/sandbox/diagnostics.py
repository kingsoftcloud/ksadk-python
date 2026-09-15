"""Step-level E2B diagnostics without credential-bearing request dumps."""

from __future__ import annotations

import logging
import os
import re
import time
import traceback
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

logger = logging.getLogger(__name__)


def _safe_detail(value: str, secrets: tuple[str, ...]) -> str:
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            value = value.replace(secret, "[REDACTED]")
    # Exception text can contain signed URLs, headers or SDK-generated tokens.
    value = re.sub(r"https?://[^\s\"'<>]+", "[REDACTED_URL]", value)
    value = re.sub(
        r"(?i)(authorization|api[_-]?key|token|secret|password)\s*[:=]\s*[^\n]+",
        r"\1=[REDACTED]",
        value,
    )
    return value


@contextmanager
def e2b_diagnostic_step(
    step: str,
    *,
    runtime_id: str = "",
    sensitive_values: tuple[str, ...] = (),
    env: Mapping[str, str] | None = None,
) -> Iterator[None]:
    """Log progress and sanitized exception chains; preserve operation semantics."""
    started = time.monotonic()
    logger.info("E2B_DIAGNOSTIC step=%s status=started runtime_id=%s", step, runtime_id)
    try:
        yield
    except Exception as exc:
        secrets = (
            sensitive_values
            + tuple((env or {}).values())
            + tuple(
                value
                for key, value in os.environ.items()
                if any(part in key.upper() for part in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
            )
        )
        # No locals or source lines: either can contain literal credentials.
        trace = traceback.TracebackException.from_exception(exc, capture_locals=False)
        pending = [trace]
        while pending:
            current = pending.pop()
            current.stack = traceback.StackSummary.from_list(
                traceback.FrameSummary(frame.filename, frame.lineno, frame.name, line="")
                for frame in current.stack
            )
            pending.extend(
                item for item in (current.__cause__, current.__context__) if item is not None
            )
            pending.extend(getattr(current, "exceptions", None) or [])
        detail = _safe_detail("".join(trace.format(chain=True)), secrets)
        logger.error(
            "E2B_DIAGNOSTIC step=%s status=failed runtime_id=%s duration_ms=%d error_type=%s\n%s",
            step,
            runtime_id,
            int((time.monotonic() - started) * 1000),
            type(exc).__name__,
            detail,
        )
        raise
    else:
        logger.info(
            "E2B_DIAGNOSTIC step=%s status=completed runtime_id=%s duration_ms=%d",
            step,
            runtime_id,
            int((time.monotonic() - started) * 1000),
        )

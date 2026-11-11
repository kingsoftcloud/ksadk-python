"""Managed A2A phase caps, always bounded by the original invocation deadline."""

import logging
import math
import os

logger = logging.getLogger(__name__)


def seconds(name, default):
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
        if math.isfinite(value) and value > 0:
            return value
    except ValueError:
        pass
    logger.warning("Invalid %s value; using %.3f seconds", name, default)
    return default


def managed_invocation_timeouts():
    from ksadk.a2a._invocation_state import A2AInvocationTimeouts

    return A2AInvocationTimeouts(
        prepare_seconds=seconds("KSADK_A2A_PREPARE_SECONDS", 30.0),
        connect_seconds=seconds("KSADK_A2A_CONNECT_SECONDS", 30.0),
        first_event_seconds=seconds("KSADK_A2A_FIRST_EVENT_SECONDS", 300.0),
        idle_seconds=seconds("KSADK_A2A_IDLE_SECONDS", 300.0),
    )


def invocation_seconds(explicit=None):
    """One immutable invocation deadline; an explicit RemoteAgentSpec wins."""
    return explicit if explicit is not None else seconds("KSADK_A2A_INVOCATION_SECONDS", 600.0)

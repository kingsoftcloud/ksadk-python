"""Public canonical RuntimeEvent schema version 2.

The schema-v1 wire model intentionally lives only in :mod:`ksadk.events.v1_compat`.
"""

from ksadk.events.canonical import *  # noqa: F403
from ksadk.events.canonical import __all__
from ksadk.events.v1_compat import EventTypeV1


class _MergedEventType(EventTypeV1):
    """EventTypeV1 plus the v1 members added by the observability branch
    (user.message / turn.* / step.* / model.call.*) so that merged callers
    keep working on top of the canonical v2 event core."""

    USER_MESSAGE = "user.message"
    TURN_STARTED = "turn.started"
    TURN_COMPLETED = "turn.completed"
    STEP_STARTED = "step.started"
    STEP_COMPLETED = "step.completed"
    MODEL_CALL_BEGIN = "model.call.begin"
    MODEL_CALL_FIRST_TOKEN = "model.call.first_token"
    MODEL_CALL_END = "model.call.end"


# Alias kept for callers integrated before the canonical v2 refactor
# (studio observability/evaluation imports on merged branches).
EventType = _MergedEventType

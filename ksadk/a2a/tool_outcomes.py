"""Preserve uncertain remote outcomes at tool boundaries without another send."""

import logging
import re

from ksadk.common.diagnostics import safe_exception_locations
from ksadk.events.canonical import ItemFailed
from ksadk.events.content import DataContent

logger = logging.getLogger(__name__)


class A2ASendOutcomeUnknown(RuntimeError):
    def __init__(self, *, agent, scope_id, task_id=None):
        super().__init__("A2A_SEND_OUTCOME_UNKNOWN")
        self.result = {
            "ok": False, "agent": agent, "scope_id": scope_id,
            "task_id": task_id, "state": "unknown", "retryable": False,
            "reply": "",
            "error_type": "A2A_SEND_OUTCOME_UNKNOWN",
            "error_message": "Remote result is unknown. Do not resend the call in this run.",
        }


def unknown_service_outcome(service, scope_id):
    try:
        handle = service.handle(scope_id)
        snapshot = service.snapshot(handle)
    except KeyError:
        return None
    if snapshot is not None and snapshot.status == "unknown":
        return A2ASendOutcomeUnknown(
            agent=handle.target_agent_id, scope_id=scope_id,
            task_id=handle.platform_task_id,
        )
    return None


async def assert_target_outcome_known(authority, target_agent_id):
    """Block a model's replacement call to an uncertain target in this run.

    Read only typed failed status facts and the matching child descriptor.
    The authority scopes every query to the current run, so a later explicit
    user turn can proceed. Canonical facts also survive process replacement.
    """
    failures = await authority.list(
        authority.session_id, event_types=["item.failed"], item_kinds=["status"],
    )
    scopes = {event.scope_id for event in failures
              if isinstance(event, ItemFailed) and event.error.code == "A2A_SEND_OUTCOME_UNKNOWN"}
    for scope_id in scopes:
        descriptors = await authority.list(
            authority.session_id, scope_id=scope_id,
            event_types=["item.completed"], item_kinds=["data"],
        )
        for event in descriptors:
            snapshot = getattr(event, "snapshot", None)
            for part in snapshot.parts if snapshot is not None else ():
                data = part.data if isinstance(part, DataContent) else None
                if (isinstance(data, dict) and data.get("schema") == "execution.scope/v1"
                        and (data.get("agent") or {}).get("id") == target_agent_id):
                    raise A2ASendOutcomeUnknown(agent=target_agent_id, scope_id=scope_id)


def tool_error_result(exc):
    # Exception strings and frame locals may contain credentials/tool payloads.
    logger.warning("Managed A2A tool failure locations=%s", safe_exception_locations(exc))
    if isinstance(exc, A2ASendOutcomeUnknown):
        return dict(exc.result)
    code = str(exc)
    if not re.fullmatch(r"A2A_[A-Z0-9_]{1,100}", code):
        code = "A2A_INVOCATION_FAILED"
    return {"ok": False, "error_type": code, "error_message": "Managed A2A invocation failed"}

"""Recognize fresh gateway approvals without adopting historical tool results."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Optional

from ksadk.events.canonical import ItemCompleted, RuntimeEvent
from ksadk.events.content import DataContent, ToolCallContent, ToolResultContent
from ksadk.runtime.adapter import RunHandle
from ksadk.tools.gateway import approval_interrupt_info_from_result

if TYPE_CHECKING:
    from ksadk.runtime.runner_adapter import _ActiveRun


def _canonical_gateway_approvals(
    self, handle: RunHandle, run: Optional[_ActiveRun], event: ItemCompleted
) -> list[RuntimeEvent]:
    """Recognize local results without treating historical state as new calls."""
    calls = handle.native_ref.setdefault("_gateway_canonical_calls", {})
    historical_calls = handle.native_ref.setdefault("_gateway_historical_result_calls", {})

    def key(call_id):
        return json.dumps([event.run_id, event.scope_id, call_id])

    results = []
    for part in event.snapshot.parts:
        if isinstance(part, ToolCallContent):
            calls[key(part.call_id)] = {"name": part.name, "arguments": part.arguments}
        elif isinstance(part, ToolResultContent):
            results.append((part.call_id, part.result))
        elif (
            isinstance(part, DataContent)
            and event.source.metadata.get("channel") in {"updates", "values"}
            and isinstance(part.data, dict)
        ):
            # Some ToolNode versions only emit values. Remember already
            # present ToolMessage IDs before admitting new native calls,
            # so loading a checkpoint cannot reopen historical approvals.
            channel = event.source.metadata.get("channel")
            updates = [part.data] if channel == "values" else part.data.values()
            seen = handle.native_ref.setdefault("_gateway_canonical_results", {})
            for update in updates:
                if not isinstance(update, dict):
                    continue
                messages = update.get("messages", [])
                if not isinstance(messages, list):
                    messages = [messages]
                for message in messages:
                    if not isinstance(message, dict) or message.get("type") != "tool":
                        continue
                    message_id = message.get("id")
                    if not message_id:
                        continue
                    result_key = key(message_id)
                    if result_key in seen:
                        continue
                    seen[result_key] = True
                    call_id = message.get("tool_call_id")
                    if call_id and key(call_id) not in calls:
                        # This result was already present before a live
                        # call. Replayed native result parts must not
                        # reopen an approval from the restored snapshot.
                        historical_calls[key(call_id)] = True
                    results.append((call_id, message.get("content")))
    approvals = []
    for call_id, result in results:
        if key(call_id) in historical_calls:
            continue
        call = calls.get(key(call_id))
        if call is None:
            continue
        detail = approval_interrupt_info_from_result(
            result, fallback_tool_name=call["name"],
            tool_args=call["arguments"], run_id=call_id,
        )
        if detail is None:
            continue
        approval_id = str(detail.get("approval_request_id") or detail.get("id") or call_id)
        if approval_id in handle.native_ref.get("pending_approval_ids", []):
            continue
        approvals.extend(self._interaction_requested_from_approval(
            handle, run, detail=detail, call_id=call_id,
        ))
    return approvals

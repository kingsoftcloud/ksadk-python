"""Decode declared public observations without executing any remote tool."""

import json

from google.protobuf.json_format import MessageToDict

from ksadk.a2a.presentation import (
    A2A_TOOL_ACTIVITY_EXTENSION_URI,
    A2A_TOOL_ACTIVITY_MAX_INVOCATION_BYTES,
    ToolActivity,
)
from ksadk.events.canonical import ItemCompleted, ItemStarted, ItemUpdated, SourceRef
from ksadk.events.content import ContentSnapshot, DataContent, ToolCallContent, ToolResultContent
from ksadk.events.identity import stable_event_id, stable_item_id, stable_part_id


def map_tool_artifact(adapter, artifact, context, timestamp):
    """Return None for ordinary artifacts, safe diagnostic data for damaged extensions."""
    metadata = MessageToDict(artifact.metadata)
    declared = metadata.get("extension_uri")
    if not declared:
        return None
    state = adapter._tool_activities
    events = []
    for index, part in enumerate(artifact.parts):
        identity = stable_item_id("a2a-tool", context.scope_id, artifact.artifact_id, index)
        native = MessageToDict(part.data) if part.WhichOneof("content") == "data" else {}
        try:
            if declared != A2A_TOOL_ACTIVITY_EXTENSION_URI:
                raise ValueError("unknown extension")
            activity = ToolActivity.model_validate(native)
            # Struct map iteration order may differ between SSE and GetTask.
            fingerprint = json.dumps(
                activity.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            )
            previous = state.get(identity)
            if previous is not None and previous[0] == fingerprint:
                continue
            if previous is not None and previous[1]:
                from ksadk.events.adapters._a2a_support import _fail

                _fail(
                    "A2A_TOOL_CLOSED_CONFLICT", "artifact", "closed public tool observation changed"
                )
            size = len(fingerprint.encode())
            if adapter._tool_activity_bytes + size > A2A_TOOL_ACTIVITY_MAX_INVOCATION_BYTES:
                raise ValueError("extension budget")
            adapter._tool_activity_bytes += size
            call_id = stable_item_id("a2a-tool", context.scope_id, activity.call_id)
            part_id = stable_part_id("a2a", identity, "public-tool")
            content = (
                ToolCallContent(
                    part_id=part_id,
                    call_id=call_id,
                    name=activity.name,
                    arguments=activity.arguments,
                )
                if activity.item_kind == "tool_call"
                else ToolResultContent(
                    part_id=part_id,
                    call_id=call_id,
                    result=activity.result,
                    is_error=activity.is_error,
                )
            )
            kind = activity.item_kind
            terminal = activity.operation in {"completed", "failed"}
            occurrence = activity.event_id
            state[identity] = (fingerprint, terminal)
        except ValueError as exc:
            if getattr(exc, "code", None) == "A2A_TOOL_CLOSED_CONFLICT":
                raise
            # Never expose malformed/raw extension data in diagnostics.
            occurrence = "extension-degraded"
            if identity in state:
                continue
            content = DataContent(
                part_id=stable_part_id("a2a", identity, "diagnostic"),
                data={"code": "A2A_TOOL_ACTIVITY_UNAVAILABLE"},
            )
            kind, terminal, previous = "status", True, None
            state[identity] = (occurrence, True)
        common = dict(
            schema_version=2,
            timestamp=timestamp,
            run_id=context.run_id,
            scope_id=context.scope_id,
            parent_scope_id=context.parent_scope_id,
            source=SourceRef(
                framework="a2a",
                native_run_id=context.native_run_id,
                native_item_id=artifact.artifact_id,
                metadata={"remote_observation": True},
            ),
        )

        def env(phase):
            return dict(
                common,
                seq=context.allocate_placeholder_seq(),
                event_id=stable_event_id(
                    "a2a", context.scope_id, identity, phase, "public-tool", occurrence, 0
                ),
            )

        if previous is None:
            events.append(ItemStarted(**env("start"), item_id=identity, item_kind=kind))
        if terminal:
            events.append(
                ItemCompleted(
                    **env("complete"),
                    item_id=identity,
                    item_kind=kind,
                    snapshot=ContentSnapshot(parts=(content,)),
                )
            )
        else:
            events.append(
                ItemUpdated(
                    **env("update"), item_id=identity, item_kind=kind, op="replace", update=content
                )
            )
    return tuple(events)

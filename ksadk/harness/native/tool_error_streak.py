"""Host-owned, compaction-proof progress for repeated definite tool errors."""

from __future__ import annotations

import hashlib
import json

from .ports import ToolDefinition, ToolResult
from .state import RunSnapshot, ToolErrorAction

REPEATED_ERROR_STOP_THRESHOLD = 6


def advance_tool_error_streak(
    snapshot: RunSnapshot,
    result: ToolResult,
    definition: ToolDefinition | None = None,
) -> dict:
    """Count model decisions with the same error until goal or workspace changes.

    A successful read, plan update or unrelated tool receipt does not prove
    that the failing action was repaired. Keep its fingerprint across those
    receipts; a changed workspace observation resets the next comparison. A
    single tool batch counts once: the model cannot correct its later calls
    using results it has not received. Budgets still count every actual call.
    Independent readonly queries can legitimately return the same generic
    error. Their admitted inputs distinguish the failing query; this is not
    successful progress or task acceptance. Writes and browser observations
    retain their stronger, input-independent failure identity.
    """
    signature = snapshot.repeated_tool_error_signature
    count = snapshot.repeated_tool_error_count
    model_call = snapshot.repeated_tool_error_model_call
    if result.status == "error":
        tool_name = snapshot.pending_tools[0]["name"]
        if (
            definition is not None
            and definition.name == tool_name
            and definition.observation_role == "browser"
            and result.progress_observation_digest
        ):
            # Only the matching host binding admits the Port's semantic page
            # identity. Action IDs and artifact locations must not create a
            # new error; arbitrary tool content cannot supply this authority.
            payload = [tool_name, result.status, result.progress_observation_digest]
        else:
            payload = [
                tool_name,
                result.status,
                result.content,
                [block.model_dump(mode="json") for block in result.content_blocks],
            ]
            if (
                definition is not None
                and definition.name == tool_name
                and definition.read_only
                and definition.observation_role == "none"
            ):
                # Authority belongs to the matching locked host definition,
                # never an argument or result that claims to be readonly.
                payload.append(snapshot.pending_tools[0]["arguments"])
        signature = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        same_observation = (
            signature == snapshot.repeated_tool_error_signature
            and snapshot.goal_revision == snapshot.repeated_tool_error_goal_revision
            and snapshot.workspace_revision == snapshot.repeated_tool_error_workspace_revision
        )
        if same_observation:
            count += int(model_call != snapshot.model_calls)
        else:
            count = 1
        model_call = snapshot.model_calls
    return {
        "repeated_tool_error_signature": signature,
        "repeated_tool_error_count": count,
        "repeated_tool_error_model_call": model_call,
        "repeated_tool_error_goal_revision": snapshot.goal_revision,
        "repeated_tool_error_workspace_revision": snapshot.workspace_revision,
        **({"repeated_tool_error_action": ToolErrorAction(
            name=tool_name,
            arguments_digest=(hashlib.sha256(json.dumps(
                snapshot.pending_tools[0]["arguments"], sort_keys=True,
                ensure_ascii=False, separators=(",", ":"),
            ).encode()).hexdigest() if definition is not None
                and definition.name == tool_name and definition.read_only
                and definition.observation_role == "none" else None),
        )} if result.status == "error" else {}),
    }


def tool_error_streak_exhausted(snapshot: RunSnapshot) -> bool:
    return (
        snapshot.repeated_tool_error_count >= REPEATED_ERROR_STOP_THRESHOLD
        and snapshot.goal_revision == snapshot.repeated_tool_error_goal_revision
        and snapshot.workspace_revision == snapshot.repeated_tool_error_workspace_revision
    )

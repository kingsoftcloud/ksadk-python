"""Codex adapter 的常量、状态 dataclass 与内容构造辅助（纯移动自 codex，行为不变）。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Callable, Literal, cast

from pydantic import JsonValue

from ksadk.events.adapters._codex_validators import (
    _approval_decision,
    _fail,
    _json_value,
    _mapping,
    _nonnegative_int,
    _optional_int,
    _required_string,
    _required_text,
    _string_sequence,
    _validated_user_input_answers,
)
from ksadk.events.canonical import (
    ApprovalResponse,
    EventPhase,
    ItemKind,
    SourceRef,
    StructuredInputRequest,
)
from ksadk.events.content import (
    ArtifactContent,
    ContentSnapshot,
    ContentValue,
    DataContent,
    TextContent,
    ToolCallContent,
    ToolResultContent,
)
from ksadk.events.identity import stable_event_id, stable_item_id, stable_part_id, stable_scope_id

_CODEX_0_147_0_NOTIFICATION_METHODS = frozenset("""
    account/login/completed account/rateLimits/updated account/updated app/list/updated
    command/exec/outputDelta configWarning deprecationNotice error
    externalAgentConfig/import/completed externalAgentConfig/import/progress fs/changed
    fuzzyFileSearch/sessionCompleted fuzzyFileSearch/sessionUpdated guardianWarning
    hook/completed hook/started item/agentMessage/delta item/autoApprovalReview/completed
    item/autoApprovalReview/started item/commandExecution/outputDelta
    item/commandExecution/terminalInteraction item/completed item/fileChange/outputDelta
    item/fileChange/patchUpdated item/mcpToolCall/progress item/plan/delta
    item/reasoning/summaryPartAdded item/reasoning/summaryTextDelta item/reasoning/textDelta
    item/started mcpServer/oauthLogin/completed mcpServer/startupStatus/updated
    model/rerouted model/safetyBuffering/updated model/verification process/exited
    process/outputDelta remoteControl/status/changed serverRequest/resolved skills/changed
    thread/archived thread/closed thread/compacted thread/deleted thread/goal/cleared
    thread/environment/connected thread/environment/disconnected
    thread/goal/updated thread/name/updated thread/realtime/closed thread/realtime/error
    thread/realtime/itemAdded thread/realtime/outputAudio/delta thread/realtime/sdp
    thread/realtime/started thread/realtime/transcript/delta thread/realtime/transcript/done
    thread/settings/updated thread/started thread/status/changed thread/tokenUsage/updated
    thread/unarchived turn/completed turn/diff/updated turn/moderationMetadata
    turn/plan/updated turn/started warning windows/worldWritableWarning
    windowsSandbox/setupCompleted
    """.split())

_CODEX_0_144_4_DATA_ITEM_KINDS = frozenset("""
    userMessage hookPrompt subAgentActivity imageView sleep enteredReviewMode
    exitedReviewMode contextCompaction
    """.split())


# Methods that carry item-lifecycle semantics and need thread/turn scoping.
_ITEM_METHODS = frozenset("""
    error item/started item/completed item/agentMessage/delta item/reasoning/textDelta
    item/reasoning/summaryPartAdded item/reasoning/summaryTextDelta
    item/commandExecution/outputDelta item/mcpToolCall/progress
    item/fileChange/patchUpdated item/fileChange/outputDelta item/plan/delta
    """.split())
_INTERACTION_METHODS = frozenset("""
    item/commandExecution/requestApproval item/fileChange/requestApproval
    item/permissions/requestApproval item/tool/call item/tool/requestUserInput
    mcpServer/elicitation/request
    """.split())
_CONTROL_INTERACTION_METHODS = frozenset(
    {"account/chatgptAuthTokens/refresh", "attestation/generate"}
)
_APPROVAL_KINDS = {
    "item/commandExecution/requestApproval": "command_execution",
    "item/fileChange/requestApproval": "file_change",
    "item/permissions/requestApproval": "permissions",
    "item/tool/call": "dynamic_tool_call",
}
# native item kind -> canonical (item_kind, default phase); agentMessage is validated separately.
_ITEM_KIND_PHASES: dict[str, tuple[ItemKind, EventPhase]] = {
    "agentMessage": ("message", "final_answer"),
    "reasoning": ("reasoning", "commentary"),
    "commandExecution": ("tool_call", "commentary"),
    "mcpToolCall": ("tool_call", "commentary"),
    "dynamicToolCall": ("tool_call", "commentary"),
    "collabAgentToolCall": ("tool_call", "commentary"),
    "webSearch": ("tool_call", "commentary"),
    "fileChange": ("data", "commentary"),
    "plan": ("data", "commentary"),
    "imageGeneration": ("artifact", "commentary"),
    **{kind: ("data", "commentary") for kind in _CODEX_0_144_4_DATA_ITEM_KINDS},
}
# native item kind -> statuses that terminate the item as failed.
_ITEM_FAIL_STATUSES = {
    "commandExecution": frozenset({"failed", "declined"}),
    "mcpToolCall": frozenset({"failed"}),
    "fileChange": frozenset({"failed", "declined"}),
    "dynamicToolCall": frozenset({"failed"}),
    "collabAgentToolCall": frozenset({"failed"}),
}
_FAILURE_CODE_KINDS = {
    "commandExecution": "command",
    "mcpToolCall": "mcp_tool",
    "fileChange": "file_change",
}


@dataclass
class CodexAdapterContext:
    """Runtime identity and deterministic pre-store placeholder ordering."""

    run_id: str
    initial_seq: int = 0
    _next_seq: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.run_id = _required_string(self.run_id, "runtime run_id")
        if self.initial_seq < 0:
            raise ValueError("Codex initial_seq must be non-negative")
        self._next_seq = self.initial_seq

    def allocate_placeholder_seq(self) -> int:
        value = self._next_seq
        self._next_seq += 1
        return value


@dataclass
class _ItemState:
    scope_id: str
    thread_id: str
    turn_id: str
    native_item_id: str
    native_item_kind: str
    item_id: str
    item_kind: ItemKind
    phase: EventPhase
    part_ids: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _InteractionState:
    request_id: str
    interaction_id: str
    interaction_kind: Literal["approval", "structured_input"]
    scope_id: str
    thread_id: str
    turn_id: str
    native_item_id: str
    method: str
    interrupts_run: bool
    question_ids: frozenset[str] = frozenset()
    secret_question_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _ReplayRecord:
    payload_digest: str
    event_ids: tuple[str, ...]


def _elicitation_request(params: Mapping[str, Any]) -> StructuredInputRequest:
    mode = _required_string(params.get("mode"), "params.mode")
    prompt = _required_text(params.get("message"), "params.message")
    if mode in {"form", "openai/form"}:
        schema_value = _mapping(params.get("requestedSchema"), "params.requestedSchema")
        schema = cast(dict[str, JsonValue], _json_value(schema_value))
    elif mode == "url":
        schema = {
            "type": "object",
            "x-codex-elicitation-url": _required_text(params.get("url"), "params.url"),
            "x-codex-elicitation-id": _required_text(
                params.get("elicitationId"), "params.elicitationId"
            ),
        }
    else:
        _fail(
            "invalid_interaction_request",
            "params.mode",
            f"Unsupported MCP elicitation mode: {mode}",
        )
    return StructuredInputRequest(prompt=prompt, schema=schema)


def _control_refresh_request(params: Mapping[str, Any]) -> StructuredInputRequest:
    reason = _required_string(params.get("reason"), "params.reason")
    if reason != "unauthorized":
        _fail(
            "invalid_interaction_request",
            "params.reason",
            f"Unsupported ChatGPT token refresh reason: {reason}",
        )
    previous_account_id = params.get("previousAccountId")
    if previous_account_id is not None:
        _required_string(previous_account_id, "params.previousAccountId")
    return StructuredInputRequest(
        prompt="Refresh ChatGPT authentication tokens",
        schema={
            "type": "object",
            "properties": {
                "accessToken": {"type": "string"},
                "chatgptAccountId": {"type": "string"},
                "chatgptPlanType": {"type": ["string", "null"]},
            },
            "required": ["accessToken", "chatgptAccountId"],
            "x-codex-request": _json_value(params),
        },
    )


def _control_attestation_request(params: Mapping[str, Any]) -> StructuredInputRequest:
    if params:
        _fail(
            "invalid_interaction_request",
            "params",
            "Codex attestation/generate params must be empty",
        )
    return StructuredInputRequest(
        prompt="Generate an upstream attestation token",
        schema={
            "type": "object",
            "properties": {"token": {"type": "string"}},
            "required": ["token"],
        },
    )


_CONTROL_REQUEST_BUILDERS: dict[str, Callable[[Mapping[str, Any]], StructuredInputRequest]] = {
    "account/chatgptAuthTokens/refresh": _control_refresh_request,
    "attestation/generate": _control_attestation_request,
}


def _approval_response(method: str, result: Mapping[str, Any]) -> ApprovalResponse:
    if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
        if result.get("decision") is None:
            _fail(
                "missing_native_identity",
                "result.decision",
                "Codex approval result.decision is required",
            )
        return ApprovalResponse(
            decision=_approval_decision(result.get("decision")),
            data=_json_value(result),
        )
    if method == "item/permissions/requestApproval":
        return ApprovalResponse(decision="approved", data=_json_value(result))
    # item/tool/call; state creation exhaustively validates the method.
    success = result.get("success")
    if not isinstance(success, bool):
        _fail(
            "invalid_interaction_response",
            "result.success",
            "Codex dynamic tool result.success must be a boolean",
        )
    return ApprovalResponse(
        decision="approved" if success else "rejected", data=_json_value(result)
    )


def _structured_response_data(
    state: _InteractionState, result: Mapping[str, Any]
) -> dict[str, JsonValue]:
    if state.method == "account/chatgptAuthTokens/refresh":
        _required_string(result.get("accessToken"), "result.accessToken")
        account_id = _required_string(result.get("chatgptAccountId"), "result.chatgptAccountId")
        plan_type = result.get("chatgptPlanType")
        if plan_type is not None:
            _required_string(plan_type, "result.chatgptPlanType")
        return {
            "accessTokenPresent": True,
            "chatgptAccountId": account_id,
            "chatgptPlanType": cast(JsonValue, plan_type),
        }
    if state.method == "attestation/generate":
        _required_string(result.get("token"), "result.token")
        return {"tokenPresent": True}
    if state.method == "item/tool/requestUserInput":
        return _validated_user_input_answers(result, state.question_ids, state.secret_question_ids)
    return cast(dict[str, JsonValue], _json_value(result))


def _item_state(
    scope_id: str,
    thread_id: str,
    turn_id: str,
    native_item_id: str,
    native_kind: str,
    item: Mapping[str, Any],
) -> _ItemState:
    item_id = stable_item_id("codex", scope_id, native_kind, native_item_id)
    rule = _ITEM_KIND_PHASES.get(native_kind)
    item_kind: ItemKind
    phase: EventPhase
    if native_kind == "agentMessage":
        item_kind, phase = "message", _agent_message_phase(item.get("phase"))
    elif rule is not None:
        item_kind, phase = rule
    else:
        _fail(
            "unsupported_item_kind",
            "params.item.type",
            f"Unsupported Codex item type: {native_kind}",
        )
    return _ItemState(
        scope_id=scope_id,
        thread_id=thread_id,
        turn_id=turn_id,
        native_item_id=native_item_id,
        native_item_kind=native_kind,
        item_id=item_id,
        item_kind=item_kind,
        phase=phase,
    )


def _agent_message_phase(value: Any) -> EventPhase:
    if value in {None, "final_answer"}:
        return "final_answer"
    if value != "commentary":
        _fail("invalid_item_phase", "params.item.phase", f"Unsupported Codex phase: {value}")
    return "commentary"


def _part_id(state: _ItemState, native_part_kind: str, native_part_id: str) -> str:
    lane = f"{native_part_kind}:{native_part_id}"
    part_id = state.part_ids.get(lane)
    if part_id is None:
        part_id = stable_part_id("codex", state.item_id, native_part_kind, native_part_id)
        state.part_ids[lane] = part_id
    return part_id


# --- item content builders (identity translation only) -----------------------


def _text_part(state: _ItemState, lane: str, text: str) -> TextContent:
    return TextContent(part_id=_part_id(state, lane, "primary"), text=text)


def _reasoning_parts(state: _ItemState, item: Mapping[str, Any]) -> tuple[TextContent, ...]:
    summary = _string_sequence(item.get("summary"), "params.item.summary")
    content = _string_sequence(item.get("content"), "params.item.content")
    return tuple(
        TextContent(part_id=_part_id(state, "reasoning_summary", str(index)), text=text)
        for index, text in enumerate(summary)
    ) + tuple(
        TextContent(part_id=_part_id(state, "reasoning_content", str(index)), text=text)
        for index, text in enumerate(content)
    )


def _command_call(state: _ItemState, item: Mapping[str, Any]) -> ToolCallContent:
    return ToolCallContent(
        part_id=_part_id(state, "command_call", "primary"),
        call_id=state.native_item_id,
        name="codex.command",
        arguments={
            "command": _required_text(item.get("command"), "params.item.command"),
            "cwd": _required_text(item.get("cwd"), "params.item.cwd"),
            "commandActions": _json_value(item.get("commandActions")),
        },
    )


def _terminal_status(item: Mapping[str, Any], allowed: set[str], label: str) -> str:
    status = _required_string(item.get("status"), "params.item.status")
    if status not in allowed:
        _fail(
            "invalid_item_snapshot",
            "params.item.status",
            f"{label} completed with non-terminal status: {status}",
        )
    return status


def _command_result(state: _ItemState, item: Mapping[str, Any]) -> ToolResultContent:
    status = _terminal_status(item, {"completed", "failed", "declined"}, "Command")
    exit_code = item.get("exitCode")
    if exit_code is not None and not isinstance(exit_code, int):
        _fail("invalid_item_snapshot", "params.item.exitCode", "Codex exitCode must be an integer")
    return ToolResultContent(
        part_id=_part_id(state, "command_result", "primary"),
        call_id=state.native_item_id,
        result={
            "status": status,
            "exit_code": exit_code,
            "duration_ms": _optional_int(item.get("durationMs"), "params.item.durationMs"),
            "output": _required_text(
                item.get("aggregatedOutput") or "", "params.item.aggregatedOutput"
            ),
            "process_id": item.get("processId"),
            "source": item.get("source"),
        },
        is_error=status in {"failed", "declined"}
        or (isinstance(exit_code, int) and exit_code != 0),
    )


def _mcp_call(state: _ItemState, item: Mapping[str, Any]) -> ToolCallContent:
    server = _required_string(item.get("server"), "params.item.server")
    tool = _required_string(item.get("tool"), "params.item.tool")
    return ToolCallContent(
        part_id=_part_id(state, "mcp_call", "primary"),
        call_id=state.native_item_id,
        name=f"mcp.{server}.{tool}",
        arguments=_json_value(item.get("arguments")),
    )


def _mcp_result(state: _ItemState, item: Mapping[str, Any]) -> ToolResultContent:
    status = _terminal_status(item, {"completed", "failed"}, "MCP call")
    result = _json_value(item.get("result"))
    error = _json_value(item.get("error"))
    result_value: dict[str, JsonValue] = {"status": status}
    if isinstance(result, dict):
        result_value.update(result)
    elif result is not None:
        result_value["result"] = result
    result_value["duration_ms"] = _optional_int(item.get("durationMs"), "params.item.durationMs")
    if error is not None:
        result_value["error"] = error
    return ToolResultContent(
        part_id=_part_id(state, "mcp_result", "primary"),
        call_id=state.native_item_id,
        result=result_value,
        is_error=status == "failed",
    )


def _file_change(state: _ItemState, item: Mapping[str, Any]) -> DataContent:
    changes = _json_value(item.get("changes"))
    if not isinstance(changes, list):
        _fail("invalid_item_snapshot", "params.item.changes", "Codex file changes must be an array")
    return DataContent(
        part_id=_part_id(state, "file_changes", "primary"),
        data={
            "changes": changes,
            "status": _required_string(item.get("status"), "params.item.status"),
        },
    )


def _generic_item_data(state: _ItemState, item: Mapping[str, Any]) -> DataContent:
    return DataContent(part_id=_part_id(state, "native_item", "primary"), data=_json_value(item))


def _additional_tool_call(state: _ItemState, item: Mapping[str, Any]) -> ToolCallContent:
    kind = state.native_item_kind
    if kind == "dynamicToolCall":
        name = _required_string(item.get("tool"), "params.item.tool")
        arguments: JsonValue = {
            "arguments": _json_value(item.get("arguments")),
            "namespace": _json_value(item.get("namespace")),
        }
    elif kind == "collabAgentToolCall":
        name = f"codex.collab.{_required_string(item.get('tool'), 'params.item.tool')}"
        arguments = cast(
            JsonValue,
            {
                "senderThreadId": _json_value(item.get("senderThreadId")),
                "receiverThreadIds": _json_value(item.get("receiverThreadIds")),
                "prompt": _json_value(item.get("prompt")),
                "model": _json_value(item.get("model")),
                "reasoningEffort": _json_value(item.get("reasoningEffort")),
            },
        )
    else:  # webSearch; caller exhaustively validates native kind
        name = "codex.web_search"
        arguments = {
            "query": _required_text(item.get("query"), "params.item.query"),
            "action": _json_value(item.get("action")),
        }
    return ToolCallContent(
        part_id=_part_id(state, "tool_call", "primary"),
        call_id=state.native_item_id,
        name=name,
        arguments=arguments,
    )


def _additional_tool_result(state: _ItemState, item: Mapping[str, Any]) -> ToolResultContent:
    kind = state.native_item_kind
    if kind in {"dynamicToolCall", "collabAgentToolCall"}:
        label = "Dynamic" if kind == "dynamicToolCall" else "Collab"
        status = _terminal_status(item, {"completed", "failed"}, label + " tool")
        if kind == "dynamicToolCall":
            result: JsonValue = {
                "status": status,
                "success": _json_value(item.get("success")),
                "contentItems": _json_value(item.get("contentItems")),
                "durationMs": _json_value(item.get("durationMs")),
            }
            is_error = status == "failed" or item.get("success") is False
        else:
            result = {"status": status, "agentsStates": _json_value(item.get("agentsStates"))}
            is_error = status == "failed"
    else:  # webSearch
        result = {"action": _json_value(item.get("action"))}
        is_error = False
    return ToolResultContent(
        part_id=_part_id(state, "tool_result", "primary"),
        call_id=state.native_item_id,
        result=result,
        is_error=is_error,
    )


def _image_artifact(state: _ItemState, item: Mapping[str, Any]) -> ArtifactContent:
    result = _required_text(item.get("result"), "params.item.result")
    saved_path = item.get("savedPath")
    if saved_path is not None and not isinstance(saved_path, str):
        _fail(
            "invalid_item_snapshot", "params.item.savedPath", "Codex image savedPath must be text"
        )
    return ArtifactContent(
        part_id=_part_id(state, "image", "primary"),
        artifact_id=state.native_item_id,
        name=(saved_path.rsplit("/", 1)[-1] if saved_path else state.native_item_id),
        uri=result or saved_path,
        data={
            "status": _required_string(item.get("status"), "params.item.status"),
            "revisedPrompt": _json_value(item.get("revisedPrompt")),
            "result": result,
            "savedPath": _json_value(saved_path),
        },
    )


# A part builder returns one ContentValue, or a tuple of them (reasoning lists).
_PART_BUILDER = Callable[[_ItemState, Mapping[str, Any]], Any]
# native item kind -> (initial part builders, completed part builders)
_ITEM_SNAPSHOT_BUILDERS: dict[str, tuple[tuple[_PART_BUILDER, ...], tuple[_PART_BUILDER, ...]]] = {
    "agentMessage": (
        (),
        (lambda s, i: _text_part(s, "text", _required_text(i.get("text"), "params.item.text")),),
    ),
    "reasoning": ((), (_reasoning_parts,)),
    "plan": (
        (),
        (
            lambda s, i: _text_part(
                s, "plan_text", _required_text(i.get("text"), "params.item.text")
            ),
        ),
    ),
    "commandExecution": ((_command_call,), (_command_call, _command_result)),
    "mcpToolCall": ((_mcp_call,), (_mcp_call, _mcp_result)),
    "fileChange": ((_file_change,), (_file_change,)),
    "dynamicToolCall": ((_additional_tool_call,), (_additional_tool_call, _additional_tool_result)),
    "collabAgentToolCall": (
        (_additional_tool_call,),
        (_additional_tool_call, _additional_tool_result),
    ),
    "webSearch": ((_additional_tool_call,), (_additional_tool_call, _additional_tool_result)),
    "imageGeneration": ((_image_artifact,), (_image_artifact,)),
    **{
        kind: ((_generic_item_data,), (_generic_item_data,))
        for kind in _CODEX_0_144_4_DATA_ITEM_KINDS
    },
}


def _build_snapshot(
    state: _ItemState, item: Mapping[str, Any], *, completed: bool
) -> ContentSnapshot:
    builders = _ITEM_SNAPSHOT_BUILDERS[state.native_item_kind][1 if completed else 0]
    parts: tuple[Any, ...] = ()
    for builder in builders:
        built = builder(state, item)
        parts += built if isinstance(built, tuple) else (built,)
    return ContentSnapshot(parts=parts)


def _initial_snapshot(state: _ItemState, item: Mapping[str, Any]) -> ContentSnapshot | None:
    if state.native_item_kind in {"agentMessage", "reasoning", "plan"}:
        return None
    return _build_snapshot(state, item, completed=False)


def _completed_snapshot(state: _ItemState, item: Mapping[str, Any]) -> ContentSnapshot:
    return _build_snapshot(state, item, completed=True)


def _item_update(
    method: str,
    params: Mapping[str, Any],
    state: _ItemState,
) -> tuple[Literal["append", "replace"], ContentValue]:
    rule = _ITEM_UPDATE_RULES.get(method)
    if rule is None or state.native_item_kind != rule[0]:
        _fail(
            "unsupported_item_mutation",
            "method",
            f"Codex method {method!r} does not match {state.native_item_kind!r}",
        )
    return rule[1](state, params)


def _delta(part_kind: str, field_name: str) -> Callable[..., tuple[Literal["append"], TextContent]]:
    def build(
        state: _ItemState, params: Mapping[str, Any]
    ) -> tuple[Literal["append"], TextContent]:
        return (
            "append",
            TextContent(
                part_id=_part_id(state, part_kind, "primary"),
                text=_required_text(params.get("delta"), field_name),
            ),
        )

    return build


def _indexed_delta(
    part_kind: str,
) -> Callable[..., tuple[Literal["append"], TextContent]]:
    def build(
        state: _ItemState, params: Mapping[str, Any]
    ) -> tuple[Literal["append"], TextContent]:
        index = _nonnegative_int(params.get("contentIndex"), "params.contentIndex")
        return (
            "append",
            TextContent(
                part_id=_part_id(state, part_kind, str(index)),
                text=_required_text(params.get("delta"), "params.delta"),
            ),
        )

    return build


def _summary_update(
    part_added: bool,
) -> Callable[..., tuple[Literal["append", "replace"], TextContent]]:
    def build(
        state: _ItemState, params: Mapping[str, Any]
    ) -> tuple[Literal["append", "replace"], TextContent]:
        summary_index = _nonnegative_int(params.get("summaryIndex"), "params.summaryIndex")
        delta = "" if part_added else _required_text(params.get("delta"), "params.delta")
        return (
            "replace" if part_added else "append",
            TextContent(
                part_id=_part_id(state, "reasoning_summary", str(summary_index)),
                text=delta,
            ),
        )

    return build


def _mcp_progress(
    state: _ItemState, params: Mapping[str, Any]
) -> tuple[Literal["replace"], TextContent]:
    return (
        "replace",
        TextContent(
            part_id=_part_id(state, "mcp_progress", "primary"),
            text=_required_text(params.get("message"), "params.message"),
        ),
    )


def _patch_updated(
    state: _ItemState, params: Mapping[str, Any]
) -> tuple[Literal["replace"], DataContent]:
    changes = _json_value(params.get("changes"))
    if not isinstance(changes, list):
        _fail("invalid_item_update", "params.changes", "Codex file changes must be an array")
    return (
        "replace",
        DataContent(
            part_id=_part_id(state, "file_changes", "primary"),
            data={"changes": changes, "status": "inProgress"},
        ),
    )


# method -> (expected native item kind, update builder)
_ITEM_UPDATE_RULES: dict[
    str,
    tuple[
        str,
        Callable[
            [_ItemState, Mapping[str, Any]],
            tuple[Literal["append", "replace"], ContentValue],
        ],
    ],
] = {
    "item/agentMessage/delta": ("agentMessage", _delta("text", "params.delta")),
    "item/reasoning/textDelta": ("reasoning", _indexed_delta("reasoning_content")),
    "item/reasoning/summaryPartAdded": ("reasoning", _summary_update(part_added=True)),
    "item/reasoning/summaryTextDelta": ("reasoning", _summary_update(part_added=False)),
    "item/commandExecution/outputDelta": (
        "commandExecution",
        _delta("command_output", "params.delta"),
    ),
    "item/fileChange/outputDelta": ("fileChange", _delta("file_output", "params.delta")),
    "item/plan/delta": ("plan", _delta("plan_text", "params.delta")),
    "item/mcpToolCall/progress": ("mcpToolCall", _mcp_progress),
    "item/fileChange/patchUpdated": ("fileChange", _patch_updated),
}


def _item_failed(state: _ItemState, item: Mapping[str, Any]) -> bool:
    fail_statuses = _ITEM_FAIL_STATUSES.get(state.native_item_kind)
    return fail_statuses is not None and item.get("status") in fail_statuses


def _source(method: str, cursor: str, state: _ItemState) -> SourceRef:
    source = _protocol_source(
        method=method,
        cursor=cursor,
        thread_id=state.thread_id,
        turn_id=state.turn_id,
        native_item_id=state.native_item_id,
    )
    return source.model_copy(
        update={"metadata": {**source.metadata, "native_item_kind": state.native_item_kind}}
    )


def _protocol_source(
    *,
    method: str,
    cursor: str,
    thread_id: str,
    turn_id: str,
    native_item_id: str | None,
    native_event_id: str | None = None,
) -> SourceRef:
    return SourceRef(
        framework="codex",
        native_event_id=native_event_id,
        native_cursor=cursor,
        native_run_id=turn_id,
        native_item_id=native_item_id,
        metadata=cast(
            dict[str, JsonValue],
            {
                "app_server_version": _installed_app_server_version(),
                "method": method,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "cursor_semantics": "jsonl",
            },
        ),
    )


@lru_cache(maxsize=1)
def _installed_app_server_version() -> str:
    """Report the packaged Codex runtime version instead of a stale constant."""

    try:
        return version("openai-codex")
    except PackageNotFoundError:
        return "unknown"


def _thread_continuation_identity(thread_id: str) -> tuple[str, str]:
    scope_id = stable_scope_id("codex", thread_id, "thread_resume")
    return scope_id, stable_item_id("codex", scope_id, "thread_resume", thread_id)


def _envelope(
    context: CodexAdapterContext,
    cursor: str,
    timestamp: float,
) -> Callable[[str, str, str, str, SourceRef], dict[str, Any]]:
    def env(
        scope_id: str, identity: str, event_type: str, part_id: str, source: SourceRef
    ) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "event_id": stable_event_id(
                "codex", scope_id, identity, event_type, part_id, cursor, 0
            ),
            "seq": context.allocate_placeholder_seq(),
            "timestamp": timestamp,
            "run_id": context.run_id,
            "scope_id": scope_id,
            "source": source,
        }

    return env

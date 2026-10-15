"""Explicit, host-injected bridges for native ports; no runtime SDK imports."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar

from pydantic import BaseModel, JsonValue

from .effect_admission import check_effect_admission
from .model import ModelImageContent, ModelTurnResult, from_reasoning_turn
from .ports import ModelRequest, ToolDefinition, ToolRequest, ToolResult, digest
from .state import RunScope
from .tool_input_validation import ToolInputValidator

if TYPE_CHECKING:
    from ksadk.harness.reasoner import HarnessReasoningTurn

ToolCallback = Callable[[ToolRequest], Awaitable[ToolResult]]
AuthorizationResult = Literal["allow", "deny"] | ToolResult
ToolAuthorizer = Callable[[ToolRequest], Awaitable[AuthorizationResult]]
_Model = TypeVar("_Model", bound=BaseModel)


def _encode(model: BaseModel) -> bytes:
    return json.dumps(
        model.model_dump(mode="python"),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _snapshot(model: _Model) -> _Model:
    # Pydantic's frozen setting is shallow. Round-tripping detaches nested JSON
    # and revalidates even objects created with model_construct/model_copy.
    return type(model).model_validate_json(_encode(model))


@dataclass(frozen=True)
class ToolBinding:
    """A trusted definition and callback supplied by the host, never the model.

    The callback must return ToolResult explicitly and preserve operation_key
    when delegating to a service with its own idempotency/receipt protocol.
    """

    definition: ToolDefinition
    callback: ToolCallback


@dataclass(frozen=True)
class _RegisteredTool:
    definition_digest: str
    validator: ToolInputValidator
    callback: ToolCallback


class StrictToolExecutor:
    """Validate scope, pinned definition, arguments and current policy per call.

    Only the exact policy result ``allow`` permits execution. An authenticated
    interaction is input to policy, never a permission bit interpreted here.
    Direct callers receive bounded results. Native runtime requests explicitly
    defer final shaping to its ArtifactPort-aware effect boundary so the full
    result is still available for offload.
    The Kernel owns operation receipts, fencing and effect timeouts.
    """

    def __init__(
        self,
        *,
        scope: RunScope,
        tools: Sequence[ToolBinding],
        authorize: ToolAuthorizer | None = None,
        max_result_bytes: int = 65536,
        max_image_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        if type(max_result_bytes) is not int or max_result_bytes < 256:
            raise ValueError("max_result_bytes must be at least 256")
        if type(max_image_bytes) is not int or max_image_bytes < 0:
            raise ValueError("max_image_bytes must be a non-negative integer")
        self._scope = _snapshot(scope)
        self._authorize = authorize
        self._max_result_bytes = max_result_bytes
        self._max_image_bytes = max_image_bytes
        self._tools: dict[str, _RegisteredTool] = {}
        for binding in tools:
            definition = _snapshot(binding.definition)
            if not definition.name.strip() or not callable(binding.callback):
                raise ValueError("tool binding requires a name and callable callback")
            if definition.name in self._tools:
                raise ValueError("duplicate tool definition")
            validator = ToolInputValidator(definition.parameters)
            self._tools[definition.name] = _RegisteredTool(
                definition_digest=digest(definition),
                validator=validator,
                callback=binding.callback,
            )

    @staticmethod
    def _error(code: str, *, uncertain: bool = False) -> ToolResult:
        return ToolResult(status="uncertain" if uncertain else "error", content={"code": code})

    def _bounded_result(self, result: ToolResult, request: ToolRequest) -> ToolResult:
        result = _snapshot(result)
        if request.result_delivery == "runtime_managed":
            return result
        images = tuple(
            block for block in result.content_blocks if isinstance(block, ModelImageContent)
        )
        media_bytes = sum(len(_encode(image)) for image in images)
        text_result = result.model_copy(
            update={
                "content_blocks": tuple(
                    block
                    for block in result.content_blocks
                    if not isinstance(block, ModelImageContent)
                )
            }
        )
        payload = _encode(text_result)
        media_overflow = media_bytes > self._max_image_bytes or len(images) > 8
        if len(payload) > self._max_result_bytes or media_overflow:
            return ToolResult(
                status="uncertain" if result.status == "uncertain" else "error",
                content={
                    "code": "tool_media_too_large" if media_overflow else "tool_result_too_large",
                    "original_bytes": media_bytes if media_overflow else len(payload),
                    "limit_bytes": (
                        self._max_image_bytes if media_overflow else self._max_result_bytes
                    ),
                    "execution_status": result.status,
                    "retryability": "requires_reconcile",
                },
                resource_usage=result.resource_usage,
                browser_session_ref=result.browser_session_ref,
                browser_session_closed=result.browser_session_closed,
            )
        return result

    async def execute(self, request: ToolRequest) -> ToolResult:
        try:
            sealed = _snapshot(request)
        except Exception:
            return self._error("invalid_tool_request")
        if sealed.scope != self._scope:
            return self._error("scope_mismatch")
        if sealed.call.wire_arguments_invalid:
            return self._error("malformed_tool_arguments")
        binding = self._tools.get(sealed.call.name)
        if binding is None:
            return self._error("unknown_tool")
        if digest(sealed.definition) != binding.definition_digest:
            return self._error("definition_mismatch")
        if rejection := binding.validator.rejection(sealed.call.arguments):
            return ToolResult(status="error", content=rejection)
        if self._authorize is None:
            return self._error("policy_denied")
        check_effect_admission()
        try:
            decision = await self._authorize(_snapshot(sealed))
        except Exception:
            return self._error("policy_error")
        check_effect_admission()
        if isinstance(decision, ToolResult) and decision.status == "approval_required":
            try:
                return self._bounded_result(decision, sealed)
            except Exception:
                return self._error("invalid_policy_decision")
        if type(decision) is not str:
            return self._error("invalid_policy_decision")
        if decision == "deny":
            return self._error("policy_denied")
        if decision != "allow":
            return self._error("invalid_policy_decision")
        try:
            callback_request = _snapshot(sealed)
            check_effect_admission()
            result = await binding.callback(callback_request)
        except Exception as exc:
            # The locked host definition is the effect authority. A callback
            # declared read-only cannot leave a write to reconcile, so report
            # a deterministic tool error the model can work around. Mutating
            # callbacks remain uncertain because a write may have committed.
            return ToolResult(
                status="error" if sealed.definition.read_only else "uncertain",
                content={
                    "code": "callback_failed",
                    "exception_type": type(exc).__name__,
                },
            )
        if not isinstance(result, ToolResult):
            return self._error(
                "invalid_tool_result", uncertain=not sealed.definition.read_only
            )
        try:
            return self._bounded_result(result, sealed)
        except Exception:
            return self._error(
                "invalid_tool_result", uncertain=not sealed.definition.read_only
            )


@dataclass(frozen=True)
class _LegacyToolSchema:
    openai_schema: dict[str, object]


class _LegacyReasoner(Protocol):
    async def complete(self, **kwargs: object) -> HarnessReasoningTurn: ...


def _project_messages(request: ModelRequest) -> list[dict[str, JsonValue]]:
    from .model_replay import project_model_message
    from .operator_guidance import operator_input_history_view
    from .request_media import project_request_media

    messages: list[dict[str, JsonValue]] = []
    view = operator_input_history_view(request.messages, request.profile)
    for source in project_request_media(view, request.profile, purpose=request.purpose):
        messages.append(project_model_message(source, request.profile))
    if request.instructions:
        first = messages[0] if messages else {}
        if first.get("role") != "system" or first.get("content") != request.instructions:
            messages.insert(0, {"role": "system", "content": request.instructions})
    from .operator_guidance import control_is_journaled

    journaled = control_is_journaled(
        request.messages, request.profile, request.instructions, request.turn_control,
    ) if request.purpose == "task" else False
    if request.turn_control is not None and not journaled:
        messages.append(request.turn_control.message())
    return messages


class LegacyReasonerModelPort:
    """Bridge a completed legacy reasoner using a host-pinned model and scope.

    The legacy turn does not expose finish_reason or stream-completion metadata.
    Their absence remains explicit in the mapped result; this adapter cannot
    provide native provider conformance or recover a truncation hidden upstream.
    Provider exceptions propagate to the Kernel without inventing retryability.
    """

    def __init__(
        self,
        reasoner: _LegacyReasoner,
        *,
        scope: RunScope,
        model: str,
        profile_digest: str,
    ) -> None:
        if not model.strip() or not profile_digest.strip():
            raise ValueError("a host model mapping and profile digest are required")
        if not callable(getattr(reasoner, "complete", None)):
            raise ValueError("reasoner must expose complete")
        self._reasoner = reasoner
        self._scope = _snapshot(scope)
        self._model = model
        self._profile_digest = profile_digest

    async def complete(self, request: ModelRequest) -> ModelTurnResult:
        sealed = _snapshot(request)
        if sealed.scope != self._scope:
            raise ValueError("model request scope mismatch")
        if sealed.profile.digest != self._profile_digest:
            raise ValueError("model request profile mismatch")
        tools = tuple(
            _LegacyToolSchema(
                openai_schema={
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
            )
            for tool in sealed.tools
        )
        turn = await self._reasoner.complete(
            model=self._model,
            prompt=sealed.instructions,
            messages=_project_messages(sealed),
            tools=tools,
            max_output_tokens=sealed.effective_max_output_tokens,
        )
        return from_reasoning_turn(turn, request_id=sealed.request_id, attempt_id=sealed.attempt_id)

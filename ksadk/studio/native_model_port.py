"""Host-owned Studio ModelGateway projection into the Native ModelPort contract.

The Native Kernel remains independent of Studio and provider wire protocols.
This adapter preserves sealed finish reasons, tool batches and reported usage;
it never interprets reasoning text as a final answer or executable tool call.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any, Literal

from ksadk._async_iterators import close_async_iterator
from ksadk._model_input import ProviderInputObservation
from ksadk._model_json import load_model_json
from ksadk.conversations.model_context import supports_native_image_input
from ksadk.harness.model_adapters.native_openai import _native_messages
from ksadk.harness.native.adapters import _snapshot
from ksadk.harness.native.model import (
    ModelFailureDiagnostic,
    ModelFailureKind,
    ModelInputReceipt,
    ModelMediaPolicy,
    ModelProfile,
    ModelStreamEvent,
    ModelTurnResult,
    ModelUsage,
    normalize_response,
)
from ksadk.harness.native.model_replay import seal_anthropic_replay, seal_model_replay
from ksadk.harness.native.ports import ModelRequest
from ksadk.harness.native.state import RunScope
from ksadk.model_gateway import (
    ModelStreamEventType,
    classify_gateway_error,
    safe_gateway_error_code,
)
from ksadk.model_proxy import compat as provider_compat
from ksadk.studio.contracts import (
    ContextSpec,
    ExecutionSpec,
    NetworkPolicy,
    ResolvedModel,
    SecuritySpec,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.model_client import OpenAICompatibleModelClient
from ksadk.studio.model_request_projection import MODEL_REQUEST_PROJECTION_POLICY
from ksadk.studio.model_wire_failure import ModelResponseError, response_error_usage
from ksadk.studio.native_gateway_stream import gateway_stream_usage, validate_gateway_event

_RETRYABLE = {"rate_limit", "timeout", "unavailable", "transport", "incomplete_response"}
_OUTPUT_LIMITS = {"length", "max_tokens", "max_output_tokens", "output_limit", "incomplete"}


def _detached_model_json(value):
    """Thaw a locked Bundle's nested JSON without pickling mapping proxies."""
    if isinstance(value, Mapping):
        return {key: _detached_model_json(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_detached_model_json(child) for child in value]
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError("native_model_binding_not_json")


_FAILURE_KIND: dict[str, ModelFailureKind] = {
    "rate_limit": "rate_limit",
    "timeout": "request_timeout",
    "unavailable": "server_error",
    "transport": "network_error",
    "incomplete_response": "incomplete_response",
    "invalid_response": "invalid_response",
    "invalid_request": "client_error",
    "authentication": "client_error",
    "permission": "client_error",
    "not_found": "client_error",
    "unknown": "unknown_error",
}


def native_model_profile_from_build(
    resolved_agent_spec: Mapping[str, Any],
    *,
    selected_model: str,
    build_id: str,
    reasoning_effort: Literal["low", "medium", "high"] | None = None,
    streaming_support: Callable[[ResolvedModel], bool] | None = None,
    model_role: Literal["agent", "vision"] = "agent",
) -> ModelProfile:
    """Project the locked Studio model and context budgets into a Native profile.

    The Build's input budget is the local effective context ceiling; a
    provider-reported smaller ceiling narrows it. Missing or contradictory
    Build budgets fail closed rather than silently selecting a test profile.
    """
    agent_id = resolved_agent_spec.get("agentId") or resolved_agent_spec.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id or not build_id:
        raise ValueError("native_model_profile_identity_unavailable")
    if model_role == "vision":
        raw_models = (
            resolved_agent_spec.get("visionModel") or resolved_agent_spec.get("vision_model"),
        )
    else:
        raw_fallbacks = (
            resolved_agent_spec.get("fallbackModels")
            or resolved_agent_spec.get("fallback_models")
            or ()
        )
        if not isinstance(raw_fallbacks, (list, tuple)):
            raise ValueError("native_model_profile_selection_invalid")
        raw_models = (resolved_agent_spec.get("model"), *raw_fallbacks)
    matches = [
        ResolvedModel.model_validate(raw)
        for raw in raw_models
        if isinstance(raw, Mapping) and raw.get("model") == selected_model
    ]
    if len(matches) != 1:
        raise ValueError("native_model_profile_selection_invalid")
    if reasoning_effort is not None and (
        type(reasoning_effort) is not str or reasoning_effort not in {"low", "medium", "high"}
    ):
        raise ValueError("native_model_profile_reasoning_effort_invalid")
    model = matches[0]
    compatibility = provider_compat.compat_profile(
        base_url=model.endpoint_url, model=model.model
    )
    if reasoning_effort is not None and not compatibility.supports_reasoning_effort:
        raise ValueError("native_model_profile_reasoning_effort_unsupported")
    raw_context = resolved_agent_spec.get("context")
    raw_execution = resolved_agent_spec.get("execution")
    if not isinstance(raw_context, Mapping) or not isinstance(raw_execution, Mapping):
        raise ValueError("native_model_profile_budget_unavailable")
    context = ContextSpec.model_validate(raw_context)
    execution = ExecutionSpec.model_validate(raw_execution)
    window = context.max_input_tokens
    provider_window = model.metadata.get("context_window_tokens")
    if type(provider_window) is int and provider_window > 0:
        window = min(window, provider_window)
    output = model.parameters.max_tokens or context.reserve_output_tokens
    if output >= window:
        raise ValueError("native_model_profile_budget_conflict")
    raw_request_timeout = model.metadata.get("request_timeout_seconds")
    request_timeout = None
    if raw_request_timeout is not None:
        if (
            type(raw_request_timeout) not in {int, float}
            or not math.isfinite(raw_request_timeout)
            or raw_request_timeout <= 0
            or raw_request_timeout + 15 >= execution.timeout_seconds
            or raw_request_timeout > 3585
        ):
            raise ValueError("native_model_profile_request_timeout_invalid")
        request_timeout = float(raw_request_timeout)
    raw_media_limit = model.metadata.get("native_request_media_max_bytes", 40 * 1024 * 1024)
    if type(raw_media_limit) is not int or raw_media_limit < 0:
        raise ValueError("native_model_profile_media_budget_invalid")
    media_policy = ModelMediaPolicy(max_inline_image_bytes=raw_media_limit)
    media_identity = json.dumps(media_policy.model_dump(mode="json"), sort_keys=True,
                                separators=(",", ":"))
    return ModelProfile(
        profile_id="studio-"
        + hashlib.sha256(
            (
                f"{agent_id}\0vision\0{selected_model}"
                if model_role == "vision"
                else f"{agent_id}\0{selected_model}"
            ).encode("utf-8")
        ).hexdigest()[:24],
        profile_version=hashlib.sha256(
            (build_id + "\0" + provider_compat.compat_profile_digest(compatibility)
             + "\0" + MODEL_REQUEST_PROJECTION_POLICY + "\0" + media_identity).encode()
        ).hexdigest()[:24],
        provider_family=model.provider,
        context_window=window,
        max_output_tokens=output,
        max_retries=execution.retry.max_attempts - 1,
        max_provider_retries=execution.retry.provider_max_attempts - 1,
        retry_initial_delay_seconds=execution.retry.backoff_seconds,
        streaming=streaming_support(model) if streaming_support is not None else False,
        reasoning_effort=reasoning_effort,
        temperature=model.parameters.temperature,
        top_p=model.parameters.top_p,
        provider_replay_mode=("anthropic_content" if model.wire_api == "anthropic"
                              else compatibility.provider_replay_mode),
        operator_guidance_policy=(
            "journaled-system/v1"
            if model.wire_api == "anthropic" and compatibility.anthropic_mid_system is True
            else None
        ),
        tool_catalog_policy=(
            "stable-task/v1"
            if model.wire_api == "anthropic" and compatibility.anthropic_mid_system is True
            else None
        ),
        provider_replay_projection_policy=(
            "anthropic-client-compaction/v1"
            if model.wire_api == "anthropic"
            and compatibility.anthropic_block_binding == "drop_block" else None
        ),
        compatibility_digest=provider_compat.compat_profile_digest(compatibility),
        image_input_mode=(
            "native" if supports_native_image_input(model.metadata) else "text_only"
        ),
        request_media_policy=media_policy,
        request_timeout_seconds=request_timeout,
    )


def native_studio_streaming_support(
    client: OpenAICompatibleModelClient, model: ResolvedModel
) -> bool:
    """Use the existing Studio endpoint and gateway capability decision."""
    supports = getattr(client, "supports_streaming", None)
    stream_events = getattr(client, "stream_events", None)
    if not callable(supports) or not callable(stream_events):
        return False
    requested = not provider_compat.prefer_buffered_transport(
        base_url=model.endpoint_url, model=model.model
    )
    return bool(supports(model, requested=requested))


def _tool_calls(calls) -> tuple[dict[str, object], ...]:  # type: ignore[no-untyped-def]
    parsed: list[dict[str, object]] = []
    for call in calls:
        call_id = getattr(call, "id", None) or getattr(call, "call_id", None)
        name = getattr(call, "name", None) or getattr(call, "tool_name", None)
        try:
            arguments = load_model_json(call.arguments)
            invalid = not isinstance(arguments, dict)
        except (TypeError, ValueError):
            invalid = True
            arguments = None
        parsed.append(
            {
                "call_id": call_id,
                "name": name,
                "arguments": {} if invalid else arguments,
                "wire_arguments_invalid": invalid,
            }
        )
    return tuple(parsed)


def _usage(value) -> ModelUsage | None:  # type: ignore[no-untyped-def]
    if value is None or not value.reported:
        return None
    return ModelUsage(
        input_tokens=value.input_tokens,
        output_tokens=value.output_tokens,
        cached_input_tokens=value.cached_input_tokens,
        reasoning_tokens=value.reasoning_output_tokens,
    )


def _failure(
    request: ModelRequest,
    category: str,
    *,
    text: str = "",
    usage: ModelUsage | None = None,
    retry_after_ms: int | None = None,
    status_code: int | None = None,
    gateway_error_code: str | None = None,
    failure_diagnostic: ModelFailureDiagnostic | None = None,
) -> ModelTurnResult:
    if category == "context_length":
        kind, finish = "context_overflow", "context_overflow"
    elif category == "output_limit":
        kind, finish = "output_limit", "output_limit"
    elif category == "empty_response":
        kind, finish = "empty_response", "empty_response"
    elif category == "malformed_tool_call":
        kind, finish = "malformed_tool_call", "malformed_tool_call"
    elif category in _RETRYABLE:
        kind, finish = "provider_retryable_error", "provider_retryable_error"
    else:
        kind, finish = "provider_terminal_error", "provider_terminal_error"
    return ModelTurnResult(
        response_kind=kind,
        request_id=request.request_id,
        attempt_id=request.attempt_id,
        finish_reason=finish,
        text=text,
        usage=usage,
        retry_after_seconds=(
            max(0.0, retry_after_ms / 1000)
            if kind == "provider_retryable_error" and retry_after_ms is not None
            else None
        ),
        failure_kind=(
            _FAILURE_KIND.get(category, "unknown_error") if kind.startswith("provider_") else None
        ),
        provider_failure_category=category,
        provider_status_code=status_code,
        gateway_error_code=gateway_error_code,
        failure_diagnostic=failure_diagnostic,
    )


def _exception(
    request: ModelRequest, error: Exception, *, usage: ModelUsage | None = None
) -> ModelTurnResult:
    from ksadk.harness.model_provider import model_failure_diagnostic, model_failure_source

    gateway_error_code = safe_gateway_error_code(error) or None
    category, status, retry_after_ms = classify_gateway_error(error)
    source = model_failure_source(error)
    if usage is None:
        usage = _usage(response_error_usage(source))
    details = source.details if isinstance(source, StudioError) else {}
    category = _gateway_failure_category(
        category, gateway_error_code or "", details.get("finishReason")
    )
    return _failure(
        request,
        category,
        usage=usage,
        retry_after_ms=retry_after_ms,
        status_code=status,
        gateway_error_code=gateway_error_code,
        failure_diagnostic=ModelFailureDiagnostic.model_validate(model_failure_diagnostic(error)),
    )


def _gateway_failure_category(category: str, code: str, finish_reason: object) -> str:
    """Keep streamed host validation failures aligned with buffered failures."""
    if code == "MODEL_TOOL_PROTOCOL_INVALID":
        return "malformed_tool_call"
    if code == "MODEL_EMPTY_RESPONSE":
        return (
            "output_limit"
            if isinstance(finish_reason, str) and finish_reason in _OUTPUT_LIMITS
            else "empty_response"
        )
    if code in {
        "MODEL_REPETITIVE_OUTPUT",
        "MODEL_RESPONSE_INVALID",
        "MODEL_RESPONSE_TOO_LARGE",
    }:
        return "invalid_response"
    return category


def _event_failure_diagnostic(metadata: Mapping[str, object]) -> ModelFailureDiagnostic | None:
    """Unknown/legacy Gateway metadata is not a diagnostic authority."""
    raw = metadata.get("failure_diagnostic")
    if not isinstance(raw, Mapping):
        return None
    try:
        return ModelFailureDiagnostic.model_validate(raw)
    except ValueError:
        return None


def _input_receipt(request: ModelRequest, value: ProviderInputObservation | None):
    if value is None:
        return None
    if not isinstance(value, ProviderInputObservation):
        raise ValueError("model input observation contract mismatch")
    return ModelInputReceipt(
        request_id=request.request_id, attempt_id=request.attempt_id,
        profile_digest=request.profile.digest,
        provider_prefix_digest=value.provider_prefix_digest,
        source=value.source, transformations_json=value.transformations_json,
    )


def _with_input(result: ModelTurnResult, receipt: ModelInputReceipt | None) -> ModelTurnResult:
    return ModelTurnResult.model_validate({**result.model_dump(), "input_receipt": receipt})


class StudioNativeModelPort:
    """One Studio-resolved model, scope and network policy per Native activation."""

    @classmethod
    def from_resolved_build(
        cls,
        client: OpenAICompatibleModelClient,
        *,
        scope: RunScope,
        resolved_agent_spec: Mapping[str, Any],
        selected_model: str,
        profile_digest: str,
        model_role: Literal["agent", "vision"] = "agent",
    ) -> "StudioNativeModelPort":
        """Bind the pinned model and network policy from a verified Studio bundle."""

        agent_id = resolved_agent_spec.get("agentId") or resolved_agent_spec.get("agent_id")
        if agent_id != scope.agent_id:
            raise ValueError("Native Studio model Agent differs from Build")
        if model_role == "vision":
            candidates = (
                resolved_agent_spec.get("visionModel") or resolved_agent_spec.get("vision_model"),
            )
        else:
            raw_primary = resolved_agent_spec.get("model")
            raw_fallbacks = resolved_agent_spec.get("fallbackModels") or resolved_agent_spec.get(
                "fallback_models", ()
            )
            if not isinstance(raw_primary, Mapping) or not isinstance(raw_fallbacks, (tuple, list)):
                raise ValueError("Native Studio Build model bindings are invalid")
            candidates = (raw_primary, *raw_fallbacks)
        matches = [
            ResolvedModel.model_validate(raw)
            for raw in candidates
            if isinstance(raw, Mapping) and raw.get("model") == selected_model
        ]
        if len(matches) != 1:
            raise ValueError("Native Studio selected model is missing or ambiguous in Build")
        raw_security = resolved_agent_spec.get("security")
        if not isinstance(raw_security, Mapping):
            raise ValueError("Native Studio Build has no security policy")
        policy = SecuritySpec.model_validate(raw_security).network
        return cls(
            client,
            scope=scope,
            model=matches[0],
            network_policy=policy,
            profile_digest=profile_digest,
        )

    def __init__(
        self,
        client: OpenAICompatibleModelClient,
        *,
        scope: RunScope,
        model: ResolvedModel,
        network_policy: NetworkPolicy,
        profile_digest: str,
    ) -> None:
        if not profile_digest.strip():
            raise ValueError("Native Studio ModelPort requires a pinned ModelProfile")
        self._client = client
        self._scope = _snapshot(scope)
        self._model = ResolvedModel.model_validate(
            _detached_model_json(model.model_dump(mode="python"))
        )
        self._network_policy = network_policy.model_copy(deep=True)
        self._profile_digest = profile_digest
        self._compat_profile = provider_compat.compat_profile(
            base_url=self._model.endpoint_url, model=self._model.model
        )

    def _request(self, request: ModelRequest) -> tuple[ModelRequest, dict]:
        sealed = _snapshot(request)
        if sealed.scope != self._scope or sealed.profile.digest != self._profile_digest:
            raise ValueError("Native Studio model scope or profile changed")
        if (
            sealed.profile.temperature != self._model.parameters.temperature
            or sealed.profile.top_p != self._model.parameters.top_p
        ):
            raise ValueError("Native Studio model sampling policy changed")
        if sealed.profile.compatibility_digest is not None and (
            sealed.profile.compatibility_digest
            != provider_compat.compat_profile_digest(self._compat_profile)
        ):
            raise ValueError("Native Studio model compatibility policy changed")
        options = {
            # Keep tool receipts and host-supplied image observations on the
            # same canonical projection as the direct Native model adapter.
            # A screenshot path alone is never treated as image content.
            "messages": _native_messages(sealed),
            "network_policy": self._network_policy,
            "compatibility_profile": self._compat_profile,
            "timeout_seconds": sealed.profile.request_timeout_seconds or 60,
            "tools": [
                {"type": "function", "function": tool.function_schema()} for tool in sealed.tools
            ],
            "max_output_tokens": sealed.effective_max_output_tokens,
            "output_budget_mode": "fixed",
            "output_token_parameter": sealed.profile.output_token_parameter,
            "reasoning_effort": provider_compat.request_reasoning_effort(
                self._compat_profile,
                model=self._model.model,
                purpose=sealed.purpose,
                requested=sealed.profile.reasoning_effort,
            ),
        }
        if provider_compat.request_thinking_disabled(
            self._compat_profile, model=self._model.model, purpose=sealed.purpose,
            requested=sealed.profile.reasoning_effort,
        ):
            options["thinking_mode"] = "disabled"
        choice = provider_compat.project_tool_choice(
            sealed.effective_tool_choice, self._compat_profile,
        )
        if choice != "auto" or sealed.effective_tool_choice != "auto":
            options["tool_choice"] = choice
        if sealed.profile.parallel_tool_calls is not None:
            options["parallel_tool_calls"] = sealed.profile.parallel_tool_calls
        if getattr(self, "_model_takeover_declaration", None) is not None:
            from .native_model_recovery import admit_studio_port, assert_local_function_proposals
            admit_studio_port(self, self._model_takeover_declaration)
            assert_local_function_proposals(options)
        return sealed, options

    async def complete(self, request: ModelRequest) -> ModelTurnResult:
        sealed, options = self._request(request)
        try:
            response = await self._client.complete(
                self._model,
                **options,
                max_attempts=1,
                backoff_seconds=0.0,
                allow_empty=bool(sealed.tools),
                retry_on_length=False,
                retry_reasoning_only=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            from ksadk.harness.model_provider import model_failure_source

            source = model_failure_source(error)
            observed = source.provider_input if type(source) is ModelResponseError else None
            return _with_input(_exception(sealed, error), _input_receipt(sealed, observed))
        try:
            observed = getattr(response, "provider_input", None)
            if observed is not None and self._model.wire_api != "anthropic":
                raise ValueError("input observation belongs to another wire protocol")
            receipt = _input_receipt(sealed, observed)
        except (ValueError, TypeError):
            return _failure(sealed, "invalid_response", usage=_usage(response.usage))
        usage = _usage(response.usage)
        try:
            calls = _tool_calls(response.tool_calls)
        except (ValueError, TypeError):
            return _with_input(ModelTurnResult(
                response_kind="malformed_tool_call",
                request_id=sealed.request_id,
                attempt_id=sealed.attempt_id,
                finish_reason="malformed_tool_call",
                text=(response.content if self._model.wire_api == "anthropic" else
                      provider_compat.project_inline_reasoning(
                          response.content, self._compat_profile,
                      )),
                usage=usage,
            ), receipt)
        if (sealed.profile.provider_replay_mode == "anthropic_content"
                and response.finish_reason != "content_filter"
                and any(call.get("wire_arguments_invalid") for call in calls)):
            return _with_input(
                _failure(sealed, "malformed_tool_call", text=response.content, usage=usage),
                receipt,
            )
        result = normalize_response(
            request_id=sealed.request_id,
            attempt_id=sealed.attempt_id,
            text=(response.content if self._model.wire_api == "anthropic" else
                  provider_compat.project_inline_reasoning(
                      response.content, self._compat_profile,
                  )),
            tool_calls=calls,
            finish_reason=response.finish_reason or None,
            stream_complete=bool(response.finish_reason),
            usage=usage,
        )
        if result.response_kind == "provider_retryable_error":
            return _with_input(ModelTurnResult.model_validate(
                {**result.model_dump(), "failure_kind": "incomplete_response"}
            ), receipt)
        try:
            if sealed.profile.provider_replay_mode == "anthropic_content":
                return _with_input(seal_anthropic_replay(
                    result, sealed.profile, response.raw_message.get("content"),
                ), receipt)
            return _with_input(
                seal_model_replay(result, sealed.profile, response.reasoning), receipt,
            )
        except (ValueError, TypeError):
            return _with_input(_failure(sealed, "invalid_response", usage=usage), receipt)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        sealed, options = self._request(request)
        if not sealed.profile.streaming:
            raise ValueError("Native Studio model profile did not enable streaming")
        events = getattr(self._client, "stream_events", None)
        if not callable(events):
            raise ValueError("Studio ModelGateway does not expose canonical stream events")
        sequence = 0
        gateway_sequence = 0
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        from ksadk.harness.public_summary import PublicSummaryBuffer

        summary_buffer = PublicSummaryBuffer()
        summary_raw = ""
        inline_reasoning = provider_compat.InlineReasoningProjector(self._compat_profile)
        calls: list[dict[str, object]] = []
        usage: ModelUsage | None = None
        malformed = False
        receipt = None
        iterator = events(self._model, **options, retry_empty_stream=False)
        try:
            async for event in iterator:
                validate_gateway_event(
                    event, model=self._model.model, sequence=gateway_sequence + 1
                )
                gateway_sequence = event.sequence
                if event.type in {
                    ModelStreamEventType.TURN_COMPLETED, ModelStreamEventType.TURN_FAILED,
                    ModelStreamEventType.PROVIDER_RATE_LIMITED,
                }:
                    summary = summary_buffer.drain(summary_raw, final=True)
                    if summary:
                        sequence += 1
                        yield ModelStreamEvent(kind="public_summary", request_id=sealed.request_id,
                                               attempt_id=sealed.attempt_id, seq=sequence, text=summary)
                if event.type is ModelStreamEventType.INPUT_TRANSFORMATIONS:
                    if self._model.wire_api != "anthropic":
                        raise ValueError("input observation belongs to another wire protocol")
                    receipt = _input_receipt(sealed, event.provider_input)
                    sequence += 1
                    yield ModelStreamEvent(
                        kind="input_metadata", request_id=sealed.request_id,
                        attempt_id=sealed.attempt_id, seq=sequence, input_receipt=receipt,
                    )
                elif event.type is ModelStreamEventType.PUBLIC_SUMMARY_DELTA:
                    if (event.text and sealed.purpose == "task"
                            and self._model.parameters.public_thinking_summary):
                        summary_raw = (summary_raw + event.text)[:summary_buffer.LIMIT]
                        summary = summary_buffer.drain(summary_raw)
                        if summary:
                            sequence += 1
                            yield ModelStreamEvent(kind="public_summary", request_id=sealed.request_id,
                                                   attempt_id=sealed.attempt_id, seq=sequence, text=summary)
                elif event.type is ModelStreamEventType.REASONING_DELTA:
                    if sealed.profile.provider_replay_mode != "none":
                        reasoning_parts.append(event.text)
                elif event.type is ModelStreamEventType.TEXT_DELTA and event.text:
                    public_text = (event.text if self._model.wire_api == "anthropic" else
                                   inline_reasoning.push(event.text))
                    if public_text:
                        text_parts.append(public_text)
                        sequence += 1
                        yield ModelStreamEvent(
                            kind="text_delta",
                            request_id=sealed.request_id,
                            attempt_id=sealed.attempt_id,
                            seq=sequence,
                            text=public_text,
                        )
                elif event.type is ModelStreamEventType.TOOL_CALL_COMPLETED:
                    try:
                        calls.extend(_tool_calls((event,)))
                    except (ValueError, TypeError, AttributeError):
                        malformed = True
                elif event.type is ModelStreamEventType.USAGE_UPDATED:
                    usage = gateway_stream_usage(event)
                elif event.type in {
                    ModelStreamEventType.TURN_FAILED,
                    ModelStreamEventType.PROVIDER_RATE_LIMITED,
                }:
                    result = _failure(
                        sealed,
                        _gateway_failure_category(
                            event.failure_category,
                            event.gateway_error_code,
                            event.metadata.get("finish_reason"),
                        ),
                        text="".join(text_parts),
                        usage=usage,
                        retry_after_ms=event.retry_after_ms,
                        status_code=event.status_code,
                        gateway_error_code=event.gateway_error_code or None,
                        failure_diagnostic=_event_failure_diagnostic(event.metadata),
                    )
                    sequence += 1
                    yield ModelStreamEvent(
                        kind="final",
                        request_id=sealed.request_id,
                        attempt_id=sealed.attempt_id,
                        seq=sequence,
                        result=_with_input(result, receipt),
                    )
                    return
                elif event.type is ModelStreamEventType.TURN_COMPLETED:
                    public_tail = ("" if self._model.wire_api == "anthropic" else
                                   inline_reasoning.seal())
                    if public_tail:
                        text_parts.append(public_tail)
                        sequence += 1
                        yield ModelStreamEvent(
                            kind="text_delta",
                            request_id=sealed.request_id,
                            attempt_id=sealed.attempt_id,
                            seq=sequence,
                            text=public_tail,
                        )
                    finish = str(event.metadata.get("finish_reason") or "")
                    result = (
                        _failure(
                            sealed, "malformed_tool_call", text="".join(text_parts), usage=usage
                        )
                        if (malformed or (
                            sealed.profile.provider_replay_mode == "anthropic_content"
                            and any(call.get("wire_arguments_invalid") for call in calls)
                        )) and finish != "content_filter"
                        else normalize_response(
                            request_id=sealed.request_id,
                            attempt_id=sealed.attempt_id,
                            text="".join(text_parts),
                            tool_calls=tuple(calls),
                            finish_reason=finish or None,
                            stream_complete=bool(finish),
                            usage=usage,
                        )
                    )
                    if result.response_kind == "provider_retryable_error":
                        result = ModelTurnResult.model_validate(
                            {**result.model_dump(), "failure_kind": "incomplete_response"}
                        )
                    if sealed.profile.provider_replay_mode == "anthropic_content":
                        content = (list(event.provider_content)
                                   if event.provider_content is not None else None)
                        try:
                            result = seal_anthropic_replay(result, sealed.profile, content)
                        except (ValueError, TypeError):
                            result = _failure(sealed, "invalid_response", usage=usage)
                    else:
                        result = seal_model_replay(result, sealed.profile, "".join(reasoning_parts))
                    sequence += 1
                    yield ModelStreamEvent(
                        kind="final",
                        request_id=sealed.request_id,
                        attempt_id=sealed.attempt_id,
                        seq=sequence,
                        result=_with_input(result, receipt),
                    )
                    return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            result = _exception(sealed, error, usage=usage)
        else:
            result = _failure(
                sealed, "incomplete_response", text="".join(text_parts), usage=usage
            )
        finally:
            await close_async_iterator(iterator, owner="studio_native_gateway")
        sequence += 1
        yield ModelStreamEvent(
            kind="final",
            request_id=sealed.request_id,
            attempt_id=sealed.attempt_id,
            seq=sequence,
            result=_with_input(result, receipt),
        )


__all__ = [
    "StudioNativeModelPort",
    "native_model_profile_from_build",
    "native_studio_streaming_support",
]

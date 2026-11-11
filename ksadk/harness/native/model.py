"""Provider-neutral contracts for sealed model turns.

This boundary accepts complete, normalized responses, not provider stream deltas.
Protocol adapters assemble a single attempt before calling ``normalize_response``.
Tool argument shape and authority still require the tool registry/policy gate;
validation here proves only the envelope and JSON payload are well formed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)

from ksadk._model_anthropic import anthropic_projection
from ksadk._model_input import transformations_json
from ksadk._model_json import load_model_json

if TYPE_CHECKING:
    from ksadk.harness.reasoner import HarnessReasoningTurn


ModelResponseKind = Literal[
    "tool_calls",
    "final_candidate",
    "output_limit",
    "context_overflow",
    "empty_response",
    "malformed_tool_call",
    "provider_retryable_error",
    "provider_terminal_error",
    "policy_abort",
    "user_abort",
]

ModelFailureKind = Literal[
    "request_timeout",
    "network_error",
    "rate_limit",
    "server_error",
    "incomplete_response",
    "invalid_response",
    "client_error",
    "host_error",
    "unknown_error",
]


class _Contract(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        allow_inf_nan=False,
        revalidate_instances="always",
    )


class ModelMediaPolicy(_Contract):
    """Encoded inline-image budget, not provider token or full-body capacity."""

    version: Literal["inline-image-budget/v1"] = "inline-image-budget/v1"
    max_inline_image_bytes: int = Field(default=40 * 1024 * 1024, strict=True, ge=0)


class ModelProfile(_Contract):
    """An immutable, credential-free policy identity fixed when a run starts.

    These limits form the minimal native profile. Provider projection, endpoint
    resolution and credentials remain outside this contract.
    """

    profile_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    provider_family: str = Field(min_length=1)
    max_output_tokens: int = Field(default=8192, gt=0, strict=True)
    context_window: int = Field(default=128000, gt=0, strict=True)
    max_retries: int = Field(default=2, ge=0, strict=True)
    # Transport failures do not consume the malformed/empty-response budget.
    # Every attempt still consumes the Run's total model-call budget.
    max_provider_retries: int = Field(default=3, ge=0, le=9, strict=True)
    max_continuations: int = Field(default=3, ge=0, strict=True)
    streaming: bool = Field(default=False, strict=True)
    stream_timeout_policy: Literal["idle/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    image_input_mode: Literal["native", "text_only"] = "native"
    request_media_policy: ModelMediaPolicy | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    provider_replay_mode: Literal["none", "reasoning_content", "anthropic_content"] = Field(
        default="none", exclude_if=lambda value: value == "none"
    )
    operator_guidance_policy: Literal["journaled-system/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    tool_catalog_policy: Literal["stable-task/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    provider_replay_projection_policy: Literal["anthropic-client-compaction/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    compatibility_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", exclude_if=lambda value: value is None
    )
    # Chat output-limit selector. Other wires retain their native field names.
    output_token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    thinking_mode: Literal["enabled", "disabled"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    provider_thinking_display: bool = Field(default=False, strict=True,
                                            exclude_if=lambda value: not value)
    temperature: float | None = Field(default=None, ge=0, le=2, strict=True)
    top_p: float | None = Field(default=None, gt=0, le=1, strict=True)
    parallel_tool_calls: bool | None = Field(default=None, strict=True)
    request_timeout_seconds: float | None = Field(default=None, gt=0, le=3600, strict=True)
    retry_initial_delay_seconds: float = Field(default=0.5, ge=0, strict=True)
    retry_max_delay_seconds: float = Field(default=8.0, ge=0, strict=True)
    retry_backoff_multiplier: float = Field(default=2.0, ge=1, strict=True)
    # A minute-window 429 must not exhaust all retry attempts within the
    # same quota window when the provider omits Retry-After.
    rate_limit_cooldown_seconds: float = Field(default=60.0, ge=0, le=300, strict=True)

    @model_validator(mode="after")
    def validate_replay_projection_policy(self):
        if (self.provider_replay_projection_policy is not None
                and self.provider_replay_mode != "anthropic_content"):
            raise ValueError("Anthropic compaction policy requires Anthropic replay")
        return self

    @field_validator("profile_id", "profile_version", "provider_family")
    @classmethod
    def _nonblank_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("profile identity must not be blank")
        return value

    @model_validator(mode="after")
    def _output_fits_context(self) -> ModelProfile:
        if self.max_output_tokens >= self.context_window:
            raise ValueError("max_output_tokens must be smaller than context_window")
        if self.retry_initial_delay_seconds > self.retry_max_delay_seconds:
            raise ValueError("retry initial delay must not exceed the maximum delay")
        return self

    @property
    def digest(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ModelUsage(_Contract):
    """A single attempt's reported counters; absent reports stay ``None``."""

    input_tokens: int = Field(default=0, ge=0, strict=True)
    output_tokens: int = Field(default=0, ge=0, strict=True)
    cached_input_tokens: int = Field(default=0, ge=0, strict=True)
    reasoning_tokens: int = Field(default=0, ge=0, strict=True)


class ModelToolCall(_Contract):
    call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, JsonValue]
    # A sealed provider call whose argument bytes could not be decoded still
    # needs one tool error receipt. It must never reach a host callback.
    wire_arguments_invalid: bool = Field(default=False, strict=True)

    @field_validator("call_id", "name")
    @classmethod
    def _nonblank_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("tool call identity must not be blank")
        return value

    @model_validator(mode="after")
    def _invalid_wire_arguments_are_empty(self) -> ModelToolCall:
        if self.wire_arguments_invalid and self.arguments:
            raise ValueError("invalid wire arguments cannot carry executable values")
        return self


class ModelTextContent(_Contract):
    type: Literal["text"] = "text"
    text: str


class ModelImageURL(_Contract):
    url: str
    detail: Literal["auto", "low", "high"] = "auto"

    @field_validator("url")
    @classmethod
    def _image_source(cls, value: str) -> str:
        if value.startswith("data:"):
            prefix, separator, payload = value.partition(",")
            allowed = {f"data:image/{kind};base64" for kind in ("png", "jpeg", "webp", "gif")}
            if prefix not in allowed or not separator or not payload:
                raise ValueError("image data URL requires a supported image and base64 content")
            try:
                base64.b64decode(payload, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("invalid image base64 content") from exc
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("image URL must be an explicit HTTP URL or image data URL")
        return value


class ModelImageContent(_Contract):
    type: Literal["image_url"] = "image_url"
    image_url: ModelImageURL


class ModelFailureDiagnostic(_Contract):
    """Host exception provenance; no raw error text, class names or retry grant."""

    version: Literal["model-failure-source/v1"] = "model-failure-source/v1"
    cause_depth: int = Field(ge=0, le=7, strict=True)
    type_code: Literal[
        "timeout_error", "connection_error", "runtime_error", "value_error", "type_error",
        "attribute_error", "key_error", "json_decode_error", "validation_error",
        "studio_error", "http_status_error", "protocol_error", "other",
    ]


class ModelReplayInputContext(_Contract):
    """Host input-view identity, not the provider's signature algorithm."""

    version: Literal[
        "native-anthropic-message-prefix/v1", "native-anthropic-input-prefix/v2",
    ] = (
        "native-anthropic-message-prefix/v1"
    )
    messages_digest: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    header_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", strict=True,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def header_matches_version(self):
        if (self.version == "native-anthropic-input-prefix/v2") != (self.header_digest is not None):
            raise ValueError("replay input version requires its header identity")
        return self


class ModelReplayMetadata(_Contract):
    """Private protocol bytes bound to the host's pinned model profile.

    They authorize neither a tool nor a final answer. Adapters may only echo
    them to the same model profile, separately from visible assistant text.
    """

    version: Literal["model-replay/v1", "model-replay/v2"] = "model-replay/v1"
    profile_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    reasoning_content: str = Field(strict=True)
    # An immutable, ordered JSON value; never a second set of tool facts.
    anthropic_content_json: str | None = Field(
        default=None, strict=True, repr=False, exclude_if=lambda value: value is None,
    )
    # Original provider ID -> canonical host ID. Content bytes stay unchanged.
    anthropic_tool_id_map: tuple[tuple[str, str], ...] = Field(
        default=(), exclude_if=lambda value: not value,
    )
    input_context: ModelReplayInputContext | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _replay_variant(self) -> ModelReplayMetadata:
        if (self.version == "model-replay/v2") != (self.anthropic_content_json is not None):
            raise ValueError("replay version requires its original content variant")
        if self.anthropic_content_json is not None and self.reasoning_content:
            raise ValueError("Anthropic content is not Chat reasoning replay")
        if self.anthropic_content_json is None and self.anthropic_tool_id_map:
            raise ValueError("identity projection requires original Anthropic content")
        if self.anthropic_content_json is None and self.input_context is not None:
            raise ValueError("input context requires original Anthropic content")
        if self.anthropic_content_json is not None:
            anthropic_projection(load_model_json(self.anthropic_content_json),
                                 self.anthropic_tool_id_map)
        return self


class ModelInputReceipt(_Contract):
    """Private input facts for one actual attempt, independently of its output."""

    version: Literal["model-input-transformations/v1"] = "model-input-transformations/v1"
    wire_api: Literal["anthropic"] = "anthropic"
    request_id: str = Field(min_length=1, strict=True)
    attempt_id: str = Field(min_length=1, strict=True)
    profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    provider_prefix_digest: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    source: Literal["buffered", "message_start", "message_delta"]
    transformations_json: str = Field(strict=True, repr=False)

    @field_validator("transformations_json")
    @classmethod
    def _input_facts(cls, value: str) -> str:
        transformations_json(load_model_json(value))
        return value

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class ModelTurnResult(_Contract):
    response_kind: ModelResponseKind
    request_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    finish_reason: str | None = None
    # A bounded text response needs an actual protocol terminal and no raw
    # tool payload. Older receipts deliberately carry no such attestation.
    text_commit_version: Literal["provider-finished-text/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    text: str = ""
    tool_calls: tuple[ModelToolCall, ...] = ()
    usage: ModelUsage | None = None
    provider_replay: ModelReplayMetadata | None = None
    input_receipt: ModelInputReceipt | None = Field(
        default=None, repr=False, exclude_if=lambda value: value is None,
    )
    retry_after_seconds: float | None = Field(default=None, ge=0, strict=True)
    failure_kind: ModelFailureKind | None = None
    # Sanitized transport classification; never store provider error text.
    provider_failure_category: str | None = Field(default=None, max_length=80)
    provider_status_code: int | None = Field(default=None, ge=100, le=599, strict=True)
    gateway_error_code: str | None = Field(
        default=None, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$"
    )
    failure_diagnostic: ModelFailureDiagnostic | None = None

    @field_validator("request_id", "attempt_id")
    @classmethod
    def _nonblank_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("request and attempt identity must not be blank")
        return value

    @model_validator(mode="after")
    def _validate_response_shape(self) -> ModelTurnResult:
        if self.input_receipt is not None and (
            self.input_receipt.request_id, self.input_receipt.attempt_id
        ) != (self.request_id, self.attempt_id):
            raise ValueError("input receipt belongs to another attempt")
        if self.text_commit_version is not None and (
            self.response_kind != "output_limit"
            or self.finish_reason not in _BOUNDED_TEXT_FINISHES
            or not self.text.strip()
            or self.tool_calls
            or self.failure_kind is not None
            or self.failure_diagnostic is not None
        ):
            raise ValueError("bounded text commit requires a finished text-only response")
        if self.provider_replay is not None and self.response_kind not in {
            "tool_calls", "final_candidate", "output_limit",
        }:
            raise ValueError("provider replay requires a sealed assistant response")
        if (self.provider_replay is not None
                and self.provider_replay.anthropic_content_json is not None):
            text, calls = anthropic_projection(load_model_json(
                self.provider_replay.anthropic_content_json,
            ), self.provider_replay.anthropic_tool_id_map)
            expected = [{"call_id": c.call_id, "name": c.name, "arguments": c.arguments}
                        for c in self.tool_calls]
            if text != self.text or calls != expected:
                raise ValueError("Anthropic replay conflicts with sealed model facts")
        if (
            self.response_kind in {"tool_calls", "final_candidate"}
            and self.finish_reason not in _COMPLETE_FINISHES
        ):
            raise ValueError("successful response kind contradicts finish_reason")
        if self.response_kind == "tool_calls":
            if not self.tool_calls:
                raise ValueError("tool_calls response requires a nonempty tool batch")
            call_ids = [call.call_id for call in self.tool_calls]
            if len(set(call_ids)) != len(call_ids):
                raise ValueError("duplicate tool call IDs in one response")
        elif self.tool_calls:
            raise ValueError("only tool_calls responses may expose executable tools")
        if self.response_kind == "final_candidate":
            if not self.text.strip():
                raise ValueError("a final candidate requires visible text")
            if self.finish_reason in _TOOL_FINISHES:
                raise ValueError("tool finish reason requires a valid tool batch")
        if (
            self.retry_after_seconds is not None
            and self.response_kind != "provider_retryable_error"
        ):
            raise ValueError("retry-after is only valid for retryable provider errors")
        if self.failure_kind is not None and self.response_kind not in {
            "provider_retryable_error",
            "provider_terminal_error",
        }:
            raise ValueError("provider failure kind requires a provider error response")
        if self.failure_diagnostic is not None and self.response_kind in {
            "tool_calls", "final_candidate",
        }:
            raise ValueError("failure diagnostic cannot describe a successful response")
        return self


class ModelStreamEvent(_Contract):
    """Visible observations and one sealed result, scoped to a single attempt.

    Sequence numbers count emitted observations, not provider chunks. Deltas
    have no tool/usage fields and never authorize execution or completion.
    """

    kind: Literal[
        "text_delta", "public_summary", "provider_reasoning", "activity", "input_metadata", "final",
    ]
    request_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    seq: int = Field(ge=1, strict=True)
    text: str = ""
    result: ModelTurnResult | None = None
    input_receipt: ModelInputReceipt | None = Field(
        default=None, repr=False, exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _valid_event(self) -> ModelStreamEvent:
        if not self.request_id.strip() or not self.attempt_id.strip():
            raise ValueError("stream event identity cannot be blank")
        if self.kind == "activity":
            if self.text or self.result is not None or self.input_receipt is not None:
                raise ValueError("stream activity cannot contain model content or a result")
            return self
        if self.kind == "input_metadata":
            if self.text or self.result is not None or self.input_receipt is None:
                raise ValueError("input metadata event contains only its receipt")
            if (self.input_receipt.request_id, self.input_receipt.attempt_id) != (
                self.request_id, self.attempt_id,
            ):
                raise ValueError("stream input receipt identity mismatch")
            return self
        if self.input_receipt is not None:
            raise ValueError("input receipt requires an input metadata event")
        if self.kind in {"text_delta", "public_summary", "provider_reasoning"}:
            if not self.text or self.result is not None:
                raise ValueError("visible delta requires text and cannot contain a result")
        elif self.text or self.result is None:
            raise ValueError("final event contains only its sealed result")
        elif (self.result.request_id, self.result.attempt_id) != (self.request_id, self.attempt_id):
            raise ValueError("sealed stream result identity mismatch")
        return self


_OUTPUT_LIMITS = {"output_limit", "length", "max_tokens", "max_output_tokens", "incomplete"}
_BOUNDED_TEXT_FINISHES = {"output_limit", "length", "max_tokens", "max_output_tokens"}
_CONTEXT_LIMITS = {"context_overflow", "context_length_exceeded"}
_POLICY_ABORTS = {"policy_abort", "content_filter"}
_USER_ABORTS = {"user_abort", "canceled", "cancelled"}
_TOOL_FINISHES = {"tool_calls", "tool_use"}
_COMPLETE_FINISHES = {None, "stop", "end_turn", "completed", "final_candidate"} | _TOOL_FINISHES


def normalize_response(
    *,
    request_id: str,
    attempt_id: str,
    text: str = "",
    tool_calls: Sequence[ModelToolCall | Mapping[str, object]] = (),
    finish_reason: str | None = None,
    stream_complete: bool = True,
    usage: ModelUsage | Mapping[str, int] | None = None,
) -> ModelTurnResult:
    """Classify one sealed attempt without merging snapshots or inferring success.

    ``stream_complete`` means the adapter observed protocol completion, not just
    socket EOF. A missing finish reason stays unknown. Partial tool payloads are
    never exposed; adapters retain raw provider evidence separately for repair.
    JSON strings are not parsed here: protocol adapters must assemble/decode them.
    """

    def result(kind: ModelResponseKind, calls: tuple[ModelToolCall, ...] = ()) -> ModelTurnResult:
        return ModelTurnResult(
            response_kind=kind,
            request_id=request_id,
            attempt_id=attempt_id,
            finish_reason=finish_reason,
            text_commit_version=(
                "provider-finished-text/v1"
                if kind == "output_limit" and stream_complete and text.strip()
                and finish_reason in _BOUNDED_TEXT_FINISHES and not tool_calls
                else None
            ),
            text=text,
            tool_calls=calls,
            usage=usage,
        )

    # Explicit aborts and transport/protocol completeness dominate visible text.
    if finish_reason in _USER_ABORTS:
        return result("user_abort")
    if finish_reason in _POLICY_ABORTS:
        return result("policy_abort")
    if finish_reason in _CONTEXT_LIMITS:
        return result("context_overflow")
    if finish_reason in _OUTPUT_LIMITS:
        return result("output_limit")
    if finish_reason == "provider_terminal_error":
        return result("provider_terminal_error")
    if not stream_complete or finish_reason == "provider_retryable_error":
        return result("provider_retryable_error")
    if finish_reason == "malformed_tool_call":
        return result("malformed_tool_call")
    if finish_reason == "empty_response":
        return result("empty_response")
    if finish_reason not in _COMPLETE_FINISHES:
        return result("provider_terminal_error")

    try:
        calls = tuple(ModelToolCall.model_validate(call) for call in tool_calls)
    except (ValidationError, TypeError):
        return result("malformed_tool_call")
    if calls:
        if len({call.call_id for call in calls}) != len(calls):
            return result("malformed_tool_call")
        return result("tool_calls", calls)
    if finish_reason in _TOOL_FINISHES:
        return result("malformed_tool_call")
    return result("final_candidate" if text.strip() else "empty_response")


def from_reasoning_turn(
    turn: HarnessReasoningTurn,
    *,
    request_id: str,
    attempt_id: str,
) -> ModelTurnResult:
    """Bridge a legacy turn while preserving its missing finish reason as None.

    The legacy DTO cannot prove truncation or stream completeness; this helper
    must receive an already completed call, never a streaming delta. It does not
    promote reasoning text into user-visible content. Native provider adapters
    should use ``normalize_response`` with actual protocol completion metadata.
    """

    usage = dict(turn.usage) if turn.usage is not None else None
    if usage is not None and "cached_tokens" in usage:
        cached = usage.pop("cached_tokens")
        if "cached_input_tokens" in usage and usage["cached_input_tokens"] != cached:
            raise ValueError("conflicting legacy cached token counters")
        usage["cached_input_tokens"] = cached
    if any(call.invalid_reason for call in turn.tool_calls):
        return ModelTurnResult(
            response_kind="malformed_tool_call",
            request_id=request_id,
            attempt_id=attempt_id,
            finish_reason=None,
            text=turn.final_text or "",
            usage=usage,
        )
    return normalize_response(
        request_id=request_id,
        attempt_id=attempt_id,
        text=turn.final_text or "",
        finish_reason=None,
        tool_calls=tuple(
            {"call_id": call.call_id, "name": call.name, "arguments": call.arguments}
            for call in turn.tool_calls
        ),
        usage=usage,
    )

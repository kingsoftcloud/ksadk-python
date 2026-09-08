"""Additive, provider-neutral conversation presentation contracts (v1)."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class ConversationContractModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )


_CAPABILITY = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")

# Provider-neutral wire extensions.  These keys are deliberately namespaced:
# the stable ConversationInput/v1 envelope must not grow a top-level field for
# every control exposed by Codex, DSH, or a future AgentProvider.
APPROVAL_MODE_EXTENSION = "ksadk.approval"
COLLABORATION_MODE_EXTENSION = "ksadk.collaboration"
GOAL_OBJECTIVE_EXTENSION = "ksadk.goal"


class ConversationCapability(ConversationContractModel):
    name: str = Field(min_length=1, max_length=128)
    mode: Literal["native", "translated", "degraded", "unavailable"]
    reason: str | None = Field(default=None, max_length=512)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not _CAPABILITY.fullmatch(value):
            raise ValueError("capability name must be a namespaced identifier")
        return value

    @model_validator(mode="after")
    def validate_reason(self) -> "ConversationCapability":
        if self.mode in {"degraded", "unavailable"} and not self.reason:
            raise ValueError("degraded or unavailable capability requires a reason")
        return self


class ConversationSurface(ConversationContractModel):
    api_version: Literal["conversation.ksadk.io/v1"] = "conversation.ksadk.io/v1"
    kind: Literal["ConversationSurface"] = "ConversationSurface"
    surface_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    provider_ref: str = Field(min_length=1, max_length=256)
    inputs: tuple[ConversationCapability, ...] = ()
    outputs: tuple[ConversationCapability, ...] = ()

    @model_validator(mode="after")
    def validate_unique_capabilities(self) -> "ConversationSurface":
        for capabilities, direction in ((self.inputs, "input"), (self.outputs, "output")):
            names = [capability.name for capability in capabilities]
            if len(names) != len(set(names)):
                raise ValueError(
                    f"conversation surface must not repeat an {direction} capability"
                )
        return self

    def permits_input(self, field: str) -> bool:
        return any(
            capability.name == field and capability.mode in {"native", "translated"}
            for capability in self.inputs
        )


class ConversationTextPart(ConversationContractModel):
    kind: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=131072)


class ConversationAttachmentPart(ConversationContractModel):
    kind: Literal["attachment"] = "attachment"
    attachment_ref: str = Field(min_length=1, max_length=2048)
    media_type: str = Field(min_length=1, max_length=256)
    name: str | None = Field(default=None, max_length=1024)


ConversationInputPart = ConversationTextPart | ConversationAttachmentPart


class ConversationInput(ConversationContractModel):
    """A provider-neutral foreground turn submitted by a conversation client.

    It deliberately carries only user intent.  A Server/Runtime adapter maps
    that intent to a provider-specific request after checking the active
    ConversationSurface; browser clients never forward arbitrary provider
    parameters.
    """

    api_version: Literal["conversation.ksadk.io/v1"] = "conversation.ksadk.io/v1"
    kind: Literal["ConversationInput"] = "ConversationInput"
    input_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    idempotency_key: str = Field(min_length=1, max_length=512)
    parts: tuple[ConversationInputPart, ...] = Field(min_length=1)
    model_ref: str | None = Field(default=None, min_length=1, max_length=256)
    reasoning: str | None = Field(default=None, min_length=1, max_length=64)
    extensions: dict[str, Any] = Field(default_factory=dict)

    @field_validator("extensions")
    @classmethod
    def validate_extensions(cls, value: dict[str, Any]) -> dict[str, Any]:
        if any(not _CAPABILITY.fullmatch(key) or "." not in key for key in value):
            raise ValueError("extensions must use namespaced capability keys")
        approval = value.get(APPROVAL_MODE_EXTENSION)
        if approval is not None and approval not in {"ask", "risk", "full"}:
            raise ValueError("ksadk.approval must be ask, risk, or full")
        collaboration = value.get(COLLABORATION_MODE_EXTENSION)
        if collaboration is not None and collaboration not in {"default", "plan"}:
            raise ValueError("ksadk.collaboration must be default or plan")
        goal = value.get(GOAL_OBJECTIVE_EXTENSION)
        if goal is not None and (
            not isinstance(goal, str) or not goal.strip() or len(goal) > 4096
        ):
            raise ValueError("ksadk.goal must be a non-empty string up to 4096 characters")
        return value

    @property
    def approval_mode(self) -> Literal["ask", "risk", "full"] | None:
        value = self.extensions.get(APPROVAL_MODE_EXTENSION)
        return value if value in {"ask", "risk", "full"} else None

    @property
    def collaboration_mode(self) -> Literal["default", "plan"] | None:
        value = self.extensions.get(COLLABORATION_MODE_EXTENSION)
        return value if value in {"default", "plan"} else None

    @property
    def goal_objective(self) -> str | None:
        value = self.extensions.get(GOAL_OBJECTIVE_EXTENSION)
        return value if isinstance(value, str) and value else None

    def required_capabilities(self) -> tuple[str, ...]:
        required: list[str] = []
        if any(isinstance(part, ConversationTextPart) for part in self.parts):
            required.append("text")
        for part in self.parts:
            if not isinstance(part, ConversationAttachmentPart):
                continue
            required.append(
                "attachment.image"
                if part.media_type.lower().startswith("image/")
                else "attachment.file"
            )
        if self.model_ref:
            required.append("model.select")
        if self.reasoning:
            required.append("reasoning.effort")
        if self.approval_mode:
            required.append("approval")
        if self.collaboration_mode == "plan":
            required.append("plan")
        if self.goal_objective:
            required.append("goal")
        extension_capabilities = {
            APPROVAL_MODE_EXTENSION: "approval",
            COLLABORATION_MODE_EXTENSION: "plan"
            if self.collaboration_mode == "plan"
            else None,
            GOAL_OBJECTIVE_EXTENSION: "goal",
        }
        required.extend(
            extension_capabilities.get(key, key)
            for key in self.extensions
            if extension_capabilities.get(key, key) is not None
        )
        return tuple(dict.fromkeys(required))


class ConversationItem(ConversationContractModel):
    api_version: Literal["conversation.ksadk.io/v1"] = "conversation.ksadk.io/v1"
    kind_version: Literal[1] = 1
    item_id: str = Field(min_length=1, max_length=512)
    parent_item_id: str | None = Field(default=None, min_length=1, max_length=512)
    source_event_ids: tuple[str, ...] = Field(min_length=1)
    session_id: str = Field(min_length=1, max_length=256)
    run_id: str = Field(min_length=1, max_length=256)
    kind: Literal[
        "user_message",
        "assistant_text",
        "reasoning",
        "tool_call",
        "approval",
        "progress",
        "plan",
        "goal",
        "artifact",
        "a2ui",
        "error",
        "unknown",
    ]
    operation: Literal["append", "replace", "completed"]
    lifecycle: Literal["pending", "streaming", "completed", "failed"]
    visibility: Literal["public", "internal", "hidden"] = "public"
    payload_schema_ref: str = Field(min_length=1, max_length=256)
    payload: dict[str, Any] = Field(default_factory=dict)
    capability_ref: str | None = Field(default=None, max_length=256)
    native_ref: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_event_ids")
    @classmethod
    def validate_source_event_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item for item in value) or len(value) != len(set(value)):
            raise ValueError("sourceEventIds must be non-empty and unique")
        return value

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "ConversationItem":
        if self.operation == "completed" and self.lifecycle not in {"completed", "failed"}:
            raise ValueError("completed operation requires a terminal lifecycle")
        return self


def validate_surface_input(surface: ConversationSurface, payload: dict[str, Any]) -> None:
    """Reject UI input fields not declared by the active ConversationSurface."""

    extensions = payload.get("extensions", {})
    if extensions is not None and not isinstance(extensions, dict):
        raise ValueError("extensions must be an object")
    for field in payload:
        if field == "extensions":
            continue
        if not surface.permits_input(field):
            raise ValueError(f"conversation input is not declared by surface: {field}")


def validate_conversation_input(
    surface: ConversationSurface,
    conversation_input: ConversationInput,
) -> None:
    """Apply the same capability allowlist before and after the network hop."""

    if surface.session_id != conversation_input.session_id:
        raise ValueError("conversation input session does not match active surface")
    for capability in conversation_input.required_capabilities():
        if not surface.permits_input(capability):
            raise ValueError(f"conversation input is not declared by surface: {capability}")


__all__ = [
    "APPROVAL_MODE_EXTENSION",
    "COLLABORATION_MODE_EXTENSION",
    "ConversationCapability",
    "ConversationInput",
    "ConversationAttachmentPart",
    "ConversationItem",
    "ConversationSurface",
    "ConversationTextPart",
    "GOAL_OBJECTIVE_EXTENSION",
    "validate_conversation_input",
    "validate_surface_input",
]

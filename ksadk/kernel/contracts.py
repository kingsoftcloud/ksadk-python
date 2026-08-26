"""Agent Kernel v1 冻结合同（Pydantic 判别模型）。

对应 docs/superpowers/plans/2026-08-17-agent-runtime-v2-phase1-agent-kernel.md 第 2 节。
所有 envelope 使用 extra="allow" 保存未知 optional 字段，保证 forward-compatible round-trip；
payload 按 command_type 判别为独立模型。
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


def _validate_json_value(value: Any) -> Any:
    """运行时校验 JsonValue（py3.10 兼容取舍）。

    Pydantic 2 对旧式递归 Union alias 在类型求值阶段直接 RecursionError
    （实测 3.10/3.11 + pydantic 2.13 均如此），PEP 695 ``type`` 语句又是
    3.12+ 语法。因此 ``JsonValue = Annotated[Any, AfterValidator(...)]``：
    静态上不再递归，运行时保证值是合法 JSON（str key / 基本类型递归）。
    """

    def walk(node: Any) -> None:
        if node is None or isinstance(node, (str, bool, int, float)):
            return
        if isinstance(node, dict):
            for key, item in node.items():
                if not isinstance(key, str):
                    raise ValueError(
                        f"JsonValue dict keys must be str, got {type(key).__name__}"
                    )
                walk(item)
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        raise ValueError(f"value is not JSON-serializable: {type(node).__name__}")

    walk(value)
    return value


JsonValue = Annotated[Any, AfterValidator(_validate_json_value)]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)


# ------------------------------------------------------------------ payloads


class EnqueuePayload(WireModel):
    content: JsonValue
    reply_to: str | None = None


class SteerPayload(WireModel):
    content: JsonValue
    run_id: str | None = None


class InjectPayload(WireModel):
    context: JsonValue
    run_id: str | None = None


class InterruptPayload(WireModel):
    run_id: str | None = None
    reason: str | None = None


class PausePayload(WireModel):
    run_id: str | None = None
    reason: str | None = None


class ResumeTarget(WireModel):
    kind: Literal["checkpoint", "continuation", "run"]
    id: str


class ResumePayload(WireModel):
    target: ResumeTarget
    input: JsonValue = None


class SubmitInteractionPayload(WireModel):
    run_id: str
    interaction_id: str
    # token_ref 是一次性 interaction 授权引用，不是可持久化的原始 token。
    token_ref: str
    response: JsonValue
    # 以下为 additive 字段（Task 6）：允许 wire 侧携带完整 submitInteractionRequest；
    # 缺省时 Worker 以权威 InteractionRecord 补齐（expected_revision=当前 revision，
    # action=submit，idempotency_key=command.idempotency_key）。
    action: str | None = None
    expected_revision: int | None = None
    idempotency_key: str | None = None


PAYLOAD_MODELS: dict[str, type[WireModel]] = {
    "enqueue": EnqueuePayload,
    "steer": SteerPayload,
    "inject": InjectPayload,
    "interrupt": InterruptPayload,
    "pause": PausePayload,
    "resume": ResumePayload,
    "submit_interaction": SubmitInteractionPayload,
}


# ----------------------------------------------------------------- commands


class ControlSource(WireModel):
    kind: Literal[
        "studio", "responses", "agui", "a2a", "parent_agent",
        "scheduler", "workflow", "channel", "system",
    ]
    ref: str


class AgentControlCommand(WireModel):
    schema_version: Literal[1] = 1
    command_id: UUID
    idempotency_key: str
    tenant_id: str
    agent_instance_id: str
    session_id: str
    command_type: Literal[
        "enqueue", "steer", "inject", "interrupt",
        "pause", "resume", "submit_interaction",
    ]
    payload: dict[str, JsonValue]
    source: ControlSource
    authorization_ref: str
    submitted_at: str
    causation_id: str | None = None
    correlation_id: str | None = None

    @model_validator(mode="after")
    def _validate_payload_shape(self) -> "AgentControlCommand":
        PAYLOAD_MODELS[self.command_type].model_validate(dict(self.payload))
        return self


class AgentControlPermit(WireModel):
    schema_version: Literal[1] = 1
    permit_id: str
    subject_ref: str
    tenant_id: str
    agent_instance_id: str
    session_id: str | None
    allowed_operations: list[Literal[
        "enqueue", "steer", "inject", "interrupt", "pause", "resume",
        "submit_interaction", "get_status", "subscribe_events",
    ]]
    issued_at: str
    expires_at: str
    nonce: str
    key_id: str
    alg: Literal["Ed25519"] = "Ed25519"
    claims_digest: str
    signature: str


# ----------------------------------------------------------------- receipts


class ControlError(WireModel):
    code: str
    message: str
    retryable: bool
    details: dict[str, JsonValue] = Field(default_factory=dict)


class AgentControlReceipt(WireModel):
    schema_version: Literal[1] = 1
    command_id: UUID
    status: Literal[
        "accepted", "duplicate", "rejected", "unsupported",
        "queue_full", "persistence_uncertain",
    ]
    message_id: UUID | None = None
    run_id: str | None = None
    accepted_seq: int | None = None
    error: ControlError | None = None

    @model_validator(mode="after")
    def _validate_receipt_constraints(self) -> "AgentControlReceipt":
        if self.status in ("accepted", "duplicate"):
            if self.message_id is None:
                raise ValueError(f"{self.status} receipt must carry message_id")
        else:
            if self.error is None:
                raise ValueError(f"{self.status} receipt must carry error")
        return self


class AgentStatusQuery(WireModel):
    schema_version: Literal[1] = 1
    tenant_id: str
    agent_instance_id: str
    authorization_ref: str
    session_id: str | None = None


class SessionEventSubscription(WireModel):
    schema_version: Literal[1] = 1
    tenant_id: str
    agent_instance_id: str
    session_id: str
    authorization_ref: str
    after_seq: int = 0


# ------------------------------------------------------------- session events


class SessionEventEnvelope(WireModel):
    schema_version: Literal[1] = 1
    event_id: UUID
    session_id: str
    seq: int
    timestamp: str
    family: Literal[
        "control", "runtime", "workflow", "schedule", "job", "relationship",
        "interaction",
    ]
    family_version: int
    event_type: str
    payload: dict[str, JsonValue]
    run_id: str | None = None
    causation_id: str | None = None
    correlation_id: str | None = None
    actor_ref: str | None = None

    @model_validator(mode="after")
    def _validate_family_version(self) -> "SessionEventEnvelope":
        expected = {"control": 1, "runtime": 2, "interaction": 1}.get(self.family)
        if expected is not None and self.family_version != expected:
            raise ValueError(
                f"family {self.family} requires family_version {expected}, "
                f"got {self.family_version}"
            )
        return self


# ------------------------------------------------------------------- guards


class AdmissionWriteGuard(WireModel):
    authorization_ref: str
    command_id: UUID


class ActivationWriteGuard(WireModel):
    activation_id: str
    fencing_token: int


SessionEventWriteGuard = Union[AdmissionWriteGuard, ActivationWriteGuard]
WriteContext = ActivationWriteGuard


# -------------------------------------------------------------------- lease


class ActivationLease(WireModel):
    schema_version: Literal[1] = 1
    agent_instance_id: str
    activation_id: str
    fencing_token: int
    lease_expires_at: str
    bundle_digest: str
    runtime_type: str
    capability_digest: str


# --------------------------------------------------------------- capability


class RuntimeCapability(WireModel):
    supported: bool
    mode: Literal["native", "emulated", "unavailable"]
    reason: str | None = None

    @model_validator(mode="after")
    def _validate_unavailable(self) -> "RuntimeCapability":
        if not self.supported:
            if self.mode != "unavailable":
                raise ValueError("supported=false must pair with mode=unavailable")
            if not self.reason:
                raise ValueError("supported=false must carry a stable reason code")
        return self


class RuntimeCapabilityMatrix(WireModel):
    schema_version: Literal[1] = 1
    cancel: RuntimeCapability
    pause: RuntimeCapability
    resume: RuntimeCapability
    submit_interaction: RuntimeCapability
    attach: RuntimeCapability
    steer: RuntimeCapability
    inject: RuntimeCapability
    checkpoint: RuntimeCapability
    durable_restore: RuntimeCapability
    # Runtime v2 execution controls are additive optional capabilities. Older
    # runtimes omit them; a runtime must never infer support from UI presence.
    # ``loop`` specifically means an externally bounded, eval-driven
    # improvement loop. It is not the runtime's ordinary agent loop and it is
    # not an alias for collaboration_mode=default.
    goal: RuntimeCapability | None = None
    loop: RuntimeCapability | None = None
    plan: RuntimeCapability | None = None


class AgentStatusSnapshot(WireModel):
    schema_version: Literal[1] = 1
    agent_instance_id: str
    instance_state: Literal["ready", "degraded", "unavailable"]
    session_id: str | None = None
    active_run_id: str | None = None
    active_run_state: Literal["pending", "running", "paused", "waiting"] | None = None
    inbox_depth: int
    activation_id: str | None = None
    lease_expires_at: str | None = None
    capability: RuntimeCapabilityMatrix


__all__ = [
    "JsonValue",
    "WireModel",
    "EnqueuePayload",
    "SteerPayload",
    "InjectPayload",
    "InterruptPayload",
    "PausePayload",
    "ResumeTarget",
    "ResumePayload",
    "SubmitInteractionPayload",
    "ControlSource",
    "AgentControlCommand",
    "AgentControlPermit",
    "ControlError",
    "AgentControlReceipt",
    "AgentStatusQuery",
    "SessionEventSubscription",
    "SessionEventEnvelope",
    "AdmissionWriteGuard",
    "ActivationWriteGuard",
    "SessionEventWriteGuard",
    "WriteContext",
    "ActivationLease",
    "RuntimeCapability",
    "RuntimeCapabilityMatrix",
    "AgentStatusSnapshot",
]

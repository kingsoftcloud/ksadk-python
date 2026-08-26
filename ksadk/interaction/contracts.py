# -*- coding: utf-8 -*-
"""Interaction/v1 冻结合同（对齐 contracts/agent-kernel/v1/interaction.schema.json）。

Wire 模型与 JSON Schema 一一对应；``InteractionRecord`` 是内核侧的完整
durable 行（含内部 ``provider_id`` / ``native_target`` / 不透明的
``continuation_metadata``），对外投影（SessionEvent payload、公共 API 返回）
必须省略这三个字段。
"""

from __future__ import annotations

from typing import Any, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

InteractionKind = Literal["approval", "structured_input", "plan_review", "custom"]
InteractionStatus = Literal[
    "pending", "resolving", "resolved", "cancelled", "expired"
]
InteractionAction = Literal["approve", "reject", "submit", "cancel"]
InteractionOutcome = Literal[
    "approved", "rejected", "submitted", "cancelled", "expired"
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class A2UIPresentation(_StrictModel):
    wire_version: Literal["0.9.1"] = "0.9.1"
    catalog_digest: str
    messages: list[dict[str, Any]]


class InteractionPresentation(_StrictModel):
    title: str
    description: str | None = None
    a2ui: A2UIPresentation | None = None


class InteractionRecord(_StrictModel):
    """一条 interaction 的 durable 状态行（内部权威形态）。

    公开投影（``public_request()`` / interaction.requested 事件 payload）
    省略 ``provider_id`` / ``native_target`` / ``continuation_metadata``。
    """

    schema_version: Literal[1] = 1
    interaction_id: str
    tenant_id: str
    agent_instance_id: str
    session_id: str
    run_id: str
    kind: InteractionKind
    request_schema: dict[str, Any]
    revision: int = 1
    status: InteractionStatus = "pending"
    created_at: str
    expires_at: str | None = None
    presentation: InteractionPresentation | None = None
    # ---- 内部字段：绝不进入公共事件 / 公共 API 投影 ----
    provider_id: str = ""
    native_target: dict[str, Any] | None = None
    continuation_metadata: dict[str, Any] | None = None

    def public_request(self) -> dict[str, Any]:
        """interactionRequest 投影（schema additionalProperties=false）。"""
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "interaction_id": self.interaction_id,
            "tenant_id": self.tenant_id,
            "agent_instance_id": self.agent_instance_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "kind": self.kind,
            "request_schema": self.request_schema,
            "revision": self.revision,
            "created_at": self.created_at,
        }
        if self.expires_at is not None:
            payload["expires_at"] = self.expires_at
        if self.presentation is not None:
            payload["presentation"] = self.presentation.model_dump(mode="json")
        return payload


class InteractionSubmission(_StrictModel):
    """用户对一条 pending interaction 的提交（submitInteractionRequest）。"""

    schema_version: Literal[1] = 1
    interaction_id: str
    expected_revision: int
    action: InteractionAction
    response: Any = None
    idempotency_key: str


class InteractionReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    interaction_id: str
    revision: int
    status: InteractionStatus
    outcome: InteractionOutcome | None = None
    event_id: UUID | str | None = None
    accepted_seq: int | None = None


RESOLVE_OUTCOMES: dict[str, InteractionOutcome] = {
    "approve": "approved",
    "reject": "rejected",
    "submit": "submitted",
}

TERMINAL_STATUSES = frozenset({"resolved", "cancelled", "expired"})


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


__all__ = [
    "A2UIPresentation",
    "InteractionAction",
    "InteractionKind",
    "InteractionOutcome",
    "InteractionPresentation",
    "InteractionRecord",
    "InteractionReceipt",
    "InteractionStatus",
    "InteractionSubmission",
    "RESOLVE_OUTCOMES",
    "TERMINAL_STATUSES",
    "is_terminal",
]

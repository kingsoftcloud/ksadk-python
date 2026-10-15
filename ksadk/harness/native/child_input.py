"""Deliver user guidance to an owned child through the original command ledgers.

ZCode 872ad960 subagent/message-steering.ts targets the same runtime's guide
channel. KsADK additionally binds parent authority and child admission in one
Store commit; enqueueing in two independent transactions would lose that bound.
"""

import hashlib
from typing import Protocol

from pydantic import Field

from .guidance import GuidanceInput
from .state import Contract, RunCommand, RunScope, RunSnapshot


class ChildInputRejected(ValueError):
    """A known control rejection, not a failed effect or an unknown write."""


class ChildInput(GuidanceInput):
    child_run_id: str = Field(min_length=1, max_length=256, pattern=r"\S")


class ChildInputTarget(Contract):
    scope: RunScope
    run_id: str = Field(min_length=1)
    configuration_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_operation_key: str = Field(min_length=1)
    parent_tool_call_id: str = Field(min_length=1)


class ChildInputDelivery(Contract):
    target: ChildInputTarget
    command: RunCommand


class ChildInputPort(Protocol):
    def child_input_target(
        self, parent: RunSnapshot, child_run_id: str
    ) -> ChildInputTarget | None: ...

    def wake_child_input(self, parent: RunSnapshot, child_run_id: str) -> None: ...


def delivery_for(parent: RunSnapshot, command: RunCommand, target: ChildInputTarget):
    value = ChildInput.model_validate(command.payload)
    identity = "\0".join((parent.run_id, command.command_id)).encode()
    guide = RunCommand(
        command_id="child-input:" + hashlib.sha256(identity).hexdigest(),
        kind="guide",
        payload=GuidanceInput(
            text=value.text,
            expected_goal_revision=value.expected_goal_revision,
        ).model_dump(mode="json"),
    )
    return ChildInputDelivery(target=target, command=guide)


def check_delivery(parent, child, command, delivery, *, parent_commands, child_commands):
    """Validate under the Store lock/transaction before any proposed write."""
    if command.kind != "message_child" or parent.phase.terminal:
        raise ChildInputRejected("parent_child_input_unavailable")
    if any(item.kind in {"cancel", "policy_revoked", "steer"} for item in parent_commands):
        raise ChildInputRejected("parent_control_preempts_child_input")
    if any(item.kind in {"cancel", "policy_revoked", "steer"} for item in child_commands):
        raise ChildInputRejected("child_control_preempts_input")
    value = ChildInput.model_validate(command.payload)
    target = delivery.target
    origin = child.origin or {}
    if (
        child.run_id != value.child_run_id
        or child.run_id != target.run_id
        or child.scope != target.scope
        or child.configuration_digest != target.configuration_digest
        or any(
            getattr(child.scope, key) != getattr(parent.scope, key)
            for key in ("tenant_id", "workspace_id", "user_id")
        )
        or origin.get("parent_run_id") != parent.run_id
        or origin.get("parent_scope") != parent.scope.model_dump(mode="json")
        or origin.get("parent_operation_key") != target.parent_operation_key
        or origin.get("parent_tool_call_id") != target.parent_tool_call_id
        or not (
            child.run_id in parent.child_run_ids
            or target.parent_operation_key == parent.pending_intent
            or (
                parent.pending_intent is not None
                and target.parent_operation_key.startswith(parent.pending_intent + ":call:")
            )
        )
        or delivery != delivery_for(parent, command, target)
    ):
        raise ChildInputRejected("child_input_origin_changed")
    if child.phase.terminal:
        raise ChildInputRejected("child_is_terminal")
    if child.goal_revision != value.expected_goal_revision:
        raise ChildInputRejected("stale_child_goal_revision")

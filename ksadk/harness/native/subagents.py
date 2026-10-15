"""Host-issued delegation grants and child observations, never parent acceptance."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_serializer, model_validator

from .ports import ToolBudget
from .state import Contract, RunScope


class ChildRole(Contract):
    role_id: str = Field(min_length=1, pattern=r"\S")
    description: str
    agent_id: str | None = Field(default=None, min_length=1)
    tool_names: tuple[str, ...] = ()
    workspace_access: Literal["read_only", "isolated_write"] = "read_only"
    concurrent_safe: bool = False


class ChildRunRequest(Contract):
    scope: RunScope
    parent_scope: RunScope
    child_run_id: str
    child_session_id: str
    parent_run_id: str
    parent_tool_call_id: str
    parent_operation_key: str
    task: str = Field(min_length=1, max_length=32000, pattern=r"\S")
    # Immutable initial mode for an explicitly host-declared dual-mode tool.
    # Promotion changes delivery through parent events, not this request.
    delivery_mode: Literal["foreground", "background"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    # A caller-declared delivery type, separate from the prose task and role
    # capability grant. Absence retains the original request wire identity.
    output_format: Literal["text", "json_object"] | None = None
    # Select the host's current parent delivery shape. This does not inherit
    # parent acceptance, tools, budget or any model-selected schema values.
    output_contract: Literal["parent"] | None = None
    # Host-bound source of a fresh turn in an already accounted child Session.
    # It is not a request to reopen the source Run or replay its effects.
    continuation_of: str | None = Field(default=None, min_length=1, max_length=256, pattern=r"\S")
    continuation_parent_run_id: str | None = Field(
        default=None, min_length=1, max_length=256, pattern=r"\S",
    )
    role: ChildRole
    budget: ToolBudget

    @model_serializer(mode="wrap")
    def omit_unconfigured_output(self, serialize):
        payload = serialize(self)
        if self.output_format is None:
            payload.pop("output_format", None)
        if self.output_contract is None:
            payload.pop("output_contract", None)
        if self.continuation_of is None:
            payload.pop("continuation_of", None)
        if self.continuation_parent_run_id is None:
            payload.pop("continuation_parent_run_id", None)
        return payload

    @model_validator(mode="after")
    def explicit_parent_delivery(self):
        if self.output_contract is not None and self.output_format != "json_object":
            raise ValueError("parent output contract requires explicit object delivery")
        if (self.continuation_of is None) != (self.continuation_parent_run_id is None):
            raise ValueError("child continuation requires its source parent identity")
        return self


class ChildObservation(Contract):
    schema_version: Literal["ksadk.child-observation/v1"] = "ksadk.child-observation/v1"
    child_run_id: str
    child_scope: RunScope
    parent_run_id: str
    parent_tool_call_id: str
    phase: str
    outcome: str | None = None
    error_code: str | None = None
    final_text: str = ""
    text_truncated: bool = False
    evidence_refs: tuple[str, ...] = ()
    parent_acceptance: Literal["not_evaluated"] = "not_evaluated"
    resource_usage_unknown: bool = False

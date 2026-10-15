"""Typed child-owner effect seam, separate from pure parent decisions."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import Field, JsonValue

from .ports import ToolResult
from .state import ChildApproval, ChildBudgetReservation, Contract, RunScope


class ChildLifecycleRequest(Contract):
    scope: RunScope
    run_id: str
    operation_key: str
    action: Literal["poll", "wait", "cancel"]
    reservations: tuple[ChildBudgetReservation, ...] = Field(min_length=1)
    child_approval: ChildApproval | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    interaction: dict[str, JsonValue] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class ChildLifecycleResult(Contract):
    operation_key: str
    results: tuple[ToolResult, ...] = ()
    pending_child_run_ids: tuple[str, ...] = ()
    error_code: Literal["child_owner_unavailable", "child_receipt_unavailable"] | None = None
    approvals: tuple[ChildApproval, ...] = Field(default=(), exclude_if=lambda value: not value)


class ChildLifecyclePort(Protocol):
    """Poll/wait observe existing children; cancel is the explicit stop action.

    An owned-background/v2 host must shield child execution from cancellation
    of its observation coroutine. Joining that coroutine closes only a waiter;
    the parent can later collect the same sealed receipts without restarting.
    """

    async def collect(self, request: ChildLifecycleRequest) -> ChildLifecycleResult: ...


class ChildResourceReleaseRequest(Contract):
    scope: RunScope
    run_id: str = Field(min_length=1)


class ChildResourceReleasePort(Protocol):
    """Release this host's tasks, independent of permission to mutate a parent."""

    async def release_run(self, request: ChildResourceReleaseRequest) -> None: ...


class ChildInterruptedReceiptRequest(Contract):
    """Read known child facts before closing this parent's original tool effect."""

    scope: RunScope
    run_id: str = Field(min_length=1)
    parent_operation_key: str = Field(min_length=1)


class ChildInterruptedReceipt(Contract):
    parent_operation_key: str = Field(min_length=1)
    parent_tool_call_id: str = Field(min_length=1)
    result: ToolResult


class ChildInterruptedReceiptPort(Protocol):
    def interrupted_receipts(
        self, request: ChildInterruptedReceiptRequest
    ) -> tuple[ChildInterruptedReceipt, ...]: ...


class ChildReadyRequest(Contract):
    """Read readiness for the original unsettled allocations, without collecting."""

    scope: RunScope
    run_id: str = Field(min_length=1)
    reservations: tuple[ChildBudgetReservation, ...] = Field(min_length=1)
    include_approvals: bool = Field(default=False, strict=True, exclude_if=lambda value: not value)


class ChildReadyResult(Contract):
    child_run_ids: tuple[str, ...] = ()


class ChildReadyPort(Protocol):
    def ready_children(self, request: ChildReadyRequest) -> ChildReadyResult: ...

"""Admit known owned-child receipts against the original tool effect grant."""

from __future__ import annotations

from .call_budget import call_room
from .child_budget import validate_child_budget_admission
from .child_lifecycle_contract import ChildInterruptedReceipt
from .model import ModelToolCall
from .ports import RunConfiguration, ToolBudget, ToolRequest
from .state import EffectIntent, RunSnapshot


def admit_interrupted_child_receipts(
    snapshot: RunSnapshot,
    intent: EffectIntent,
    config: RunConfiguration,
    receipts: tuple[ChildInterruptedReceipt, ...],
) -> tuple[tuple[int, dict, ChildInterruptedReceipt], ...]:
    """Validate the entire factual batch before the Kernel commits any member.

    This does not execute, replay, or cancel a child. The persisted grant is
    the original admission, including the parent's once-only final reserve.
    """
    if not receipts:
        return ()
    if intent.kind != "tool" or snapshot.pending_intent != intent.operation_key:
        raise ValueError("child_interrupted_receipt_parent_changed")
    parallel = intent.payload.get("parallel_delegation") is True
    calls = intent.payload["calls"] if parallel else [intent.payload["call"]]
    budgets = intent.payload.get("child_dispatch_budgets")
    if not isinstance(budgets, list) or len(budgets) != len(calls):
        raise ValueError("child_interrupted_receipt_missing_admission")
    admitted, seen = [], set()
    for supplied in receipts:
        frame = ChildInterruptedReceipt.model_validate_json(supplied.model_dump_json())
        index = next(
            (
                index
                for index, call in enumerate(calls)
                if call["call_id"] == frame.parent_tool_call_id
            ),
            None,
        )
        if index is None or index in seen:
            raise ValueError("child_interrupted_receipt_member_conflict")
        seen.add(index)
        call = ModelToolCall.model_validate(calls[index])
        key = f"{intent.operation_key}:call:{index + 1}" if parallel else intent.operation_key
        definition = next((tool for tool in config.tools if tool.name == call.name), None)
        if (
            frame.parent_operation_key != key
            or definition is None
            or definition.child_budget_policy not in {"delegate/v1", "start/v1"}
            or parallel
            and not (
                definition.delegated_read_only
                and definition.read_only
                and definition.concurrent_safe
            )
        ):
            raise ValueError("child_interrupted_receipt_not_admitted")
        budget = ToolBudget.model_validate(budgets[index])
        request = ToolRequest(
            scope=snapshot.scope,
            run_id=snapshot.run_id,
            operation_key=key,
            call=call,
            definition=definition,
            budget=budget,
        )
        validate_child_budget_admission(request, frame.result)
        usage = frame.result.resource_usage
        if (
            frame.result.child_budget_settlement is not None
            or not call_room(budget.max_model_calls_remaining, 0, usage.model_calls)
            or not call_room(budget.max_tool_calls_remaining, 0, usage.tool_calls)
            or frame.result.child_budget_reservation is None
            and not usage.child_run_ids
        ):
            raise ValueError("child_interrupted_receipt_usage_not_admitted")
        admitted.append((index, calls[index], frame))
    return tuple(admitted)

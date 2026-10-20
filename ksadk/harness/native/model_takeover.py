"""Opt-in takeover planning only; no I/O, dispatch, clock reads or provider result fiction.

The private opt-in Kernel integration persists reservations before first claim,
uses atomic command barriers at settlement and claim, and retains ordinary
control handling. The helper itself does not access stores, clocks or ports.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from math import isfinite
from typing import Literal

from pydantic import Field, JsonValue, ValidationError

from .call_budget import call_room
from .effective_configuration import effective_run_configuration
from .model_recovery_contract import RunClockQuote, unavailable_uncertain_usage
from .ports import RunConfiguration
from .state import (
    CommitBatch,
    Contract,
    EffectIntent,
    EffectReceipt,
    RunCommand,
    RunPhase,
    RunSnapshot,
)
from .transitions import commit_transition, prepare_effect

POLICY = "pure-inference-takeover/v1"
KEY = "model_takeover_reservation"


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


class ModelTakeoverCapability(Contract):
    version: Literal["pure-inference-host/v1"] = "pure-inference-host/v1"
    host_binding_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    model_port_identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_side_effects_absent: Literal[True]
    tool_calls_are_local_proposals: Literal[True]
    hard_aggregate_token_or_cost_budget: Literal[False]
    clock_domain_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))


class ModelTakeoverReservation(Contract):
    version: Literal["model-takeover-reservation/v1"] = "model-takeover-reservation/v1"
    policy: Literal["pure-inference-takeover/v1"] = POLICY
    configuration_digest: str
    capability_digest: str
    semantic_digest: str
    scope_digest: str
    run_id: str
    operation_key: str
    predecessor_operation_key: str | None = None
    prepared_monotonic_seconds: float = Field(strict=True, ge=0, allow_inf_nan=False)
    clock_domain_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    prepared_at: float = Field(strict=True, ge=0, allow_inf_nan=False)
    effect_timeout_seconds: float = Field(strict=True, gt=0, allow_inf_nan=False)
    elapsed_at_preparation: float = Field(strict=True, ge=0, allow_inf_nan=False)
    debt_at_preparation: float = Field(strict=True, ge=0, allow_inf_nan=False)
    shared_provider_retries_spent: int = Field(strict=True, ge=0)


@dataclass(frozen=True)
class TakeoverDecision:
    reason: str
    batch: CommitBatch | None = None
    debt_increment_seconds: float = 0.0


def _semantic(snapshot: RunSnapshot, payload: dict[str, JsonValue]) -> str:
    # Provisional stream events and counters are deliberately not transcript input.
    return _digest(
        {
            "goal": snapshot.goal,
            "goal_revision": snapshot.goal_revision,
            "messages": snapshot.messages,
            "prepared_context": snapshot.prepared_context,
            "context_binding": snapshot.context_binding,
            "task_contract": snapshot.task_contract,
            "contract_digest": snapshot.contract_digest,
            "payload": {key: value for key, value in payload.items() if key != KEY},
        }
    )


def _admission(snapshot, config, capability, commands) -> str | None:
    if config.model_recovery_policy != POLICY:
        return "policy_disabled"
    if capability is None or capability.host_binding_digest != config.host_binding_digest:
        return "host_capability_missing_or_changed"
    if (
        snapshot.configuration_digest != config.digest
        or snapshot.profile_digest != config.profile.digest
    ):
        return "configuration_identity_changed"
    if commands:
        return "pending_control_command"
    if snapshot.phase != RunPhase.REQUESTING_MODEL:
        return "phase_not_admitted"
    if (
        snapshot.parent_run_id is not None
        or snapshot.child_run_ids
        or snapshot.pending_tools
        or snapshot.pending_interaction
        or snapshot.compaction_request
        or snapshot.continuations
        or snapshot.output_restarts
        or snapshot.candidate
        or snapshot.delivery_review
    ):
        return "unsupported_partial_or_nested_state"
    return None


def _payload_admitted(payload) -> bool:
    return (
        set(payload) <= {"model_call", "purpose", "turn_control", KEY}
        and type(payload.get("model_call")) is int
        and payload["model_call"] >= 1
        and payload.get("purpose", "task") == "task"
        and payload.get("turn_control") is None
    )


def _now_valid(now) -> bool:
    return type(now) in (int, float) and isfinite(now) and now >= 0


def _call_room(snapshot, config, *, unknown_calls: int) -> bool:
    # Reserve the original host allowances, never create or borrow a new pool.
    protected = (
        config.limits.finalization_reserve_model_calls
        + config.limits.verification_reserve_model_calls
    )
    return (
        call_room(config.limits.max_model_calls,
                  snapshot.model_calls + snapshot.descendant_model_calls,
                  unknown_calls + 1 + protected)
    )


def _wall_room(snapshot, config, debt: float) -> bool:
    limit = config.limits.max_wall_seconds
    return (
        limit is None
        or snapshot.execution_elapsed_seconds + debt + config.limits.effect_timeout_seconds <= limit
    )


def _reservation(snapshot, config, capability, key, payload, now, retries, quote, predecessor=None):
    return ModelTakeoverReservation(
        configuration_digest=config.digest,
        capability_digest=capability.digest,
        semantic_digest=_semantic(snapshot, payload),
        scope_digest=_digest(snapshot.scope.model_dump(mode="json")),
        run_id=snapshot.run_id,
        operation_key=key,
        predecessor_operation_key=predecessor,
        prepared_at=float(now),
        prepared_monotonic_seconds=quote.monotonic_seconds,
        clock_domain_digest=quote.clock_domain_digest,
        effect_timeout_seconds=config.limits.effect_timeout_seconds,
        elapsed_at_preparation=snapshot.execution_elapsed_seconds,
        debt_at_preparation=snapshot.model_recovery_debt_seconds,
        shared_provider_retries_spent=retries,
    )


def _quoted(snapshot, capability, now, quote):
    if quote is None:
        return None
    if (
        quote.source_snapshot_digest
        != hashlib.sha256(snapshot.model_dump_json().encode()).hexdigest()
        or quote.wall_time != now
        or quote.clock_domain_digest != capability.clock_domain_digest
        or quote.measured_elapsed_seconds < snapshot.execution_elapsed_seconds
    ):
        return None
    return snapshot.model_copy(update={"execution_elapsed_seconds": quote.measured_elapsed_seconds})


def prepare_reserved_model(
    snapshot: RunSnapshot,
    config: RunConfiguration,
    capability: ModelTakeoverCapability | None,
    *,
    now: float,
    payload: dict[str, JsonValue] | None = None,
    commands: tuple[RunCommand, ...] = (),
    clock_quote: RunClockQuote | None = None,
) -> TakeoverDecision:
    """Create the *initial* persisted reservation before claim/dispatch.

    Never retrofit a grant onto a previously prepared/running intent. The normal
    request must have room for its full original effect timeout when wall-limited.
    """
    if reason := _admission(snapshot, config, capability, commands):
        return TakeoverDecision(reason)
    config = effective_run_configuration(config, snapshot)
    if snapshot.pending_intent is not None:
        return TakeoverDecision("existing_intent_cannot_be_retrofitted")
    if (quoted := _quoted(snapshot, capability, now, clock_quote)) is None:
        return TakeoverDecision("clock_quote_missing_or_invalid")
    snapshot = quoted
    if payload is None:
        from .machine import next_transition

        payload = next_transition(snapshot, config).intents[0].payload
    payload = dict(payload)
    if (
        KEY in payload
        or not _payload_admitted(payload)
        or payload["model_call"] != snapshot.model_calls + 1
    ):
        return TakeoverDecision("unsupported_model_purpose_or_payload")
    if not _now_valid(now):
        return TakeoverDecision("invalid_clock")
    if not _call_room(snapshot, config, unknown_calls=0):
        return TakeoverDecision("model_budget_reserved")
    if not _wall_room(snapshot, config, snapshot.model_recovery_debt_seconds):
        return TakeoverDecision("wall_reservation_unaffordable")
    batch = prepare_effect(snapshot, "model", payload)
    intent = batch.intents[0]
    reserved = _reservation(
        snapshot,
        config,
        capability,
        intent.operation_key,
        payload,
        now,
        max(snapshot.provider_errors, snapshot.model_recovery_retries_spent),
        clock_quote,
    )
    intent = intent.model_copy(
        update={"payload": {**payload, KEY: reserved.model_dump(mode="json")}}
    )
    return TakeoverDecision(
        "initial_reserved",
        batch.model_copy(
            update={
                "intents": (intent,),
                "require_no_pending_commands": True,
            }
        ),
    )


def settle_lost_model(
    snapshot: RunSnapshot,
    intent: EffectIntent,
    receipt: EffectReceipt | None,
    config: RunConfiguration,
    capability: ModelTakeoverCapability | None,
    *,
    now: float,
    commands: tuple[RunCommand, ...] = (),
    clock_quote: RunClockQuote | None = None,
) -> TakeoverDecision:
    """Plan one atomic unknown settlement + unique prepared successor; never dispatch.

    A store must enforce revision/fence/lease and require_no_pending_commands in
    the same transaction. Existing receipts are delegated to the ordinary path.
    """
    if reason := _admission(snapshot, config, capability, commands):
        return TakeoverDecision(reason)
    config = effective_run_configuration(config, snapshot)
    if receipt is not None:
        return TakeoverDecision("already_settled")
    if (
        intent.kind != "model"
        or intent.status != "running"
        or intent.operation_key != snapshot.pending_intent
    ):
        return TakeoverDecision("not_lost_running_model")
    if (
        not _payload_admitted(intent.payload)
        or intent.payload["model_call"] != snapshot.model_calls + 1
    ):
        return TakeoverDecision("unsupported_model_purpose_or_payload")
    if (quoted := _quoted(snapshot, capability, now, clock_quote)) is None:
        return TakeoverDecision("clock_quote_missing_or_invalid")
    snapshot = quoted
    try:
        reservation = ModelTakeoverReservation.model_validate(intent.payload.get(KEY))
    except (ValidationError, TypeError):
        return TakeoverDecision("reservation_missing_or_invalid")
    if (
        reservation.configuration_digest != config.digest
        or reservation.capability_digest != capability.digest
        or reservation.scope_digest != _digest(snapshot.scope.model_dump(mode="json"))
        or reservation.run_id != snapshot.run_id
        or reservation.operation_key != intent.operation_key
        or reservation.semantic_digest != _semantic(snapshot, intent.payload)
        or reservation.effect_timeout_seconds != config.limits.effect_timeout_seconds
        or reservation.clock_domain_digest != clock_quote.clock_domain_digest
    ):
        return TakeoverDecision("reservation_identity_changed")
    if (
        not _now_valid(now)
        or now < reservation.prepared_at
        or clock_quote.monotonic_seconds < reservation.prepared_monotonic_seconds
        or snapshot.execution_elapsed_seconds < reservation.elapsed_at_preparation
        or snapshot.model_recovery_debt_seconds < reservation.debt_at_preparation
    ):
        return TakeoverDecision("clock_or_accounting_regressed")
    retries = (
        max(
            snapshot.provider_errors,
            snapshot.model_recovery_retries_spent,
            reservation.shared_provider_retries_spent,
        )
        + 1
    )
    if retries > config.profile.max_provider_retries:
        return TakeoverDecision("shared_provider_retry_budget_exhausted")
    if not _call_room(snapshot, config, unknown_calls=1):
        return TakeoverDecision("model_budget_reserved")
    # Reserve a full original effect timeout, or all elapsed crash/wait time if
    # greater. Subtract only measured time already committed since preparation.
    # This is budget debt, not invented provider usage or measured active time.
    conservative = max(
        reservation.effect_timeout_seconds,
        clock_quote.monotonic_seconds - reservation.prepared_monotonic_seconds,
    )
    measured = snapshot.execution_elapsed_seconds - reservation.elapsed_at_preparation
    increment = max(0.0, conservative - measured)
    debt = snapshot.model_recovery_debt_seconds + increment
    if not _wall_room(snapshot, config, debt):
        return TakeoverDecision("wall_reservation_unaffordable")
    successor = f"{intent.operation_key}:takeover"
    updated = snapshot.model_copy(
        update={
            "pending_intent": successor,
            "model_calls": snapshot.model_calls + 1,
            "provider_errors": retries,
            "model_recovery_retries_spent": retries,
            "model_recovery_debt_seconds": debt,
        }
    )
    payload = {key: value for key, value in intent.payload.items() if key != KEY}
    payload["model_call"] = updated.model_calls + 1
    next_reservation = _reservation(
        updated,
        config,
        capability,
        successor,
        payload,
        now,
        retries,
        clock_quote,
        intent.operation_key,
    )
    new_intent = EffectIntent(
        operation_key=successor,
        kind="model",
        payload={
            **payload,
            KEY: next_reservation.model_dump(mode="json"),
        },
    )
    unknown = EffectReceipt(
        operation_key=intent.operation_key,
        status="uncertain",
        result={"reason": "effect_outcome_unknown"},
    )
    batch = commit_transition(
        snapshot,
        updated,
        "effect.received",
        {
            "operation_key": intent.operation_key,
            "kind": "model",
            "status": "uncertain",
            # Close the abandoned preview using the existing explicit retirement
            # projection contract. The original delta remains in the Native ledger.
            "provisional_display_policy": "native.provisional-display/v1",
        },
        receipts=(unknown,),
        intents=(new_intent,),
        extra_events=(
            unavailable_uncertain_usage(intent.operation_key),
            (
                "model.attempt_abandoned",
                {
                    "policy": POLICY,
                    "operation_key": intent.operation_key,
                    "successor_operation_key": successor,
                    "usage_known": False,
                    "shared_provider_retries_spent": retries,
                    "wall_reservation_debt_increment_seconds": increment,
                    "model_recovery_debt_seconds": debt,
                },
            ),
            ("effect.prepared", {"operation_key": successor, "kind": "model"}),
        ),
    )
    return TakeoverDecision(
        "successor_prepared",
        batch.model_copy(
            update={
                "require_no_pending_commands": True,
            }
        ),
        increment,
    )

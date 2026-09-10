from __future__ import annotations

from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.readiness import runtime_readiness
from ksadk.harness.spec import (
    ApprovalPolicy,
    CapabilityBinding,
    CapabilityBindings,
    HarnessSpec,
    ModelBinding,
    PromptSpec,
)


def _spec(*, required: bool = True, approval_roles: tuple[str, ...] = ()) -> HarnessSpec:
    return HarnessSpec(
        agent_revision_ref="agent-revision://finance@3",
        model=ModelBinding(profile_ref="model-profile://glm@1"),
        prompt=PromptSpec(instructions="分析预算"),
        capabilities=CapabilityBindings(
            mcp_bindings=(
                CapabilityBinding(
                    capability_ref="mcp://budget@1",
                    required=required,
                    load_policy="on_demand",
                ),
            )
        ),
        approval_policy=ApprovalPolicy(mode="policy", approver_roles=approval_roles),
    )


def _event(seq: int, event_type: str, payload: dict) -> RuntimeEvent:
    return RuntimeEvent.create(
        event_type,
        agent_id="agent-1",
        user_id="user-1",
        session_id="session-1",
        invocation_id="run-1",
        run_id="run-1",
        scope_id="agent:agent-1",
        seq_id=seq,
        payload=payload,
    )


def _check(report: dict, check_id: str) -> dict:
    return next(item for item in report["checks"] if item["id"] == check_id)


def test_unobserved_progressive_capability_warns_but_does_not_block():
    report = runtime_readiness(_spec())
    capability = _check(report, "mcp:mcp://budget@1")
    assert capability["status"] == "warning"
    assert capability["reason_code"] == "capability_not_observed"
    assert report["status"] == "warning"
    assert report["deployable"] is True


def test_required_degraded_capability_blocks_deployment():
    events = [
        _event(
            1,
            EventType.CAPABILITY_DECLARED,
            {
                "capability_ref": "mcp://budget@1",
                "kind": "mcp",
                "state": "unknown",
                "required": True,
                "load_policy": "on_demand",
            },
        ),
        _event(
            2,
            EventType.CAPABILITY_DEGRADED,
            {
                "capability_ref": "mcp://budget@1",
                "kind": "mcp",
                "state": "degraded",
                "operation": "tools_list",
                "reason": "transport_error",
            },
        ),
    ]
    report = runtime_readiness(_spec(), events)
    assert report["status"] == "blocked"
    assert report["deployable"] is False
    assert _check(report, "mcp:mcp://budget@1")["reason_code"] == (
        "required_capability_degraded"
    )


def test_optional_degraded_capability_only_warns():
    events = [
        _event(
            1,
            EventType.CAPABILITY_DEGRADED,
            {
                "capability_ref": "mcp://budget@1",
                "kind": "mcp",
                "state": "degraded",
                "operation": "tools_list",
                "reason": "timeout",
            },
        )
    ]
    report = runtime_readiness(_spec(required=False), events)
    assert report["deployable"] is True
    assert _check(report, "mcp:mcp://budget@1")["status"] == "warning"


def test_successful_smoke_run_and_available_capability_are_ready():
    events = [
        _event(
            1,
            EventType.CAPABILITY_RECOVERED,
            {
                "capability_ref": "mcp://budget@1",
                "kind": "mcp",
                "state": "available",
                "operation": "tools_list",
                "reason": "operation_succeeded",
            },
        ),
        _event(2, EventType.MODEL_CALL_COMPLETED, {"model": "glm"}),
        _event(3, EventType.RUN_COMPLETED, {"status": "completed"}),
    ]
    report = runtime_readiness(_spec(approval_roles=("finance-admin",)), events)
    assert report["status"] == "ready"
    assert report["counts"]["blocked"] == 0
    assert report["observed_run_ids"] == ["run-1"]


def test_latest_model_failure_and_failed_run_block_without_exposing_error():
    events = [
        _event(1, EventType.MODEL_CALL_COMPLETED, {"model": "glm"}),
        _event(2, EventType.MODEL_CALL_FAILED, {"model": "glm", "error": "secret-url"}),
        _event(3, EventType.RUN_FAILED, {"status": "failed", "error": "private-host"}),
    ]
    report = runtime_readiness(_spec(), events)
    assert report["status"] == "blocked"
    assert _check(report, "model")["reason_code"] == "model_call_failed"
    assert _check(report, "smoke-run")["reason_code"] == "smoke_run_failed"
    assert "secret-url" not in str(report)
    assert "private-host" not in str(report)


def test_successful_fallback_reports_effective_model_without_blocking():
    events = [
        _event(
            1,
            EventType.MODEL_CALL_FAILED,
            {"model": "model-profile://glm@1", "attempt": 1, "fallback": False, "error": "x"},
        ),
        _event(
            2,
            EventType.MODEL_CALL_COMPLETED,
            {
                "model": "model-profile://backup@1",
                "attempt": 2,
                "fallback": True,
            },
        ),
        _event(3, EventType.RUN_COMPLETED, {"status": "completed"}),
    ]
    report = runtime_readiness(_spec(approval_roles=("admin",)), events)
    model = _check(report, "model")
    assert report["deployable"] is True
    assert model["resource_ref"] == "model-profile://backup@1"
    assert model["configured_resource_ref"] == "model-profile://glm@1"
    assert model["fallback_used"] is True
    assert model["attempt"] == 2

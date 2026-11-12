"""A model cannot expose one fixed remote target through competing surfaces."""

from __future__ import annotations

import pytest

from ksadk.a2a.exposure import compile_exposure_plan
from ksadk.a2a.invocation_types import RemoteAgentSpec

SPACE = "a2a-space-00000000000040008000000000000041"
TARGET = "a2a-agent-00000000000040008000000000000041"
OTHER = "a2a-agent-00000000000040008000000000000042"
VERSION = "a2a-version-00000000000040008000000000000041"


def spec(
    name: str = "finance",
    target: str = TARGET,
    exposure: str = "auto",
) -> RemoteAgentSpec:
    return RemoteAgentSpec(
        local_name=name,
        binding_id=f"binding-{name}",
        space_id=SPACE,
        target_agent_id=target,
        expected_version_id=VERSION,
        exposure=exposure,
    )


@pytest.mark.parametrize(
    ("framework", "requested", "want"),
    [
        ("adk", "auto", "native"),
        ("langgraph", "auto", "native"),
        ("codex", "auto", "expert_tool"),
        ("custom", "auto", "dynamic_tool"),
        ("adk", "tool", "dynamic_tool"),
        ("codex", "tool", "dynamic_tool"),
        ("langgraph", "native", "native"),
    ],
)
def test_framework_strategy_is_resolved_before_runtime(
    framework: str, requested: str, want: str
) -> None:
    plan = compile_exposure_plan(
        [spec(exposure=requested)], framework=framework, consumer_id="root"
    )
    assert plan.entries[0].exposure == want


@pytest.mark.parametrize("framework", ["codex", "custom"])
def test_explicit_unsupported_native_is_not_silently_downgraded(framework: str) -> None:
    with pytest.raises(ValueError, match="NATIVE_UNSUPPORTED"):
        compile_exposure_plan(
            [spec(exposure="native")], framework=framework, consumer_id="root"
        )


def test_target_or_local_name_conflicts_are_rejected() -> None:
    with pytest.raises(ValueError, match="EXPOSURE_CONFLICT"):
        compile_exposure_plan(
            [spec(), spec(name="writer")], framework="adk", consumer_id="root"
        )
    with pytest.raises(ValueError, match="LOCAL_NAME_CONFLICT"):
        compile_exposure_plan(
            [spec(), spec(target=OTHER)], framework="adk", consumer_id="root"
        )
    with pytest.raises(ValueError, match="BINDING_ID_CONFLICT"):
        compile_exposure_plan(
            [
                spec(),
                spec(name="writer", target=OTHER).model_copy(
                    update={"binding_id": "binding-finance"}
                ),
            ],
            framework="adk",
            consumer_id="root",
        )


def test_generic_guard_blocks_native_and_expert_but_not_dynamic() -> None:
    for framework in ("adk", "codex"):
        plan = compile_exposure_plan([spec()], framework=framework, consumer_id="root")
        with pytest.raises(ValueError, match="EXPOSURE_CONFLICT"):
            plan.assert_generic_target_allowed(TARGET)
        plan.assert_generic_target_allowed(OTHER)
    compile_exposure_plan(
        [spec(exposure="tool")], framework="adk", consumer_id="root"
    ).assert_generic_target_allowed(TARGET)


def test_plan_is_order_independent_and_revalidates_recovered_data() -> None:
    from ksadk.a2a.exposure import InvocationExposurePlan

    left = compile_exposure_plan(
        [spec(), spec(name="writer", target=OTHER)],
        framework="adk",
        consumer_id="root",
    )
    right = compile_exposure_plan(
        [spec(name="writer", target=OTHER), spec()],
        framework="adk",
        consumer_id="root",
    )
    assert left.model_dump_json() == right.model_dump_json()
    payload = left.model_dump()
    payload["entries"] = [payload["entries"][0], payload["entries"][0]]
    with pytest.raises(ValueError, match="EXPOSURE_CONFLICT"):
        InvocationExposurePlan.model_validate(payload)


def test_floating_version_cannot_enter_production_plan() -> None:
    floating = spec().model_copy(update={"expected_version_id": None})
    with pytest.raises(ValueError, match="VERSION_PIN_REQUIRED"):
        compile_exposure_plan([floating], framework="adk", consumer_id="root")

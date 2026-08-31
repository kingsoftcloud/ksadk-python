from __future__ import annotations

from ksadk.harness.compiler import compile_revision_payload


def test_compiler_accepts_full_control_plane_revision_shape() -> None:
    compiled = compile_revision_payload(
        {
            "role": {"name": "Finance", "objective": "Analyze a budget."},
            "orchestration": {"pattern": "single-agent"},
            "model": {
                "profileRef": "model-profile://finance@1.0.0",
                "fallbackProfileRefs": ["model-profile://backup@1.0.0"],
                "providerPolicy": {
                    "maxAttemptsPerModel": 3,
                    "totalAttemptBudget": 5,
                    "initialBackoffMs": 10,
                    "maxBackoffMs": 100,
                },
            },
            "capabilities": {
                "mcpBindings": [],
                "skillBindings": [],
                "knowledgeBindings": [],
            },
            "memory": {},
            "evaluation": {"evalPackRef": "eval-pack://finance@1.0.0"},
            "policy": {"policyRef": "policy://finance@1.0.0"},
            "deployment": {"target": "local"},
        },
        revision_ref="agent-revision://finance@1.0.0",
    )

    assert compiled.agent_revision_ref == "agent-revision://finance@1.0.0"
    assert compiled.model.profile_ref == "model-profile://finance@1.0.0"
    assert compiled.model.fallback_profile_refs == ("model-profile://backup@1.0.0",)
    assert compiled.model.provider_policy.max_attempts_per_model == 3
    assert compiled.model.provider_policy.total_attempt_budget == 5

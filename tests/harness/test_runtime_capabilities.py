from __future__ import annotations

from ksadk.harness.engine.langgraph import ManagedLangGraphEngine
from ksadk.harness.runtime_capabilities import (
    CapabilityOwner,
    CapabilitySupport,
    RuntimeHarnessCapabilities,
    managed_langgraph_capabilities,
    project_adapter_capabilities,
)
from ksadk.kernel.contracts import RuntimeCapability, RuntimeCapabilityMatrix


def test_runtime_harness_capabilities_require_all_governance_dimensions():
    declaration = managed_langgraph_capabilities()
    payload = declaration.model_dump(mode="json")
    assert payload["schema_version"] == "harness.runtime-capabilities/v1"
    assert set(payload["capabilities"]) == {
        "instructions",
        "history",
        "compaction",
        "memory",
        "tools",
        "streaming",
        "approval",
        "checkpoint",
        "recovery",
        "events",
    }
    assert all(item["owner"] for item in payload["capabilities"].values())
    assert declaration.capabilities["events"].evidence_refs


def test_managed_engine_exposes_runtime_declaration_from_production_object():
    declaration = ManagedLangGraphEngine().harness_capabilities()
    assert declaration.runtime_type == "managed-langgraph"
    assert declaration.capabilities["checkpoint"].support is CapabilitySupport.SUPPORTED


def test_runtime_capability_declaration_is_forward_compatible():
    declaration = RuntimeHarnessCapabilities.model_validate(
        {
            "runtime_type": "future-runtime",
            "capabilities": {
                name: {
                    "support": "unavailable",
                    "owner": "runtime",
                    "reason": "not_configured",
                }
                for name in RuntimeHarnessCapabilities.required_capability_names()
            },
            "future_field": {"safe": True},
        }
    )
    assert declaration.runtime_type == "future-runtime"


def test_external_adapter_projection_never_invents_unobservable_capabilities():
    unavailable = RuntimeCapability(
        supported=False, mode="unavailable", reason="runtime_no_native_checkpoint"
    )
    matrix = RuntimeCapabilityMatrix(
        cancel=RuntimeCapability(supported=True, mode="emulated"),
        pause=unavailable,
        resume=unavailable,
        submit_interaction=unavailable,
        attach=unavailable,
        steer=unavailable,
        inject=unavailable,
        checkpoint=unavailable,
        durable_restore=unavailable,
    )
    declaration = project_adapter_capabilities("adk", matrix)
    assert declaration.capabilities["checkpoint"].support is CapabilitySupport.UNAVAILABLE
    assert declaration.capabilities["recovery"].support is CapabilitySupport.UNAVAILABLE
    assert declaration.capabilities["memory"].support is CapabilitySupport.OPAQUE
    assert declaration.capabilities["memory"].owner is CapabilityOwner.RUNTIME

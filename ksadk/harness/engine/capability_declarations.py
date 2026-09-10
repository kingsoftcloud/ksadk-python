"""Project immutable Revision capability bindings into canonical runtime events."""

from __future__ import annotations

from typing import Any

from ksadk.harness.events import EventType, RuntimeEvent


def capability_declarations(engine: Any, run: Any) -> list[RuntimeEvent]:
    """Declare bound capabilities as unknown before their first observation."""

    spec = run.compiled.spec
    runtime_declaration = engine.harness_capabilities()
    declarations: list[tuple[str, str, bool, str, dict[str, Any]]] = [
        (
            f"runtime://{runtime_declaration.runtime_type}@v1#{run.handle.run_id}",
            "runtime",
            True,
            "eager",
            {"declaration": runtime_declaration.model_dump(mode="json")},
        )
    ]
    declarations.extend(
        (binding.capability_ref, "mcp", binding.required, binding.load_policy, {})
        for binding in spec.capabilities.mcp_bindings
    )
    declarations.extend(
        (binding.capability_ref, "skill", binding.required, binding.load_policy, {})
        for binding in spec.capabilities.skill_bindings
    )
    if any(name.startswith("sandbox_") for name in engine._tools):
        declarations.append(
            (
                f"sandbox://{spec.sandbox_policy.backend}@1",
                "sandbox",
                True,
                "on_demand",
                {},
            )
        )
    return [
        engine._event(
            run,
            EventType.CAPABILITY_DECLARED,
            {
                "capability_ref": capability_ref,
                "kind": kind,
                "state": "unknown",
                "required": required,
                "load_policy": load_policy,
                **extra,
            },
        )
        for capability_ref, kind, required, load_policy, extra in declarations
    ]


__all__ = ["capability_declarations"]

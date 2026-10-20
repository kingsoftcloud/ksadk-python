"""Trusted model and resource binding projection for authoring proposals."""

from __future__ import annotations

from typing import Any

from ksadk.plugins.providers.legacy_catalog import CODEX_AGENT_PROVIDER_PLUGIN_ID
from ksadk.studio.contracts import (
    AgentBindings, CapabilitiesSpec, CapabilityBinding, ModelSpec, RuntimeRef,
)


def _deduplicated_bindings(resource_ids: list[str]) -> list[CapabilityBinding]:
    """Build a deterministic binding list from Studio-selected resource ids."""

    seen: set[str] = set()
    result: list[CapabilityBinding] = []
    for resource_id in resource_ids:
        normalized = str(resource_id or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(CapabilityBinding(resource_id=normalized))
    return result

def _uses_codex_native_tools(runtime: RuntimeRef | None) -> bool:
    if runtime is None:
        return False
    return runtime.type == "codex" or (
        runtime.type == "plugin"
        and bool(runtime.provider_ref)
        and runtime.provider_ref.startswith(f"plugin://{CODEX_AGENT_PROVIDER_PLUGIN_ID}@")
    )

def _inject_managed_bindings(
    proposal: Any,
    *,
    model_spec: ModelSpec,
    bindings: AgentBindings,
) -> Any:
    """Replace all model/resource fields invented by the authoring model.

    The LLM may propose product semantics (name, runtime, instructions and
    execution strategy), but it is never a source of connection information or
    capability identities.  Those are selected in Studio and validated before
    this point.  The Profile id remains the sole persisted model reference;
    compiler/run materialisation resolves the current endpoint, credential and
    parameters from the catalog.  This also prevents provider discovery data,
    limits and pricing from leaking into every Agent Revision.  Resetting
    capabilities is intentional: a model-generated inline Tool/MCP/Skill
    contract must not become a deployable capability.
    """

    spec = proposal.spec
    managed_bindings = bindings
    # Codex Runtime owns its native tool surface.  Studio-selected KsADK
    # Tool contracts are meaningful for generic ADK/LangGraph Agents, but
    # serialising them into a Codex Draft Patch advertises a binding the
    # runtime cannot execute.  Enforce the same boundary as the UI here so a
    # direct API caller cannot bypass it.
    if proposal.runtimeType == "codex" and bindings.tools:
        managed_bindings = bindings.model_copy(update={"tools": []})
    # Resolution above is still intentional: it validates the selected Profile
    # before a proposal is returned.  Do not serialize its catalog contract.
    _ = model_spec
    return proposal.model_copy(
        update={
            "spec": spec.model_copy(
                update={
                    "model": None,
                    "bindings": managed_bindings.model_copy(deep=True),
                    "capabilities": CapabilitiesSpec(),
                }
            )
        }
    )

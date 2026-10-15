"""Host-captured read-only inputs for local engine comparison.

No tool callback, external transport, shared store or writable workspace is
passed to the shadow. Missing observations fail closed instead of falling back
to the primary executor. Capture is the host's responsibility, before launch.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ksadk.harness.native.adapters import StrictToolExecutor, ToolBinding, _snapshot
from ksadk.harness.native.completion import CompletionCandidate
from ksadk.harness.native.ports import (
    ToolDefinition,
    ToolResult,
    VerificationBundle,
    WorkspaceObservation,
    digest,
)
from ksadk.harness.native.state import Contract
from ksadk.harness.stores.native_memory import MemoryRunStore


class ShadowObservation(Contract):
    tool_name: str
    arguments: dict
    result: ToolResult


class ShadowInputs(Contract):
    """Trusted, immutable capture, never populated from a StartRequest."""

    definitions: tuple[ToolDefinition, ...] = ()
    observations: tuple[ShadowObservation, ...] = ()


class SnapshotTools:
    def __init__(self, scope, inputs: ShadowInputs):
        self.inputs = _snapshot(inputs)
        definitions = {d.name: d for d in self.inputs.definitions}
        if len(definitions) != len(self.inputs.definitions) or any(
            not d.read_only or d.delegated_read_only or d.observation_role != "none"
            for d in definitions.values()
        ):
            raise ValueError("native_shadow_requires_read_only_snapshot_tools")
        observations = {}
        for observation in self.inputs.observations:
            result = observation.result
            if observation.tool_name not in definitions or (
                result.status not in {"ok", "error"}
                or result.workspace_changed_paths
                or result.artifact_refs
                or result.browser_session_ref
                or result.resource_usage.child_run_ids
                or result.resource_usage.model_calls
                or result.resource_usage.tool_calls
            ):
                raise ValueError("native_shadow_invalid_observation")
            key = self._key(observation.tool_name, observation.arguments)
            if key in observations:
                raise ValueError("native_shadow_duplicate_observation")
            observations[key] = result

        async def captured(request):
            result = observations.get(self._key(request.call.name, request.call.arguments))
            if result is None:
                return ToolResult(status="error", content={"code": "shadow_observation_missing"})
            return _snapshot(result)

        async def authorize(_request):
            return "allow"

        self._executor = StrictToolExecutor(
            scope=scope,
            tools=tuple(ToolBinding(d, captured) for d in definitions.values()),
            authorize=authorize,
        )

    @staticmethod
    def _key(name, arguments):
        import json

        return name, json.dumps(arguments, sort_keys=True, allow_nan=False)

    async def execute(self, request):
        return await self._executor.execute(request)


@dataclass(frozen=True)
class SnapshotWorkspace:
    revision: str

    async def capture(self, request):
        return WorkspaceObservation(
            operation_key=request.operation_key,
            status="captured",
            revision=self.revision,
        )


@dataclass(frozen=True)
class ShadowVerifier:
    """No user verifier/test command is duplicated during shadow execution."""

    environment_digest: str

    async def verify(self, request):
        import hashlib

        return VerificationBundle(
            candidate=CompletionCandidate(
                candidate_id=request.candidate_id,
                final_text=request.final_text,
                subject_digest=hashlib.sha256(request.final_text.encode()).hexdigest(),
                subject_context_digest=request.subject_context_digest,
                environment_digest=self.environment_digest,
            ),
            report=None,
        )


def isolate_bindings(bindings, inputs: ShadowInputs):
    """Replace every execution-bearing tool/workspace/store binding."""
    captured = _snapshot(inputs)
    if bindings.configuration.tools != captured.definitions:
        raise ValueError("native_shadow_tool_definitions_unbound")
    if bindings.configuration.tool_recovery_policy is not None:
        raise ValueError("native_shadow_child_recovery_unavailable")
    identity = "sha256:" + digest(captured)
    return replace(
        bindings,
        tools=SnapshotTools(bindings.scope, captured),
        store=MemoryRunStore(),
        verifier=ShadowVerifier(identity),
        durable=False,
        lease_ttl_seconds=None,
        workspace=SnapshotWorkspace(identity),
        workspace_revision=identity,
        artifacts=None,
        skill_source=None,
        skill_refs=(),
        skill_authorizer=None,
        studio_admission=None,
        task_contract_factory=None,
    )

"""Studio execution vertical for immutable Harness/external Provider Builds."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import pytest

from ksadk.harness.reasoner import HarnessReasoningTurn
from ksadk.plugins.contracts import PluginManifest
from ksadk.plugins.providers.codex import CodexProviderInventory, CodexTurnResult
from ksadk.studio.contracts import (
    AgentSpec,
    Instructions,
    ModelSpec,
    NetworkPolicy,
    RunStatus,
    RuntimeRef,
    SecuritySpec,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.plugin_runtime import _normalize_result
from ksadk.studio.service import StudioService

_EXTERNAL_PROVIDER_REF = "plugin://io.example.echo-provider@1.0.0"


def _external_provider_manifest() -> PluginManifest:
    return PluginManifest.model_validate(
        {
            "metadata": {"id": "io.example.echo-provider", "version": "1.0.0"},
            "spec": {
                "domain": "runtime-native",
                "runtime": "process",
                "entrypoint": "deepseek-harness:profile-agent-provider",
                "provides": [
                    {
                        "definition": "agent.provider/v1",
                        "slot": "agent.execution",
                        "mode": "unique",
                    }
                ],
                "permissions": ["process:host-user"],
                "isolation": "sidecar",
                "compatibility": {
                    "kernelApi": ">=1,<2",
                    "runtimeProtocols": ["agentkit.runtime/v1"],
                },
                "healthContract": "plugin.health/v1",
                "provenance": {
                    "source": "runtime-native",
                    "digest": "sha256:" + "a" * 64,
                },
            },
        }
    )


class _ExternalPreparedAgent:
    def __init__(self, *, system: str) -> None:
        self.system = system
        self.turn = 0
        self.ready = False

    async def start(self) -> None:
        self.ready = True

    async def health(self) -> bool:
        return self.ready

    async def execute(self, request):  # noqa: ANN001
        self.turn += 1
        message = str(request["messages"][-1]["content"])
        return {
            "outputText": f"{self.system}:{message}:turn-{self.turn}",
            "sessionId": request["session_id"],
        }

    async def drain(self) -> None:
        self.ready = False

    async def dispose(self) -> None:
        self.ready = False


class _ExternalProviderRuntime:
    def __init__(self) -> None:
        self.ready = False
        self.prepared: list[_ExternalPreparedAgent] = []

    async def start(self) -> None:
        self.ready = True

    async def health(self) -> bool:
        return self.ready

    async def prepare(self, bundle, *, capabilities):  # noqa: ANN001
        assert capabilities.require("agent.provider/v1", slot="agent.execution")
        instructions = bundle.resolved_agent_spec["instructions"]
        prepared = _ExternalPreparedAgent(system=str(instructions["system"]))
        self.prepared.append(prepared)
        return prepared

    async def drain(self) -> None:
        self.ready = False

    async def dispose(self) -> None:
        self.ready = False


class _ExternalProviderFactory:
    def __init__(self, manifest: PluginManifest) -> None:
        self.manifest = manifest
        self.runtimes: list[_ExternalProviderRuntime] = []

    async def stage(self, manifest, *, profile, services):  # noqa: ANN001
        del profile, services
        assert manifest == self.manifest
        runtime = _ExternalProviderRuntime()
        self.runtimes.append(runtime)
        return runtime


def _model() -> ModelSpec:
    return ModelSpec(
        model="fixture-model",
        endpoint_url="https://model.example.test/v1/chat/completions",
        credential_ref="env://MODEL_API_KEY",
    )


def _security(*permissions: str) -> SecuritySpec:
    return SecuritySpec(
        allowed_permissions=list(permissions),
        network=NetworkPolicy(allowed_hosts=["model.example.test"]),
    )


def test_studio_normalizes_native_codex_provider_result() -> None:
    raw = CodexTurnResult(
        session_id="codex-session",
        output_text="codex-provider-ok",
        usage={"output_tokens": 3},
        metadata={"threadId": "thread-1"},
        inventory=CodexProviderInventory(
            provider="codex",
            model="fixture-model",
            mcp_servers=("metaso-inner",),
            skills=("skill-creator",),
        ),
    )

    result = _normalize_result(raw, session_id="fallback-session")

    assert result.output_text == "codex-provider-ok"
    assert result.session_id == "codex-session"
    assert result.usage == {"output_tokens": 3}
    assert result.metadata == {"threadId": "thread-1"}
    assert result.raw is raw


class _ConversationReasoner:
    def __init__(self) -> None:
        self.turns: list[list[dict[str, Any]]] = []

    async def complete(self, *, model, prompt, messages, tools):  # noqa: ANN001
        assert model == "fixture-model"
        assert "Retain conversation context" in prompt
        assert "sandbox_read_file" in {tool.name for tool in tools}
        snapshot = [dict(message) for message in messages]
        self.turns.append(snapshot)
        prior_answers = [
            str(message.get("content") or "")
            for message in snapshot[:-1]
            if message.get("role") == "assistant"
        ]
        return HarnessReasoningTurn(
            final_text=(
                "second:remembered"
                if any(answer == "first:stored" for answer in prior_answers)
                else "first:stored"
            )
        )


@pytest.mark.asyncio
async def test_new_harness_build_without_dsh_registration_fails_closed(
    tmp_path: Path,
) -> None:
    reasoner = _ConversationReasoner()
    studio = StudioService(tmp_path, harness_reasoner=reasoner)
    studio.create_agent(
        agent_id="harness-memory",
        name="Harness Memory",
        spec=AgentSpec(
            runtime=RuntimeRef(type="harness"),
            instructions=Instructions(system="Retain conversation context and answer exactly."),
            model=_model(),
            security=_security(),
        ),
    )
    with pytest.raises(StudioError) as captured:
        await studio.ensure_current_build("harness-memory")

    assert getattr(captured.value, "code", None) == "AGENT_PROVIDER_NOT_REGISTERED"
    assert studio.builds.list_for_agent("harness-memory") == []
    await studio.aclose()
    assert studio.plugin_runs.active_activation_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_type", ["adk", "langgraph"])
async def test_legacy_framework_build_selection_does_not_enter_pluginhost(
    tmp_path: Path,
    runtime_type: Literal["adk", "langgraph"],
) -> None:
    studio = StudioService(tmp_path)
    studio.create_agent(
        agent_id=f"legacy-{runtime_type}",
        name=f"Legacy {runtime_type}",
        spec=AgentSpec(
            runtime=RuntimeRef(
                type=runtime_type,
                project_path=f"agents/legacy-{runtime_type}/source",
                entry_point="agent.py",
                agent_variable="root_agent" if runtime_type == "adk" else "graph",
            ),
            instructions=Instructions(system="Preserve the framework path."),
            model=_model(),
            security=_security(),
        ),
    )

    build = await studio.ensure_current_build(f"legacy-{runtime_type}")
    spec = studio.resolve_run_spec(build.id)

    assert spec.launch_context.runtime_type == runtime_type
    assert spec.plugin_bundle_root is None
    assert studio.plugin_runs.active_activation_count == 0
    await studio.aclose()


@pytest.mark.asyncio
async def test_studio_uses_one_startup_provider_registration_for_build_and_runtime(
    tmp_path: Path,
) -> None:
    manifest = _external_provider_manifest()
    factory = _ExternalProviderFactory(manifest)
    studio = StudioService(
        tmp_path,
        plugin_provider_manifests={_EXTERNAL_PROVIDER_REF: manifest},
        plugin_provider_factories={_EXTERNAL_PROVIDER_REF: factory},
    )
    studio.create_agent(
        agent_id="external-provider-agent",
        name="External Provider Agent",
        spec=AgentSpec(
            runtime=RuntimeRef(
                type="plugin",
                provider_ref=_EXTERNAL_PROVIDER_REF,
            ),
            instructions=Instructions(system="registration-gated"),
            model=_model(),
            security=_security("process:host-user"),
        ),
    )

    build = await studio.ensure_current_build("external-provider-agent")
    first = await studio.run_build(build.id, "first", "external-session")
    second = await studio.run_build(build.id, "second", "external-session")

    assert first.status == RunStatus.COMPLETED, first.error
    assert first.output == "registration-gated:first:turn-1"
    assert second.output == "registration-gated:second:turn-2"
    assert len(factory.runtimes) == 1
    assert len(factory.runtimes[0].prepared) == 1

    await studio.aclose()
    assert factory.runtimes[0].ready is False


@pytest.mark.asyncio
async def test_plugin_result_usage_reaches_run_record(tmp_path: Path) -> None:
    """Provider 结果携带的用量必须落到运行记录（pluginhost 来源、reported=True）。"""

    manifest = _external_provider_manifest()
    factory = _ExternalProviderFactory(manifest)

    original_stage = factory.stage

    async def staged_with_usage(*args, **kwargs):  # noqa: ANN002, ANN003
        runtime = await original_stage(*args, **kwargs)
        original_prepare = runtime.prepare

        async def prepare_with_usage(bundle, *, capabilities):  # noqa: ANN001, ANN202
            prepared = await original_prepare(bundle, capabilities=capabilities)
            original_execute = prepared.execute

            async def execute_with_usage(request):  # noqa: ANN001
                result = await original_execute(request)
                result["usage"] = {
                    "input_tokens": 210,
                    "output_tokens": 33,
                    "cached_tokens": 12,
                    "reasoning_tokens": 9,
                }
                return result

            prepared.execute = execute_with_usage
            return prepared

        runtime.prepare = prepare_with_usage
        return runtime

    factory.stage = staged_with_usage  # type: ignore[method-assign]

    studio = StudioService(
        tmp_path,
        plugin_provider_manifests={_EXTERNAL_PROVIDER_REF: manifest},
        plugin_provider_factories={_EXTERNAL_PROVIDER_REF: factory},
    )
    studio.create_agent(
        agent_id="external-provider-usage",
        name="External Provider Usage",
        spec=AgentSpec(
            runtime=RuntimeRef(
                type="plugin",
                provider_ref=_EXTERNAL_PROVIDER_REF,
            ),
            instructions=Instructions(system="usage-boundary"),
            model=_model(),
            security=_security("process:host-user"),
        ),
    )

    build = await studio.ensure_current_build("external-provider-usage")
    record = await studio.run_build(build.id, "hello", "external-usage-session")

    assert record.status == RunStatus.COMPLETED, record.error
    assert record.usage.reported is True
    assert record.usage.input_tokens == 210
    assert record.usage.output_tokens == 33
    assert record.usage.total_tokens == 243
    assert record.usage.cached_input_tokens == 12
    assert record.usage.reasoning_output_tokens == 9
    assert record.usage.source == "pluginhost"

    await studio.aclose()


def test_studio_rejects_partial_provider_registration(tmp_path: Path) -> None:
    manifest = _external_provider_manifest()

    with pytest.raises(ValueError, match="same exact references"):
        StudioService(
            tmp_path,
            plugin_provider_manifests={_EXTERNAL_PROVIDER_REF: manifest},
        )

"""PluginHost vertical for the real KsADK Harness RuntimeAdapter provider."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ksadk.events.canonical import ItemCompleted
from ksadk.events.canonical_store import session_event_to_runtime_event
from ksadk.harness.config import McpToolSpec
from ksadk.harness.reasoner import HarnessReasoningTurn, HarnessToolCall
from ksadk.plugins.bundle import PluginBundleResolver
from ksadk.plugins.contracts import CompositionProfile, PluginManifest
from ksadk.plugins.host import PluginHost, PluginHostError
from ksadk.plugins.providers.harness import (
    HarnessSkillContribution,
    KsADKHarnessProviderFactory,
)
from ksadk.plugins.resolver import PluginRegistry
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.studio.contracts import BundleManifest, FileEntry
from tests.harness.fixtures.mcp_server import run_fixture_mcp_server


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _manifest(
    plugin_id: str,
    *,
    definition: str,
    slot: str,
    digit: str,
    mode: str = "unique",
) -> PluginManifest:
    return PluginManifest.model_validate(
        {
            "metadata": {"id": plugin_id, "version": "1.0.0"},
            "spec": {
                "domain": "ksadk-platform",
                "runtime": "python",
                "entrypoint": "ksadk.plugins.providers.harness:factory",
                "provides": [
                    {"definition": definition, "slot": slot, "mode": mode}
                ],
                "isolation": "in-process",
                "compatibility": {"kernelApi": ">=1,<2"},
                "healthContract": "plugin.health/v1",
                "provenance": {
                    "source": "builtin",
                    "digest": "sha256:" + digit * 64,
                },
            },
        }
    )


def _composition() -> tuple[PluginRegistry, CompositionProfile]:
    manifests = [
        _manifest(
            "io.ksadk.harness-provider",
            definition="agent.provider/v1",
            slot="agent.execution",
            digit="1",
        ),
        _manifest(
            "io.ksadk.weather-mcp",
            definition="mcp.connector/v1",
            slot="capability.mcp.weather",
            digit="3",
            mode="multiple",
        ),
        _manifest(
            "io.ksadk.weather-skill",
            definition="skill.source/v1",
            slot="capability.skill.weather",
            digit="4",
            mode="multiple",
        ),
        _manifest(
            "io.ksadk.tenant-context",
            definition="context.contributor/v1",
            slot="context.tenant",
            digit="5",
            mode="multiple",
        ),
    ]
    profile = CompositionProfile.model_validate(
        {
            "agentProvider": {
                "ref": "plugin://io.ksadk.harness-provider@1.0.0"
            },
            "capabilities": [
                {"ref": f"plugin://{manifest.metadata.id}@1.0.0"}
                for manifest in manifests[1:]
            ],
        }
    )
    return PluginRegistry(manifests), profile


def _write_bundle(
    root: Path,
    registry: PluginRegistry,
    profile: CompositionProfile,
):
    root.mkdir()
    composition = registry.resolve(profile)
    payloads = {
        "composition-profile.json": profile.model_dump(
            by_alias=True, exclude_none=True, mode="json"
        ),
        "plugin-lock.json": composition.plugin_lock.model_dump(
            by_alias=True, exclude_none=True, mode="json"
        ),
        "resolved-agent-spec.json": {
            "metadata": {"id": "weather-agent"},
            "model": {"model": "fixture-model"},
            "instructions": {
                "system": "You are a weather assistant.",
                "task": "Use bound capabilities and retain conversation context.",
            },
        },
    }
    files: list[FileEntry] = []
    for relative, payload in payloads.items():
        content = _json_bytes(payload)
        (root / relative).write_bytes(content)
        files.append(FileEntry(path=relative, sha256=_sha256(content), size=len(content)))
    manifest = BundleManifest(
        bundle_format="agentkit.bundle/v2",
        agent_id="weather-agent",
        source_revision=1,
        resolved_digest="sha256:" + "a" * 64,
        plugin_lock_digest=composition.plugin_lock_digest,
        composition_profile_digest=composition.profile_digest,
        files=files,
    )
    unsigned = manifest.model_dump(
        by_alias=True,
        exclude={"bundle_digest"},
        exclude_none=True,
        mode="json",
    )
    manifest.bundle_digest = _sha256(_json_bytes(unsigned))
    (root / "manifest.json").write_bytes(
        _json_bytes(manifest.model_dump(by_alias=True, exclude_none=True, mode="json"))
    )
    return PluginBundleResolver(registry).resolve(root)


@dataclass
class _Lifecycle:
    calls: list[str]
    plugin_id: str
    disposed: bool = False

    async def start(self) -> None:
        self.calls.append(f"start:{self.plugin_id}")

    async def health(self) -> bool:
        return not self.disposed

    async def drain(self) -> None:
        self.calls.append(f"drain:{self.plugin_id}")

    async def dispose(self) -> None:
        self.calls.append(f"dispose:{self.plugin_id}")
        self.disposed = True


class _MCPSource(_Lifecycle):
    def __init__(self, calls: list[str], url: str) -> None:
        super().__init__(calls=calls, plugin_id="io.ksadk.weather-mcp")
        self._url = url

    def harness_mcp_specs(self, bundle):  # noqa: ANN001
        self.calls.append(f"mcp.bind:{bundle.manifest.agent_id}")
        return (
            McpToolSpec(
                name="weather",
                url=self._url,
                api_key="harness-secret",
                tool_filter=("lookup",),
                tool_name_prefix="weather",
            ),
        )


class _SkillSource(_Lifecycle):
    def __init__(self, calls: list[str]) -> None:
        super().__init__(calls=calls, plugin_id="io.ksadk.weather-skill")

    def harness_skill(self, bundle):  # noqa: ANN001
        self.calls.append(f"skill.bind:{bundle.manifest.agent_id}")
        return HarnessSkillContribution(
            name="weather-style",
            instructions="Always cite the bound weather observation.",
        )


class _ContextSource(_Lifecycle):
    def __init__(self, calls: list[str]) -> None:
        super().__init__(calls=calls, plugin_id="io.ksadk.tenant-context")

    async def harness_context(self, bundle, request):  # noqa: ANN001
        self.calls.append(f"context:{request.session_id or 'new'}")
        return f"Tenant context for {bundle.manifest.agent_id}: city=Beijing."


class _SourceFactory:
    def __init__(self, runtime: _Lifecycle) -> None:
        self.runtime = runtime

    async def stage(self, manifest, *, profile, services):  # noqa: ANN001, ARG002
        assert manifest.metadata.id == self.runtime.plugin_id
        return self.runtime


class _HistoryMCPReasoner:
    def __init__(self) -> None:
        self.turn_messages: list[list[dict[str, Any]]] = []
        self.prompts: list[str] = []

    async def complete(self, *, model, prompt, messages, tools):  # noqa: ANN001
        assert model == "fixture-model"
        self.prompts.append(prompt)
        snapshot = [dict(item) for item in messages]
        self.turn_messages.append(snapshot)
        latest_user_index = max(
            index for index, item in enumerate(snapshot) if item.get("role") == "user"
        )
        latest_user = str(snapshot[latest_user_index].get("content") or "")
        current_tool_results = [
            item
            for item in snapshot[latest_user_index + 1 :]
            if item.get("role") == "tool"
        ]
        if "天气" in latest_user and not current_tool_results:
            assert "weather_lookup" in {tool.name for tool in tools}
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(
                        call_id="weather-1",
                        name="weather_lookup",
                        arguments={"value": "beijing"},
                    ),
                )
            )
        if current_tool_results:
            return HarnessReasoningTurn(
                final_text=f"第一轮观测：{current_tool_results[-1]['content']}"
            )
        prior_answers = [
            str(item.get("content") or "")
            for item in snapshot[:latest_user_index]
            if item.get("role") == "assistant"
        ]
        remembered = any("第一轮观测" in item for item in prior_answers)
        return HarnessReasoningTurn(
            final_text=f"第二轮记忆：{'已保留' if remembered else '缺失'}"
        )


def _host(
    registry: PluginRegistry,
    *,
    reasoner: _HistoryMCPReasoner,
    service: InMemorySessionService,
    mcp_url: str,
    incompatible_skill: bool = False,
):
    calls: list[str] = []
    provider_factory = KsADKHarnessProviderFactory(
        session_service=service,
        reasoner=reasoner,
    )
    mcp = _MCPSource(calls, mcp_url)
    skill: _Lifecycle = (
        _Lifecycle(calls, "io.ksadk.weather-skill")
        if incompatible_skill
        else _SkillSource(calls)
    )
    context = _ContextSource(calls)
    host = PluginHost(
        registry,
        {
            "io.ksadk.harness-provider": provider_factory,
            "io.ksadk.weather-mcp": _SourceFactory(mcp),
            "io.ksadk.weather-skill": _SourceFactory(skill),
            "io.ksadk.tenant-context": _SourceFactory(context),
        },
    )
    return host, provider_factory, calls, (mcp, skill, context)


@pytest.mark.asyncio
async def test_harness_provider_real_multiturn_mcp_skill_inventory_and_dispose(
    tmp_path: Path,
) -> None:
    registry, profile = _composition()
    bundle = _write_bundle(tmp_path / "agent-bundle", registry, profile)
    reasoner = _HistoryMCPReasoner()
    service = InMemorySessionService()

    with run_fixture_mcp_server(label="sunny") as fixture:
        host, provider_factory, calls, source_runtimes = _host(
            registry,
            reasoner=reasoner,
            service=service,
            mcp_url=fixture.url,
        )
        await host.apply(profile)
        first = await host.execute(
            bundle,
            {
                "user_id": "user-1",
                "messages": [{"role": "user", "content": "北京天气如何？"}],
            },
        )
        second = await host.execute(
            bundle,
            {
                "user_id": "user-1",
                "session_id": first.session_id,
                "messages": [{"role": "user", "content": "还记得上一轮吗？"}],
            },
        )

        assert first.output_text.startswith("第一轮观测：")
        assert "sunny:beijing" in first.output_text
        assert second.output_text == "第二轮记忆：已保留", reasoner.turn_messages[-1]
        assert second.session_id == first.session_id
        assert fixture.log.calls == [("lookup", "beijing")]
        assert first.inventory.mcp_servers == ("io.ksadk.weather-mcp",)
        assert first.inventory.skills == ("weather-style",)
        assert first.inventory.context_contributors == ("io.ksadk.tenant-context",)
        assert first.inventory.execution_strategy == "direct"
        assert first.inventory.history_owner == "canonical_session_service"
        assert all(
            "Always cite the bound weather observation." in item
            for item in reasoner.prompts
        )
        assert all("city=Beijing" in item for item in reasoner.prompts)
        assert [item.get("role") for item in reasoner.turn_messages[-1]] == [
            "system",
            "user",
            "assistant",
            "user",
        ]
        assert [
            str(item.get("content") or "") for item in reasoner.turn_messages[-1]
        ].count("第一轮观测：sunny:beijing") == 1
        stored = await service.get_events(first.session_id)
        assert [item.event_type for item in stored].count("user_message") == 2
        completed_messages = [
            runtime_event
            for item in stored
            if (runtime_event := session_event_to_runtime_event(item)) is not None
            and isinstance(runtime_event, ItemCompleted)
            and runtime_event.item_kind == "message"
        ]
        assert len(completed_messages) == 2
        assert calls.count("mcp.bind:weather-agent") == 2
        assert calls.count("skill.bind:weather-agent") == 2

        await host.dispose()

    assert provider_factory.runtime is not None
    assert provider_factory.runtime.disposed is True
    assert all(runtime.disposed for runtime in source_runtimes)


@pytest.mark.asyncio
async def test_harness_provider_rejects_unprojectable_skill_without_polluting_graph(
    tmp_path: Path,
) -> None:
    registry, profile = _composition()
    bundle = _write_bundle(tmp_path / "agent-bundle", registry, profile)
    service = InMemorySessionService()
    host, _provider_factory, _calls, _sources = _host(
        registry,
        reasoner=_HistoryMCPReasoner(),
        service=service,
        mcp_url="http://127.0.0.1:1/mcp",
        incompatible_skill=True,
    )
    stable = await host.apply(profile)

    with pytest.raises(PluginHostError, match="cannot project Harness instructions") as error:
        await host.execute(
            bundle,
            {
                "user_id": "user-1",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert error.value.code == "harness_skill_incompatible"
    assert host.inventory() == stable
    assert await service.list_sessions("weather-agent", "user-1") == []
    await host.dispose()

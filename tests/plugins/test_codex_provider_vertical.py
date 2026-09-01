"""Bundle -> PluginHost -> native Codex RuntimeAdapter vertical."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from ksadk.codex.client import CodexClient
from ksadk.events.store import RuntimeEventStore
from ksadk.plugins.bundle import PluginBundleResolver
from ksadk.plugins.contracts import CompositionProfile, PluginManifest
from ksadk.plugins.host import PluginHost, PluginHostError
from ksadk.plugins.providers.codex import CodexAgentProviderFactory
from ksadk.plugins.resolver import PluginRegistry
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.studio.contracts import BundleManifest, FileEntry

pytest.importorskip("openai_codex")


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _provider_manifest() -> PluginManifest:
    return PluginManifest.model_validate(
        {
            "metadata": {"id": "io.ksadk.codex-provider", "version": "1.0.0"},
            "spec": {
                "domain": "runtime-native",
                "runtime": "native",
                "provides": [
                    {
                        "definition": "agent.provider/v1",
                        "slot": "agent.execution",
                        "mode": "unique",
                    }
                ],
                "isolation": "native",
                "compatibility": {
                    "kernelApi": ">=1,<2",
                    "runtimeProtocols": ["AgentControlChannel/v1"],
                },
                "healthContract": "plugin.health/v1",
                "provenance": {
                    "source": "runtime-native",
                    "digest": "sha256:" + "1" * 64,
                },
            },
        }
    )


def _profile(*, provider_config: dict[str, Any] | None = None) -> CompositionProfile:
    return CompositionProfile.model_validate(
        {
            "agentProvider": {
                "ref": "plugin://io.ksadk.codex-provider@1.0.0",
                "config": provider_config or {},
            }
        }
    )


def _write_bundle(
    root: Path,
    registry: PluginRegistry,
    profile: CompositionProfile,
    *,
    execution_strategy: str = "direct",
    approval_mode: str = "risk",
    mcp_servers: list[dict[str, Any]] | None = None,
    models: list[str] | None = None,
):
    root.mkdir()
    skill = (
        b"---\nname: report-style\ndescription: Use concise reports.\n---\n\n"
        b"# Report style\n\nUse concise reports.\n"
    )
    composition = registry.resolve(profile)
    payloads: dict[str, bytes] = {
        "composition-profile.json": _json_bytes(
            profile.model_dump(by_alias=True, exclude_none=True, mode="json")
        ),
        "plugin-lock.json": _json_bytes(
            composition.plugin_lock.model_dump(
                by_alias=True, exclude_none=True, mode="json"
            )
        ),
        "resolved-agent-spec.json": _json_bytes(
            {
                "schemaVersion": "agentkit.resolved/v1",
                "agentId": "codex-report-agent",
                "model": {"model": "fixture-codex-model"},
                "instructions": {
                    "system": "You are a report assistant.",
                    "task": "Use the locked Bundle capabilities.",
                },
                "capabilities": {
                    "tools": [],
                    "mcpServers": (
                        [
                            {
                                "name": "weather",
                                "transport": "http",
                                "endpointUrl": "https://mcp.invalid.example/rpc",
                                "envRefs": {},
                            }
                        ]
                        if mcp_servers is None
                        else mcp_servers
                    ),
                    "skills": [
                        {
                            "name": "report-style",
                            "bundlePath": "capabilities/skills/report-style",
                        }
                    ],
                },
                "execution": {
                    "strategy": execution_strategy,
                    "timeoutSeconds": 30,
                    "sandbox": "read_only",
                    "approvalMode": approval_mode,
                },
            }
        ),
        "capabilities/skills/report-style/SKILL.md": skill,
    }
    if models is not None:
        payloads["runtime-lock.json"] = _json_bytes(
            {
                "type": "codex",
                "model": "fixture-codex-model",
                "models": models,
            }
        )
    files: list[FileEntry] = []
    for relative, content in payloads.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        files.append(FileEntry(path=relative, sha256=_sha256(content), size=len(content)))
    manifest = BundleManifest(
        bundle_format="agentkit.bundle/v2",
        agent_id="codex-report-agent",
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


class _StrictCodexBackend:
    def __init__(self) -> None:
        self.thread_count = 0
        self.turn_count = 0
        self.threads: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.prompts: list[tuple[str, Any]] = []
        self.turn_configs: list[dict[str, Any]] = []
        self.goal_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.configs: list[Any] = []
        self.closed = 0

    def client(self, config=None):  # noqa: ANN001
        self.configs.append(config)
        return _StrictCodexClient(self)


class _StrictCodexClient(CodexClient):
    """Strict dynamic App Server client; not a real Codex binary fixture."""

    def __init__(self, backend: _StrictCodexBackend) -> None:
        self.backend = backend
        self.attached: set[str] = set()

    async def start_thread(self, config=None) -> str:  # noqa: ANN001
        self.backend.thread_count += 1
        thread_id = f"thread-{self.backend.thread_count}"
        self.backend.threads.add(thread_id)
        self.backend.calls.append(("thread/start", thread_id))
        self.attached.add(thread_id)
        return thread_id

    async def resume_thread(self, thread_id: str, config=None) -> str:  # noqa: ANN001
        if thread_id not in self.backend.threads:
            raise RuntimeError(f"unknown thread {thread_id}")
        self.backend.calls.append(("thread/resume", thread_id))
        self.attached.add(thread_id)
        return thread_id

    def run_turn(
        self,
        thread_id: str,
        prompt: Any,
        *,
        config=None,  # noqa: ANN001
    ) -> AsyncIterator[dict[str, Any]]:
        self.backend.turn_configs.append(dict(config or {}))

        async def events() -> AsyncIterator[dict[str, Any]]:
            if thread_id not in self.attached:
                await self.resume_thread(thread_id)
            self.backend.turn_count += 1
            turn_id = f"turn-{self.backend.turn_count}"
            item_id = f"answer-{self.backend.turn_count}"
            answer = f"answer from {thread_id} turn {self.backend.turn_count}"
            self.backend.calls.append(("turn/start", thread_id))
            self.backend.prompts.append((thread_id, prompt))
            turn = {
                "id": turn_id,
                "status": "inProgress",
                "items": [],
                "error": None,
            }
            item = {
                "id": item_id,
                "memoryCitation": None,
                "phase": "final_answer",
                "text": "",
                "type": "agentMessage",
            }
            yield {"method": "turn/started", "params": {"threadId": thread_id, "turn": turn}}
            yield {
                "method": "item/started",
                "params": {"threadId": thread_id, "turnId": turn_id, "item": item},
            }
            yield {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "itemId": item_id,
                    "delta": answer,
                },
            }
            yield {
                "method": "item/completed",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "item": {**item, "text": answer},
                },
            }
            yield {
                "method": "turn/completed",
                "params": {
                    "threadId": thread_id,
                    "turn": {**turn, "status": "completed"},
                },
            }

        return events()

    def run_goal(
        self,
        thread_id: str,
        objective: str,
        *,
        config=None,  # noqa: ANN001
    ) -> AsyncIterator[dict[str, Any]]:
        self.backend.goal_calls.append((thread_id, objective, dict(config or {})))

        async def events() -> AsyncIterator[dict[str, Any]]:
            if thread_id not in self.attached:
                await self.resume_thread(thread_id)
            self.backend.turn_count += 1
            turn_id = f"goal-turn-{self.backend.turn_count}"
            item_id = f"goal-answer-{self.backend.turn_count}"
            answer = f"goal from {thread_id}: {objective}"
            self.backend.calls.append(("goal/start", thread_id))
            turn = {
                "id": turn_id,
                "status": "inProgress",
                "items": [],
                "error": None,
            }
            item = {
                "id": item_id,
                "memoryCitation": None,
                "phase": "final_answer",
                "text": "",
                "type": "agentMessage",
            }
            yield {"method": "turn/started", "params": {"threadId": thread_id, "turn": turn}}
            yield {
                "method": "item/started",
                "params": {"threadId": thread_id, "turnId": turn_id, "item": item},
            }
            yield {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "itemId": item_id,
                    "delta": answer,
                },
            }
            yield {
                "method": "item/completed",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "item": {**item, "text": answer},
                },
            }
            yield {
                "method": "turn/completed",
                "params": {
                    "threadId": thread_id,
                    "turn": {**turn, "status": "completed"},
                },
            }

        return events()

    async def interrupt_active_turn(self, thread_id: str) -> bool:
        del thread_id
        return False

    async def close(self) -> None:
        self.backend.closed += 1
        self.attached.clear()


def _setup(
    *,
    provider_config: dict[str, Any] | None = None,
) -> tuple[
    PluginRegistry,
    CompositionProfile,
    PluginHost,
    CodexAgentProviderFactory,
    _StrictCodexBackend,
    InMemorySessionService,
]:
    registry = PluginRegistry([_provider_manifest()])
    profile = _profile(provider_config=provider_config)
    backend = _StrictCodexBackend()
    service = InMemorySessionService()
    provider = CodexAgentProviderFactory(
        session_service=service,
        codex_client_factory=backend.client,
    )
    host = PluginHost(
        registry,
        {"io.ksadk.codex-provider": provider},
    )
    return registry, profile, host, provider, backend, service


@pytest.mark.asyncio
async def test_codex_provider_reuses_native_thread_and_isolates_other_session(
    tmp_path: Path,
) -> None:
    registry, profile, host, provider, backend, service = _setup()
    bundle = _write_bundle(tmp_path / "bundle", registry, profile)
    await host.apply(profile)

    first = await host.execute(bundle, {"user_id": "u1", "input": "first"})
    first_activation = provider.runtime.last_activation if provider.runtime else None
    second = await host.execute(
        bundle,
        {"user_id": "u1", "session_id": first.session_id, "input": "second"},
    )
    third = await host.execute(bundle, {"user_id": "u1", "input": "isolated"})

    assert first.output_text == "answer from thread-1 turn 1"
    assert second.output_text == "answer from thread-1 turn 2"
    assert third.output_text == "answer from thread-2 turn 3"
    assert first.session_id == second.session_id
    assert third.session_id != first.session_id
    assert backend.calls == [
        ("thread/start", "thread-1"),
        ("turn/start", "thread-1"),
        ("thread/resume", "thread-1"),
        ("turn/start", "thread-1"),
        ("thread/start", "thread-2"),
        ("turn/start", "thread-2"),
    ]
    assert first.inventory.model == "fixture-codex-model"
    assert first.inventory.mcp_servers == ("weather",)
    assert first.inventory.skills == ("report-style",)
    assert first_activation is not None and first_activation.disposed is True
    assert provider.runtime is not None and provider.runtime.disposed is False
    assert backend.closed == 3

    canonical = await RuntimeEventStore(service).list(first.session_id)
    assert [event.event_type for event in canonical].count("continuation.created") == 1
    assert [event.event_type for event in canonical].count("run.completed") == 2

    # Factory receives Bundle MCP config, while the native turn input receives
    # a real openai_codex SkillInput rather than prompt text pasted by PluginHost.
    overrides = tuple(getattr(backend.configs[0], "config_overrides", ()) or ())
    assert "mcp_servers.weather.url=https://mcp.invalid.example/rpc" in overrides
    prompt_items = backend.prompts[0][1]
    assert isinstance(prompt_items, list)
    bound_skill_inputs = [
        item
        for item in prompt_items
        if type(item).__name__ == "SkillInput"
        and getattr(item, "name", None) == "report-style"
    ]
    assert len(bound_skill_inputs) == 1
    bound_skill_path = Path(str(getattr(bound_skill_inputs[0], "path", "")))
    assert (
        bound_skill_path.name == "SKILL.md"
        and bound_skill_path.parent.name.endswith("-report-style")
        and bound_skill_path.is_file()
    )

    await host.dispose()
    assert provider.runtime.disposed is True


@pytest.mark.asyncio
async def test_codex_provider_rejects_unsupported_execution_strategy(
    tmp_path: Path,
) -> None:
    registry, profile, host, _provider, backend, _service = _setup(
        provider_config={}
    )
    bundle = _write_bundle(
        tmp_path / "bundle", registry, profile, execution_strategy="plan-act-observe"
    )
    await host.apply(profile)

    with pytest.raises(PluginHostError) as raised:
        await host.execute(bundle, {"user_id": "u1", "input": "must reject"})

    assert raised.value.code == "codex_external_execution_unsupported"
    assert backend.calls == []
    await host.dispose()


@pytest.mark.asyncio
async def test_codex_provider_rejects_undeclared_input_before_starting_app_server(
    tmp_path: Path,
) -> None:
    registry, profile, host, _provider, backend, _service = _setup()
    bundle = _write_bundle(tmp_path / "bundle", registry, profile)
    await host.apply(profile)

    with pytest.raises(PluginHostError) as raised:
        await host.execute(
            bundle,
            {
                "user_id": "u1",
                "input": "must reject",
                "web_search": True,
            },
        )

    assert raised.value.code == "codex_input_unsupported"
    assert backend.calls == []
    assert backend.configs == []
    await host.dispose()


@pytest.mark.asyncio
async def test_codex_provider_projects_native_plan_and_goal_without_prompt_commands(
    tmp_path: Path,
) -> None:
    registry, profile, host, _provider, backend, _service = _setup()
    bundle = _write_bundle(tmp_path / "bundle", registry, profile)
    await host.apply(profile)

    planned = await host.execute(
        bundle,
        {
            "user_id": "u1",
            "input": "plan this",
            "collaboration_mode": "plan",
            "invocation_id": "invocation-plan",
        },
    )
    goal = await host.execute(
        bundle,
        {
            "user_id": "u1",
            "input": "this text must not emulate a /goal command",
            "collaboration_mode": "plan",
            "goal_objective": "finish the provider closure",
            "invocation_id": "invocation-goal",
        },
    )

    assert planned.output_text == "answer from thread-1 turn 1"
    assert backend.turn_configs[0]["collaboration_mode"] == "plan"
    assert goal.output_text == "goal from thread-2: finish the provider closure"
    assert len(backend.goal_calls) == 1
    goal_thread, objective, goal_config = backend.goal_calls[0]
    assert goal_thread == "thread-2"
    assert objective == "finish the provider closure"
    assert goal_config["collaboration_mode"] == "plan"
    assert goal_config["sandbox_read_only"] is False
    assert goal_config["sandbox"] == "workspace-write"
    await host.dispose()


@pytest.mark.asyncio
async def test_codex_provider_only_accepts_models_locked_into_bundle(
    tmp_path: Path,
) -> None:
    registry, profile, host, _provider, backend, _service = _setup()
    bundle = _write_bundle(
        tmp_path / "bundle",
        registry,
        profile,
        models=["fixture-codex-model", "fixture-codex-model-next"],
    )
    await host.apply(profile)

    selected = await host.execute(
        bundle,
        {
            "user_id": "u1",
            "input": "use the selected model",
            "model": "fixture-codex-model-next",
        },
    )
    assert selected.inventory.model == "fixture-codex-model-next"
    assert backend.turn_configs[0]["model"] == "fixture-codex-model-next"

    with pytest.raises(PluginHostError) as raised:
        await host.execute(
            bundle,
            {
                "user_id": "u1",
                "input": "must fail before a second App Server starts",
                "model": "undeclared-model",
            },
        )
    assert raised.value.code == "codex_model_unsupported"
    assert len(backend.configs) == 1
    await host.dispose()


@pytest.mark.asyncio
async def test_codex_activation_exposes_and_disposes_kernel_runtime_adapter(
    tmp_path: Path,
) -> None:
    registry, profile, host, provider, backend, _service = _setup()
    bundle = _write_bundle(tmp_path / "bundle", registry, profile)
    await host.apply(profile)
    session = await host.open_activation(bundle, activation_key="session-kernel")
    activation = provider.runtime.last_activation if provider.runtime else None

    assert activation is not None
    adapter = activation.runtime_adapter()
    assert adapter.capabilities().goal.supported is True
    assert adapter.capabilities().plan.supported is True
    assert backend.closed == 0

    await session.close()
    assert activation.disposed is True
    assert backend.closed == 1
    await host.dispose()

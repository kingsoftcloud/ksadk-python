"""Real lifecycle and immutable-Bundle behavior for built-in capabilities."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ksadk.conversations.contracts import ConversationItem
from ksadk.plugins.builtins import (
    CORE_RENDERER_PLUGIN_ID,
    READ_ONLY_CONTEXT_PLUGIN_ID,
    SQLITE_SESSION_STORE_PLUGIN_ID,
    WORKSPACE_MCP_PLUGIN_ID,
    WORKSPACE_SKILL_PLUGIN_ID,
    CoreConversationRendererRuntime,
    ReadOnlyBundleContextRuntime,
    SQLiteSessionStoreFactory,
    WorkspaceMCPRuntime,
    WorkspaceSkillRuntime,
    builtin_capability_factories,
    builtin_capability_manifests,
)
from ksadk.plugins.bundle import PluginBundleResolver, ResolvedPluginBundle
from ksadk.plugins.contracts import CompositionProfile, PluginManifest
from ksadk.plugins.host import (
    PluginExecutionContext,
    PluginHost,
    PluginHostError,
)
from ksadk.plugins.providers.harness import HarnessTurnRequest
from ksadk.plugins.resolver import PluginRegistry
from ksadk.sessions.base import SessionEvent
from ksadk.studio.contracts import BundleManifest, FileEntry


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _directory_digest(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for relative, content in sorted(files.items()):
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def _provider_manifest() -> PluginManifest:
    return PluginManifest.model_validate(
        {
            "metadata": {"id": "io.ksadk.fixture-provider", "version": "1.0.0"},
            "spec": {
                "domain": "ksadk-platform",
                "runtime": "python",
                "entrypoint": "tests.plugins.test_builtin_capabilities:factory",
                "provides": [
                    {
                        "definition": "agent.provider/v1",
                        "slot": "agent.execution",
                        "mode": "unique",
                    }
                ],
                "isolation": "in-process",
                "compatibility": {"kernelApi": ">=1,<2"},
                "healthContract": "plugin.health/v1",
                "provenance": {
                    "source": "builtin",
                    "digest": "sha256:" + "f" * 64,
                },
            },
        }
    )


def _profile(*, context_path: str = "context/tenant.md") -> CompositionProfile:
    mcp_digest = _sha256(
        _json_bytes(
            {
                "name": "weather",
                "version": "1.0.0",
                "transport": "http",
                "endpointUrl": "http://mcp.example.test/rpc",
                "envRefs": {"Authorization": "env://MCP_API_KEY"},
                "enabled": True,
            }
        )
    )
    skill_files = {
        "SKILL.md": b"Use the weather observation and cite its source.\n",
        "skill.yaml": b"name: weather-style\ninstructionsFile: SKILL.md\n",
    }
    skill_digest = _directory_digest(skill_files)
    capabilities = [
        {
            "ref": f"plugin://{SQLITE_SESSION_STORE_PLUGIN_ID}@1.0.0",
            "config": {},
        },
        {
            "ref": f"plugin://{WORKSPACE_MCP_PLUGIN_ID}@1.0.0",
            "config": {
                "resources": [
                    {
                        "resourceId": "mcp:local:weather:1.0.0",
                        "kind": "mcp",
                        "name": "weather",
                        "version": "1.0.0",
                        "digest": mcp_digest,
                        "binding": {},
                        "materializer": {
                            "apiKeyRef": "env://MCP_API_KEY",
                            "toolFilter": ["lookup"],
                            "toolNamePrefix": "weather",
                        },
                    }
                ]
            },
        },
        {
            "ref": f"plugin://{WORKSPACE_SKILL_PLUGIN_ID}@1.0.0",
            "config": {
                "resources": [
                    {
                        "resourceId": "skill:local:weather-style:1.0.0",
                        "kind": "skill",
                        "name": "weather-style",
                        "version": "1.0.0",
                        "digest": skill_digest,
                        "binding": {},
                        "materializer": {},
                    }
                ]
            },
        },
        {
            "ref": f"plugin://{READ_ONLY_CONTEXT_PLUGIN_ID}@1.0.0",
            "config": {"paths": [context_path], "maxChars": 2048},
        },
        {
            "ref": f"plugin://{CORE_RENDERER_PLUGIN_ID}@1.0.0",
            "config": {},
        },
    ]
    return CompositionProfile.model_validate(
        {
            "agentProvider": {
                "ref": "plugin://io.ksadk.fixture-provider@1.0.0"
            },
            "capabilities": capabilities,
            "uiContributions": [
                f"plugin://{CORE_RENDERER_PLUGIN_ID}@1.0.0"
            ],
        }
    )


def _write_bundle(
    root: Path,
    registry: PluginRegistry,
    profile: CompositionProfile,
) -> ResolvedPluginBundle:
    root.mkdir()
    skill_files = {
        "SKILL.md": b"Use the weather observation and cite its source.\n",
        "skill.yaml": b"name: weather-style\ninstructionsFile: SKILL.md\n",
    }
    skill_digest = _directory_digest(skill_files)
    mcp = {
        "name": "weather",
        "version": "1.0.0",
        "transport": "http",
        "endpointUrl": "http://mcp.example.test/rpc",
        "envRefs": {"Authorization": "env://MCP_API_KEY"},
        "enabled": True,
    }
    mcp["digest"] = _sha256(_json_bytes(mcp))
    composition = registry.resolve(profile)
    payloads = {
        "composition-profile.json": profile.model_dump(
            by_alias=True, exclude_none=True, mode="json"
        ),
        "plugin-lock.json": composition.plugin_lock.model_dump(
            by_alias=True, exclude_none=True, mode="json"
        ),
        "resolved-agent-spec.json": {
            "agentId": "builtin-agent",
            "instructions": {"system": "Use immutable capabilities."},
            "model": {"model": "fixture-model"},
            "capabilities": {
                "mcpServers": [mcp],
                "skills": [
                    {
                        "name": "weather-style",
                        "version": "1.0.0",
                        "digest": skill_digest,
                        "bundlePath": "capabilities/skills/weather-style",
                        "instructions": skill_files["SKILL.md"].decode("utf-8"),
                    }
                ],
            },
        },
    }
    raw_files: dict[str, bytes] = {
        name: _json_bytes(payload) for name, payload in payloads.items()
    }
    raw_files.update(
        {
            f"capabilities/skills/weather-style/{name}": content
            for name, content in skill_files.items()
        }
    )
    raw_files["context/tenant.md"] = b"Tenant city: Beijing.\n"
    files: list[FileEntry] = []
    for relative, content in raw_files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        files.append(FileEntry(path=relative, sha256=_sha256(content), size=len(content)))
    manifest = BundleManifest(
        bundle_format="agentkit.bundle/v2",
        agent_id="builtin-agent",
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
class _Prepared:
    bundle: ResolvedPluginBundle
    capabilities: PluginExecutionContext
    ready: bool = False

    async def start(self) -> None:
        self.ready = True

    async def health(self) -> bool:
        return self.ready

    async def drain(self) -> None:
        self.ready = False

    async def dispose(self) -> None:
        self.ready = False

    async def execute(self, request: Any) -> dict[str, Any]:
        del request
        store = self.capabilities.require("session.event-store/v1").runtime
        mcp = self.capabilities.require("mcp.connector/v1").runtime
        skill = self.capabilities.require("skill.source/v1").runtime
        context = self.capabilities.require("context.contributor/v1").runtime
        renderer = self.capabilities.require("session.item.renderer/v1").runtime
        assert isinstance(mcp, WorkspaceMCPRuntime)
        assert isinstance(skill, WorkspaceSkillRuntime)
        assert isinstance(context, ReadOnlyBundleContextRuntime)
        assert isinstance(renderer, CoreConversationRendererRuntime)
        assert hasattr(store, "session_service_for")
        service = store.session_service_for(self.bundle)
        session = await service.create_session("builtin-agent", "user-1", "session-1")
        await service.append_event(
            session.id,
            SessionEvent(
                id="event-1",
                session_id=session.id,
                author="user",
                event_type="user_message",
                content={"text": "hello"},
            ),
        )
        specs = mcp.harness_mcp_specs(self.bundle)
        contribution = skill.harness_skill(self.bundle)
        context_text = await context.harness_context(
            self.bundle,
            HarnessTurnRequest(
                user_id="user-1",
                session_id=session.id,
                messages=({"role": "user", "content": "hello"},),
            ),
        )
        rendered = renderer.render(
            ConversationItem(
                item_id="item-1",
                source_event_ids=("event-1",),
                session_id=session.id,
                run_id="run-1",
                kind="assistant_text",
                operation="completed",
                lifecycle="completed",
                payload_schema_ref="conversation.item.assistant_text/v1",
                payload={"text": "hello"},
            )
        )
        return {
            "sessionCount": await service.count_sessions("builtin-agent", "user-1"),
            "mcp": mcp.inventory(self.bundle),
            "mcpSecretResolved": specs[0].api_key == "resolved-mcp-secret",
            "skill": contribution.name,
            "skillInstructions": contribution.instructions,
            "context": context_text,
            "rendered": rendered,
        }


class _Provider:
    async def start(self) -> None:
        return None

    async def health(self) -> bool:
        return True

    async def drain(self) -> None:
        return None

    async def dispose(self) -> None:
        return None

    async def prepare(
        self,
        bundle: ResolvedPluginBundle,
        *,
        capabilities: PluginExecutionContext,
    ) -> _Prepared:
        return _Prepared(bundle=bundle, capabilities=capabilities)


class _ProviderFactory:
    async def stage(self, manifest, *, profile, services):  # noqa: ANN001, ARG002
        return _Provider()


def _registry() -> PluginRegistry:
    return PluginRegistry([_provider_manifest(), *builtin_capability_manifests()])


def test_builtin_catalog_is_pinned_and_covers_the_phase_two_capabilities() -> None:
    manifests = builtin_capability_manifests()

    assert {item.metadata.id for item in manifests} == {
        SQLITE_SESSION_STORE_PLUGIN_ID,
        WORKSPACE_MCP_PLUGIN_ID,
        WORKSPACE_SKILL_PLUGIN_ID,
        READ_ONLY_CONTEXT_PLUGIN_ID,
        CORE_RENDERER_PLUGIN_ID,
    }
    assert {
        offer.definition
        for manifest in manifests
        for offer in manifest.spec.provides
    } == {
        "session.event-store/v1",
        "mcp.connector/v1",
        "skill.source/v1",
        "context.contributor/v1",
        "session.item.renderer/v1",
    }
    assert all(item.spec.provenance.source == "builtin" for item in manifests)
    assert all(item.metadata.version == "1.0.0" for item in manifests)


@pytest.mark.asyncio
async def test_plugin_host_executes_real_builtin_capabilities_from_bundle_only(
    tmp_path: Path,
) -> None:
    registry = _registry()
    profile = _profile()
    bundle = _write_bundle(tmp_path / "bundle", registry, profile)
    poison_catalog = object()
    factories = builtin_capability_factories(
        state_root=tmp_path / "state",
        secret_resolver=lambda ref: (
            "resolved-mcp-secret" if ref == "env://MCP_API_KEY" else None
        ),
    )
    store_factory = factories[SQLITE_SESSION_STORE_PLUGIN_ID]
    assert isinstance(store_factory, SQLiteSessionStoreFactory)
    host = PluginHost(
        registry,
        {"io.ksadk.fixture-provider": _ProviderFactory(), **factories},
        allowed_permissions=frozenset(
            {"filesystem:session-store", "filesystem:bundle-read", "network:mcp"}
        ),
        services={"studio_catalog": poison_catalog},
    )

    await host.apply(profile)
    result = await host.execute(bundle, {"text": "hello"})

    assert result["sessionCount"] == 1
    assert result["mcp"] == (
        {
            "name": "weather",
            "version": "1.0.0",
            "transport": "http",
            "endpointUrl": "http://mcp.example.test/rpc",
            "toolFilter": ("lookup",),
        },
    )
    assert result["mcpSecretResolved"] is True
    assert "resolved-mcp-secret" not in json.dumps(result, ensure_ascii=False)
    assert result["skill"] == "weather-style"
    assert "cite its source" in result["skillInstructions"]
    assert result["context"] == "Tenant city: Beijing."
    assert result["rendered"] == {
        "component": "markdown",
        "itemId": "item-1",
        "lifecycle": "completed",
        "text": "hello",
    }
    assert store_factory.runtime is not None
    assert store_factory.runtime.db_path.is_file()
    assert await store_factory.runtime.health() is True

    await host.dispose()
    assert await store_factory.runtime.health() is False


def test_core_renderer_uses_safe_fallback_for_unknown_item() -> None:
    renderer = CoreConversationRendererRuntime(CORE_RENDERER_PLUGIN_ID, "1.0.0")
    item = ConversationItem(
        item_id="unknown-1",
        source_event_ids=("event-1",),
        session_id="session-1",
        run_id="run-1",
        kind="unknown",
        operation="completed",
        lifecycle="completed",
        payload_schema_ref="vendor.unknown/v9",
        payload={"html": "<script>danger()</script>", "answer": 42},
    )

    assert renderer.render(item) == {
        "component": "unknown",
        "itemId": "unknown-1",
        "lifecycle": "completed",
        "schemaRef": "vendor.unknown/v9",
        "summary": '{"answer":42,"html":"<script>danger()</script>"}',
    }


@pytest.mark.asyncio
async def test_context_rejects_bundle_path_escape(tmp_path: Path) -> None:
    registry = _registry()
    profile = _profile(context_path="../outside.txt")
    bundle = _write_bundle(tmp_path / "bundle", registry, profile)
    runtime = ReadOnlyBundleContextRuntime(READ_ONLY_CONTEXT_PLUGIN_ID, "1.0.0")
    await runtime.start()

    with pytest.raises(PluginHostError) as captured:
        await runtime.harness_context(
            bundle,
            HarnessTurnRequest(
                user_id="user-1",
                session_id=None,
                messages=({"role": "user", "content": "hello"},),
            ),
        )

    assert captured.value.code == "builtin_context_path_invalid"


@pytest.mark.asyncio
async def test_bundle_profile_mutation_is_detected_before_capability_use(
    tmp_path: Path,
) -> None:
    registry = _registry()
    profile = _profile()
    bundle = _write_bundle(tmp_path / "bundle", registry, profile)
    runtime = WorkspaceMCPRuntime(
        WORKSPACE_MCP_PLUGIN_ID,
        "1.0.0",
        secret_resolver=lambda _ref: "resolved-mcp-secret",
    )
    await runtime.start()
    capability = next(
        item
        for item in bundle.composition.profile.capabilities
        if WORKSPACE_MCP_PLUGIN_ID in item.ref
    )
    capability.config["resources"][0]["name"] = "tampered"

    with pytest.raises(PluginHostError) as captured:
        runtime.inventory(bundle)

    assert captured.value.code == "plugin_bundle_profile_mutated"

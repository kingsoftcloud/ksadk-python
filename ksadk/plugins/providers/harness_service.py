"""Expose PluginHost-owned MCP and Skills to the independent engine."""

from __future__ import annotations

from ksadk._mcp_compat import tool_input_schema

import hashlib
import json
import re
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any

from ksadk.harness.native.hook_executor import HookAuthorizer
from ksadk.harness.service_runtime import HarnessServiceRuntimeAdapter, ServiceHostTool
from ksadk.plugins.host import PluginExecutionContext, PluginHostError
from ksadk.plugins.providers.harness_contributions import McpToolSpecTransport, ProviderSkillSource
from ksadk.plugins.providers.harness_service_context import service_context_sources
from ksadk.plugins.providers.harness_service_hooks import service_hooks
from ksadk.plugins.providers.harness_service_memory import (
    service_local_memory,
    service_memory_verifier,
)
from ksadk.plugins.providers.platform_resources import (
    PLATFORM_RESOURCE_MCP_NAME,
    PLATFORM_RESOURCE_MCP_PLUGIN_ID,
    PLATFORM_RESOURCE_MCP_PLUGIN_VERSION,
)
from ksadk.runtime import StartRequest
from ksadk.studio.contracts import ExecutionSpec, MemorySpec

if TYPE_CHECKING:
    from ksadk.plugins.providers.harness import HarnessEngineActivationContext


def admit_service_memory(
    context: HarnessEngineActivationContext, capabilities: PluginExecutionContext,
) -> None:
    """Platform memory uses its locked connector and existing worker write gate."""
    policy = MemorySpec.model_validate(context.bundle.resolved_agent_spec.get("memory") or {})
    if not policy.enabled or not policy.scopes or policy.provider_ref == "local-default":
        return
    connectors = capabilities.all("mcp.connector/v1", slot="mcp.platform-resources")
    if (
        policy.provider_ref.startswith("binding://")
        and len(context.memory_sources) == 1 and len(connectors) == 1
    ):
        memory = context.memory_sources[0]
        connector = connectors[0]
        if (
            memory.slot == "memory.primary"
            and memory.plugin_id == connector.plugin_id == PLATFORM_RESOURCE_MCP_PLUGIN_ID
            and memory.plugin_version == connector.plugin_version
            and memory.plugin_version == PLATFORM_RESOURCE_MCP_PLUGIN_VERSION
            and memory.runtime is connector.runtime
            and any(spec.name == PLATFORM_RESOURCE_MCP_NAME for spec in context.config.mcp_tools)
        ):
            return
    raise PluginHostError(
        "harness_service_memory_unsupported",
        "Independent Harness requires the locked platform memory connector for this activation",
    )


def bind_service_resources(
    adapter: HarnessServiceRuntimeAdapter,
    context: HarnessEngineActivationContext,
    cleanup: AsyncExitStack,
    *,
    hook_authorizer: HookAuthorizer | None = None,
    hook_authorization_version: str | None = None,
) -> None:
    credentials = {item.name: item for item in context.mcp_runtime_credentials}
    transports = []
    for spec in context.config.mcp_tools:
        transport = McpToolSpecTransport(spec, credential=credentials.get(spec.name))
        cleanup.push_async_callback(transport.close)
        transports.append((spec.name, transport))
    skills = ProviderSkillSource(context.skills)
    memory = service_local_memory(context)

    async def check_activation() -> None:
        if context.activation_available is not None and not context.activation_available():
            raise RuntimeError("Harness activation is no longer available")

    async def check_mcp() -> None:
        await check_activation()
        if context.mcp_available is not None and not await context.mcp_available():
            raise RuntimeError("Harness MCP binding is no longer available")

    async def list_skills(_: dict[str, Any]) -> Any:
        await check_activation()
        manifests = []
        for ref in skills.refs:
            manifest = skills.manifest(ref)
            manifests.append({"ref": ref, "name": manifest.name, "summary": manifest.summary})
        return manifests

    async def read_skill(arguments: dict[str, Any]) -> Any:
        await check_activation()
        ref = arguments["ref"]
        path = arguments.get("path")
        if not isinstance(ref, str) or (path is not None and not isinstance(path, str)):
            raise ValueError("invalid Skill resource request")
        if path is None:
            return skills.full_text(ref)
        return skills.resource(ref, path).decode("utf-8")

    async def load_tools() -> tuple[ServiceHostTool, ...]:
        await check_activation()
        result = memory.tools() if memory is not None else []
        if skills.refs:
            result.extend((
                ServiceHostTool(
                    "ksadk_skill_list", "List Skills bound to this Agent.",
                    {"type": "object", "properties": {}, "additionalProperties": False},
                    list_skills, read_only=True,
                ),
                ServiceHostTool(
                    "ksadk_skill_read", "Read a bound Skill or one of its UTF-8 text resources.",
                    {"type": "object", "properties": {
                        "ref": {"type": "string"}, "path": {"type": "string"},
                    }, "required": ["ref"], "additionalProperties": False},
                    read_skill, read_only=True,
                ),
            ))
        for server, transport in transports:
            await check_mcp()
            for definition in await transport.list_tools():
                original_name = definition["name"]
                canonical = json.dumps([server, definition], sort_keys=True, allow_nan=False)
                digest = hashlib.sha256(canonical.encode()).hexdigest()[:20]
                label = re.sub(r"[^A-Za-z0-9_]", "_", original_name)[:30]

                async def call(arguments, *, bound=transport, name=original_name):
                    await check_mcp()
                    return await bound.call_tool(name, arguments)

                result.append(ServiceHostTool(
                    f"ksadk_mcp_{label}_{digest}", definition["description"],
                    tool_input_schema(definition), call, deferred=True, source_name=original_name,
                ))
        return tuple(result)

    claims = dict(context.activation_claims)
    scope = {"agent_id": context.agent_name}
    scope.update({key: claims[key] for key in ("user_id", "session_id") if key in claims})
    if claims.get("agent_id", context.agent_name) != context.agent_name or (
        "session_id" in scope and scope["session_id"] != context.activation_key
    ):
        raise PluginHostError(
            "harness_service_activation_unbound", "Invalid Harness activation scope",
        )
    identity = json.dumps([
        context.agent_name, context.activation_key, context.profile_digest,
        context.plugin_lock_digest, context.bundle.bundle_digest, claims,
    ], sort_keys=True)

    async def turn_context(request: StartRequest, rows: list[dict[str, Any]]) -> list[str]:
        from ksadk.plugins.providers.harness import HarnessTurnRequest

        await check_activation()
        turn = HarnessTurnRequest(
            user_id=request.user_id, session_id=request.session_id, messages=tuple(rows),
            model=request.model, request_metadata=request.metadata,
            invocation_id=request.metadata.get("invocation_id") or request.metadata.get("run_id"),
        )
        return [
            text.strip()
            for binding in context.context_sources
            if not binding.contributor
            and (text := await binding.runtime.harness_context(context.bundle, turn)).strip()
        ]

    hooks, stop_limit = service_hooks(
        context, cleanup, authorize=hook_authorizer,
        authorization_version=hook_authorization_version,
    )
    execution = ExecutionSpec.model_validate(
        context.bundle.resolved_agent_spec.get("execution") or {},
    )
    limits = execution.native_call_limits()
    adapter.bind_read_only_workspace(
        binding=hashlib.sha256(identity.encode()).hexdigest(),
        model=context.config.model, load_tools=load_tools, context=turn_context,
        scope=scope,
        context_sources=(*service_context_sources(context),
                         *(memory.context_sources() if memory else ())),
        hooks=(*hooks, *(memory.hooks() if memory else ())), stop_hook_limit=stop_limit,
        tool_effects=memory.writes if memory is not None else False,
        complete=memory.complete if memory and memory.flush_after else None,
        verifier=service_memory_verifier(context, memory),
        execution_policy={
            "strategy": execution.strategy,
            "maxWallMs": execution.timeout_seconds * 1000 or None,
            "maxModelCalls": limits.max_model_calls,
            "maxToolCalls": limits.max_tool_calls,
        },
    )

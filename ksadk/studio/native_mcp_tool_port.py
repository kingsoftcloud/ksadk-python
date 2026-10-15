"""Project activation-owned DSH MCP bindings into the Native ToolPort.

The PluginHost owns transport credentials and disposal. This projection pins
the discovered model schema for one activation, checks Run scope and policy on
every invocation, and passes Native's stable operation identity to transports
that explicitly support idempotency. A transport exception is uncertain: the
remote side may have executed the call before the connection failed.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ksadk.harness.capabilities import LoadPolicy
from ksadk.harness.mcp_runtime import McpCapabilityRuntime, McpToolCallContext
from ksadk.harness.model_adapters.native_mcp_media import model_mcp_observation
from ksadk.harness.model_adapters.native_mcp_result import model_mcp_content
from ksadk.harness.native.adapters import StrictToolExecutor, ToolAuthorizer, ToolBinding
from ksadk.harness.native.ports import ToolDefinition, ToolPort, ToolRequest, ToolResult
from ksadk.harness.native.state import RunScope
from ksadk.harness.stores.native_contract import RunStore

_MODEL_TOOL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}\Z")


@dataclass(frozen=True)
class NativeMcpToolSurface:
    definitions: tuple[ToolDefinition, ...]
    executor: ToolPort


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


async def build_native_mcp_tool_surface(
    *,
    runtime: McpCapabilityRuntime,
    binding_refs: Sequence[str],
    scope: RunScope,
    store: RunStore,
    authorize: ToolAuthorizer,
    activation_available: Callable[[], Awaitable[bool]] | None = None,
) -> NativeMcpToolSurface:
    """Discover only the activation's locked MCP bindings and fail closed.

    ON_DEMAND bindings expose only the fixed discovery contract until used. The caller
    owns ``runtime`` and its transports; its activation disposal must close
    them after the Native adapter stops.
    """
    if not isinstance(store, RunStore) or not callable(authorize):
        raise ValueError("native_mcp_host_authorization_unavailable")
    if len(set(binding_refs)) != len(binding_refs):
        raise ValueError("native_mcp_binding_duplicate")

    def run_admitted(request: ToolRequest) -> bool:
        # Native effect identities are generated from the owning Run. A
        # foreign operation key must never become this Run's remote MCP
        # idempotency key, even if the host policy authorizer is permissive.
        if not request.operation_key.startswith(f"{request.run_id}:effect:"):
            return False
        try:
            snapshot = store.load(scope, request.run_id)
        except Exception:
            return False
        return snapshot.run_id == request.run_id and snapshot.scope == scope

    async def available() -> bool:
        try:
            return activation_available is None or await activation_available() is True
        except Exception:
            return False

    disclosure = None

    async def checked_authorize(request: ToolRequest):
        if not run_admitted(request) or not await available():
            return "deny"
        projected = (
            await disclosure.authorization_request(request) if disclosure is not None else request
        )
        target_decision = await authorize(projected)
        if projected is request or target_decision == "deny":
            return target_decision
        # Neither target policy nor an explicit denial of the wrapper can be
        # bypassed. Studio binds both views to the same concrete approval ID.
        admission_decision = await authorize(request)
        for decision in (target_decision, admission_decision):
            if decision == "deny":
                return "deny"
            if decision != "allow" and not (
                isinstance(decision, ToolResult) and decision.status == "approval_required"
            ):
                return decision
        return target_decision if isinstance(target_decision, ToolResult) else admission_decision

    async def invoke_remote(request, *, server_ref, tool_name, arguments):
        if not run_admitted(request):
            return ToolResult(status="error", content={"code": "run_session_unavailable"})
        # Policy may have suspended for approval. Check resource
        # ownership again before any transport call can take effect.
        if not await available():
            return ToolResult(status="error", content={"code": "activation_unavailable"})
        try:
            value = await runtime.call(
                server_ref,
                tool_name,
                arguments,
                context=McpToolCallContext.create(
                    invocation_id=request.operation_key,
                    call_id=request.call.call_id,
                ),
            )
        except Exception:
            return ToolResult(status="uncertain", content={"code": "mcp_result_uncertain"})
        result = _json_value(value)
        failed = isinstance(result, dict) and (
            result.get("isError") is True or result.get("ok") is False
        )
        try:
            model_view = model_mcp_content(result)
            # An oversized textual projection is an observation limit,
            # not a transport failure. Preserve the known raw result so
            # Native can archive it under the owning Run's budget.
            return ToolResult(
                status="error" if failed else "ok",
                content=result,
                model_observation=(
                    model_mcp_observation(result, failed=failed)
                    if not isinstance(model_view, str)
                    else None
                ),
                model_content=(
                    model_view
                    if (
                        not failed
                        and isinstance(model_view, str)
                        and model_view
                        and len(model_view.encode("utf-8")) <= 65536
                    )
                    else None
                ),
            )
        except Exception:
            return ToolResult(status="uncertain", content={"code": "mcp_result_invalid"})

    bindings: list[ToolBinding] = []
    names: set[str] = set()
    deferred = []
    for ref in binding_refs:
        binding = runtime.binding(ref)
        if binding.descriptor.load_policy == LoadPolicy.EXPLICIT:
            continue
        if binding.descriptor.load_policy == LoadPolicy.ON_DEMAND:
            deferred.append(ref)
            continue
        listed = await runtime.tools(ref)
        # McpToolSpecTransport delegates to the DSH toolset, which has
        # already applied ``prefix_name`` before exposing list_tools/call_tool.
        allowed = {
            f"{binding.tool_prefix}_{name}" if binding.tool_prefix else name
            for name in binding.static_tool_names
        }
        found: set[str] = set()
        for item in listed:
            raw_name = item["name"]
            if not isinstance(raw_name, str):
                raise ValueError("native_mcp_tool_name_invalid")
            if allowed and raw_name not in allowed:
                continue
            found.add(raw_name)
            name = raw_name
            if not _MODEL_TOOL_NAME.fullmatch(name) or name in names:
                raise ValueError("native_mcp_tool_name_invalid_or_duplicate")
            names.add(name)
            definition = ToolDefinition(
                name=name,
                description=str(item.get("description") or ""),
                parameters=item["inputSchema"],
                result_model_policy="typed-tool-observation/v1",
                result_disclosure_policy="mcp-default/v1",
            )

            async def invoke(request, *, server_ref=ref, tool_name=raw_name):
                return await invoke_remote(
                    request,
                    server_ref=server_ref,
                    tool_name=tool_name,
                    arguments=request.call.arguments,
                )

            bindings.append(ToolBinding(definition, invoke))
        if allowed and found != allowed:
            raise ValueError("native_mcp_locked_tool_missing")

    if deferred:
        from ksadk.studio.native_mcp_disclosure import NativeMcpDisclosure

        disclosure = NativeMcpDisclosure(
            runtime=runtime,
            refs=deferred,
            scope=scope,
            store=store,
            available=available,
            invoke=invoke_remote,
        )
        if names.intersection(tool.name for tool in disclosure.definitions):
            raise ValueError("native_mcp_tool_name_invalid_or_duplicate")
        bindings.extend(disclosure.bindings())

    return NativeMcpToolSurface(
        definitions=tuple(item.definition for item in bindings),
        executor=StrictToolExecutor(scope=scope, tools=bindings, authorize=checked_authorize),
    )

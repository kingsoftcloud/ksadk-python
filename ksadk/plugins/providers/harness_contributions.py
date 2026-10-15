"""DSH-locked Skill and MCP contributions shared by Harness engine adapters.

PluginHost owns their activation and disposal. This module has no dependency
on LangGraph or the Native Kernel and does not select a runtime engine.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from ksadk.harness.capabilities import (
    CapabilityDescriptor,
    CapabilityKind,
    LoadPolicy,
    RiskLevel,
)
from ksadk.harness.config import McpToolSpec
from ksadk.harness.mcp_runtime import McpCapabilityRuntime, McpServerBinding
from ksadk.harness.skill_runtime import SkillManifest
from ksadk.harness.spec import CapabilityBinding
from ksadk.harness.tools import HarnessTool, load_mcp_tools
from ksadk.plugins.providers.mcp_projection import MCPRuntimeCredential, _finish_owned_task
from ksadk.skills.loader import _parse_frontmatter


def resource_id(value: str, *, fallback: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return normalized or fallback


class ProviderSkillSource:
    """Expose only locked DSH Skill contributions through L0–L3 reads."""

    def __init__(self, contributions: Sequence[Any]) -> None:
        self._skills: dict[str, Any] = {}
        for contribution in contributions:
            name = str(getattr(contribution, "name", "")).strip()
            instructions = str(getattr(contribution, "instructions", "")).strip()
            if not name or not instructions:
                continue
            ref = f"skill://{resource_id(name, fallback='provider-skill')}@1.0.0"
            if ref in self._skills:
                raise ValueError(f"duplicate DSH Skill contribution: {ref}")
            self._skills[ref] = contribution

    @property
    def refs(self) -> tuple[str, ...]:
        return tuple(self._skills)

    def manifest(self, skill_id: str) -> SkillManifest:
        contribution = self._require(skill_id)
        instructions = str(contribution.instructions).strip()
        metadata, body = _parse_frontmatter(instructions)
        summary = str(metadata.get("description") or "").strip()
        if not summary:
            summary = next(
                (
                    line.strip().lstrip("#").strip()
                    for line in body.splitlines()
                    if line.strip() and line.strip() != "---"
                ),
                str(contribution.name),
            )
        return SkillManifest(name=str(contribution.name), summary=summary[:512])

    def full_text(self, skill_id: str) -> str:
        return str(self._require(skill_id).instructions)

    def resource(self, skill_id: str, resource_ref: str) -> bytes:
        contribution = self._require(skill_id)
        root = getattr(contribution, "resource_root", None)
        if root is None:
            raise FileNotFoundError(
                f"DSH inline Skill {skill_id} has no packaged resource {resource_ref!r}"
            )
        root = Path(root).resolve()
        candidate = (root / resource_ref).resolve()
        if Path(resource_ref).is_absolute() or not candidate.is_relative_to(root):
            raise ValueError("Skill resource path escapes locked Bundle directory")
        if not candidate.is_file():
            raise FileNotFoundError(resource_ref)
        return candidate.read_bytes()

    def _require(self, skill_id: str) -> Any:
        try:
            return self._skills[skill_id]
        except KeyError:
            raise KeyError(f"DSH Skill {skill_id!r} is not bound") from None


class McpToolSpecTransport:
    """Adapt one locked DSH MCP contribution to the Harness MCP transport."""

    def __init__(
        self,
        spec: McpToolSpec,
        *,
        credential: MCPRuntimeCredential | None = None,
    ) -> None:
        self._spec = spec
        self._credential = credential
        self._active_token: str | None = None
        self._schema_digest: str | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._toolset: Any | None = None
        self._tools: dict[str, HarnessTool] = {}

    async def list_tools(self) -> list[dict[str, Any]]:
        async with self._lock:
            await self._ensure_loaded()
            return [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.parameters,
                }
                for tool in self._tools.values()
            ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        async with self._lock:
            await self._ensure_loaded()
            try:
                tool = self._tools[name]
            except KeyError:
                raise RuntimeError(
                    f"MCP tool {name!r} is not exposed by {self._spec.name!r}"
                ) from None
            # Auth renewal happens before execution, never by replaying a call.
            return await tool.call(arguments)

    async def close(self) -> None:
        self._closed = True
        async with self._lock:
            await self._close_current()

    async def _close_current(self) -> None:
        if self._toolset is not None:
            await _finish_owned_task(asyncio.create_task(self._toolset.close()))
        self._toolset = None
        self._tools.clear()

    async def _ensure_loaded(self) -> None:
        if self._closed:
            raise RuntimeError("MCP transport is closed")
        token = await self._credential.token() if self._credential else self._spec.api_key
        if self._credential is not None and (not isinstance(token, str) or not token.strip()):
            raise RuntimeError("MCP runtime credential is unavailable")
        if self._closed:
            raise RuntimeError("MCP transport is closed")
        if self._toolset is not None and token == self._active_token:
            return
        await self._close_current()
        selected = replace(self._spec, api_key=token) if self._credential else self._spec
        toolset, tools = await load_mcp_tools(selected)
        try:
            schema_digest = hashlib.sha256(
                json.dumps(
                    sorted((tool.name, tool.description, tool.parameters) for tool in tools),
                    sort_keys=True,
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode()
            ).hexdigest()
            if self._closed or (
                self._schema_digest is not None and schema_digest != self._schema_digest
            ):
                raise RuntimeError("MCP transport closed or locked tool schema changed")
        except BaseException:
            await _finish_owned_task(asyncio.create_task(toolset.close()))
            raise
        self._schema_digest = schema_digest
        self._active_token = token
        self._toolset = toolset
        self._tools = {tool.name: tool for tool in tools}


def mcp_runtime(
    specs: Sequence[McpToolSpec],
    *,
    runtime_credentials: Sequence[MCPRuntimeCredential] = (),
) -> tuple[
    McpCapabilityRuntime | None,
    tuple[McpToolSpecTransport, ...],
    tuple[CapabilityBinding, ...],
]:
    credentials = {item.name: item for item in runtime_credentials}
    if len(credentials) != len(runtime_credentials) or not set(credentials).issubset(
        {spec.name for spec in specs}
    ):
        raise ValueError("invalid MCP runtime credential binding")
    if not specs:
        return None, (), ()
    runtime = McpCapabilityRuntime()
    transports: list[McpToolSpecTransport] = []
    bindings: list[CapabilityBinding] = []
    seen: set[str] = set()
    for spec in specs:
        ref = f"mcp://{resource_id(spec.name, fallback='provider-mcp')}@1.0.0"
        if ref in seen:
            raise ValueError(f"duplicate DSH MCP contribution: {ref}")
        seen.add(ref)
        transport = McpToolSpecTransport(spec, credential=credentials.get(spec.name))
        runtime.bind(
            McpServerBinding(
                descriptor=CapabilityDescriptor(
                    id=ref,
                    kind=CapabilityKind.MCP,
                    name=spec.name,
                    description=(spec.description or "").strip()
                    or f"DSH-provided MCP server {spec.name}",
                    version="1.0.0",
                    risk_level=RiskLevel.MEDIUM,
                    load_policy=LoadPolicy.ON_DEMAND,
                ),
                transport=transport,
                required=True,
                tool_prefix=spec.tool_name_prefix or "",
                static_tool_names=tuple(spec.tool_filter),
            )
        )
        transports.append(transport)
        bindings.append(
            CapabilityBinding(
                capability_ref=ref,
                required=True,
                load_policy="on_demand",
            )
        )
    return runtime, tuple(transports), tuple(bindings)


__all__ = ["ProviderSkillSource", "McpToolSpecTransport", "mcp_runtime", "resource_id"]

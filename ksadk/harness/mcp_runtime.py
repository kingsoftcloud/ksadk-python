"""Harness MCP Capability Runtime（plan §10.2/§10.3）。

生命周期：binding 解析 → 健康探测（带 TTL 缓存）→ tools/list 缓存 →
Schema 校验（无效 Tool 隔离）→ 调用 → 降级决策。

- 健康缓存：探测结果带 TTL，窗口内不重复探测；
- 熔断：连续失败达到阈值进入 open，冷却期后半开试探一次；
- 降级（§10.3）：可选 MCP 不可用 → Draft 告警降级 / Revision 按 Policy；
  必需 MCP 不可用 → Revision 阻止（policy_required）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

from ksadk.harness.capabilities import CapabilityDescriptor


class McpRuntimeError(RuntimeError):
    """MCP 生命周期错误（含必需能力不可用）。"""


class McpTransport(Protocol):
    """MCP 服务器传输的最小协议（宿主注入，SDK 不绑定具体客户端）。"""

    async def list_tools(self) -> list[dict[str, Any]]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


@dataclass
class _HealthState:
    healthy: bool | None = None
    last_probe_at: float = 0.0
    consecutive_failures: int = 0
    opened_at: float | None = None


@dataclass
class McpServerBinding:
    """一条 Revision binding 的运行时形态。"""

    descriptor: CapabilityDescriptor
    transport: McpTransport
    #: 必需能力：Revision 运行不可用时阻止（§10.3）。
    required: bool = False
    #: 工具前缀（避免跨服务器重名）。
    tool_prefix: str = ""


@dataclass
class McpHealthReport:
    """一次健康评估的结果（缓存命中也产出报告）。"""

    server_id: str
    healthy: bool
    degraded: bool = False
    reason: str = ""
    circuit_open: bool = False
    cached: bool = False


@dataclass
class McpRuntimeOptions:
    health_ttl_seconds: float = 30.0
    failure_threshold: int = 3
    cooldown_seconds: float = 60.0


class McpCapabilityRuntime:
    """多 MCP 服务器的健康缓存 + 熔断 + 降级决策。"""

    def __init__(self, *, options: McpRuntimeOptions | None = None) -> None:
        self._options = options or McpRuntimeOptions()
        self._servers: dict[str, McpServerBinding] = {}
        self._health: dict[str, _HealthState] = {}
        self._tools_cache: dict[str, list[dict[str, Any]]] = {}

    # ------------------------------------------------------------- 注册

    def bind(self, binding: McpServerBinding) -> None:
        self._servers[binding.descriptor.id] = binding
        self._health.setdefault(binding.descriptor.id, _HealthState())

    def binding(self, server_id: str) -> McpServerBinding:
        try:
            return self._servers[server_id]
        except KeyError:
            raise McpRuntimeError(f"unknown mcp binding: {server_id}") from None

    # ------------------------------------------------------------- 健康

    async def health(
        self, server_id: str, *, now: float | None = None
    ) -> McpHealthReport:
        """探测（或复用 TTL 缓存）并应用熔断状态机。"""
        binding = self.binding(server_id)
        state = self._health[server_id]
        clock = time.monotonic() if now is None else now

        if state.opened_at is not None:
            if clock - state.opened_at < self._options.cooldown_seconds:
                return McpHealthReport(
                    server_id=server_id,
                    healthy=False,
                    degraded=not binding.required,
                    reason="circuit_open",
                    circuit_open=True,
                )
            # 半开：冷却期到，试探一次。
            state.opened_at = None
            state.consecutive_failures = max(
                0, self._options.failure_threshold - 1
            )

        if (
            state.healthy is True
            and clock - state.last_probe_at < self._options.health_ttl_seconds
        ):
            return McpHealthReport(
                server_id=server_id,
                healthy=state.healthy,
                degraded=not state.healthy and not binding.required,
                reason="cached",
                cached=True,
            )

        try:
            await binding.transport.list_tools()
        except Exception as exc:  # noqa: BLE001 - 任何传输失败都计入熔断
            state.healthy = False
            state.last_probe_at = clock
            state.consecutive_failures += 1
            if state.consecutive_failures >= self._options.failure_threshold:
                state.opened_at = clock
            return McpHealthReport(
                server_id=server_id,
                healthy=False,
                degraded=not binding.required,
                reason=f"probe_failed: {exc}",
            )

        state.healthy = True
        state.last_probe_at = clock
        state.consecutive_failures = 0
        return McpHealthReport(server_id=server_id, healthy=True)

    # ------------------------------------------------------------- 工具

    async def tools(self, server_id: str) -> list[dict[str, Any]]:
        """列工具（缓存）并做 Schema 校验：无效 Tool 隔离，不拖垮整站（§10.3）。"""
        binding = self.binding(server_id)
        cached = self._tools_cache.get(server_id)
        if cached is not None:
            return cached
        raw = await binding.transport.list_tools()
        valid: list[dict[str, Any]] = []
        for tool in raw:
            schema = tool.get("inputSchema")
            if not tool.get("name") or not isinstance(schema, dict):
                continue  # 单 Tool Schema 无效 → 隔离该 Tool
            valid.append(tool)
        self._tools_cache[server_id] = valid
        return valid

    def invalidate_tools(self, server_id: str) -> None:
        self._tools_cache.pop(server_id, None)

    # ------------------------------------------------------------- 调用

    async def call(
        self, server_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> Any:
        """调用（熔断打开时抛结构化错误）。"""
        binding = self.binding(server_id)
        state = self._health[server_id]
        if state.opened_at is not None:
            raise McpRuntimeError(f"mcp {server_id} circuit open")
        try:
            result = await binding.transport.call_tool(tool_name, arguments)
        except Exception as exc:  # noqa: BLE001
            state.consecutive_failures += 1
            if state.consecutive_failures >= self._options.failure_threshold:
                state.opened_at = time.monotonic()
            raise McpRuntimeError(f"mcp call failed: {exc}") from exc
        state.consecutive_failures = 0
        return result

    # ------------------------------------------------------------- 降级

    def degradation_decision(
        self, report: McpHealthReport, *, environment: str
    ) -> str:
        """§10.3 降级策略：draft/revision 两列决策表。"""
        if report.healthy:
            return "available"
        binding = self.binding(report.server_id)
        if environment == "revision":
            if binding.required:
                return "block"  # 必需 MCP 不可用 → 阻止激活/运行失败
            if binding.descriptor.load_policy.value == "explicit":
                return "fail"
            return "degrade"
        # Draft 调试：可选/必需都允许无工具调试并明确提示。
        return "warn_and_continue"


__all__ = [
    "McpCapabilityRuntime",
    "McpHealthReport",
    "McpRuntimeError",
    "McpRuntimeOptions",
    "McpServerBinding",
]

"""Harness MCP Capability Runtime（plan §10.2/§10.3）。

生命周期：binding 解析 → 健康探测（带 TTL 缓存）→ tools/list 缓存 →
Schema 校验（无效 Tool 隔离）→ 调用 → 降级决策。

- 健康缓存：探测结果带 TTL，窗口内不重复探测；
- 熔断：连续失败达到阈值进入 open，冷却期后半开试探一次；
- 降级（§10.3）：可选 MCP 不可用 → Draft 告警降级 / Revision 按 Policy；
  必需 MCP 不可用 → Revision 阻止（policy_required）。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ksadk.harness.capabilities import CapabilityDescriptor


class McpRuntimeError(RuntimeError):
    """MCP 生命周期错误（含必需能力不可用）。"""


class McpAuthenticationError(RuntimeError):
    """Transport-authentication failure known to occur before tool execution."""


class McpCredentialRefresher(Protocol):
    """Rotate credentials out-of-band without returning secret material.

    The implementation updates the injected Transport/credential provider and
    returns whether a new credential generation became active.  Secret values must
    never be returned to the Harness or persisted in a Bundle/Event.
    """

    async def refresh(self, *, server_id: str, transport: McpTransport) -> bool: ...


class McpTransport(Protocol):
    """MCP 服务器传输的最小协议（宿主注入，SDK 不绑定具体客户端）。"""

    async def list_tools(self) -> list[dict[str, Any]]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


@dataclass(frozen=True)
class McpToolCallContext:
    """传给显式支持幂等的 MCP Transport 的低信任调用元数据。"""

    invocation_id: str
    call_id: str
    idempotency_key: str

    @classmethod
    def create(cls, *, invocation_id: str, call_id: str) -> McpToolCallContext:
        digest = hashlib.sha256(f"{invocation_id}\0{call_id}".encode()).hexdigest()
        return cls(
            invocation_id=invocation_id,
            call_id=call_id,
            idempotency_key=f"ksadk-{digest}",
        )


class IdempotentMcpTransport(McpTransport, Protocol):
    """可把稳定幂等键传到远端 MCP/Gateway 的扩展 Transport。"""

    async def call_tool_with_context(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        context: McpToolCallContext,
    ) -> Any: ...


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
    #: ``transport`` 表示 Transport 能将幂等键透传到真正执行副作用的服务。
    #: 默认 ``none`` 保持旧 MCP Transport 行为，不虚假承诺 exactly-once。
    idempotency_mode: Literal["none", "transport"] = "none"
    #: Stable Secret reference for audit/readiness only; never the secret value.
    credential_ref: str = ""
    credential_refresher: McpCredentialRefresher | None = None


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
    discovery_timeout_seconds: float = 10.0
    call_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        for name in (
            "health_ttl_seconds",
            "cooldown_seconds",
            "discovery_timeout_seconds",
            "call_timeout_seconds",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")


class McpCapabilityRuntime:
    """多 MCP 服务器的健康缓存 + 熔断 + 降级决策。"""

    def __init__(self, *, options: McpRuntimeOptions | None = None) -> None:
        self._options = options or McpRuntimeOptions()
        self._servers: dict[str, McpServerBinding] = {}
        self._health: dict[str, _HealthState] = {}
        self._tools_cache: dict[str, list[dict[str, Any]]] = {}
        self._credential_generation: dict[str, int] = {}

    # ------------------------------------------------------------- 注册

    def bind(self, binding: McpServerBinding) -> None:
        if binding.idempotency_mode == "transport" and not callable(
            getattr(binding.transport, "call_tool_with_context", None)
        ):
            raise McpRuntimeError(
                f"mcp {binding.descriptor.id} 声明 transport 幂等，"
                "但 Transport 未实现 call_tool_with_context"
            )
        self._servers[binding.descriptor.id] = binding
        self._health.setdefault(binding.descriptor.id, _HealthState())
        self._credential_generation.setdefault(binding.descriptor.id, 0)

    def binding(self, server_id: str) -> McpServerBinding:
        try:
            return self._servers[server_id]
        except KeyError:
            raise McpRuntimeError(f"unknown mcp binding: {server_id}") from None

    def availability(self, server_id: str) -> Literal["unknown", "available", "degraded"]:
        """返回 Harness 当前观察到的 Server 可用性。

        该投影只暴露稳定的平台语义，不把失败计数、时间戳等熔断器私有状态
        泄漏给 Studio。调用方可用它识别 ``available ↔ degraded`` 转换并产出
        RuntimeEvent；初次绑定但尚未发生任何 I/O 时为 ``unknown``。
        """
        self.binding(server_id)
        healthy = self._health[server_id].healthy
        if healthy is None:
            return "unknown"
        return "available" if healthy else "degraded"

    def credential_generation(self, server_id: str) -> int:
        """Opaque rotation generation; contains no credential material."""
        self.binding(server_id)
        return self._credential_generation[server_id]

    # ------------------------------------------------------------- 健康

    async def health(self, server_id: str, *, now: float | None = None) -> McpHealthReport:
        """探测（或复用 TTL 缓存）并应用熔断状态机。"""
        binding = self.binding(server_id)
        state = self._health[server_id]
        clock = time.monotonic() if now is None else now

        if not self._allow_operation(state, now=clock):
            return McpHealthReport(
                server_id=server_id,
                healthy=False,
                degraded=not binding.required,
                reason="circuit_open",
                circuit_open=True,
            )

        if state.healthy is True and clock - state.last_probe_at < self._options.health_ttl_seconds:
            return McpHealthReport(
                server_id=server_id,
                healthy=state.healthy,
                degraded=not state.healthy and not binding.required,
                reason="cached",
                cached=True,
            )

        try:
            await self._list_tools_with_rotation(server_id, binding)
        except asyncio.CancelledError:
            # 调用方取消不是远端故障，不污染健康状态或熔断计数。
            raise
        except TimeoutError:
            self._record_failure(state, now=clock)
            return McpHealthReport(
                server_id=server_id,
                healthy=False,
                degraded=not binding.required,
                reason="probe_timeout",
                circuit_open=state.opened_at is not None,
            )
        except Exception as exc:  # noqa: BLE001 - 任何传输失败都计入熔断
            self._record_failure(state, now=clock)
            return McpHealthReport(
                server_id=server_id,
                healthy=False,
                degraded=not binding.required,
                reason=_safe_transport_error(exc, operation="probe"),
                circuit_open=state.opened_at is not None,
            )

        self._record_success(state, now=clock)
        return McpHealthReport(server_id=server_id, healthy=True)

    # ------------------------------------------------------------- 工具

    async def tools(self, server_id: str) -> list[dict[str, Any]]:
        """列工具（缓存）并做 Schema 校验：无效 Tool 隔离，不拖垮整站（§10.3）。"""
        binding = self.binding(server_id)
        cached = self._tools_cache.get(server_id)
        if cached is not None:
            return cached
        state = self._health[server_id]
        clock = time.monotonic()
        if not self._allow_operation(state, now=clock):
            raise McpRuntimeError(f"mcp {server_id} circuit open")
        try:
            raw = await self._list_tools_with_rotation(server_id, binding)
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            self._record_failure(state, now=clock)
            raise McpRuntimeError(f"mcp {server_id} tools/list timeout") from exc
        except Exception as exc:  # noqa: BLE001 - Transport 失败统一计入熔断
            self._record_failure(state, now=clock)
            reason = _safe_transport_error(exc, operation="tools/list")
            raise McpRuntimeError(f"mcp {server_id} tools/list failed: {reason}") from exc
        self._record_success(state, now=clock)
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
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        context: McpToolCallContext | None = None,
    ) -> Any:
        """调用（熔断打开时抛结构化错误）。"""
        binding = self.binding(server_id)
        state = self._health[server_id]
        clock = time.monotonic()
        if not self._allow_operation(state, now=clock):
            raise McpRuntimeError(f"mcp {server_id} circuit open")
        try:
            result = await self._call_with_rotation(
                server_id,
                binding,
                tool_name,
                arguments,
                context=context,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            self._record_failure(state, now=clock)
            raise McpRuntimeError(f"mcp {server_id} tool {tool_name} call timeout") from exc
        except Exception as exc:  # noqa: BLE001
            self._record_failure(state, now=clock)
            reason = _safe_transport_error(exc, operation="tool/call")
            raise McpRuntimeError(
                f"mcp call failed: {server_id} tool {tool_name}: {reason}"
            ) from exc
        self._record_success(state, now=clock)
        return result

    async def _list_tools_with_rotation(
        self, server_id: str, binding: McpServerBinding
    ) -> list[dict[str, Any]]:
        try:
            return await self._with_timeout(
                binding.transport.list_tools(),
                timeout_seconds=self._options.discovery_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not _is_authentication_failure(exc) or not await self._refresh_credentials(
                server_id, binding
            ):
                raise
        return await self._with_timeout(
            binding.transport.list_tools(),
            timeout_seconds=self._options.discovery_timeout_seconds,
        )

    async def _call_with_rotation(
        self,
        server_id: str,
        binding: McpServerBinding,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        context: McpToolCallContext | None,
    ) -> Any:
        async def execute() -> Any:
            if binding.idempotency_mode == "transport":
                if context is None:
                    raise McpRuntimeError(
                        f"mcp {server_id} 的 transport 幂等调用缺少稳定调用上下文"
                    )
                operation = getattr(binding.transport, "call_tool_with_context")(
                    tool_name, arguments, context=context
                )
            else:
                operation = binding.transport.call_tool(tool_name, arguments)
            return await self._with_timeout(
                operation,
                timeout_seconds=self._options.call_timeout_seconds,
            )

        try:
            return await execute()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Only an explicit/pre-execution authentication failure is safe to retry.
            # Timeouts and generic transport failures keep their existing at-most-once
            # behavior because remote side effects may already have happened.
            if not _is_authentication_failure(exc) or not await self._refresh_credentials(
                server_id, binding
            ):
                raise
        return await execute()

    async def _refresh_credentials(
        self, server_id: str, binding: McpServerBinding
    ) -> bool:
        refresher = binding.credential_refresher
        if refresher is None:
            return False
        refreshed = bool(
            await refresher.refresh(server_id=server_id, transport=binding.transport)
        )
        if refreshed:
            self._credential_generation[server_id] += 1
            self.invalidate_tools(server_id)
        return refreshed

    # ------------------------------------------------------------- 故障状态

    def _allow_operation(self, state: _HealthState, *, now: float) -> bool:
        """应用 open/cooldown/half-open 门禁。

        不要求调用方额外执行 ``health()``：tools/list 和 Tool Call 自身在冷却
        到期后也能进入半开试探，避免渐进披露链路永久卡在 open 状态。
        """
        if state.opened_at is None:
            return True
        if now - state.opened_at < self._options.cooldown_seconds:
            return False
        state.opened_at = None
        state.consecutive_failures = max(0, self._options.failure_threshold - 1)
        return True

    def _record_failure(self, state: _HealthState, *, now: float) -> None:
        state.healthy = False
        state.last_probe_at = now
        state.consecutive_failures += 1
        if state.consecutive_failures >= self._options.failure_threshold:
            state.opened_at = now

    @staticmethod
    def _record_success(state: _HealthState, *, now: float) -> None:
        state.healthy = True
        state.last_probe_at = now
        state.consecutive_failures = 0
        state.opened_at = None

    @staticmethod
    async def _with_timeout(operation: Any, *, timeout_seconds: float) -> Any:
        # 0 明确表示不启用 Harness 侧超时，由调用方/Transport 自己控制。
        if timeout_seconds == 0:
            return await operation
        return await asyncio.wait_for(operation, timeout=timeout_seconds)

    # ------------------------------------------------------------- 降级

    def degradation_decision(self, report: McpHealthReport, *, environment: str) -> str:
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
    "McpAuthenticationError",
    "McpCapabilityRuntime",
    "McpCredentialRefresher",
    "McpHealthReport",
    "McpToolCallContext",
    "IdempotentMcpTransport",
    "McpRuntimeError",
    "McpRuntimeOptions",
    "McpServerBinding",
]


def _is_authentication_failure(exc: Exception) -> bool:
    """Classify only failures that indicate rejection before remote execution."""
    if isinstance(exc, McpAuthenticationError):
        return True
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    return status in {401, 403}


def _safe_transport_error(exc: Exception, *, operation: str) -> str:
    """Project a stable error category without echoing transport/secret text."""
    if _is_authentication_failure(exc):
        return f"{operation} authentication_failed"
    if isinstance(exc, (ConnectionError, OSError)):
        return f"{operation} transport_unavailable"
    return f"{operation} transport_failed"

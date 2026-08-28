"""MCP Transport 故障、超时、取消与半开恢复合同。"""

from __future__ import annotations

import asyncio

import pytest

from ksadk.harness.capabilities import CapabilityDescriptor, CapabilityKind
from ksadk.harness.mcp_runtime import (
    McpCapabilityRuntime,
    McpRuntimeError,
    McpRuntimeOptions,
    McpServerBinding,
)

_TOOLS = [
    {
        "name": "lookup",
        "description": "lookup a value",
        "inputSchema": {"type": "object", "properties": {}},
    }
]


class _ControllableTransport:
    def __init__(self) -> None:
        self.list_mode = "success"
        self.call_mode = "success"
        self.list_calls = 0
        self.call_calls = 0
        self.cancelled = asyncio.Event()

    async def list_tools(self):  # type: ignore[no-untyped-def]
        self.list_calls += 1
        return await self._complete(self.list_mode, _TOOLS)

    async def call_tool(self, name, arguments):  # type: ignore[no-untyped-def]
        self.call_calls += 1
        return await self._complete(
            self.call_mode,
            {"name": name, "arguments": arguments},
        )

    async def _complete(self, mode, result):  # type: ignore[no-untyped-def]
        if mode == "success":
            return result
        if mode == "error":
            raise ConnectionError("transport unavailable")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("unreachable")


def _runtime(
    transport: _ControllableTransport,
    *,
    failure_threshold: int = 1,
    cooldown_seconds: float = 60,
    discovery_timeout_seconds: float = 0.01,
    call_timeout_seconds: float = 0.01,
) -> McpCapabilityRuntime:
    runtime = McpCapabilityRuntime(
        options=McpRuntimeOptions(
            health_ttl_seconds=0,
            failure_threshold=failure_threshold,
            cooldown_seconds=cooldown_seconds,
            discovery_timeout_seconds=discovery_timeout_seconds,
            call_timeout_seconds=call_timeout_seconds,
        )
    )
    runtime.bind(
        McpServerBinding(
            descriptor=CapabilityDescriptor(
                id="mcp://finance@1.0.0",
                name="finance",
                kind=CapabilityKind.MCP,
                version="1.0.0",
            ),
            transport=transport,
        )
    )
    return runtime


@pytest.mark.asyncio
async def test_health_probe_timeout_is_visible_and_opens_circuit():
    transport = _ControllableTransport()
    transport.list_mode = "hang"
    runtime = _runtime(transport)

    report = await runtime.health("mcp://finance@1.0.0")

    assert report.healthy is False
    assert report.reason == "probe_timeout"
    assert report.circuit_open is True
    assert transport.cancelled.is_set(), "Harness 超时必须取消悬挂的 Transport 请求"


@pytest.mark.asyncio
async def test_tools_list_timeout_opens_circuit_without_hanging_loop():
    transport = _ControllableTransport()
    transport.list_mode = "hang"
    runtime = _runtime(transport)

    with pytest.raises(McpRuntimeError, match=r"tools/list timeout"):
        await runtime.tools("mcp://finance@1.0.0")
    with pytest.raises(McpRuntimeError, match=r"circuit open"):
        await runtime.tools("mcp://finance@1.0.0")

    assert transport.list_calls == 1


@pytest.mark.asyncio
async def test_tool_call_timeout_is_visible_and_opens_circuit():
    transport = _ControllableTransport()
    transport.call_mode = "hang"
    runtime = _runtime(transport)

    with pytest.raises(McpRuntimeError, match=r"tool lookup call timeout"):
        await runtime.call("mcp://finance@1.0.0", "lookup", {"q": "x"})
    with pytest.raises(McpRuntimeError, match=r"circuit open"):
        await runtime.call("mcp://finance@1.0.0", "lookup", {"q": "x"})

    assert transport.call_calls == 1
    assert transport.cancelled.is_set()


@pytest.mark.asyncio
async def test_caller_cancellation_does_not_poison_circuit():
    transport = _ControllableTransport()
    transport.call_mode = "hang"
    runtime = _runtime(transport, call_timeout_seconds=0)

    task = asyncio.create_task(runtime.call("mcp://finance@1.0.0", "lookup", {"q": "x"}))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    transport.call_mode = "success"
    result = await runtime.call("mcp://finance@1.0.0", "lookup", {"q": "ok"})
    assert result["arguments"] == {"q": "ok"}


@pytest.mark.asyncio
async def test_tools_list_can_half_open_and_recover_without_explicit_health_probe():
    transport = _ControllableTransport()
    transport.list_mode = "error"
    runtime = _runtime(transport, cooldown_seconds=0)

    with pytest.raises(McpRuntimeError, match=r"tools/list failed"):
        await runtime.tools("mcp://finance@1.0.0")

    transport.list_mode = "success"
    tools = await runtime.tools("mcp://finance@1.0.0")

    assert [tool["name"] for tool in tools] == ["lookup"]
    assert transport.list_calls == 2


@pytest.mark.asyncio
async def test_tool_call_can_half_open_and_recover_without_explicit_health_probe():
    transport = _ControllableTransport()
    transport.call_mode = "error"
    runtime = _runtime(transport, cooldown_seconds=0)

    with pytest.raises(McpRuntimeError, match=r"mcp call failed"):
        await runtime.call("mcp://finance@1.0.0", "lookup", {"q": "x"})

    transport.call_mode = "success"
    result = await runtime.call("mcp://finance@1.0.0", "lookup", {"q": "ok"})

    assert result["arguments"] == {"q": "ok"}
    assert transport.call_calls == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("failure_threshold", 0),
        ("discovery_timeout_seconds", -1),
        ("call_timeout_seconds", -1),
    ],
)
def test_invalid_runtime_options_are_rejected(field: str, value: float):
    kwargs = {field: value}
    with pytest.raises(ValueError, match=field):
        McpRuntimeOptions(**kwargs)

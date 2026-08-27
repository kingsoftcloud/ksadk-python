"""ExecutionEngine 接口（plan §6.3）——引擎无关协议，隔离领域语义。

一期实现 ManagedLangGraphEngine；接口存在不代表向用户提供多 Engine 选择。
本模块不得 import LangGraph（守卫见 tests/architecture/test_harness_contract.py）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol, runtime_checkable

from ksadk.harness.events import RuntimeEvent
from ksadk.harness.spec import HarnessSpec
from ksadk.harness.state import HarnessState
from ksadk.runtime import CancelResult, ResumePayload, ResumeTarget, RunHandle, StartRequest


@dataclass(frozen=True)
class ExecutionPlan:
    """Strategy 产物：图拓扑描述（纯数据，引擎无关，plan §6.3.1）。"""

    strategy_kind: str
    nodes: tuple[str, ...]
    edges: tuple[tuple[str, str], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CompiledHarness:
    """Engine.compile 产物：把 ExecutionPlan 绑定到具体执行机制后的可执行单元。"""

    spec: HarnessSpec
    plan: ExecutionPlan
    engine_kind: str


@dataclass(frozen=True)
class EngineCapability:
    """诚实的能力声明（plan §15.1：不支持的能力必须声明，不伪造一致性）。"""

    supported: bool
    reason: str | None = None


@dataclass(frozen=True)
class EngineCapabilityMatrix:
    cancel: EngineCapability
    resume: EngineCapability
    checkpoint: EngineCapability
    interrupt: EngineCapability
    durable_across_process: EngineCapability
    streaming: EngineCapability


class ExecutionEngineError(RuntimeError):
    """引擎不支持的能力被显式调用时抛出（诚实声明原则）。"""


@runtime_checkable
class ExecutionEngine(Protocol):
    """统一执行引擎协议（plan §6.3）。"""

    async def compile(self, spec: HarnessSpec) -> CompiledHarness: ...

    async def start(self, request: StartRequest, compiled: CompiledHarness) -> RunHandle: ...

    def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]: ...

    async def cancel(self, handle: RunHandle) -> CancelResult: ...

    async def resume(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: ResumePayload | None,
    ) -> RunHandle: ...

    async def snapshot_state(self, handle: RunHandle) -> HarnessState | None:
        """读取当前影子状态（HarnessState，非引擎图 State）。"""
        ...

    def capabilities(self) -> EngineCapabilityMatrix: ...

    async def close(self, handle: RunHandle) -> None: ...


def single_agent_plan() -> ExecutionPlan:
    """单 Agent 默认拓扑（plan §7.1 最小子集：reason 自环 + final 出口）。"""
    return ExecutionPlan(
        strategy_kind="single-agent",
        nodes=("prepare_context", "reason", "tool_calls", "final"),
        edges=(
            ("prepare_context", "reason"),
            ("reason", "tool_calls"),
            ("tool_calls", "reason"),
            ("reason", "final"),
        ),
    )


def default_capability_matrix() -> EngineCapabilityMatrix:
    """基线矩阵：Phase 0 引擎未实现前，除 stream/start 外全部诚实标为不支持。"""
    unsupported = lambda reason: EngineCapability(supported=False, reason=reason)  # noqa: E731
    return EngineCapabilityMatrix(
        cancel=unsupported("Phase 0: engine implementation lands in Phase 1"),
        resume=unsupported("Phase 0: engine implementation lands in Phase 1"),
        checkpoint=unsupported("Phase 0: engine implementation lands in Phase 1"),
        interrupt=unsupported("Phase 0: engine implementation lands in Phase 1"),
        durable_across_process=unsupported("Phase 0: engine implementation lands in Phase 1"),
        streaming=EngineCapability(supported=True),
    )


__all__ = [
    "CompiledHarness",
    "EngineCapability",
    "EngineCapabilityMatrix",
    "ExecutionEngine",
    "ExecutionEngineError",
    "ExecutionPlan",
    "default_capability_matrix",
    "single_agent_plan",
]

"""Harness ExecutionEngine 子包（plan §6.3/§16）。

``base`` 与 ``thread_ids`` 引擎无关；``langgraph`` 引擎实现在 Phase 1 落地。
"""

from ksadk.harness.engine.base import (
    CompiledHarness,
    EngineCapability,
    EngineCapabilityMatrix,
    ExecutionEngine,
    ExecutionEngineError,
    ExecutionPlan,
    default_capability_matrix,
    single_agent_plan,
)
from ksadk.harness.engine.thread_ids import (
    HarnessThreadId,
    ThreadIdError,
    decode_thread_id,
    encode_thread_id,
    matches_tenant,
    validate_tenant,
)

__all__ = [
    "CompiledHarness",
    "EngineCapability",
    "EngineCapabilityMatrix",
    "ExecutionEngine",
    "ExecutionEngineError",
    "ExecutionPlan",
    "HarnessThreadId",
    "ThreadIdError",
    "decode_thread_id",
    "default_capability_matrix",
    "encode_thread_id",
    "matches_tenant",
    "single_agent_plan",
    "validate_tenant",
]

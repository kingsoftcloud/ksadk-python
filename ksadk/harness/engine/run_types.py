"""Internal checkpoint and live-state records for the Managed engine."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, TypedDict

from ksadk.harness.engine.base import CompiledHarness
from ksadk.harness.events import RuntimeEvent
from ksadk.harness.run_control import RunController
from ksadk.harness.state import HarnessState
from ksadk.runtime import RunHandle, StartRequest


class _GraphState(TypedDict, total=False):
    """图 State——只存最小路由信息（plan §6.2.1），正文活在 HarnessState。"""

    messages: list[dict[str, Any]]  # OpenAI 形态消息（含 tool_calls）
    pending_tool_calls: list[dict[str, Any]]
    turn_count: int
    route: str  # "reason" | "final"
    # MCP 披露游标（P0.1）：随图状态进 Checkpoint，跨进程审批恢复不丢。
    mcp_listed: list[tuple[str, str]]
    mcp_schema_read: list[tuple[str, str, str]]
    usage_tokens: int
    finalization_retries: int
    run_control: dict[str, Any]
    budget: dict[str, Any]
    tool_batch_succeeded: list[str]
    tool_batch_failed: bool
    execution_terminal: str
    child_approval: dict[str, Any]
    child_approval_decision: dict[str, Any]


@dataclass
class _EngineRun:
    handle: RunHandle
    request: StartRequest
    compiled: CompiledHarness
    state: HarnessState
    thread_id: str
    task: asyncio.Task[list[RuntimeEvent]] | None = None
    events: list[RuntimeEvent] = field(default_factory=list)
    seq: int = 0
    cancel_requested: bool = False
    pause_requested: bool = False
    done: bool = False
    started_emitted: bool = False
    pending_approval_call_id: str | None = None
    pending_approval: dict[str, Any] = field(default_factory=dict)
    child_approval_decision: dict[str, Any] | None = None
    #: 最近一次 ContextManifest（Actual Token 由 usage 回填，长任务方案 §6.2）。
    context_manifest: Any | None = None
    #: 本 Run 的 CompactionRecord 列表（长任务方案 §6.4）。
    compaction_records: list[Any] = field(default_factory=list)
    #: Revision 绑定且可由默认 Loop 按需披露的 Level 0 Skill 目录。
    skill_catalog: tuple[dict[str, str], ...] = ()
    #: Revision 绑定的 Level 0 MCP Server 目录（名称/描述/风险等级）。
    mcp_catalog: tuple[dict[str, str], ...] = ()
    #: 本 Revision 的子 Agent 工具；Run 级冻结，避免多 Spec 并发串配置。
    sub_agents: dict[str, Any] = field(default_factory=dict)
    tools: dict[str, Any] = field(default_factory=dict)
    approval_required: set[str] = field(default_factory=set)
    execution_policy: Any = None
    execution_policy_resolver: Any = None
    execution_policy_request: StartRequest | None = None
    budget_usage: dict[str, int] = field(default_factory=dict)
    child_budget_usage: dict[str, dict[str, int]] = field(default_factory=dict)
    budget_tool_calls: set[str] = field(default_factory=set)
    budget_parent: Any = None
    execution_elapsed_seconds: float = 0.0
    artifact_refs: list[str] = field(default_factory=list)
    tool_calls_started: int = 0
    artifacts_created: int = 0
    #: 可选长任务控制器；合同来自不可变 Revision execution config。
    controller: RunController | None = None
    #: 验收只读取这份追加式证据，不依赖会被 stream 消费的输出队列。
    control_events: list[RuntimeEvent] = field(default_factory=list)

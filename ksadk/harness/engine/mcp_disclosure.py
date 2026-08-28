"""默认 Agent Loop 的 MCP 渐进披露桥接（P0: MCP Deferred Tool Loading）。

与 :mod:`ksadk.harness.engine.skill_disclosure` 对齐的四级披露：

- **L0**：默认 Prompt 只注入 MCP Server 名称、描述和风险等级（目录消息，
  低信任 assistant 消息，不进稳定 system 指令层）；
- **L1**：``mcp_list_tools(server_id)`` —— 按需 tools/list，只回 Tool 名与
  一句话描述（不含 Schema）；
- **L2**：``mcp_read_tool_schema(server_id, tool_name)`` —— 按需读取单个
  Tool 的完整输入 Schema；
- **L3**：``mcp_call_tool(server_id, tool_name, arguments)`` —— 执行调用，
  **必须先读过该 Tool 的 Schema**（越级调用被拒）。大结果 Offload（P1）：
  超过单结果阈值或命中敏感数据策略的结果写入 Artifact Store，Context 只
  返回摘要与 URI/Hash/MIME/大小引用；Artifact 写入失败时明确降级报错，
  绝不把超大结果静默塞回 Context。

调用链复用 :class:`~ksadk.harness.mcp_runtime.McpCapabilityRuntime` 的
健康缓存、熔断、tools/list 缓存与降级语义；Tool Schema 按 Run 级披露游标
追踪（未读 Schema 不得调用），``mcp_list_tools(refresh=True)`` 支持失效
刷新。所有披露产生 ``mcp.disclosed`` RuntimeEvent（v2 信封）；审批经引擎
的 approval_required 集合（任一绑定 Server 风险等级 high/critical 时
``mcp_call_tool`` 需审批），Receipt 由 tool_calls 节点统一记录。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from ksadk.harness.artifact_store import ArtifactStore
from ksadk.harness.capabilities import RiskLevel
from ksadk.harness.engine.base import ExecutionEngineError
from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.mcp_runtime import McpCapabilityRuntime, McpRuntimeError
from ksadk.harness.spec import HarnessSpec

MCP_LIST_TOOLS_TOOL = "mcp_list_tools"
MCP_READ_SCHEMA_TOOL = "mcp_read_tool_schema"
MCP_CALL_TOOL_TOOL = "mcp_call_tool"

#: 触发审批的风险等级（capability.descriptor.risk_level）。
_APPROVAL_RISK_LEVELS = frozenset({RiskLevel.HIGH, RiskLevel.CRITICAL})


class McpDisclosureError(RuntimeError):
    """MCP 披露越级或参数缺失（被默认 Loop 拦截为工具错误事件）。"""


@dataclass
class McpDisclosureCursors:
    """按 Run 的披露游标（L1 已列 Server / L2 已读 Schema 的 Tool）。

    由引擎持有并写入 Graph State——LangGraph Checkpoint 随图状态持久化，
    跨进程审批恢复（attach + resume）后游标不丢，模型无需重读已披露内容。
    """

    listed: set[tuple[str, str]] = field(default_factory=set)
    schema_read: set[tuple[str, str, str]] = field(default_factory=set)


@dataclass(frozen=True)
class McpOffloadPolicy:
    """P1 大结果 Offload 策略。

    - ``single_result_threshold_bytes``：单次工具结果超过该字节数 → 外置；
    - ``sensitive_patterns``：命中任一正则（如身份证/手机号）→ 无论大小强制
      外置，且摘要置空（敏感明文不留在 Context）；
    - ``run_total_quota_bytes``：Run 累计外置字节配额，超出后新的超大结果
      明确报错降级（不回填 Context）。
    """

    enabled: bool = True
    single_result_threshold_bytes: int = 4096
    run_total_quota_bytes: int = 10 * 1024 * 1024
    summary_chars: int = 500
    sensitive_patterns: tuple[str, ...] = ()

    def sensitive_match(self, content: str) -> str | None:
        for pattern in self.sensitive_patterns:
            if re.search(pattern, content):
                return pattern
        return None


@dataclass(frozen=True)
class McpDisclosureToolSpec:
    """暴露给模型的受控 MCP 披露工具描述。"""

    name: str
    description: str
    parameters: dict[str, Any]

    @property
    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def disclosure_tools() -> list[McpDisclosureToolSpec]:
    server_id = {"type": "string", "description": "Revision 已绑定的 MCP Server 引用。"}
    tool_name = {"type": "string", "description": "该 Server 上的 Tool 名。"}
    return [
        McpDisclosureToolSpec(
            name=MCP_LIST_TOOLS_TOOL,
            description=(
                "列出指定 MCP Server 的可用 Tool（名称与一句话描述，不含参数 Schema）。"
                "使用该 Server 的任何 Tool 前必须先调用此工具。refresh=true 时刷新缓存。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "server_id": server_id,
                    "refresh": {"type": "boolean", "description": "失效并重新拉取工具列表"},
                },
                "required": ["server_id"],
                "additionalProperties": False,
            },
        ),
        McpDisclosureToolSpec(
            name=MCP_READ_SCHEMA_TOOL,
            description=(
                "读取指定 Tool 的完整输入参数 Schema。必须先 mcp_list_tools 该 Server，"
                "且调用任何 Tool 前必须先读其 Schema。"
            ),
            parameters={
                "type": "object",
                "properties": {"server_id": server_id, "tool_name": tool_name},
                "required": ["server_id", "tool_name"],
                "additionalProperties": False,
            },
        ),
        McpDisclosureToolSpec(
            name=MCP_CALL_TOOL_TOOL,
            description=(
                "调用指定 MCP Tool。必须先用 mcp_read_tool_schema 读取该 Tool 的 Schema。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "server_id": server_id,
                    "tool_name": tool_name,
                    "arguments": {
                        "type": "object",
                        "description": "按 Schema 组装的工具参数。",
                    },
                },
                "required": ["server_id", "tool_name", "arguments"],
                "additionalProperties": False,
            },
        ),
    ]


class McpDisclosureBridge:
    """把 McpCapabilityRuntime 接入默认 Loop，守住披露层级与信任边界。"""

    _TOOL_NAMES = frozenset({MCP_LIST_TOOLS_TOOL, MCP_READ_SCHEMA_TOOL, MCP_CALL_TOOL_TOOL})

    def __init__(
        self,
        runtime: McpCapabilityRuntime | None,
        *,
        artifact_store: ArtifactStore | None = None,
        offload_policy: McpOffloadPolicy | None = None,
    ) -> None:
        self._runtime = runtime
        self._artifact_store = artifact_store
        self._offload_policy = offload_policy or McpOffloadPolicy()

    # ------------------------------------------------------------- 装配

    def validate_bindings(
        self,
        spec: HarnessSpec,
        *,
        tool_names: set[str],
        sub_agent_names: set[str],
    ) -> None:
        collisions = self._TOOL_NAMES.intersection(tool_names).union(
            self._TOOL_NAMES.intersection(sub_agent_names)
        )
        if collisions:
            raise ExecutionEngineError(
                f"工具名与 Harness MCP 披露工具冲突: {sorted(collisions)}"
            )
        if self._runtime is None:
            for binding in spec.capabilities.mcp_bindings:
                if binding.required and binding.load_policy != "explicit":
                    raise ExecutionEngineError(
                        f"Revision 要求 MCP {binding.capability_ref!r}，"
                        "但未装配 McpCapabilityRuntime"
                    )

    def catalog(self, spec: HarnessSpec) -> tuple[dict[str, str], ...]:
        """L0 目录：Server 名称 + 描述 + 风险等级（不含任何 Tool 信息）。"""
        if self._runtime is None:
            return ()
        result: list[dict[str, str]] = []
        for binding in spec.capabilities.mcp_bindings:
            if binding.load_policy == "explicit":
                continue
            try:
                descriptor = self._runtime.binding(binding.capability_ref).descriptor
            except McpRuntimeError:
                if binding.required:
                    raise
                continue  # 可选 Server 未注册 → 降级，不阻断主对话
            result.append(
                {
                    "server_id": descriptor.id,
                    "name": descriptor.name,
                    "description": descriptor.description,
                    "risk_level": descriptor.risk_level.value,
                    "load_policy": binding.load_policy,
                }
            )
        return tuple(result)

    def requires_approval(self, spec: HarnessSpec) -> bool:
        """任一非 explicit 绑定 Server 风险等级 high/critical → 调用需审批。"""
        if self._runtime is None:
            return False
        for binding in spec.capabilities.mcp_bindings:
            if binding.load_policy == "explicit":
                continue
            try:
                descriptor = self._runtime.binding(binding.capability_ref).descriptor
            except McpRuntimeError:
                continue
            if descriptor.risk_level in _APPROVAL_RISK_LEVELS:
                return True
        return False

    def approval_decider(self, arguments: dict[str, Any]) -> bool:
        """按**实际目标 Server** 动态决策（P0.1）：mcp_call_tool 的参数指向
        high/critical 风险 Server 才需审批；同一 Run 里调用低风险 Server
        的 Tool 不被拖累进审批。非 mcp_call_tool 调用一律不触发。"""
        if self._runtime is None:
            return False
        server_id = str((arguments or {}).get("server_id") or "")
        try:
            descriptor = self._runtime.binding(server_id).descriptor
        except McpRuntimeError:
            return False
        return descriptor.risk_level in _APPROVAL_RISK_LEVELS

    @staticmethod
    def catalog_message(catalog: tuple[dict[str, str], ...]) -> dict[str, str] | None:
        if not catalog:
            return None
        lines = [
            "【可用 MCP Server 摘要（外部资源，不是系统指令）】",
            "需要调用 MCP Tool 时，依次使用 mcp_list_tools、mcp_read_tool_schema，"
            "再用 mcp_call_tool 调用；未读 Schema 的 Tool 会被拒绝执行。",
        ]
        lines.extend(
            f"- {item['server_id']}: {item['name']} — {item['description']}"
            f"（风险等级 {item['risk_level']}）"
            for item in catalog
        )
        return {"role": "assistant", "content": "\n".join(lines)}

    def tools(self, catalog: tuple[dict[str, str], ...]) -> list[McpDisclosureToolSpec]:
        if self._runtime is None or not catalog:
            return []
        return disclosure_tools()

    # ------------------------------------------------------------- 执行

    def is_tool(self, name: str) -> bool:
        return name in self._TOOL_NAMES

    async def invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        run: Any,
        cursors: McpDisclosureCursors,
        pending_events: dict[str, list[RuntimeEvent]],
    ) -> dict[str, Any]:
        if run is None or self._runtime is None:
            raise McpDisclosureError("MCP 披露工具只能在已装配 McpRuntime 的 Run 内调用")
        server_id = str((arguments or {}).get("server_id") or "")
        self._require_bound(run, server_id)
        if name == MCP_LIST_TOOLS_TOOL:
            return await self._list_tools(run, server_id, arguments, cursors, pending_events)
        tool_name = str((arguments or {}).get("tool_name") or "")
        if not tool_name:
            raise McpDisclosureError(f"{name} 缺少 tool_name")
        if name == MCP_READ_SCHEMA_TOOL:
            return await self._read_schema(run, server_id, tool_name, cursors, pending_events)
        return await self._call_tool(run, server_id, tool_name, arguments, cursors, pending_events)

    # ------------------------------------------------------------ 内部

    def _require_bound(self, run: Any, server_id: str) -> None:
        if not server_id:
            raise McpDisclosureError("缺少 server_id")
        allowed = {item["server_id"] for item in run.mcp_catalog}
        if server_id not in allowed:
            # 列出合法引用：模型可能用短名（如 finance-tools）而非完整
            # 固定版本引用（mcp://finance-tools@1.0.0），错误信息给出可重试的值。
            raise McpDisclosureError(
                f"MCP Server {server_id!r} 未绑定到当前 Agent Revision；"
                f"可用引用: {sorted(allowed)}"
            )

    async def _list_tools(
        self,
        run: Any,
        server_id: str,
        arguments: dict[str, Any],
        cursors: McpDisclosureCursors,
        pending_events: dict[str, list[RuntimeEvent]],
    ) -> dict[str, Any]:
        if arguments.get("refresh"):
            self._runtime.invalidate_tools(server_id)
        tools = await self._runtime.tools(server_id)
        cursors.listed.add((run.handle.run_id, server_id))
        entries = [
            {
                "tool_name": str(tool.get("name") or ""),
                "description": str(tool.get("description") or "")[:200],
            }
            for tool in tools
        ]
        self._emit(
            run,
            pending_events,
            server_id,
            level=1,
            content=json.dumps(entries, ensure_ascii=False, sort_keys=True),
        )
        return {"server_id": server_id, "tools": entries}

    async def _read_schema(
        self,
        run: Any,
        server_id: str,
        tool_name: str,
        cursors: McpDisclosureCursors,
        pending_events: dict[str, list[RuntimeEvent]],
    ) -> dict[str, Any]:
        if (run.handle.run_id, server_id) not in cursors.listed:
            raise McpDisclosureError(
                f"MCP {server_id!r} 须先 mcp_list_tools 再读取 Tool Schema"
            )
        tools = await self._runtime.tools(server_id)
        match = next((tool for tool in tools if tool.get("name") == tool_name), None)
        if match is None:
            raise McpDisclosureError(f"MCP {server_id!r} 无 Tool {tool_name!r}")
        cursors.schema_read.add((run.handle.run_id, server_id, tool_name))
        content = json.dumps(match, ensure_ascii=False, sort_keys=True)
        self._emit(
            run,
            pending_events,
            server_id,
            level=2,
            content=content,
            tool_name=tool_name,
        )
        return {
            "server_id": server_id,
            "tool_name": tool_name,
            "input_schema": match.get("inputSchema"),
            "description": match.get("description") or "",
        }

    async def _call_tool(
        self,
        run: Any,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        cursors: McpDisclosureCursors,
        pending_events: dict[str, list[RuntimeEvent]],
    ) -> dict[str, Any]:
        # 校验模型不能跳过 Schema 直接调用未知 Tool（P0 要求 3）。
        if (run.handle.run_id, server_id, tool_name) not in cursors.schema_read:
            raise McpDisclosureError(
                f"MCP {server_id!r} 的 Tool {tool_name!r} 须先 mcp_read_tool_schema 再调用"
            )
        call_arguments = arguments.get("arguments")
        if not isinstance(call_arguments, dict):
            raise McpDisclosureError("mcp_call_tool 缺少 arguments 对象")
        result = await self._runtime.call(server_id, tool_name, call_arguments)
        rendered = result if isinstance(result, (str, int, float, bool)) else json.dumps(
            result, ensure_ascii=False, default=str
        )
        self._emit(
            run,
            pending_events,
            server_id,
            level=3,
            content=str(rendered),
            tool_name=tool_name,
        )
        payload = self._offload_if_needed(
            run, pending_events, server_id, tool_name, rendered
        )
        if payload.get("offloaded"):
            # 大结果不回 Context：只保留摘要与引用（完整内容在 Artifact URI）。
            return {"server_id": server_id, "tool_name": tool_name, **payload}
        return {"server_id": server_id, "tool_name": tool_name, "result": rendered}

    def _offload_if_needed(
        self,
        run: Any,
        pending_events: dict[str, list[RuntimeEvent]],
        server_id: str,
        tool_name: str,
        rendered: Any,
    ) -> dict[str, Any]:
        """P1 大结果 Offload：超阈值/命中敏感策略 → Artifact Store 外置。

        返回并入工具结果的引用字段（``offloaded=true`` 时含摘要与
        URI/Hash/MIME/size）。写入失败或超额 → 抛 McpDisclosureError 明确
        降级，绝不把超大/敏感结果静默留在 Context。
        """
        policy = self._offload_policy
        content = str(rendered)
        size = len(content.encode("utf-8"))
        sensitive = policy.sensitive_match(content) if policy.enabled else None
        oversized = policy.enabled and size > policy.single_result_threshold_bytes
        if not sensitive and not oversized:
            return {}
        if self._artifact_store is None:
            raise McpDisclosureError(
                f"MCP {server_id!r} 的 Tool {tool_name!r} 结果 {size} 字节需外置"
                "（超大或含敏感数据），但未装配 ArtifactStore：结果已丢弃，"
                "不会写回上下文"
            )
        run_id = run.handle.run_id
        # Run 累计配额（从 Artifact 索引实算，跨进程恢复后依然正确）。
        used = sum(record.bytes for record in self._artifact_store.list(run_id))
        if used + size > policy.run_total_quota_bytes:
            raise McpDisclosureError(
                f"MCP {server_id!r} 的 Tool {tool_name!r} 结果 {size} 字节超出 "
                f"Run 外置配额（已用 {used}/{policy.run_total_quota_bytes}）："
                "结果已丢弃，不会写回上下文"
            )
        name = f"mcp_{server_id}_{tool_name}"
        try:
            record = self._artifact_store.save(
                run_id=run_id, name=name, content=content.encode("utf-8"),
                mime="application/json",
            )
        except (OSError, ValueError) as exc:
            raise McpDisclosureError(
                f"MCP {server_id!r} 的 Tool {tool_name!r} 结果 {size} 字节 "
                f"写入 Artifact Store 失败（{exc}）：结果已丢弃，不会写回上下文"
            ) from exc
        pending_events.setdefault(run_id, []).append(
            RuntimeEvent.create(
                EventType.ARTIFACT_CREATED,
                agent_id=run.state.agent_id,
                user_id=run.state.user_id,
                session_id=run.state.session_id,
                invocation_id=run_id,
                seq_id=0,
                payload={
                    "name": record.name,
                    "version": record.version,
                    "uri": record.uri,
                    "mime": record.mime,
                },
            )
        )
        # 敏感命中时摘要置空，敏感明文不留在 Context。
        summary = "" if sensitive else content[: policy.summary_chars]
        return {
            "offloaded": True,
            "artifact_uri": record.uri,
            "content_hash": record.content_hash,
            "mime": record.mime,
            "size_bytes": record.bytes,
            "summary": summary,
        }

    def _emit(
        self,
        run: Any,
        pending_events: dict[str, list[RuntimeEvent]],
        server_id: str,
        *,
        level: int,
        content: str,
        tool_name: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "server_id": server_id,
            "level": level,
            "content_hash": _digest(content),
            "size_bytes": len(content.encode("utf-8")),
        }
        if tool_name is not None:
            payload["tool_name"] = tool_name
        pending_events.setdefault(run.handle.run_id, []).append(
            RuntimeEvent.create(
                EventType.MCP_DISCLOSED,
                agent_id=run.state.agent_id,
                user_id=run.state.user_id,
                session_id=run.state.session_id,
                invocation_id=run.handle.run_id,
                seq_id=0,
                payload=payload,
            )
        )

    def clear_run(self, run_id: str) -> None:
        """游标由引擎写入 Graph State（随 Checkpoint 持久化），无需清理。"""
        del run_id


def _digest(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


__all__ = [
    "MCP_CALL_TOOL_TOOL",
    "McpDisclosureError",
    "MCP_LIST_TOOLS_TOOL",
    "MCP_READ_SCHEMA_TOOL",
    "McpOffloadPolicy",
    "disclosure_tools",
]

"""P1 大结果 Offload：MCP L3 结果外置 Artifact Store 的集成测试。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from ksadk.harness.artifact_store import ArtifactStore
from ksadk.harness.capabilities import CapabilityDescriptor, RiskLevel
from ksadk.harness.engine.langgraph import ManagedLangGraphEngine
from ksadk.harness.engine.mcp_disclosure import (
    MCP_CALL_TOOL_TOOL,
    MCP_LIST_TOOLS_TOOL,
    MCP_READ_SCHEMA_TOOL,
    McpOffloadPolicy,
)
from ksadk.harness.events import EventType
from ksadk.harness.mcp_runtime import (
    McpCapabilityRuntime,
    McpServerBinding,
    McpTransport,
)
from ksadk.harness.reasoner import HarnessReasoningTurn, HarnessToolCall
from ksadk.harness.spec import (
    CapabilityBinding,
    CapabilityBindings,
    HarnessSpec,
    ModelBinding,
    PromptSpec,
)
from ksadk.runtime import StartRequest

_SERVER = "mcp://big-server@1.0.0"

_TOOLS = {
    "get_invoice": {
        "name": "get_invoice",
        "description": "查询发票（可能返回大结果）",
        "inputSchema": {
            "type": "object",
            "properties": {"invoice_id": {"type": "string"}},
            "required": ["invoice_id"],
        },
    },
}


class _BigTransport(McpTransport):
    def __init__(self, result: Any) -> None:
        self._result = result

    async def list_tools(self) -> list[dict[str, Any]]:
        return list(_TOOLS.values())

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return self._result


class _ScriptedReasoner:
    def __init__(self, calls: list[tuple[str, dict]], final_text: str = "完成。"):
        self._calls = calls
        self._final = final_text
        self.step = 0
        self.last_messages: tuple[dict, ...] = ()

    async def complete(self, *, model, prompt, messages, tools):
        del model, prompt, tools
        self.last_messages = tuple(messages)
        if self.step < len(self._calls):
            name, arguments = self._calls[self.step]
            self.step += 1
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(call_id=f"c{self.step}", name=name, arguments=arguments),
                )
            )
        return HarnessReasoningTurn(final_text=self._final)


_CHAIN: list[tuple[str, dict]] = [
    (MCP_LIST_TOOLS_TOOL, {"server_id": _SERVER}),
    (MCP_READ_SCHEMA_TOOL, {"server_id": _SERVER, "tool_name": "get_invoice"}),
    (
        MCP_CALL_TOOL_TOOL,
        {"server_id": _SERVER, "tool_name": "get_invoice",
         "arguments": {"invoice_id": "INV-1"}},
    ),
]


def _spec() -> HarnessSpec:
    return HarnessSpec(
        agent_revision_ref="agent-revision://big@1",
        model=ModelBinding(profile_ref="model-profile://test@1.0.0"),
        prompt=PromptSpec(instructions="你是助手。"),
        capabilities=CapabilityBindings(
            mcp_bindings=(
                CapabilityBinding(
                    capability_ref=_SERVER, required=True, load_policy="on_demand"
                ),
            )
        ),
    )


def _runtime(result: Any) -> McpCapabilityRuntime:
    runtime = McpCapabilityRuntime()
    runtime.bind(
        McpServerBinding(
            descriptor=CapabilityDescriptor(
                id=_SERVER, kind="mcp", name="大结果站", description="测试",
                version="1.0.0", risk_level=RiskLevel.MEDIUM,
            ),
            transport=_BigTransport(result),
            required=True,
        )
    )
    return runtime


def _drive(reasoner, runtime, store=None, policy=None):
    async def run():
        kwargs: dict[str, Any] = {"reasoner": reasoner, "mcp_runtime": runtime}
        if store is not None:
            kwargs["artifact_store"] = store
        if policy is not None:
            kwargs["mcp_offload_policy"] = policy
        engine = ManagedLangGraphEngine(**kwargs)
        compiled = await engine.compile(_spec())
        handle = await engine.start(
            StartRequest(
                agent_id="a", user_id="u", session_id="s", input="查发票",
                runtime_type="managed-langgraph",
            ),
            compiled,
        )
        events = [e async for e in engine.stream(handle)]
        return events

    return asyncio.run(run())


def _tool_result_message(reasoner: _ScriptedReasoner) -> str:
    # 最后一次模型调用看到的消息即工具结果回填后的完整 Context。
    return json.dumps(
        [m.get("content") for m in reasoner.last_messages], ensure_ascii=False
    )


def test_small_result_stays_inline(tmp_path):
    store = ArtifactStore(tmp_path / "art")
    reasoner = _ScriptedReasoner(_CHAIN)
    events = _drive(reasoner, _runtime({"amount": 100, "status": "ok"}), store=store)
    text = _tool_result_message(reasoner)
    assert "amount" in text
    assert not [e for e in events if e.event_type == EventType.ARTIFACT_CREATED]


def test_large_result_offloaded_to_artifact_store(tmp_path):
    store = ArtifactStore(tmp_path / "art")
    big = {"amount": 88600, "rows": [{"line": i, "memo": "明细行" * 20} for i in range(200)]}
    reasoner = _ScriptedReasoner(_CHAIN)
    events = _drive(reasoner, _runtime(big), store=store)
    text = _tool_result_message(reasoner)
    # Context 只有引用与摘要，没有完整大结果。
    assert "artifact://" in text and "sha256:" in text
    assert '"rows"' not in text
    # artifact.created 事件带 URI/Hash/MIME/size。
    created = [e for e in events if e.event_type == EventType.ARTIFACT_CREATED]
    assert created
    payload = created[0].payload
    assert payload["uri"].startswith("artifact://")
    assert payload["mime"] == "application/json"
    # P1.1：事件与 Context 引用对齐，便于 Studio 展示与审计。
    assert payload["content_hash"].startswith("sha256:")
    assert payload["size_bytes"] > 4096
    assert payload["source"] == "mcp"
    assert payload["source_ref"] == f"{_SERVER}/get_invoice"
    # 完整内容在 Store，可按 URI 取回。
    records = store.list(_run_id(events))
    assert records and b"rows" in store.read(records[0])


def test_artifact_write_failure_degrades_explicitly(tmp_path):
    # 阈值压到 1 字节：任何结果都要外置；Store 指向只读/坏路径 → 写失败。
    class _BrokenStore:
        def list(self, run_id):
            return []

        def save(self, **kwargs):
            raise OSError("disk full")

    reasoner = _ScriptedReasoner(_CHAIN)
    events = _drive(
        reasoner, _runtime({"amount": 88600}),
        store=_BrokenStore(),
        policy=McpOffloadPolicy(single_result_threshold_bytes=1),
    )
    failures = [
        e for e in events
        if e.event_type == EventType.TOOL_CALL_END and e.payload.get("name") == MCP_CALL_TOOL_TOOL
        and "写入 Artifact Store 失败" in str(e.payload.get("error") or "")
    ]
    assert failures, "Artifact 写失败必须产生明确错误事件"
    # 关键：超大结果没有被静默塞回 Context。
    assert "88600" not in _tool_result_message(reasoner)
    assert events[-1].event_type == EventType.RUN_COMPLETED  # Run 不因此失败


def test_missing_store_with_oversized_result_degrades(tmp_path):
    reasoner = _ScriptedReasoner(_CHAIN)
    events = _drive(
        reasoner, _runtime({"amount": 88600}),
        policy=McpOffloadPolicy(single_result_threshold_bytes=1),
    )
    assert any(
        "未装配 ArtifactStore" in str(e.payload.get("error") or "") for e in events
        if e.event_type == EventType.TOOL_CALL_END
    )
    assert "88600" not in _tool_result_message(reasoner)


def test_run_quota_exceeded_degrades_explicitly(tmp_path):
    store = ArtifactStore(tmp_path / "art")
    reasoner = _ScriptedReasoner(_CHAIN)
    events = _drive(
        reasoner, _runtime({"amount": 88600, "rows": [0] * 500}),
        store=store,
        policy=McpOffloadPolicy(
            single_result_threshold_bytes=10, run_total_quota_bytes=1
        ),
    )
    assert any(
        "超出 Run 外置配额" in str(e.payload.get("error") or "") for e in events
        if e.event_type == EventType.TOOL_CALL_END
    )
    assert "88600" not in _tool_result_message(reasoner)


def test_sensitive_result_forced_offload_without_summary(tmp_path):
    store = ArtifactStore(tmp_path / "art")
    sensitive = {"name": "张三", "id_card": "110101199001011234", "amount": 100}
    reasoner = _ScriptedReasoner(_CHAIN)
    events = _drive(
        reasoner, _runtime(sensitive),
        store=store,
        policy=McpOffloadPolicy(sensitive_patterns=(r"\d{17}[\dXx]",)),
    )
    created = [e for e in events if e.event_type == EventType.ARTIFACT_CREATED]
    assert created, "命中敏感策略的小结果也必须外置"
    text = _tool_result_message(reasoner)
    # 敏感明文不留在 Context（摘要置空）。
    assert "id_card" not in text and "110101" not in text
    assert "artifact://" in text


def test_default_sensitive_patterns_active(tmp_path):
    """P1.1：默认策略开启基础敏感识别——手机号/身份证即使小结果也外置。"""
    store = ArtifactStore(tmp_path / "art")
    reasoner = _ScriptedReasoner(_CHAIN)
    events = _drive(reasoner, _runtime({"contact": "13812345678"}), store=store)
    assert [e for e in events if e.event_type == EventType.ARTIFACT_CREATED]
    assert "13812345678" not in _tool_result_message(reasoner)


def test_string_result_offloaded_as_text_plain(tmp_path):
    """P1.1：MIME 按结果形态选择——纯字符串 → text/plain。"""
    store = ArtifactStore(tmp_path / "art")
    reasoner = _ScriptedReasoner(_CHAIN)
    events = _drive(reasoner, _runtime("x" * 8192), store=store)
    created = [e for e in events if e.event_type == EventType.ARTIFACT_CREATED]
    assert created and created[0].payload["mime"] == "text/plain"


def test_run_id_path_traversal_is_rejected(tmp_path):
    """P1.1：调用方控制 invocation_id 时不得目录穿越。"""
    store = ArtifactStore(tmp_path / "art")
    record = store.save(run_id="../../evil", name="r", content=b"x")
    # run_id 已规范化为安全名，URI 与磁盘路径都留在 Store 根目录内。
    assert "../" not in record.uri
    assert (tmp_path / "art" / ".._.._evil" / "r.v1").is_file()
    with pytest.raises(ValueError):
        # 非法 uri 解析后路径越界 → 拒绝读取。
        store._read_uri("artifact://../../etc/passwd/r@v1")


def _run_id(events):
    for e in events:
        if e.event_type == EventType.RUN_STARTED:
            return e.invocation_id
    raise AssertionError("no run.started")

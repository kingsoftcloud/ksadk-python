"""长任务方案 P3：平台消费合同 + LangGraph Store 适配层测试。"""

from __future__ import annotations

import asyncio

from ksadk.harness.context_engine import HarnessContextEngine
from ksadk.harness.engine.langgraph import ManagedLangGraphEngine
from ksadk.harness.observability import compaction_trace, context_trace, token_report
from ksadk.harness.reasoner import HarnessReasoner, HarnessReasoningTurn
from ksadk.harness.spec import HarnessSpec, ModelBinding, PromptSpec
from ksadk.memory.models import (
    MemoryDeleteRequest,
    MemoryRecord,
    MemorySearchRequest,
)
from ksadk.memory.store_adapter import LangGraphStoreAdapter
from ksadk.runtime import StartRequest

# ------------------------------------------------------------- 查询合同

def _spec() -> HarnessSpec:
    return HarnessSpec(
        agent_revision_ref="agent-revision://proj-1@2",
        model=ModelBinding(profile_ref="model-profile://kimi-k3@1.0.0"),
        prompt=PromptSpec(instructions="你是财务分析助手。"),
    )


class _ScriptedReasoner(HarnessReasoner):
    def __init__(self, turns: list[HarnessReasoningTurn]) -> None:
        self._turns = list(turns)

    async def complete(self, *, model, prompt, messages, tools):
        return self._turns.pop(0)


def _run_engine() -> list:
    engine = ManagedLangGraphEngine(
        reasoner=_ScriptedReasoner(
            [
                HarnessReasoningTurn(final_text="摘要：审批号 AP-1024。"),
                HarnessReasoningTurn(
                    final_text="完成", usage={"input_tokens": 640, "output_tokens": 32}
                ),
            ]
        ),
        context_engine=HarnessContextEngine(),
    )
    history = [
        {"role": "user", "content": f"历史问题 {i} " + "细节" * 400} for i in range(30)
    ]
    request = StartRequest(
        agent_id="ar-1",
        user_id="user-1",
        session_id="sess-1",
        input="总结",
        runtime_type="managed-langgraph",
        metadata={"conversation_history": history, "context_window_tokens": 2048},
    )

    async def drive():
        compiled = await engine.compile(_spec())
        handle = await engine.start(request, compiled)
        return [event async for event in engine.stream(handle)]

    return asyncio.run(drive())


def test_context_trace_is_plain_payload_contract():
    events = _run_engine()
    manifests = context_trace(events)
    assert manifests, "context.built 必须可投影"
    m = manifests[-1]
    assert m["manifest_id"].startswith("ctxm_")
    assert "planned_tokens" in m and "projected_tokens" in m and "sections" in m
    # 纯 dict / JSON 兼容（Studio 不解析 Harness 私有对象）。
    import json

    json.dumps(manifests)


def test_compaction_trace_and_token_report():
    events = _run_engine()
    compactions = compaction_trace(events)
    assert compactions and compactions[-1]["compaction_id"].startswith("cmp_")
    assert "quality_checks" in compactions[-1]
    report = token_report(events)
    assert report["manifest_count"] >= 1
    assert report["compactions"] >= 1
    assert report["actual_total_input_tokens"] == 640
    assert report["actual_total_output_tokens"] == 32
    # Planned/Projected/Actual 闭环：usage 事件携带 manifest_id，
    # 可稳定查询「某次模型调用实际对应哪个 Manifest」。
    assert all(u["manifest_id"] for u in report["usage_events"])
    manifests = context_trace(events)
    last = manifests[-1]
    assert last["actual"]["input_tokens"] == 640
    assert last["actual"]["usage_event_id"]
    # 有 usage 的 manifest 必须能按 manifest_id 配对回 usage 事件。
    by_manifest = {u["manifest_id"] for u in report["usage_events"]}
    assert last["manifest_id"] in by_manifest


# ------------------------------------------------------------- Store 适配

class _FakeStore:
    """LangGraph BaseStore 最小仿真（aput/aget/asearch/adelete）。"""

    def __init__(self) -> None:
        self.data: dict[tuple, dict[str, dict]] = {}

    async def aput(self, ns, key, value):
        self.data.setdefault(ns, {})[key] = value

    async def aget(self, ns, key):
        return type("Item", (), {"value": self.data.get(ns, {}).get(key)})()

    async def asearch(self, ns, query=None, limit=10):
        items = []
        for key, value in self.data.get(ns, {}).items():
            if query is None or query in str(value.get("content", "")):
                items.append(type("Item", (), {"value": value})())
        return items[:limit]

    async def adelete(self, ns, key):
        self.data.get(ns, {}).pop(key, None)


def _record(memory_id: str = "mem_x", status: str = "active") -> MemoryRecord:
    return MemoryRecord(
        memory_id=memory_id,
        tenant_id="local",
        workspace_id="store",
        scope="user",
        scope_id="user:u1",
        memory_type="fact",
        content="用户偏好报表用中文",
        summary="用户偏好报表用中文",
        status=status,
        confidence=0.8,
        importance=0.6,
        valid_from="",
        valid_to="",
        expires_at="",
        source_session_id="",
        source_event_ids=[],
        source_seq_range=None,
        content_hash="sha256:x",
        version=1,
    )


def test_store_adapter_roundtrip():
    adapter = LangGraphStoreAdapter(_FakeStore())
    record = _record()
    # 同步入口（无事件循环时）驱动 async Store。
    stored = adapter.upsert(record, expected_version=None)
    assert stored.memory_id == "mem_x"
    result = adapter.search(
        MemorySearchRequest(query="报表", scopes=[("user", "user:u1")], memory_types=["fact"])
    )
    assert result.status == "ok" and len(result.records) == 1
    assert result.records[0].content == "用户偏好报表用中文"
    # 逻辑删除（tombstone）。
    deleted = adapter.delete(
        MemoryDeleteRequest(memory_id="mem_x", scope="user", scope_id="user:u1")
    )
    assert deleted.deleted
    after = adapter.search(
        MemorySearchRequest(query="报表", scopes=[("user", "user:u1")], memory_types=["fact"])
    )
    assert all(r.status != "active" for r in after.records)


def test_store_adapter_version_conflict():
    adapter = LangGraphStoreAdapter(_FakeStore())
    adapter.upsert(_record(), expected_version=None)
    try:
        adapter.upsert(_record(), expected_version=999)
        raise AssertionError("版本冲突必须抛错")
    except RuntimeError as exc:
        assert "version_conflict" in str(exc)

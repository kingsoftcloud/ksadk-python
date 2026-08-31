"""长任务方案 P1-2：MemoryRecord 来源/TTL/敏感级/写策略治理测试。"""

from __future__ import annotations

from ksadk.harness.memory_runtime import HarnessMemoryRuntime, MemoryWriteRequest
from ksadk.harness.spec import HarnessSpec, MemoryPolicy, ModelBinding, PromptSpec
from ksadk.memory.models import MemoryArtifactRef, MemorySearchRequest


def _spec() -> HarnessSpec:
    return HarnessSpec(
        agent_revision_ref="agent-revision://proj-1@2",
        model=ModelBinding(profile_ref="model-profile://kimi-k3@1.0.0"),
        prompt=PromptSpec(instructions="你是财务分析助手。"),
        memory_policy=MemoryPolicy(enabled=True, scopes=("session", "agent", "user", "org")),
    )


def _write(runtime: HarnessMemoryRuntime, content: str, **overrides):
    overrides.setdefault("slot_key", "preference:report_language")
    request = MemoryWriteRequest(
        operation="add",
        content=content,
        scope="agent",
        scope_id="agent:a1",
        source="user_explicit",
        **overrides,
    )
    return runtime.write(request, _spec(), run_id="run_gov")


def test_record_carries_source_artifact_sensitivity_ttl():
    runtime = HarnessMemoryRuntime.local_sqlite()
    _write(
        runtime,
        "Q3 预算口径以财务系统为准",
        source_artifact_refs=("artifact://run_gov/budget-summary@1",),
        source_artifacts=(
            MemoryArtifactRef(
                uri="artifact://run_gov/budget-chart@1",
                mime_type="image/png",
                content_hash="sha256:0123456789abcdef",
                size_bytes=4096,
                modality="image",
            ),
        ),
        sensitivity="low",
        write_policy="user_confirm",
        expires_at="2027-01-01T00:00:00Z",
    )
    result = runtime.coordinator.recall(
        MemorySearchRequest(
            query="预算口径", scopes=[("agent", "agent:a1")], memory_types=["fact"]
        )
    )
    assert result.records
    record = result.records[0]
    assert record.source_artifact_refs == ("artifact://run_gov/budget-summary@1",)
    assert record.source_artifacts[0].modality == "image"
    assert record.source_artifacts[0].mime_type == "image/png"
    assert record.sensitivity == "low"
    assert record.write_policy == "user_confirm"
    assert record.expires_at == "2027-01-01T00:00:00Z"


def test_multimodal_memory_rejects_inline_or_temporary_source():
    try:
        MemoryArtifactRef(
            uri="data:image/png;base64,AAAA",
            mime_type="image/png",
            content_hash="sha256:0123456789abcdef",
            size_bytes=4,
            modality="image",
        )
    except ValueError as exc:
        assert "artifact://" in str(exc)
    else:  # pragma: no cover - safety invariant
        raise AssertionError("inline media must never enter Memory")


def test_expired_records_filtered_from_recall():
    runtime = HarnessMemoryRuntime.local_sqlite()
    _write(runtime, "临时口径（明天过期）", expires_at="2000-01-01T00:00:00Z")
    result = runtime.coordinator.recall(
        MemorySearchRequest(query="临时口径", scopes=[("agent", "agent:a1")], memory_types=["fact"])
    )
    assert not result.records, "过期记录不得进入召回"


def test_locked_record_rejects_pipeline_update():
    runtime = HarnessMemoryRuntime.local_sqlite()
    _write(runtime, "用户偏好：报表用中文", write_policy="locked")
    # 模型猜测同槽位更新 → 拒绝（locked），不静默覆盖。
    evaluation, event = _write(runtime, "用户偏好：报表用英文")
    assert evaluation.decision == "reject"
    assert evaluation.reason == "locked_record"
    assert event is not None and event.event_type == "memory.conflict"


def test_update_supersedes_keeps_audit_chain():
    runtime = HarnessMemoryRuntime.local_sqlite()
    _write(runtime, "用户爱好是羽毛球")
    evaluation, _ = _write(runtime, "用户爱好其实是爬山")
    assert evaluation.operation == "update"
    result = runtime.coordinator.recall(
        MemorySearchRequest(query="爱好", scopes=[("agent", "agent:a1")], memory_types=["fact"])
    )
    assert result.records
    fresh = result.records[0]
    assert fresh.version >= 2
    assert fresh.supersedes, "新记录必须携带 supersedes 审计链"

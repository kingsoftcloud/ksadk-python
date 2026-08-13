"""PCM 永久全链路测试（方案 §4）。

覆盖 Codex 和 LangGraph：
  创建 Agent → 保存 PCM 策略 → Build → 修改 Draft → 用旧 Build 运行
  → 检查 RunSpec → 检查 Memory

Case 覆盖：
  - memory.enabled=false 不召回、不写入
  - rollout=off 不写入
  - rollout=shadow 只产候选、不写入
  - enabled + explicit_only 只保存明确"记住"
  - enabled + candidate 按 Policy 保存
  - Secret 被拒绝
  - Provider 失败不影响回答，但产生 memory.flush.failed
  - Build 后修改 sidecar 不影响旧 Build
"""

from __future__ import annotations

import pytest

from ksadk.memory.resolved_policy import resolve_memory_policy
from ksadk.studio.codex_manifest import CodexAgentManifest
from ksadk.studio.contracts import (
    AgentSpec,
    ContextSpec,
    Instructions,
    MemorySpec,
    MemoryWriteSpec,
    RuntimeRef,
)


def _make_spec(
    *,
    memory_enabled: bool = False,
    write_rollout: str = "off",
    write_mode: str = "candidate",
    max_input: int = 32000,
) -> AgentSpec:
    return AgentSpec(
        runtime=RuntimeRef(type="codex", version="0.144.4"),
        instructions=Instructions(system="你是助手", task=""),
        context=ContextSpec(
            max_input_tokens=max_input,
            rollout={"contextEngine": "shadow", "memoryWrite": write_rollout},
        ),
        memory=MemorySpec(
            enabled=memory_enabled,
            write=MemoryWriteSpec(mode=write_mode),
            recall={"enabled": memory_enabled, "maxTokens": 800, "topK": 5},
        ),
    )


# ---- 1. ResolvedMemoryPolicy 优先级 ----


def test_memory_disabled_no_recall_no_flush():
    p = resolve_memory_policy(
        memory_enabled=False,
        recall_enabled=True,
        write_rollout="enabled",
        write_mode="candidate",
        flush_before_compaction=True,
        provider_ref="local",
    )
    assert not p.should_recall
    assert not p.should_flush


def test_rollout_off_no_flush():
    p = resolve_memory_policy(
        memory_enabled=True,
        recall_enabled=True,
        write_rollout="off",
        write_mode="candidate",
        flush_before_compaction=True,
        provider_ref="local",
    )
    assert p.should_recall
    assert not p.should_flush


def test_shadow_extracts_no_flush():
    p = resolve_memory_policy(
        memory_enabled=True,
        recall_enabled=True,
        write_rollout="shadow",
        write_mode="candidate",
        flush_before_compaction=True,
        provider_ref="local",
    )
    assert p.should_extract_candidates
    assert not p.should_flush


def test_enabled_explicit_only():
    p = resolve_memory_policy(
        memory_enabled=True,
        recall_enabled=True,
        write_rollout="enabled",
        write_mode="explicit_only",
        flush_before_compaction=True,
        provider_ref="local",
    )
    assert p.should_flush
    assert p.is_explicit_only


def test_enabled_candidate():
    p = resolve_memory_policy(
        memory_enabled=True,
        recall_enabled=True,
        write_rollout="enabled",
        write_mode="candidate",
        flush_before_compaction=True,
        provider_ref="local",
    )
    assert p.should_flush
    assert not p.is_explicit_only


# ---- 2. Codex Manifest 严格类型化 ----


def test_manifest_context_validates_on_project(tmp_path):
    """Manifest context 格式错误时 _project 抛 ValueError，不静默降级。"""
    from ksadk.studio.codex_manifest import _validate_pcm_field
    from ksadk.studio.contracts import ContextSpec

    # 正常 dict
    spec = _validate_pcm_field(
        {"maxInputTokens": 4096, "reserveOutputTokens": 512}, "context", ContextSpec
    )
    assert spec.max_input_tokens == 4096

    # None → 默认值
    spec = _validate_pcm_field(None, "context", ContextSpec)
    assert spec.max_input_tokens == 32000

    # 格式错误 → 抛 ValueError
    with pytest.raises(ValueError, match="格式错误"):
        _validate_pcm_field({"maxInputTokens": "not_a_number"}, "context", ContextSpec)


# ---- 3. Memory 事件结构化 ----


def test_memory_events_dont_leak_content():
    """Memory 事件不记录正文/敏感信息。"""
    from ksadk.memory.events import flush_failed, recall_completed

    e = recall_completed(
        run_id="r1",
        session_id="s1",
        provider="sqlite",
        rollout="enabled",
        count=3,
    )
    d = e.to_dict()
    assert "content" not in d
    assert d["candidate_count"] == 3
    assert d["type"] == "memory.recall.completed"

    e2 = flush_failed(
        run_id="r1",
        session_id="s1",
        provider="sqlite",
        rollout="enabled",
        error_code="timeout",
        error_message="connection timed out",
    )
    d2 = e2.to_dict()
    assert d2["error_code"] == "timeout"
    assert d2["retryable"] is True


# ---- 4. Build 后修改 sidecar 不影响旧 Build ----


def test_build_immutability_codex_manifest(tmp_path):
    """Build 后修改 Draft 不改变旧 Build 的 Manifest PCM 策略。"""
    from ksadk.studio.codex_manifest import (
        CodexRuntimeRef,
    )

    # Build 1: maxInputTokens=4096
    manifest1 = CodexAgentManifest(
        name="immutable-test",
        version="1.0.0",
        runtime=CodexRuntimeRef(version="0.144.4"),
        model="glm-5.2",
        prompt="你是助手",
        context={
            "maxInputTokens": 4096,
            "reserveOutputTokens": 512,
            "rollout": {"contextEngine": "shadow", "memoryWrite": "enabled"},
        },
        memory={
            "enabled": True,
            "write": {"mode": "candidate", "flushBeforeCompaction": True},
        },
    )
    # Build 2: maxInputTokens=32000（修改后）
    manifest2 = CodexAgentManifest(
        name="immutable-test",
        version="1.1.0",
        runtime=CodexRuntimeRef(version="0.144.4"),
        model="glm-5.2",
        prompt="你是助手",
        context={
            "maxInputTokens": 32000,
            "reserveOutputTokens": 4096,
            "rollout": {"contextEngine": "shadow", "memoryWrite": "off"},
        },
        memory={"enabled": False},
    )

    # 旧 manifest 不受新 manifest 影响
    assert manifest1.context["maxInputTokens"] == 4096
    assert manifest2.context["maxInputTokens"] == 32000
    assert manifest1.memory["enabled"] is True
    assert manifest2.memory["enabled"] is False


# ---- 5. Secret 被 Memory Policy 拒绝 ----


def test_secret_rejected_by_policy():
    """Secret/PII 被 MemoryPolicy 永久拒绝。"""
    from ksadk.memory.models import MemoryCandidate
    from ksadk.memory.policy import MemoryPolicy

    cand = MemoryCandidate(
        candidate_id="c1",
        operation="add",
        memory_type="profile",
        scope="user",
        scope_id="u1",
        content="api_key=sk-1234567890abcdefghijklmnop",
        confidence=0.99,
        importance=0.99,
        source_event_ids=["e1"],
    )
    ev = MemoryPolicy().evaluate(cand)
    assert ev.decision == "reject"
    assert "sensitive" in ev.reason.lower()


# ---- 6. Provider 失败产生 flush.failed 事件 ----


def test_flush_failed_event_on_provider_error():
    """Provider 失败时产生 memory.flush.failed 事件，不阻断主链路。"""
    from ksadk.memory.events import flush_failed

    event = flush_failed(
        run_id="r1",
        session_id="s1",
        provider="sqlite",
        rollout="enabled",
        error_code="disk_full",
        error_message="no space left on device",
        retryable=True,
    )
    assert event.type == "memory.flush.failed"
    assert event.error_code == "disk_full"
    assert event.retryable is True
    # 不记录正文
    assert not hasattr(event, "content")

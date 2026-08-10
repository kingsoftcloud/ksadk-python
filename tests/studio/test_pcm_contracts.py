"""PR-S1：AgentSpec PCM 合同扩展 + ownership capability 校验（方案 §5.1/§5.2）。"""

from __future__ import annotations

import pytest

from ksadk.context_engine.capabilities import (
    allowed_ownership_choices,
    resolve_ownership,
    validate_ownership_for_runtime,
)
from ksadk.studio.contracts import (
    AgentSpec,
    CompactionSpec,
    ContextSpec,
    MemorySpec,
    RolloutSpec,
)


# ---- ContextSpec 扩展 ----

def test_context_spec_defaults_backward_compatible():
    c = ContextSpec()
    assert c.ownership == "auto"
    assert c.prompt_ownership == "framework"  # 旧默认不变
    assert c.tokenizer == "auto"
    assert c.policy_version == "context-v2"
    assert c.rollout.context_engine == "shadow"
    assert c.compaction.soft_threshold_ratio == 0.50
    assert c.compaction.hard_threshold_ratio == 0.85
    assert c.compaction.preserve_working_state is True


def test_ownership_ksadk_narrows_prompt_ownership():
    c = ContextSpec(ownership="ksadk")
    assert c.prompt_ownership == "ksadk"


def test_ownership_framework_narrows_prompt_ownership():
    c = ContextSpec(ownership="framework")
    assert c.prompt_ownership == "framework"


def test_compaction_ratio_validation():
    with pytest.raises(Exception):
        CompactionSpec(soft_threshold_ratio=0.90, hard_threshold_ratio=0.85)
    CompactionSpec(soft_threshold_ratio=0.50, hard_threshold_ratio=0.85)  # ok


def test_memory_spec_defaults():
    m = MemorySpec()
    assert m.enabled is False
    assert m.provider_ref == "local-default"
    assert m.recall.enabled is True
    assert m.write.mode == "candidate"
    assert "user" in m.scopes


def test_agent_spec_has_memory_field():
    s = AgentSpec()
    assert isinstance(s.memory, MemorySpec)
    assert s.context.rollout.context_engine == "shadow"


# ---- ownership capability 校验 ----

def test_allowed_ownership_choices():
    assert allowed_ownership_choices("codex") == ("native",)
    assert allowed_ownership_choices("langgraph") == ("framework", "ksadk")
    assert allowed_ownership_choices("adk") == ("framework",)
    assert allowed_ownership_choices("unknown") == ("framework",)  # 保守


def test_validate_ownership_rejects_unsupported():
    with pytest.raises(ValueError):
        validate_ownership_for_runtime("ksadk", runtime_type="codex")
    with pytest.raises(ValueError):
        validate_ownership_for_runtime("native", runtime_type="langgraph")


def test_validate_ownership_accepts_auto_and_supported():
    validate_ownership_for_runtime("auto", runtime_type="codex")  # auto 总合法
    validate_ownership_for_runtime("native", runtime_type="codex")
    validate_ownership_for_runtime("ksadk", runtime_type="langgraph")
    validate_ownership_for_runtime("framework", runtime_type="langgraph")


def test_resolve_ownership_auto_is_conservative_default():
    # auto 不取 capability 上限，取保守产品默认（§5.2）
    assert resolve_ownership("auto", runtime_type="langgraph") == "framework"
    assert resolve_ownership("auto", runtime_type="adk") == "framework"
    assert resolve_ownership("auto", runtime_type="codex") == "native"
    assert resolve_ownership("auto", runtime_type="unknown") == "framework"


def test_resolve_ownership_explicit_passthrough():
    assert resolve_ownership("ksadk", runtime_type="langgraph") == "ksadk"
    assert resolve_ownership("native", runtime_type="codex") == "native"

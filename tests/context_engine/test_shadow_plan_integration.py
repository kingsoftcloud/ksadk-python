"""shadow ContextPlan 端到端挂载集成测试。

用真实 ``InMemorySessionService`` 跑 ``build_run_input``，断言 ``PreparedConversationTurn``
携带非空 ``shadow_context_plan``、token 分区累加正确、ownership 对齐传入的 runner。
这只验证可观测旁路，不触发任何模型/压缩行为变更。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ksadk.conversations.runtime_observability import _set_context_plan_attributes
from ksadk.conversations.runtime_preparation import build_run_input
from ksadk.sessions.in_memory import InMemorySessionService


def _langgraph_runner() -> SimpleNamespace:
    return SimpleNamespace(
        detection_result=SimpleNamespace(type=SimpleNamespace(value="langgraph"))
    )


@pytest.mark.asyncio
async def test_build_run_input_attaches_shadow_plan_with_token_breakdown() -> None:
    service = InMemorySessionService()
    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-shadow")
    prepared = await build_run_input(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-shadow",
        messages=[{"role": "user", "content": "帮我总结这段中文内容"}],
        instructions="你是摘要助手",
        session_service_provider=lambda: service,
        runner=_langgraph_runner(),
    )
    plan = prepared.shadow_context_plan
    assert plan is not None
    assert plan["integration_mode"] == "framework_assisted"
    assert plan["accounting_accuracy"] == "estimated"
    assert plan["prompt_owner"] == "ksadk"
    assert plan["tokens_by_kind"]["compiled_prompt"] > 0
    assert plan["tokens_by_kind"]["current_input"] > 0
    assert plan["planned_input_tokens"] == sum(plan["tokens_by_kind"].values())


@pytest.mark.asyncio
async def test_shadow_plan_without_runner_or_runtime_type_is_opaque() -> None:
    # 既无 runner 也无 runtime_type（如未知入口）→ DEFAULT opaque，但仍生成结构。
    service = InMemorySessionService()
    await service.create_session(
        agent_id="demo-agent", user_id="user-1", session_id="sess-shadow-none"
    )
    prepared = await build_run_input(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-shadow-none",
        messages=[{"role": "user", "content": "hello"}],
        session_service_provider=lambda: service,
    )
    plan = prepared.shadow_context_plan
    assert plan is not None
    assert plan["accounting_accuracy"] == "opaque"
    assert plan["integration_mode"] == "framework_assisted"
    assert plan["runtime_type"] == ""
    assert plan["capability_hash"].startswith("sha256:")


@pytest.mark.asyncio
async def test_canonical_path_runtime_type_yields_correct_ownership_not_opaque() -> None:
    # canonical conversation execution 路径在 build_run_input 阶段尚未拿到 adapter/runner，
    # 但传入 launch_context.runtime_type 即可取得正确 ownership，不落成默认 opaque。
    service = InMemorySessionService()
    await service.create_session(
        agent_id="demo-agent", user_id="user-1", session_id="sess-canonical"
    )
    prepared = await build_run_input(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-canonical",
        messages=[{"role": "user", "content": "帮我做个总结"}],
        instructions="你是助手",
        session_service_provider=lambda: service,
        runtime_type="langgraph",
    )
    plan = prepared.shadow_context_plan
    assert plan is not None
    assert plan["accounting_accuracy"] == "estimated"  # 非 opaque
    assert plan["integration_mode"] == "framework_assisted"
    assert plan["runtime_type"] == "langgraph"
    assert plan["history_owner"] == "ksadk"
    assert plan["capability_hash"].startswith("sha256:")


@pytest.mark.asyncio
async def test_canonical_path_codex_runtime_type_is_native_runtime() -> None:
    service = InMemorySessionService()
    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-codex")
    prepared = await build_run_input(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-codex",
        messages=[{"role": "user", "content": "继续"}],
        session_service_provider=lambda: service,
        runtime_type="codex",
    )
    plan = prepared.shadow_context_plan
    assert plan["integration_mode"] == "native_runtime"
    assert plan["accounting_accuracy"] == "runtime_reported"
    assert plan["history_owner"] == "native"
    assert plan["runtime_type"] == "codex"


def test_set_context_plan_attributes_noop_on_none_and_populates_on_dict() -> None:
    class _Span:
        def __init__(self) -> None:
            self.attrs: dict[str, object] = {}

        def set_attribute(self, key: str, value: object) -> None:
            self.attrs[key] = value

    # None plan / None span 不报错。
    _set_context_plan_attributes(None, None)
    span = _Span()
    _set_context_plan_attributes(span, None)
    assert span.attrs == {}

    span = _Span()
    plan = {
        "plan_id": "ctxplan_abc",
        "policy_version": "v1",
        "tokenizer": "heuristic_cjk_ascii",
        "planned_input_tokens": 42,
        "integration_mode": "framework_assisted",
        "accounting_accuracy": "estimated",
        "tokens_by_kind": {"compiled_prompt": 10, "history_round": 32},
        "stable_prefix_hash": "",
        "prompt_owner": "ksadk",
        "history_owner": "ksadk",
        "compaction_owner": "ksadk",
        "memory_owner": "framework",
        "skill_owner": "framework",
    }
    _set_context_plan_attributes(span, plan)
    assert span.attrs["context.plan_id"] == "ctxplan_abc"
    assert span.attrs["context.planned_input_tokens"] == 42
    assert span.attrs["context.integration_mode"] == "framework_assisted"
    assert "compiled_prompt" in str(span.attrs["context.tokens_by_kind"])
    # 空 stable_prefix_hash 不落（_set_span_attribute 跳过空串）。
    assert "context.stable_prefix_hash" not in span.attrs

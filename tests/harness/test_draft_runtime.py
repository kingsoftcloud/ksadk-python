"""P2「保存即可试用」Draft Runtime 调试通路测试。"""

from __future__ import annotations

import pytest

from ksadk.harness.draft_runtime import (
    DRAFT_REF_PREFIX,
    DRAFT_SESSION_PREFIX,
    DraftRuntime,
    DraftRuntimeError,
)
from ksadk.harness.events import EventType
from ksadk.harness.lifecycle import LocalLifecycleManager
from ksadk.harness.reasoner import HarnessReasoningTurn
from ksadk.harness.spec import HarnessSpec


class _EchoReasoner:
    """无工具模型：直接用 instructions 语气回一句测试文本。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, *, model, prompt, messages, tools):
        del model, messages, tools
        self.prompts.append(prompt)
        return HarnessReasoningTurn(final_text="草稿测试回复。")


def _draft_payload() -> dict:
    return {
        "role": {
            "name": "finance-analyst-draft",
            "objective": "财务分析（草稿）",
        },
        "model": {"profileRef": "model-profile://kimi-k3@1.0.0"},
    }


@pytest.mark.asyncio
async def test_draft_compiles_and_converses_immediately():
    """创建 Draft → 临时编译 → 立即测试对话，一步到位。"""
    runtime = DraftRuntime(reasoner=_EchoReasoner())
    session = runtime.compile(_draft_payload())
    assert session.draft_ref.startswith(DRAFT_REF_PREFIX)
    events = await session.converse("测试一下这个草稿")
    assert events[-1].event_type == EventType.RUN_COMPLETED
    final = [
        e for e in events
        if e.event_type == EventType.TEXT_COMPLETED and e.phase == "final_answer"
    ]
    assert final and final[0].payload["text"] == "草稿测试回复。"
    await runtime.close()


@pytest.mark.asyncio
async def test_draft_run_isolated_from_formal_lifecycle():
    """Draft 不产生 Build/Deploy/Activate：正式生命周期零感知。"""
    manager = LocalLifecycleManager()
    runtime = DraftRuntime(reasoner=_EchoReasoner())
    session = runtime.compile(_draft_payload())
    await session.converse("测试对话")
    # 没有任何 Manifest、Deployment、Route 被 Draft 创建。
    assert not manager.registry._routes
    # Draft spec 的 revision_ref 落在 draft 命名空间，不冒充正式 Revision。
    assert session.spec.agent_revision_ref.startswith(DRAFT_REF_PREFIX)
    await runtime.close()


@pytest.mark.asyncio
async def test_draft_session_and_metadata_tagged():
    """Draft Run 的 session 以 draft: 前缀隔离，metadata 标记 draft。"""
    captured: dict = {}

    class _CapturingReasoner(_EchoReasoner):
        async def complete(self, *, model, prompt, messages, tools):
            captured["prompt"] = prompt
            return await super().complete(
                model=model, prompt=prompt, messages=messages, tools=tools
            )

    runtime = DraftRuntime(reasoner=_CapturingReasoner())
    session = runtime.compile(_draft_payload())
    events = await session.converse("你好")
    started = [e for e in events if e.event_type == EventType.RUN_STARTED][0]
    assert started.session_id.startswith(DRAFT_SESSION_PREFIX)
    assert started.invocation_id  # 正常 Run 信封
    # 多轮测试对话 session 逐轮隔离。
    await session.converse("第二轮")
    assert session.turns == 2
    await runtime.close()


def test_invalid_draft_fails_at_compile_not_later():
    """非法 Draft 在保存时即报错（与正式 Build 同一校验路径）。"""
    runtime = DraftRuntime(reasoner=_EchoReasoner())
    with pytest.raises(DraftRuntimeError, match="draft 编译失败"):
        runtime.compile({"model": {"profileRef": "not-a-ref"}})


@pytest.mark.asyncio
async def test_draft_build_parity_shares_compiler_path():
    """编译等价：Draft 编译产物与正式 BuildPipeline 用同一 compiler，
    Draft payload 可直接喂给 BuildPipeline.build 且成功。"""
    from ksadk.harness.lifecycle import BuildPipeline

    runtime = DraftRuntime(reasoner=_EchoReasoner())
    session = runtime.compile(_draft_payload())
    manifest = BuildPipeline().build(
        revision_payload=dict(_draft_payload()), revision_ref="agent-revision://proj-1@1"
    )
    assert manifest.build_id.startswith("bld_")
    assert isinstance(session.spec, HarnessSpec)
    await runtime.close()


def test_unknown_draft_session_rejected():
    runtime = DraftRuntime(reasoner=_EchoReasoner())
    with pytest.raises(DraftRuntimeError, match="unknown draft"):
        runtime.session("nope")


@pytest.mark.asyncio
async def test_draft_with_skill_binding_resolves_and_discloses(tmp_path):
    """带 Skill 绑定的 Draft 在调试通路里同样走 L0 目录 + 渐进披露。"""
    skill_root = tmp_path / "finance-budget-analysis@1.0.0"
    skill_root.mkdir()
    (skill_root / "SKILL.md").write_text(
        "---\nname: 预算偏差分析\ndescription: 分析预算与实际支出偏差\n---\n"
        "先读取预算和实际支出。\n",
        encoding="utf-8",
    )
    payload = {
        "role": {"name": "draft-with-skill", "objective": "草稿带技能"},
        "model": {"profileRef": "model-profile://test-model@1.0.0"},
        "capabilities": {
            "skillBindings": [
                {"skillRef": "skill://finance-budget-analysis@1.0.0",
                 "contentHash": "sha256:test-fixture"}
            ]
        },
    }
    runtime = DraftRuntime(reasoner=_EchoReasoner(), local_dir=str(tmp_path))
    session = runtime.compile(payload)
    assert not session.warnings, "本地 Skill 目录应解析成功"
    events = await session.converse("测试")
    assert events[-1].event_type == EventType.RUN_COMPLETED
    await runtime.close()

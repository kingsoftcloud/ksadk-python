"""Revision skillRefs → SkillRuntime → 默认 Agent Loop 全链路装配测试。

此前链路各段已就位（编译产出 skill_bindings；引擎侧 SkillDisclosureBridge
接入默认 Loop），缺的是中间装配层：本文件验证 ``ksadk.harness.skill_composition``
把 ``skill://name@version`` 解析为本地内容目录并构建 SkillRuntime，以及
子 Agent 对 SkillRuntime 的透传。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ksadk.harness.engine.langgraph import ManagedLangGraphEngine
from ksadk.harness.events import EventType
from ksadk.harness.reasoner import HarnessReasoningTurn, HarnessToolCall
from ksadk.harness.skill_composition import (
    SkillResolutionError,
    build_skill_runtime,
    compose_engine,
    parse_skill_ref,
    resolve_skill_roots,
)
from ksadk.harness.skill_runtime import SKILL_MANIFEST_TOOL, SkillRuntime
from ksadk.harness.spec import (
    CapabilityBinding,
    CapabilityBindings,
    HarnessSpec,
    ModelBinding,
    PromptSpec,
)
from ksadk.runtime import StartRequest

_SKILL_REF = "skill://budget-analysis@1.0.0"


def _write_skill(
    root: Path, *, name: str = "budget-analysis", description: str = "分析预算偏差"
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n"
        "先查预算与实际支出，再计算偏差率。\n",
        encoding="utf-8",
    )


def _spec(
    *, ref: str = _SKILL_REF, required: bool = True, load_policy: str = "on_demand"
) -> HarnessSpec:
    return HarnessSpec(
        agent_revision_ref="agent-revision://finance@1",
        model=ModelBinding(profile_ref="model-profile://test@1.0.0"),
        prompt=PromptSpec(instructions="你是财务助手。"),
        capabilities=CapabilityBindings(
            skill_bindings=(
                CapabilityBinding(
                    capability_ref=ref, required=required, load_policy=load_policy
                ),
            )
        ),
    )


# ------------------------------------------------------------ 解析


def test_parse_skill_ref():
    assert parse_skill_ref("skill://budget-analysis@1.0.0") == ("budget-analysis", "1.0.0")
    assert parse_skill_ref("skill://budget-analysis") == ("budget-analysis", "")
    with pytest.raises(SkillResolutionError):
        parse_skill_ref("mcp://not-a-skill")


def test_resolve_via_explicit_roots(tmp_path: Path):
    skill_root = tmp_path / "anywhere"
    _write_skill(skill_root)
    roots, warnings = resolve_skill_roots(
        _spec(), explicit_roots={_SKILL_REF: skill_root}
    )
    assert roots == {_SKILL_REF: skill_root.resolve()}
    assert warnings == []


def test_resolve_via_local_dir_name_version(tmp_path: Path):
    _write_skill(tmp_path / "budget-analysis@1.0.0")
    roots, _ = resolve_skill_roots(_spec(), local_dir=tmp_path)
    assert roots[_SKILL_REF].name == "budget-analysis@1.0.0"


def test_resolve_via_local_dir_frontmatter_fallback(tmp_path: Path):
    # 目录名任意，靠 SKILL.md frontmatter name 匹配。
    _write_skill(tmp_path / "odd-dir-name", name="budget-analysis")
    roots, _ = resolve_skill_roots(_spec(), local_dir=tmp_path)
    assert roots[_SKILL_REF].name == "odd-dir-name"


def test_required_unresolvable_raises(tmp_path: Path):
    with pytest.raises(SkillResolutionError, match="budget-analysis"):
        resolve_skill_roots(_spec(), local_dir=tmp_path)


def test_optional_unresolvable_degrades_with_warning(tmp_path: Path):
    roots, warnings = resolve_skill_roots(
        _spec(required=False), local_dir=tmp_path
    )
    assert roots == {}
    assert len(warnings) == 1
    assert "optional" in warnings[0]


def test_explicit_load_policy_skips_resolution(tmp_path: Path):
    roots, warnings = resolve_skill_roots(_spec(load_policy="explicit"), local_dir=tmp_path)
    assert roots == {}
    assert warnings == []


def test_build_skill_runtime_none_without_bindings(tmp_path: Path):
    spec = HarnessSpec(
        agent_revision_ref="agent-revision://x@1",
        model=ModelBinding(profile_ref="model-profile://test@1.0.0"),
        prompt=PromptSpec(instructions="i"),
    )
    assert build_skill_runtime(spec, local_dir=tmp_path) is None


# ------------------------------------------------------------ 全链路


class _ManifestThenFinalReasoner:
    """先读 Manifest（L1），再给最终答案——验证披露工具真的进了 Loop。"""

    def __init__(self) -> None:
        self.calls = 0
        self.first_messages: tuple[dict, ...] = ()

    async def complete(self, *, model, prompt, messages, tools):
        del model, prompt
        self.calls += 1
        if self.calls == 1:
            self.first_messages = tuple(messages)
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(
                        call_id="m1",
                        name=SKILL_MANIFEST_TOOL,
                        arguments={"skill_id": _SKILL_REF},
                    ),
                )
            )
        return HarnessReasoningTurn(final_text="完成。")


def test_compose_engine_end_to_end_disclosure_loop(tmp_path: Path):
    _write_skill(tmp_path / "budget-analysis@1.0.0")

    async def drive():
        reasoner = _ManifestThenFinalReasoner()
        engine = compose_engine(
            _spec(), reasoner=reasoner, local_dir=tmp_path
        )
        assert engine.skill_warnings == []
        compiled = await engine.compile(_spec())
        handle = await engine.start(
            StartRequest(
                agent_id="a1",
                user_id="u1",
                session_id="s1",
                input="分析预算",
                runtime_type="managed-langgraph",
            ),
            compiled,
        )
        events = [event async for event in engine.stream(handle)]
        return reasoner, engine, handle, events

    reasoner, engine, handle, events = asyncio.run(drive())

    # L0 目录进入首次模型输入（描述可见，正文不可见）。
    assert any("分析预算偏差" in str(m.get("content")) for m in reasoner.first_messages)
    assert all(
        "先查预算与实际支出" not in str(m.get("content")) for m in reasoner.first_messages
    )
    # L1 披露成功 + 内容哈希入事件（metadata-only）。
    disclosed = [e for e in events if e.event_type == EventType.SKILL_DISCLOSED]
    assert [e.payload["level"] for e in disclosed] == [1]
    assert str(disclosed[0].payload["content_hash"]).startswith("sha256:")
    assert engine._skill_runtime.level(handle.run_id, _SKILL_REF) == 1
    assert events[-1].event_type == EventType.RUN_COMPLETED


def test_compose_engine_required_unresolvable_raises_at_compose(tmp_path: Path):
    with pytest.raises(SkillResolutionError):
        compose_engine(_spec(), reasoner=_ManifestThenFinalReasoner(), local_dir=tmp_path)


# ------------------------------------------------------------ 子 Agent 透传


def test_child_spec_inherits_skill_bindings_and_engine_exposes_runtime(tmp_path: Path):
    from ksadk.harness.subagent import SubAgentSpec, child_spec

    parent = _spec()
    sub = SubAgentSpec(name="analyst", instructions="做分析")
    child = child_spec(parent, sub)
    assert child.capabilities.skill_bindings == parent.capabilities.skill_bindings

    # 引擎持有 _skill_runtime，run_subagent 据此透传给子引擎。
    _write_skill(tmp_path / "budget-analysis@1.0.0")
    engine = compose_engine(_spec(), reasoner=_ManifestThenFinalReasoner(), local_dir=tmp_path)
    assert isinstance(engine._skill_runtime, SkillRuntime)
    # 未装配 Skill 的引擎为 None（子引擎回退纯文本推理）。
    bare = ManagedLangGraphEngine(reasoner=_ManifestThenFinalReasoner())
    assert bare._skill_runtime is None

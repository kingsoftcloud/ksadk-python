"""Skill 渐进披露接入默认 Managed Agent Loop 的集成测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ksadk.harness.context_engine import ContextRequest, HarnessContextEngine
from ksadk.harness.engine.base import ExecutionEngineError
from ksadk.harness.engine.langgraph import ManagedLangGraphEngine
from ksadk.harness.events import EventType
from ksadk.harness.reasoner import HarnessReasoningTurn, HarnessToolCall
from ksadk.harness.skill_runtime import (
    SKILL_INSTRUCTIONS_TOOL,
    SKILL_MANIFEST_TOOL,
    SKILL_RESOURCE_TOOL,
    LocalSkillSource,
    SkillDisclosureError,
    SkillManifest,
    SkillRuntime,
)
from ksadk.harness.spec import (
    CapabilityBinding,
    CapabilityBindings,
    HarnessSpec,
    ModelBinding,
    PromptSpec,
)
from ksadk.harness.state import HarnessState
from ksadk.runtime import StartRequest

_SKILL_REF = "skill://finance-budget-analysis@1.0.0"


class _Source:
    def manifest(self, skill_id: str) -> SkillManifest:
        if skill_id != _SKILL_REF:
            raise KeyError(skill_id)
        return SkillManifest(
            name="预算分析",
            summary="分析预算与实际支出偏差",
            conditions="用户询问预算执行情况时使用",
            required_tools=("budget_lookup",),
        )

    def full_text(self, skill_id: str) -> str:
        if skill_id != _SKILL_REF:
            raise KeyError(skill_id)
        return "先查询预算与实际支出，再按 references/formula.md 计算偏差率并列出证据。"

    def resource(self, skill_id: str, resource_ref: str) -> bytes:
        if skill_id != _SKILL_REF or resource_ref != "references/formula.md":
            raise KeyError((skill_id, resource_ref))
        return b"variance = actual - budget"


class _DisclosureReasoner:
    def __init__(self) -> None:
        self.calls = 0
        self.messages: list[tuple[dict, ...]] = []
        self.tool_names: list[set[str]] = []

    async def complete(self, *, model, prompt, messages, tools):
        del model, prompt
        self.calls += 1
        self.messages.append(tuple(messages))
        self.tool_names.append({tool.openai_schema["function"]["name"] for tool in tools})
        if self.calls == 1:
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(
                        call_id="skill-manifest",
                        name=SKILL_MANIFEST_TOOL,
                        arguments={"skill_id": _SKILL_REF},
                    ),
                )
            )
        if self.calls == 2:
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(
                        call_id="skill-instructions",
                        name=SKILL_INSTRUCTIONS_TOOL,
                        arguments={"skill_id": _SKILL_REF},
                    ),
                )
            )
        if self.calls == 3:
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(
                        call_id="skill-resource",
                        name=SKILL_RESOURCE_TOOL,
                        arguments={
                            "skill_id": _SKILL_REF,
                            "resource_ref": "references/formula.md",
                        },
                    ),
                )
            )
        return HarnessReasoningTurn(final_text="已按预算分析 Skill 完成。")


def _spec(*, skill_ref: str = _SKILL_REF, required: bool = True) -> HarnessSpec:
    return HarnessSpec(
        agent_revision_ref="agent-revision://finance@1",
        model=ModelBinding(profile_ref="model-profile://test@1.0.0"),
        prompt=PromptSpec(instructions="你是财务助手。"),
        capabilities=CapabilityBindings(
            skill_bindings=(
                CapabilityBinding(
                    capability_ref=skill_ref,
                    required=required,
                    load_policy="on_demand",
                ),
            )
        ),
    )


def test_default_loop_discloses_skill_in_order_and_emits_metadata_only_events():
    async def drive():
        reasoner = _DisclosureReasoner()
        runtime = SkillRuntime(_Source())
        engine = ManagedLangGraphEngine(reasoner=reasoner, skill_runtime=runtime)
        compiled = await engine.compile(_spec())
        handle = await engine.start(
            StartRequest(
                agent_id="finance-agent",
                user_id="u1",
                session_id="s1",
                input="分析预算偏差",
                runtime_type="managed-langgraph",
            ),
            compiled,
        )
        events = [event async for event in engine.stream(handle)]
        return reasoner, runtime, handle, events

    reasoner, runtime, handle, events = asyncio.run(drive())

    first_messages = reasoner.messages[0]
    assert any(
        "分析预算与实际支出偏差" in str(message.get("content")) for message in first_messages
    )
    assert all(
        "先查询预算与实际支出" not in str(message.get("content")) for message in first_messages
    )
    assert {SKILL_MANIFEST_TOOL, SKILL_INSTRUCTIONS_TOOL}.issubset(reasoner.tool_names[0])

    disclosed = [event for event in events if event.event_type == EventType.SKILL_DISCLOSED]
    assert [event.payload["level"] for event in disclosed] == [1, 2, 3]
    assert all("instructions" not in event.payload for event in disclosed)
    assert all(str(event.payload["content_hash"]).startswith("sha256:") for event in disclosed)
    assert disclosed[-1].payload["resource_ref"] == "references/formula.md"
    assert runtime.level(handle.run_id, _SKILL_REF) == 3
    assert events[-1].event_type == EventType.RUN_COMPLETED


def test_context_engine_projects_only_level0_skill_catalog():
    plan = HarnessContextEngine().plan(
        ContextRequest(
            spec=_spec(),
            state=HarnessState(tenant_id="t1", user_id="u1", agent_id="a1", session_id="s1"),
            user_input="分析预算",
            context_window_tokens=32768,
            skill_catalog=(SkillRuntime(_Source()).catalog_entry(_SKILL_REF),),
        )
    )
    catalog = next(item for item in plan.selected if item.item_id == "skill_catalog")
    assert catalog.trust_level == "untrusted"
    assert "分析预算与实际支出偏差" in str(catalog.content)
    assert "先查询预算与实际支出" not in str(catalog.content)


def test_required_skill_without_runtime_fails_at_compile():
    async def compile_spec():
        await ManagedLangGraphEngine(reasoner=_DisclosureReasoner()).compile(_spec())

    with pytest.raises(ExecutionEngineError, match="未装配 SkillRuntime"):
        asyncio.run(compile_spec())


def test_unbound_skill_cannot_be_disclosed():
    async def invoke():
        engine = ManagedLangGraphEngine(
            reasoner=_DisclosureReasoner(), skill_runtime=SkillRuntime(_Source())
        )
        compiled = await engine.compile(_spec())
        handle = await engine.start(
            StartRequest(
                agent_id="a1",
                user_id="u1",
                session_id="s1",
                input="x",
                runtime_type="managed-langgraph",
            ),
            compiled,
        )
        run = engine._runs[handle.run_id]
        return engine._invoke_skill_tool(
            SKILL_MANIFEST_TOOL,
            {"skill_id": "skill://other-unbound@1.0.0"},
            run=run,
        )

    with pytest.raises(RuntimeError, match="未绑定"):
        asyncio.run(invoke())


def test_local_skill_source_reads_validated_package_and_blocks_path_escape(tmp_path: Path):
    skill_root = tmp_path / "budget-skill"
    resource = skill_root / "references" / "formula.md"
    resource.parent.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: 预算分析\ndescription: 分析预算偏差\n---\n"
        "读取 references/formula.md 后计算偏差。\n",
        encoding="utf-8",
    )
    resource.write_text("variance = actual - budget", encoding="utf-8")
    runtime = SkillRuntime(LocalSkillSource({_SKILL_REF: skill_root}))

    assert runtime.catalog_entry(_SKILL_REF)["summary"] == "分析预算偏差"
    runtime.level1("run-1", _SKILL_REF)
    runtime.level2("run-1", _SKILL_REF)
    assert runtime.level3("run-1", _SKILL_REF, "references/formula.md") == resource.read_bytes()

    with pytest.raises(SkillDisclosureError, match="未引用资源|路径越界"):
        runtime.level3("run-1", _SKILL_REF, "../secret.txt")

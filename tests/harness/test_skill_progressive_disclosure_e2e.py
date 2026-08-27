"""Revision 到默认 Agent Loop 的 Skill L0→L3 渐进披露 E2E。"""

from __future__ import annotations

import json

import pytest

from ksadk.harness.compiler import compile_revision_payload
from ksadk.harness.engine.langgraph import ManagedLangGraphEngine
from ksadk.harness.events import EventType
from ksadk.harness.lifecycle import BuildPipeline
from ksadk.harness.reasoner import HarnessReasoningTurn, HarnessToolCall
from ksadk.harness.skill_runtime import (
    SKILL_INSTRUCTIONS_TOOL,
    SKILL_MANIFEST_TOOL,
    SKILL_RESOURCE_TOOL,
    LocalSkillSource,
    SkillRuntime,
)
from ksadk.runtime import StartRequest

_REVISION_REF = "agent-revision://finance-budget-agent@7"
_SKILL_REF = "skill://finance-budget-analysis@1.0.0"
_RESOURCE_REF = "references/formula.md"


def _revision_payload() -> dict:
    return {
        "role": {
            "name": "finance-budget-agent",
            "objective": "根据预算与实际支出计算偏差，回答必须给出计算依据。",
        },
        "model": {"profileRef": "model-profile://test-model@1.0.0"},
        "capabilities": {
            "skillBindings": [
                {
                    "skillRef": _SKILL_REF,
                    "contentHash": "sha256:test-fixture",
                }
            ]
        },
    }


class _ProgressiveDisclosureModel:
    """根据已看到的披露内容选择下一步，模拟真实模型的工具决策。"""

    def __init__(self) -> None:
        self.tool_sequence: list[str] = []
        self.turn_snapshots: list[tuple[dict, ...]] = []
        self.level0_seen = False

    async def complete(self, *, model, prompt, messages, tools):
        del model, prompt
        snapshot = tuple(dict(message) for message in messages)
        self.turn_snapshots.append(snapshot)
        available = {tool.openai_schema["function"]["name"] for tool in tools}
        assert {
            SKILL_MANIFEST_TOOL,
            SKILL_INSTRUCTIONS_TOOL,
            SKILL_RESOURCE_TOOL,
        }.issubset(available)

        rendered = "\n".join(str(message.get("content") or "") for message in snapshot)
        tool_results = {
            str(message.get("name")): str(message.get("content") or "")
            for message in snapshot
            if message.get("role") == "tool"
        }

        if not tool_results:
            # L0 只包含名称和摘要；正文与资源不得提前进入模型上下文。
            assert _SKILL_REF in rendered
            assert "分析预算与实际支出偏差" in rendered
            assert "先读取预算和实际支出" not in rendered
            assert "variance_rate" not in rendered
            self.level0_seen = True
            return self._call(SKILL_MANIFEST_TOOL, {"skill_id": _SKILL_REF})

        if SKILL_INSTRUCTIONS_TOOL not in tool_results:
            manifest = json.loads(tool_results[SKILL_MANIFEST_TOOL])
            assert manifest["name"] == "预算偏差分析"
            assert manifest["required_tools"] == []
            assert "先读取预算和实际支出" not in rendered
            return self._call(SKILL_INSTRUCTIONS_TOOL, {"skill_id": _SKILL_REF})

        if SKILL_RESOURCE_TOOL not in tool_results:
            instructions = json.loads(tool_results[SKILL_INSTRUCTIONS_TOOL])["instructions"]
            assert "先读取预算和实际支出" in instructions
            assert _RESOURCE_REF in instructions
            assert "variance_rate" not in rendered
            return self._call(
                SKILL_RESOURCE_TOOL,
                {"skill_id": _SKILL_REF, "resource_ref": _RESOURCE_REF},
            )

        resource = json.loads(tool_results[SKILL_RESOURCE_TOOL])
        assert resource["encoding"] == "utf-8"
        assert "variance_rate = (actual - budget) / budget" in resource["content"]
        return HarnessReasoningTurn(final_text="已按 Skill 公式计算预算偏差率。")

    def _call(self, name: str, arguments: dict) -> HarnessReasoningTurn:
        self.tool_sequence.append(name)
        return HarnessReasoningTurn(
            tool_calls=(
                HarnessToolCall(
                    call_id=f"skill-call-{len(self.tool_sequence)}",
                    name=name,
                    arguments=arguments,
                ),
            )
        )


@pytest.mark.asyncio
async def test_revision_model_discloses_local_skill_l0_to_l3_end_to_end(tmp_path):
    skill_root = tmp_path / "finance-budget-analysis"
    resource_path = skill_root / _RESOURCE_REF
    resource_path.parent.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\n"
        "name: 预算偏差分析\n"
        "description: 分析预算与实际支出偏差\n"
        "---\n"
        "先读取预算和实际支出，再使用 references/formula.md 中的公式计算偏差率。\n",
        encoding="utf-8",
    )
    resource_path.write_text(
        "variance_rate = (actual - budget) / budget\n",
        encoding="utf-8",
    )

    revision_payload = _revision_payload()
    manifest = BuildPipeline().build(
        revision_payload=revision_payload,
        revision_ref=_REVISION_REF,
    )
    assert manifest.skill_refs == (_SKILL_REF,)

    spec = compile_revision_payload(revision_payload, revision_ref=_REVISION_REF)
    model = _ProgressiveDisclosureModel()
    skill_runtime = SkillRuntime(LocalSkillSource({_SKILL_REF: skill_root}))
    engine = ManagedLangGraphEngine(reasoner=model, skill_runtime=skill_runtime)
    compiled = await engine.compile(spec)
    handle = await engine.start(
        StartRequest(
            agent_id="finance-budget-agent",
            user_id="user-1",
            session_id="session-1",
            input="本月预算 100 万，实际支出 120 万，偏差率是多少？",
            runtime_type="managed-langgraph",
        ),
        compiled,
    )
    events = [event async for event in engine.stream(handle)]

    assert model.level0_seen is True
    assert model.tool_sequence == [
        SKILL_MANIFEST_TOOL,
        SKILL_INSTRUCTIONS_TOOL,
        SKILL_RESOURCE_TOOL,
    ]
    assert len(model.turn_snapshots) == 4

    disclosed = [event for event in events if event.event_type == EventType.SKILL_DISCLOSED]
    assert [event.payload["level"] for event in disclosed] == [1, 2, 3]
    assert [event.payload.get("resource_ref") for event in disclosed] == [None, None, _RESOURCE_REF]
    assert all(str(event.payload["content_hash"]).startswith("sha256:") for event in disclosed)
    assert all("content" not in event.payload for event in disclosed)

    tool_calls = [
        event.payload["name"] for event in events if event.event_type == EventType.TOOL_CALL_BEGIN
    ]
    assert tool_calls == model.tool_sequence
    assert any(
        event.event_type == EventType.TEXT_COMPLETED
        and event.payload.get("text") == "已按 Skill 公式计算预算偏差率。"
        for event in events
    )
    assert events[-1].event_type == EventType.RUN_COMPLETED

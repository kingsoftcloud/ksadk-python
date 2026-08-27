"""真实模型 Skill E2E 的离线回归（脚本化 reasoner 验证统计口径）。

真实模型端点跑法：``KSADK_REAL_MODEL_EVAL=1 python -m ksadk.harness.skill_e2e_eval``
（另有独立门控用例见本文件末尾）。这里用脚本化 reasoner 固定统计逻辑：
成功率、披露序列合规、越级访问、无效披露、Token 聚合。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.reasoner import HarnessReasoningTurn, HarnessToolCall
from ksadk.harness.skill_e2e_eval import (
    SKILL_E2E_DATASET,
    SkillE2ECase,
    analyze_case,
    run_case,
)
from ksadk.harness.skill_runtime import (
    SKILL_INSTRUCTIONS_TOOL,
    SKILL_MANIFEST_TOOL,
    SKILL_RESOURCE_TOOL,
)

_CASE = SKILL_E2E_DATASET[0]
_REF = _CASE.skill_ref


class _ScriptedReasoner:
    """按脚本依次发起工具调用，最后给最终答案。"""

    def __init__(self, calls: list[tuple[str, dict]], final_text: str) -> None:
        self._calls = calls
        self._final = final_text
        self.step = 0

    async def complete(self, *, model, prompt, messages, tools):
        del model, prompt, messages, tools
        if self.step < len(self._calls):
            name, arguments = self._calls[self.step]
            self.step += 1
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(call_id=f"c{self.step}", name=name, arguments=arguments),
                )
            )
        return HarnessReasoningTurn(final_text=self._final)


def _good_reasoner() -> _ScriptedReasoner:
    return _ScriptedReasoner(
        [
            (SKILL_MANIFEST_TOOL, {"skill_id": _REF}),
            (SKILL_INSTRUCTIONS_TOOL, {"skill_id": _REF}),
            (SKILL_RESOURCE_TOOL, {"skill_id": _REF, "resource_ref": _CASE.resource_ref}),
        ],
        "偏差率=20%（口径：标准预算偏差率），口径代号 STD-BUDGET-VAR-2026。",
    )


def test_dataset_shape():
    assert len(SKILL_E2E_DATASET) >= 3
    for case in SKILL_E2E_DATASET:
        assert case.resource_ref in case.body, f"{case.case_id} 的 SKILL.md 必须引用资源"
        assert case.expected_facts


def test_good_path_full_chain_success(tmp_path: Path):
    report = asyncio.run(run_case(_CASE, reasoner=_good_reasoner(), tmp_root=tmp_path))
    assert report.success
    assert report.levels_disclosed == [1, 2, 3]
    assert report.sequence_valid
    assert report.level_violations == 0
    assert report.invalid_disclosures == 0
    # Token 聚合口径由真实模型回填（脚本 reasoner 无 usage 事件）。


def test_skip_level_counted_as_violation(tmp_path: Path):
    """直接读 L3（未读 L1/L2）→ Loop 拦截，计越级、不计披露、任务失败。"""
    reasoner = _ScriptedReasoner(
        [(SKILL_RESOURCE_TOOL, {"skill_id": _REF, "resource_ref": _CASE.resource_ref})],
        "我不知道口径。",
    )
    report = asyncio.run(run_case(_CASE, reasoner=reasoner, tmp_root=tmp_path))
    assert not report.success
    assert report.levels_disclosed == []
    assert report.level_violations == 1
    assert report.invalid_disclosures == 0


def test_unbound_skill_counted_as_invalid(tmp_path: Path):
    reasoner = _ScriptedReasoner(
        [(SKILL_MANIFEST_TOOL, {"skill_id": "skill://unbound@1.0.0"})],
        "没有可用 Skill。",
    )
    report = asyncio.run(run_case(_CASE, reasoner=reasoner, tmp_root=tmp_path))
    assert not report.success
    assert report.level_violations == 0
    assert report.invalid_disclosures == 1


def test_repeated_read_keeps_sequence_valid():
    """重复读同层不破坏序列（0 差分允许），跳级（差分>1）判不合规。"""
    case = SkillE2ECase(
        case_id="x",
        skill_ref="skill://x@1",
        skill_name="x",
        description="x",
        body="b",
        resource_ref="r.md",
        resource_content="c",
        task="t",
        expected_facts=("c",),
    )

    def _disclosed_event(level: int) -> RuntimeEvent:
        return RuntimeEvent.create(
            EventType.SKILL_DISCLOSED,
            agent_id="a",
            user_id="u",
            session_id="s",
            invocation_id="r",
            seq_id=level,
            payload={"skill_ref": "skill://x@1", "level": level, "content_hash": "sha256:x",
                     "size_bytes": 1},
        )

    ok = analyze_case(case, [_disclosed_event(1), _disclosed_event(1), _disclosed_event(2)], "c")
    assert ok.sequence_valid and ok.levels_disclosed == [1, 1, 2]
    bad = analyze_case(case, [_disclosed_event(1), _disclosed_event(3)], "c")
    assert not bad.sequence_valid


@pytest.mark.parametrize("case", SKILL_E2E_DATASET)
def test_real_model_skill_e2e(case, tmp_path: Path):
    """门控用例：真实模型自主走 L0→L1→L2→L3 并命中关键事实。"""
    import os

    if not os.getenv("KSADK_REAL_MODEL_EVAL"):
        pytest.skip("需要真实模型端点（KSADK_REAL_MODEL_EVAL=1）")
    from ksadk.harness.real_model_eval import RealModelReasoner

    report = asyncio.run(run_case(case, reasoner=RealModelReasoner(), tmp_root=tmp_path))
    assert report.success, report.final_answer
    assert report.levels_disclosed == [1, 2, 3]
    assert report.level_violations == 0

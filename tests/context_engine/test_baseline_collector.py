"""BaselineCollector 采集器单测（评测方案 §10 阶段 1 基线采集）。"""

from __future__ import annotations

import json
from pathlib import Path

from ksadk.context_engine.baseline import BaselineCollector
from ksadk.context_engine.shadow_plan import build_shadow_context_plan_dict


def _plan(runtime_type: str = "langgraph", instructions: str = "你是助手") -> dict:
    return build_shadow_context_plan_dict(
        instructions=instructions, user_input="hi", runtime_type=runtime_type
    )


def test_record_turn_populates_fields_from_shadow_plan() -> None:
    collector = BaselineCollector(execution_target="local")
    plan = _plan("langgraph")
    record = collector.record_turn(
        plan,
        session_id="s1",
        invocation_id="i1",
        model="m",
        usage={
            "input_tokens": 120,
            "output_tokens": 40,
            "input_token_details": {"cached_tokens": 60},
        },
        turn_latency_ms=320,
    )
    assert record.runner_type == "langgraph"
    assert record.accounting_accuracy == "estimated"
    assert record.capability_hash.startswith("sha256:")
    assert record.prompt_content_hash.startswith("sha256:")
    assert record.tokens_by_kind["compiled_prompt"] > 0
    assert record.planned_input_tokens > 0
    assert record.runtime_reported_input_tokens == 120
    assert record.runtime_output_tokens == 40
    assert record.cache_read_tokens == 60
    assert record.ksadk_version  # 自动填充


def test_record_turn_with_none_plan_marks_opaque() -> None:
    collector = BaselineCollector()
    record = collector.record_turn(None, session_id="s", invocation_id="i")
    assert record.accounting_accuracy == "opaque"
    assert record.tokens_by_kind == {}


def test_summary_computes_ptl_and_opaque_rates() -> None:
    collector = BaselineCollector()
    collector.record_turn(_plan(), prompt_too_long=False, turn_latency_ms=100)
    collector.record_turn(_plan(), prompt_too_long=True, retry_attempts=1, turn_latency_ms=500)
    collector.record_turn(None)  # opaque
    summary = collector.summary()
    assert summary["turn_count"] == 3
    assert summary["ptl_rate"] == round(1 / 3, 4)
    assert summary["opaque_request_rate"] == round(1 / 3, 4)
    assert summary["compaction_count"] == 0
    assert summary["turn_latency_ms"]["count"] == 2


def test_summary_tracks_stable_prefix_hash_changes_and_runner_breakdown() -> None:
    collector = BaselineCollector()
    collector.record_turn(_plan("langgraph", instructions="A"))
    collector.record_turn(_plan("langgraph", instructions="A"))  # 相同 stable prefix
    collector.record_turn(_plan("codex", instructions="B"))
    summary = collector.summary()
    # request_instructions 是 volatile，stable_prefix_hash 为空 → changes=0（honest）
    assert summary["stable_prefix_hash_changes"] == 0
    assert summary["runner_type_breakdown"] == {"langgraph": 2, "codex": 1}


def test_dump_writes_jsonl_with_summary_line(tmp_path: Path) -> None:
    collector = BaselineCollector()
    collector.record_turn(_plan(), session_id="s", invocation_id="i", turn_latency_ms=10)
    out = collector.dump(tmp_path / "baseline.jsonl")
    assert out.exists()
    lines = out.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2  # 1 turn + 1 summary
    turn = json.loads(lines[0])
    assert turn["runner_type"] == "langgraph"
    summary = json.loads(lines[1])
    assert "__summary__" in summary
    assert summary["__summary__"]["turn_count"] == 1


def test_dump_does_not_contain_prompt_plaintext(tmp_path: Path) -> None:
    """安全要求 §19：基线只记 hash/计数/分类，不含 Prompt 正文。"""
    collector = BaselineCollector()
    secret = "SECRET-INSTRUCTION-NEVER-LEAK"
    collector.record_turn(_plan(instructions=secret))
    out = collector.dump(tmp_path / "baseline.jsonl")
    content = out.read_text(encoding="utf-8")
    assert secret not in content
    assert "sha256:" in content  # 只记 hash

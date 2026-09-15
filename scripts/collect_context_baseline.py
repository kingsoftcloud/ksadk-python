"""Shadow 基线采集脚本（评测方案 §10 阶段 1：建立 Baseline）。

在 shadow 阶段（不改运行行为）用固定 Fixture 跑多个 Runner / Case 组合，采集每次 turn
的 shadow plan 信号 + 模拟 runtime usage/compaction/PTL，落盘 JSONL 作为后续 A/B 对比基准。

用法::

    uv run python scripts/collect_context_baseline.py --out /tmp/ksadk-baseline.jsonl

注意：本脚本只采集"可观测信号"，不调用真实模型。runtime usage / compaction / PTL 用
Fixture 注入的确定性信号模拟（标注 ``execution_target=local-shadow-sim``），供结构对比；
真实运行时 usage 需在接入真实模型后由 RuntimeAdapter 回报填充。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from ksadk.context_engine.baseline import BaselineCollector
from ksadk.conversations.runtime_preparation import build_run_input
from ksadk.sessions.in_memory import InMemorySessionService


# 固定 Fixture（评测方案 §3 A/B 必须保持相同；§7.4 Fixture 占位）。
FIXTURE_CASES = [
    {
        "case_id": "PCM-BASE-001",
        "runner_type": "langgraph",
        "instructions": "你是 Python 升级助手，默认 Python 3.12 和 uv。",
        "messages": [{"role": "user", "content": "请把这个项目升级到稳定的 Python 环境，并修复测试。"}],
        "usage": {"input_tokens": 240, "output_tokens": 80, "input_token_details": {"cached_tokens": 120}},
        "compaction_triggered": False,
        "ptl": False,
        "latency_ms": 410,
    },
    {
        "case_id": "PCM-CONTEXT-001",
        "runner_type": "langgraph",
        "instructions": "你是助手，保留关键错误，丢弃重复日志。",
        "messages": [{"role": "user", "content": "运行测试并修复失败。"}],
        "usage": {"input_tokens": 52000, "output_tokens": 200, "input_token_details": {"cached_tokens": 0}},
        "compaction_triggered": False,
        "ptl": True,  # 单个大型 tool result 挤爆
        "latency_ms": 1800,
    },
    {
        "case_id": "PCM-CONTEXT-002",
        "runner_type": "langgraph",
        "instructions": "你是助手。",
        "messages": [{"role": "user", "content": "只升级运行环境，不要升级业务依赖版本。"}],
        "usage": {"input_tokens": 300, "output_tokens": 60},
        "compaction_triggered": True,
        "ptl": False,
        "latency_ms": 520,
    },
    {
        "case_id": "PCM-RUNNER-001",
        "runner_type": "codex",
        "instructions": "你是 codex 助手。",
        "messages": [{"role": "user", "content": "继续之前的任务。"}],
        "usage": {"input_tokens": 180, "output_tokens": 50, "cache_read_input_tokens": 90},
        "compaction_triggered": False,
        "ptl": False,
        "latency_ms": 300,
    },
    {
        "case_id": "PCM-RUNNER-002-adk",
        "runner_type": "adk",
        "instructions": "你是 ADK 助手。",
        "messages": [{"role": "user", "content": "分析项目依赖。"}],
        "usage": {"input_tokens": 220, "output_tokens": 70},
        "compaction_triggered": False,
        "ptl": False,
        "latency_ms": 350,
    },
]


async def _run_case(case: dict, service: InMemorySessionService, session_id: str) -> dict:
    prepared = await build_run_input(
        agent_id="baseline-agent",
        user_id="baseline-user",
        session_id=session_id,
        messages=case["messages"],
        model="baseline-model",
        instructions=case["instructions"],
        session_service_provider=lambda: service,
        runtime_type=case["runner_type"],
    )
    return {"shadow_plan": prepared.shadow_context_plan, "invocation_id": prepared.invocation_id}


async def main_async(out_path: Path, execution_target: str) -> int:
    collector = BaselineCollector(execution_target=execution_target)
    service = InMemorySessionService()
    for index, case in enumerate(FIXTURE_CASES):
        session_id = f"baseline-session-{index}"
        await service.create_session(
            agent_id="baseline-agent", user_id="baseline-user", session_id=session_id
        )
        result = await _run_case(case, service, session_id)
        collector.record_turn(
            result["shadow_plan"],
            session_id=session_id,
            invocation_id=result["invocation_id"],
            model="baseline-model",
            usage=case["usage"],
            compaction_triggered=case["compaction_triggered"],
            compaction_trigger="auto" if case["compaction_triggered"] else "",
            prompt_too_long=case["ptl"],
            retry_attempts=1 if case["ptl"] else 0,
            turn_latency_ms=case["latency_ms"],
        )
        print(f"  collected {case['case_id']}: runner={case['runner_type']} accuracy={result['shadow_plan']['accounting_accuracy']}")

    dumped = collector.dump(out_path)
    summary = collector.summary()
    print(f"\n基线已落盘: {dumped}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="采集 Prompt/Context/Memory shadow 基线")
    parser.add_argument(
        "--out",
        default="/tmp/ksadk-context-baseline.jsonl",
        help="输出 JSONL 路径（每行一条 turn 记录 + 末尾 __summary__）",
    )
    parser.add_argument(
        "--execution-target",
        default="local-shadow-sim",
        help="执行目标标识（评测方案 §3 execution_target）",
    )
    args = parser.parse_args()
    return asyncio.run(main_async(Path(args.out), args.execution_target))


if __name__ == "__main__":
    sys.exit(main())

"""真实模型基线采集脚本（评测方案 §10 阶段 1：建立 Baseline）。

用真实模型（minimax openai 兼容端点，凭证来自 ANTHROPIC_AUTH_TOKEN/ANTHROPIC_BASE_URL）
经 LangGraphRunner + invoke_conversation_once 跑多个 turn，触发 env-gated baseline 旁路，
落盘 JSONL + summary 作为后续 A/B 对比基准。

环境要求（缺失则如实退出，不假装能跑）：
- ANTHROPIC_AUTH_TOKEN / ANTHROPIC_MODEL（模型名）
- KSADK_BASELINE_COLLECT=1
- KSADK_BASELINE_PATH（可选，默认 /tmp/ksadk-real-model-baseline.jsonl）

用法::

    KSADK_BASELINE_COLLECT=1 .venv/bin/python scripts/collect_real_model_baseline.py

注意：本脚本会真实调用外部模型 API（产生真实 token 用量与延迟）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def _required_env() -> tuple[dict[str, str], list[str]]:
    env = {
        "api_key": os.environ.get("ANTHROPIC_AUTH_TOKEN", ""),
        "model": os.environ.get("ANTHROPIC_MODEL", os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "")),
    }
    missing = [k for k, v in env.items() if not v]
    return env, missing


def _build_llm(env: dict[str, str]) -> Any:
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=env["model"],
        api_key=env["api_key"],
        base_url="https://api.minimaxi.com/v1",
        temperature=0,
        max_tokens=256,
    )


def _build_runner(llm: Any) -> Any:
    from ksadk.runners.base_runner import BaseRunner

    class _RealModelRunner(BaseRunner):
        """最小自定义 runner：直接调 ChatOpenAI，不依赖项目文件加载。

        detection_result.type.value='langgraph' → capability 走 framework_assisted/estimated，
        与真实 LangGraphRunner 同类（history 由 runner 组装）。
        """

        def __init__(self, llm: Any) -> None:
            super().__init__(
                SimpleNamespace(type=SimpleNamespace(value="langgraph")),
                ".",
            )
            self._llm = llm
            self._agent = llm  # 标记已加载

        def load_agent(self) -> None:
            return None

        async def invoke(self, input_data: dict[str, Any]) -> dict[str, Any]:
            history = list(input_data.get("history") or [])
            instructions = str(input_data.get("instructions") or "")
            messages: list = []
            if instructions:
                messages.append(("system", instructions))
            for turn in history:
                role = turn.get("role")
                content = turn.get("content", "")
                if role and content:
                    messages.append((role, content))
            user_input = str(input_data.get("input") or "")
            if user_input:
                messages.append(("user", user_input))
            response = self._llm.invoke(messages)
            text = getattr(response, "content", str(response))
            usage = getattr(response, "usage_metadata", None) or {}
            return {"output": text, "usage": dict(usage)}

        async def stream(self, input_data: dict[str, Any]):  # pragma: no cover - baseline 用非流式
            result = await self.invoke(input_data)
            yield {"type": "final", "output": result["output"]}

    return _RealModelRunner(llm)


# 固定 Fixture（评测方案 §3 A/B 必须相同；§7.4 占位）。每个 case 跑真实模型 turn。
REAL_CASES = [
    {
        "case_id": "REAL-001",
        "instructions": "你是助手，用中文简短回答。",
        "messages": [{"role": "user", "content": "用一句话介绍 Python 的 GIL。"}],
    },
    {
        "case_id": "REAL-002",
        "instructions": "你是助手，严格遵守约束：不得修改生产配置；用 uv run 跑测试。",
        "messages": [{"role": "user", "content": "把这个项目升级到 Python 3.12 并修复测试，先分析不要改文件。"}],
    },
    {
        "case_id": "REAL-003",
        "instructions": "你是助手。",
        "messages": [{"role": "user", "content": "请重复一遍：OK"}],
    },
]


async def _run_case(case: dict, runner: Any, service: Any, session_id: str) -> None:
    from ksadk.conversations.runtime_invocation import invoke_conversation_once

    _, result = await invoke_conversation_once(
        runner=runner,
        agent_id="real-baseline-agent",
        user_id="real-baseline-user",
        session_id=session_id,
        messages=case["messages"],
        model=os.environ.get("ANTHROPIC_MODEL", ""),
        prepare_runner=lambda _runner, _model: None,
        instructions=case["instructions"],
        session_service_provider=lambda: service,
    )
    print(f"  {case['case_id']}: output={result.get('output_text','')[:40]!r}")


async def main_async(out_path: Path) -> int:
    env, missing = _required_env()
    if missing:
        print(f"缺少必要环境变量: {missing}（ANTHROPIC_AUTH_TOKEN / ANTHROPIC_MODEL）", file=sys.stderr)
        print("无法跑真实模型基线，如实退出（不假装能跑）。", file=sys.stderr)
        return 2
    if not os.environ.get("KSADK_BASELINE_COLLECT", "").strip().lower() in ("1", "true", "yes", "on"):
        print("提示：KSADK_BASELINE_COLLECT 未开启，将不会落盘采集（仍会真实调模型）。", file=sys.stderr)

    from ksadk.context_engine.baseline import get_baseline_collector
    from ksadk.sessions.in_memory import InMemorySessionService

    llm = _build_llm(env)
    runner = _build_runner(llm)
    service = InMemorySessionService()

    start = time.monotonic()
    for index, case in enumerate(REAL_CASES):
        session_id = f"real-baseline-{index}"
        await service.create_session(agent_id="real-baseline-agent", user_id="real-baseline-user", session_id=session_id)
        await _run_case(case, runner, service, session_id)
    elapsed = int((time.monotonic() - start) * 1000)

    collector = get_baseline_collector()
    if collector is None or not collector.records:
        print("\n基线采集未启用或无记录。设置 KSADK_BASELINE_COLLECT=1 以落盘。")
        return 0

    dumped = collector.dump(out_path)
    summary = collector.summary()
    print(f"\n真实模型基线已落盘: {dumped} (耗时 {elapsed}ms)")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    out = os.environ.get("KSADK_BASELINE_PATH", "/tmp/ksadk-real-model-baseline.jsonl")
    return asyncio.run(main_async(Path(out)))


if __name__ == "__main__":
    sys.exit(main())

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
                # 历史投影里 assistant_message → role="model"（ksadk 内部口径），
                # LangChain ChatOpenAI 只认 "assistant"，这里归一化。
                if role == "model":
                    role = "assistant"
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
        "agent_system": "你是 Python 升级助手，用中文简短回答。",
        "agent_task": "默认使用 Python 3.12 和 uv。",
        "instructions": "本次问题：用一句话介绍 Python 的 GIL。",
        "messages": [{"role": "user", "content": "用一句话介绍 Python 的 GIL。"}],
    },
    {
        "case_id": "REAL-002",
        "agent_system": "你是助手，严格遵守约束。",
        "agent_task": "不得修改生产配置；用 uv run 跑测试。",
        "instructions": "本次问题：分析升级方案，先不要改文件。",
        "messages": [{"role": "user", "content": "把这个项目升级到 Python 3.12 并修复测试，先分析不要改文件。"}],
    },
    {
        "case_id": "REAL-003",
        "agent_system": "你是助手。",
        "agent_task": "",
        "instructions": "本次问题：请重复一遍 OK。",
        "messages": [{"role": "user", "content": "请重复一遍：OK"}],
    },
]


async def _run_case(case: dict, runner: Any, service: Any, session_id: str) -> None:
    from ksadk.conversations.runtime_invocation import invoke_conversation_once

    # PR B：可选接管。设 KSADK_PROMPT_INTEGRATION_MODE=ksadk_hosted（配合
    # KSADK_PROMPT_COMPILER_ENABLED=1）时，invoke_conversation_once 把
    # payload["instructions"] 替换为 CompiledPrompt.content（XML），agent_system/task
    # 首次进模型输入。不设则与 PR A 基线一致（payload instructions == request instructions）。
    prompt_integration_mode = os.environ.get("KSADK_PROMPT_INTEGRATION_MODE", "").strip()

    _, result = await invoke_conversation_once(
        runner=runner,
        agent_id="real-baseline-agent",
        user_id="real-baseline-user",
        session_id=session_id,
        messages=case["messages"],
        model=os.environ.get("ANTHROPIC_MODEL", ""),
        prepare_runner=lambda _runner, _model: None,
        instructions=case["instructions"],
        # PR A：agent_system/agent_task 进 prompt source contract（编译真实 CompiledPrompt，
        # 含 stable section → stable_prefix_hash 非空）。不改 Runner 输入（payload instructions 不变）。
        agent_system=case.get("agent_system", ""),
        agent_task=case.get("agent_task", ""),
        # PR B：per-Build 接管标记。ksadk_hosted → 触发接管（flag 同时需开）。
        prompt_integration_mode=prompt_integration_mode,
        session_service_provider=lambda: service,
    )
    print(f"  {case['case_id']}: output={result.get('output_text','')[:40]!r}")


async def _run_prd_long_history_case(runner: Any, service: Any, session_id: str) -> None:
    """PR D 真实模型验证：长 transcript 触发 proactive compact（D1 双阈值）+
    checkpoint 写 working_state（D2），经一个真实模型 turn 落地。

    预置 6 轮长历史（合成文本，非模型生成——只为撑过 soft/hard 阈值），
    再跑一个真实 turn。验证点（打印，非断言）：
    - 触发 proactive compact（trigger_band ∈ soft/hard，非 emergency/none）
    - checkpoint metadata 含 working_state.content_hash / source_seq_range / status
    本函数只消耗 1 个真实 turn；预置历史不调模型。
    """
    from ksadk.conversations.context import canonical_event_type
    from ksadk.conversations.runtime_invocation import invoke_conversation_once
    from ksadk.sessions import SessionEvent

    # 6 轮 × (60k user + 60k assistant) ≈ 720k chars ≈ 180k tokens
    # （tokenizer ~4 chars/token）> hard_limit ~167k → trigger_band="hard"。
    for i in range(6):
        await service.append_event(
            session_id,
            SessionEvent(
                id=f"prd-u{i}",
                seq_id=i * 2 + 1,
                event_type="user_message",
                author="user",
                invocation_id=f"prd-{i}",
                content={"role": "user", "parts": [{"text": "x" * 60_000}]},
            ),
        )
        await service.append_event(
            session_id,
            SessionEvent(
                id=f"prd-a{i}",
                seq_id=i * 2 + 2,
                event_type="assistant_message",
                author="runner",
                invocation_id=f"prd-{i}",
                content={"role": "model", "parts": [{"text": "y" * 60_000}]},
            ),
        )

    prompt_integration_mode = os.environ.get("KSADK_PROMPT_INTEGRATION_MODE", "").strip()
    _, result = await invoke_conversation_once(
        runner=runner,
        agent_id="real-baseline-agent",
        user_id="real-baseline-user",
        session_id=session_id,
        messages=[{"role": "user", "content": "继续。"}],
        model=os.environ.get("ANTHROPIC_MODEL", ""),
        prepare_runner=lambda _runner, _model: None,
        instructions="本次问题：用一句话回答 OK。",
        agent_system="你是助手，用中文简短回答。",
        agent_task="保持简短。",
        prompt_integration_mode=prompt_integration_mode,
        session_service_provider=lambda: service,
    )

    events = await service.get_events(session_id)
    checkpoint = next(
        (e for e in reversed(events) if canonical_event_type(e.event_type) == "context_checkpoint"),
        None,
    )
    print("  PRD-001: 长历史 proactive compact 验证")
    if checkpoint is None:
        print("    [未触发 compact] 预置历史 token 估算未超阈值，或门控未开。")
        return
    meta = checkpoint.metadata or {}
    print(f"    trigger_band={meta.get('trigger_band')!r}  trigger={meta.get('trigger')!r}")
    ws = meta.get("working_state")
    if ws is None:
        print("    [无 working_state] 非 ksadk_hosted 路径（向后兼容，预期不写该键）。")
    else:
        print(
            f"    working_state: status={ws.get('status')!r} "
            f"source_seq_range={ws.get('source_seq_range')!r} "
            f"content_hash={(ws.get('content_hash') or '')[:14]}..."
        )
    print(f"    output={result.get('output_text','')[:40]!r}")


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
    # PR D 长历史验证（可选，env 开启；不开启则保持廉价 3-turn B 基线不变）。
    if os.environ.get("KSADK_BASELINE_PRD", "").strip().lower() in ("1", "true", "yes", "on"):
        prd_session = "real-baseline-prd"
        await service.create_session(
            agent_id="real-baseline-agent",
            user_id="real-baseline-user",
            session_id=prd_session,
        )
        await _run_prd_long_history_case(runner, service, prd_session)
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

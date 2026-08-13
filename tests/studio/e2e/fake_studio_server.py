"""Deterministic Studio server used by the internal-browser smoke test."""

from __future__ import annotations

import argparse
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import uvicorn

from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.runtime import RunHandle, StartRequest
from ksadk.studio.api import create_studio_app
from ksadk.studio.service import StudioService
from tests.studio.runtime_adapter_fixtures import RuntimeFixture


def _runtime_inspector(_runtime: object) -> tuple[str, str, str]:
    return "0.8.0", "0.144.4", "codex-cli 0.144.4"


async def _browser_runtime_events(
    request: StartRequest,
    handle: RunHandle,
) -> AsyncIterator[RuntimeEvent]:
    common: dict[str, Any] = {
        "agent_id": request.agent_id or "agent",
        "user_id": request.user_id,
        "session_id": request.session_id,
        "invocation_id": handle.run_id,
    }
    yield RuntimeEvent.create(
        EventType.RUN_STARTED,
        **common,
        seq_id=1,
        payload={"status": "in_progress"},
    )
    yield RuntimeEvent.create(
        EventType.REASONING_DELTA,
        **common,
        seq_id=2,
        phase="commentary",
        payload={"text": "正在读取 src/demo.py"},
    )
    yield RuntimeEvent.create(
        EventType.RUN_PROGRESS,
        **common,
        seq_id=3,
        payload={
            "native_event": "proxy.requested",
            "native_data": {
                "responseId": "resp_browser_smoke",
                "model": "glm-5.2",
                "protocol": "responses-to-chat",
                "stream": True,
            },
        }
    )
    yield RuntimeEvent.create(
        EventType.RUN_PROGRESS,
        **common,
        seq_id=4,
        payload={
            "native_event": "proxy.upstream",
            "native_data": {
                "requestId": "req_browser_smoke",
                "statusCode": 200,
            },
        }
    )
    yield RuntimeEvent.create(
        EventType.TOOL_CALL_BEGIN,
        **common,
        seq_id=5,
        payload={
            "call_id": "cmd-browser-smoke",
            "name": "codex.command",
            "args": {
                "command": "sed -n '1,80p' src/demo.py",
                "cwd": str(request.config.get("cwd") or ""),
                "command_actions": [{"type": "read", "path": "src/demo.py"}],
            },
        }
    )
    yield RuntimeEvent.create(
        EventType.RUN_PROGRESS,
        **common,
        seq_id=6,
        payload={
            "native_event": "proxy.completed",
            "native_data": {
                "responseId": "resp_browser_smoke",
                "model": "glm-5.2",
                "statusCode": 200,
                "durationMs": 900,
                "usage": {
                    "inputTokens": 128,
                    "outputTokens": 32,
                    "totalTokens": 160,
                    "cachedInputTokens": 16,
                    "reasoningOutputTokens": 8,
                },
            },
        }
    )
    yield RuntimeEvent.create(
        EventType.TOOL_CALL_END,
        **common,
        seq_id=7,
        payload={
            "call_id": "cmd-browser-smoke",
            "name": "codex.command",
            "result": {
                "status": "completed",
                "exit_code": 0,
                "duration_ms": 210,
            },
        }
    )
    yield RuntimeEvent.create(
        EventType.USAGE_REPORTED,
        **common,
        seq_id=8,
        payload={
                "input_tokens": 128,
                "output_tokens": 32,
                "total_tokens": 160,
                "cached_tokens": 16,
                "reasoning_tokens": 8,
                "source": "browser-fixture",
        }
    )
    yield RuntimeEvent.create(
        EventType.TEXT_DELTA,
        **common,
        seq_id=9,
        phase="final_answer",
        payload={"text": "## 审查结果\n\n发现空列表会触发除零风险。"},
    )
    yield RuntimeEvent.create(
        EventType.TEXT_COMPLETED,
        **common,
        seq_id=10,
        phase="final_answer",
        payload={
            "text": (
                "## 审查结果\n\n**确定问题**：空列表会触发除零风险。\n\n"
                "建议在计算平均值前处理空列表。"
            ),
        }
    )
    yield RuntimeEvent.create(
        EventType.RUN_COMPLETED,
        **common,
        seq_id=11,
        payload={
            "status": "completed",
            "duration_ms": 1340,
            "started_at": "2026-08-04T04:00:00Z",
            "completed_at": "2026-08-04T04:00:01.340000Z",
            "source": "browser-fixture",
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    service = StudioService(
        args.workspace,
        codex_runtime_inspector=_runtime_inspector,
        runtime_executor=RuntimeFixture(_browser_runtime_events).executor,
    )
    app = create_studio_app(
        args.workspace,
        service=service,
        security_enabled=False,
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

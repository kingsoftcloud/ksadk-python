"""Deterministic Studio server used by the internal-browser smoke test."""

from __future__ import annotations

import argparse
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import uvicorn

from ksadk.events.canonical import (
    ContentSnapshot,
    ItemCompleted,
    ItemStarted,
    ItemUpdated,
    OutputRef,
    RunCompleted,
    RunProgress,
    RunStarted,
    RuntimeEvent,
    SourceRef,
    UsageReported,
)
from ksadk.events.content import TextContent, ToolCallContent, ToolResultContent
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
        "schema_version": 2,
        "timestamp": 1.0,
        "run_id": handle.run_id,
        "scope_id": f"scope-{handle.run_id}",
    }
    source = SourceRef(framework="codex")
    yield RunStarted(
        event_id="e1",
        seq=1,
        status="running",
        source=source,
        **common,
    )
    yield ItemUpdated(
        event_id="e2",
        seq=2,
        item_id="reasoning-1",
        item_kind="reasoning",
        op="append",
        update=TextContent(part_id="text-0", text="正在读取 src/demo.py"),
        source=source,
        **common,
    )
    yield RunProgress(
        event_id="e3",
        seq=3,
        status="running",
        source=SourceRef(
            framework="codex",
            metadata={
                "native_event": "proxy.requested",
                "native_data": {
                    "responseId": "resp_browser_smoke",
                    "model": "glm-5.2",
                    "protocol": "responses-to-chat",
                    "stream": True,
                },
            },
        ),
        **common,
    )
    yield RunProgress(
        event_id="e4",
        seq=4,
        status="running",
        source=SourceRef(
            framework="codex",
            metadata={
                "native_event": "proxy.upstream",
                "native_data": {
                    "requestId": "req_browser_smoke",
                    "statusCode": 200,
                },
            },
        ),
        **common,
    )
    tool_args = {
        "command": "sed -n '1,80p' src/demo.py",
        "cwd": str(request.config.get("cwd") or ""),
        "command_actions": [{"type": "read", "path": "src/demo.py"}],
    }
    yield ItemStarted(
        event_id="e5",
        seq=5,
        item_id="tool-cmd-browser-smoke",
        item_kind="tool_call",
        initial=ContentSnapshot(
            parts=(
                ToolCallContent(
                    part_id="tool-0",
                    call_id="cmd-browser-smoke",
                    name="codex.command",
                    arguments=tool_args,
                ),
            )
        ),
        source=source,
        **common,
    )
    yield RunProgress(
        event_id="e6",
        seq=6,
        status="running",
        source=SourceRef(
            framework="codex",
            metadata={
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
            },
        ),
        **common,
    )
    yield ItemCompleted(
        event_id="e7",
        seq=7,
        item_id="tool-cmd-browser-smoke",
        item_kind="tool_call",
        snapshot=ContentSnapshot(
            parts=(
                ToolCallContent(
                    part_id="tool-0",
                    call_id="cmd-browser-smoke",
                    name="codex.command",
                    arguments=tool_args,
                ),
                ToolResultContent(
                    part_id="tool-0",
                    call_id="cmd-browser-smoke",
                    result={
                        "status": "completed",
                        "exit_code": 0,
                        "duration_ms": 210,
                    },
                ),
            )
        ),
        source=source,
        **common,
    )
    yield UsageReported(
        event_id="e8",
        seq=8,
        input_tokens=128,
        output_tokens=32,
        total_tokens=160,
        cached_tokens=16,
        reasoning_tokens=8,
        source=source,
        **common,
    )
    yield ItemUpdated(
        event_id="e9",
        seq=9,
        item_id="msg-1",
        item_kind="message",
        op="append",
        update=TextContent(
            part_id="text-0",
            text="## 审查结果\n\n发现空列表会触发除零风险。",
        ),
        source=source,
        **common,
    )
    yield ItemCompleted(
        event_id="e10",
        seq=10,
        item_id="msg-1",
        item_kind="message",
        snapshot=ContentSnapshot(
            parts=(
                TextContent(
                    part_id="text-0",
                    text=(
                        "## 审查结果\n\n**确定问题**：空列表会触发除零风险。\n\n"
                        "建议在计算平均值前处理空列表。"
                    ),
                ),
            )
        ),
        source=source,
        **common,
    )
    yield RunCompleted(
        event_id="e11",
        seq=11,
        status="completed",
        output_refs=(
            OutputRef(
                scope_id=common["scope_id"],
                item_id="msg-1",
                part_id="text-0",
            ),
        ),
        source=SourceRef(
            framework="codex",
            metadata={
                "duration_ms": 1340,
                "started_at": "2026-08-04T04:00:00Z",
                "completed_at": "2026-08-04T04:00:01.340000Z",
            },
        ),
        **common,
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

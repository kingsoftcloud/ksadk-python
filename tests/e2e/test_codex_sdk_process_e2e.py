"""Real-account Codex CLI process evidence.

Set ``KSADK_CODEX_E2E=1`` only in an environment with a configured Codex
provider: either an API-key/provider configuration (including a supported
third-party provider) or an authenticated Codex CLI. A skipped module is
intentionally not release evidence.
"""

from __future__ import annotations

import json
import os

import pytest

from ksadk.codex.client import AsyncCodexClient
from ksadk.codex.runtime import CodexRuntime
from ksadk.runtime.adapter import CancelResult, ResumePayload, ResumeTarget, StartRequest

pytestmark = pytest.mark.skipif(
    os.environ.get("KSADK_CODEX_E2E") != "1",
    reason="set KSADK_CODEX_E2E=1 in an environment with a configured Codex provider",
)


@pytest.mark.asyncio
async def test_live_cli_one_turn_resume_and_process_exit() -> None:
    client = AsyncCodexClient()
    runtime = CodexRuntime(client)
    process = None
    try:
        handle = await runtime.start(
            StartRequest(
                input="Reply with exactly KSADK_CODEX_E2E_OK and do not use tools.",
                user_id="e2e-user",
                session_id="e2e-session",
            )
        )
        process = client._codex._client._sync._proc
        first = [event async for event in runtime.stream(handle)]
        await runtime.resume(
            handle,
            ResumeTarget(kind="thread_id", id=handle.run_id),
            ResumePayload(
                kind="free_text",
                data="Reply with exactly KSADK_CODEX_RESUME_OK and do not use tools.",
            ),
        )
        second = [event async for event in runtime.stream(handle)]
        print(
            json.dumps(
                {
                    "sdk_version": client.sdk_version,
                    "thread_id": handle.run_id,
                    "process_pid": process.pid if process is not None else None,
                    "first_turn": [
                        {
                            "event_type": event.event_type,
                            "phase": event.phase,
                            "text": str(event.payload.get("text", ""))[:80],
                        }
                        for event in first
                    ],
                    "resume_turn": [
                        {
                            "event_type": event.event_type,
                            "phase": event.phase,
                            "text": str(event.payload.get("text", ""))[:80],
                        }
                        for event in second
                    ],
                },
                sort_keys=True,
            )
        )
        assert any(event.event_type == "text.delta" for event in first)
        assert any(event.phase == "final_answer" for event in first)
        assert any("KSADK_CODEX_E2E_OK" in str(event.payload) for event in first)
        assert any("KSADK_CODEX_RESUME_OK" in str(event.payload) for event in second)
    finally:
        if process is not None:
            print(f"codex_pid={process.pid} before_close={process.poll()}")
        if "handle" in locals():
            await runtime.close(handle)
        else:
            await client.close()
        if process is not None:
            print(f"codex_pid={process.pid} after_close={process.poll()}")

    assert process is not None
    assert process.poll() is not None


@pytest.mark.asyncio
async def test_live_cli_pending_cancel_is_consumed_without_persisting_thread() -> None:
    """Pending cancel is a real SDK process lifecycle path, not a fake-only case."""
    client = AsyncCodexClient()
    runtime = CodexRuntime(client)
    try:
        handle = await runtime.start(
            StartRequest(input="unused", user_id="e2e-user", session_id="e2e-cancel")
        )
        assert await runtime.cancel(handle) is CancelResult.PENDING_CANCEL_RECORDED
        events = [event async for event in runtime.stream(handle)]
        assert [event.event_type for event in events] == ["run.cancelled"]
        assert handle.run_id not in runtime._do_not_persist
    finally:
        await runtime.close(handle)

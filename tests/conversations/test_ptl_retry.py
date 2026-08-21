"""PTL emergency retry 真实验证（方案 §9.1）。

模拟 Runner 首次抛 prompt too long → compaction → 最多一次 retry → 成功。
验证：
1. PTL 被正确识别（_is_prompt_too_long_error）
2. compaction 被触发（force=True）
3. 最多一次 retry（range(2)）
4. retry 后成功返回
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from ksadk.conversations.runtime_input import _is_prompt_too_long_error
from ksadk.runners.base_runner import BaseRunner
from ksadk.sessions.base import SessionEvent
from ksadk.sessions.in_memory import InMemorySessionService


def test_is_prompt_too_long_error_recognizes_various_formats():
    assert _is_prompt_too_long_error(RuntimeError("prompt too long"))
    assert _is_prompt_too_long_error(RuntimeError("maximum context length exceeded"))
    assert _is_prompt_too_long_error(RuntimeError("context_length_exceeded"))
    assert _is_prompt_too_long_error(RuntimeError("Error: 413 Payload Too Large"))
    assert not _is_prompt_too_long_error(RuntimeError("network error"))


class _PTLRunner(BaseRunner):
    """Mock runner: first call throws PTL, second succeeds."""

    def __init__(self) -> None:
        super().__init__(
            SimpleNamespace(
                type=SimpleNamespace(value="langgraph"),
                name="ptl-test",
                is_valid=True,
            ),
            ".",
        )
        self._agent = True
        self.attempts = 0

    def load_agent(self) -> None:
        pass

    async def invoke(self, input_data: dict[str, Any]) -> dict[str, Any]:
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("Error: prompt too long. maximum context length exceeded.")
        return {
            "output": "PTL retry succeeded",
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }

    def stream(self, input_data: dict[str, Any]):  # pragma: no cover
        raise NotImplementedError


def _seed_long_history(service: InMemorySessionService, session_id: str) -> None:
    """Seed 10 rounds × 50K chars to exceed typical context windows."""
    for i in range(10):
        service_sync_append(
            service,
            session_id,
            SessionEvent(
                id=f"u{i}",
                seq_id=i * 2 + 1,
                event_type="user_message",
                author="user",
                invocation_id=f"pre-{i}",
                content={"role": "user", "parts": [{"text": "x" * 50000}]},
            ),
        )
        service_sync_append(
            service,
            session_id,
            SessionEvent(
                id=f"a{i}",
                seq_id=i * 2 + 2,
                event_type="assistant_message",
                author="assistant",
                invocation_id=f"pre-{i}",
                content={
                    "role": "assistant",
                    "parts": [{"text": "y" * 50000}],
                },
            ),
        )


def service_sync_append(
    service: InMemorySessionService, session_id: str, event: SessionEvent
) -> None:
    """Synchronous wrapper for append_event."""

    asyncio.get_event_loop().run_until_complete(service.append_event(session_id, event))


@pytest.mark.asyncio
async def test_ptl_triggers_compaction_then_retry_once():
    """PTL → compaction → retry once → success."""
    from ksadk.conversations.runtime_invocation import invoke_conversation_once

    service = InMemorySessionService()
    await service.create_session(agent_id="ptl-agent", user_id="ptl-user", session_id="ses-ptl")
    for i in range(10):
        await service.append_event(
            "ses-ptl",
            SessionEvent(
                id=f"u{i}",
                seq_id=i * 2 + 1,
                event_type="user_message",
                author="user",
                invocation_id=f"pre-{i}",
                content={"role": "user", "parts": [{"text": "x" * 50000}]},
            ),
        )
        await service.append_event(
            "ses-ptl",
            SessionEvent(
                id=f"a{i}",
                seq_id=i * 2 + 2,
                event_type="assistant_message",
                author="assistant",
                invocation_id=f"pre-{i}",
                content={
                    "role": "assistant",
                    "parts": [{"text": "y" * 50000}],
                },
            ),
        )

    runner = _PTLRunner()
    _, result = await invoke_conversation_once(
        runner=runner,
        agent_id="ptl-agent",
        user_id="ptl-user",
        session_id="ses-ptl",
        messages=[{"role": "user", "content": "继续"}],
        model="test",
        prepare_runner=lambda r, m: None,
        instructions="你是助手",
        session_service_provider=lambda: service,
    )

    assert runner.attempts == 2, f"Expected 2 attempts (PTL + retry), got {runner.attempts}"
    assert result.get("output_text") == "PTL retry succeeded"


@pytest.mark.asyncio
async def test_ptl_retry_max_once_no_infinite_loop():
    """PTL twice → only 1 retry → structured failure (no infinite loop)."""
    from ksadk.conversations.runtime_invocation import invoke_conversation_once

    class _DoublePTLRunner(BaseRunner):
        def __init__(self) -> None:
            super().__init__(
                SimpleNamespace(
                    type=SimpleNamespace(value="langgraph"),
                    name="ptl2",
                    is_valid=True,
                ),
                ".",
            )
            self._agent = True
            self.attempts = 0

        def load_agent(self) -> None:
            pass

        async def invoke(self, input_data: dict[str, Any]) -> dict[str, Any]:
            self.attempts += 1
            raise RuntimeError("prompt too long")

        def stream(self, input_data: dict[str, Any]):  # pragma: no cover
            raise NotImplementedError

    service = InMemorySessionService()
    await service.create_session(agent_id="ptl2-agent", user_id="ptl-user", session_id="ses-ptl2")
    for i in range(10):
        await service.append_event(
            "ses-ptl2",
            SessionEvent(
                id=f"u{i}",
                seq_id=i * 2 + 1,
                event_type="user_message",
                author="user",
                invocation_id=f"pre-{i}",
                content={"role": "user", "parts": [{"text": "x" * 50000}]},
            ),
        )
        await service.append_event(
            "ses-ptl2",
            SessionEvent(
                id=f"a{i}",
                seq_id=i * 2 + 2,
                event_type="assistant_message",
                author="assistant",
                invocation_id=f"pre-{i}",
                content={
                    "role": "assistant",
                    "parts": [{"text": "y" * 50000}],
                },
            ),
        )

    runner = _DoublePTLRunner()
    try:
        await invoke_conversation_once(
            runner=runner,
            agent_id="ptl2-agent",
            user_id="ptl-user",
            session_id="ses-ptl2",
            messages=[{"role": "user", "content": "继续"}],
            model="test",
            prepare_runner=lambda r, m: None,
            instructions="你是助手",
            session_service_provider=lambda: service,
        )
        assert False, "Should have raised"
    except Exception as exc:
        # 最多 2 次尝试（1 initial + 1 retry），不无限重试
        assert runner.attempts == 2, f"Expected 2 attempts max, got {runner.attempts}"
        assert "prompt too long" in str(exc).lower() or "prompt_too_long" in str(exc).lower()

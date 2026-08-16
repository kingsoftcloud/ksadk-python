from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from ksadk.codex.client import AsyncCodexClient
from ksadk.codex.runtime import CodexRuntimeAdapter
from ksadk.runtime.adapter import (
    CancelResult,
    ResumePayload,
    ResumeTarget,
    StartRequest,
)

openai_codex = pytest.importorskip("openai_codex")


def _config(tmp_path: Path, *, reject_resume_id: str | None = None):
    server = Path(__file__).with_name("fake_app_server.py")
    env = {
        "KSADK_TEST_CODEX_PID_FILE": str(tmp_path / "pid"),
        "KSADK_TEST_CODEX_REQUEST_LOG": str(tmp_path / "requests.jsonl"),
    }
    if reject_resume_id is not None:
        env["KSADK_TEST_CODEX_REJECT_RESUME_ID"] = reject_resume_id
    return openai_codex.CodexConfig(
        launch_args_override=(sys.executable, str(server)),
        env=env,
    )


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_for_process_exit(pid: int) -> None:
    for _ in range(100):
        if not _pid_exists(pid):
            return
        await asyncio.sleep(0.02)
    pytest.fail(f"Codex transport child {pid} remained alive after close")


@pytest.mark.asyncio
async def test_real_sdk_transport_surface_and_process_cleanup(tmp_path: Path):
    client = AsyncCodexClient(config=_config(tmp_path))
    try:
        thread_id = await client.start_thread({"sandbox_read_only": True})
        events = [
            event
            async for event in client.run_turn(
                thread_id,
                "complete",
                config={"sandbox_read_only": True},
            )
        ]
        pid = int((tmp_path / "pid").read_text(encoding="utf-8"))
        assert _pid_exists(pid)
    finally:
        await client.close()

    await _wait_for_process_exit(pid)

    # A new SDK process has no cached AsyncThread and must use thread_resume.
    resume_client = AsyncCodexClient(config=_config(tmp_path))
    try:
        resumed_id = await resume_client.resume_thread(thread_id, {"sandbox_read_only": True})
        resume_pid = int((tmp_path / "pid").read_text(encoding="utf-8"))
        assert _pid_exists(resume_pid)
    finally:
        await resume_client.close()
    await _wait_for_process_exit(resume_pid)

    assert resumed_id == thread_id
    assert [event["method"] for event in events] == [
        "turn/started",
        "item/started",
        "item/completed",
        "item/started",
        "item/agentMessage/delta",
        "item/completed",
        "item/autoApprovalReview/started",
        "item/autoApprovalReview/completed",
        "item/started",
        "item/agentMessage/delta",
        "item/completed",
        "turn/completed",
    ]
    final_started = next(
        event
        for event in events
        if event["method"] == "item/started"
        and event["params"]["item"].get("phase") == "final_answer"
    )
    assert final_started["params"]["item"]["phase"] == "final_answer"

    requests = [
        json.loads(line)
        for line in (tmp_path / "requests.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    start_params = next(row["params"] for row in requests if row["method"] == "thread/start")
    turn_params = next(row["params"] for row in requests if row["method"] == "turn/start")
    assert start_params["sandbox"] == "read-only"
    assert start_params["approvalPolicy"] == "never"
    assert start_params["ephemeral"] is True
    assert turn_params["sandboxPolicy"]["type"] == "readOnly"
    assert turn_params["approvalPolicy"] == "never"


@pytest.mark.asyncio
async def test_real_sdk_transport_interrupt_and_approval_drain(tmp_path: Path):
    client = AsyncCodexClient(config=_config(tmp_path))
    thread_id = await client.start_thread({"sandbox_read_only": True})
    seen = []

    async def consume() -> None:
        async for event in client.run_turn(
            thread_id,
            "BLOCK",
            config={"sandbox_read_only": True},
        ):
            seen.append(event)

    task = asyncio.create_task(consume())
    try:
        for _ in range(100):
            if any(event["method"].endswith("/started") for event in seen):
                break
            await asyncio.sleep(0.02)
        assert await client.interrupt_active_turn(thread_id) is True
        await asyncio.wait_for(task, timeout=2)
        assert await client.interrupt_active_turn(thread_id) is False
    finally:
        await client.close()

    methods = [event["method"] for event in seen]
    assert "item/autoApprovalReview/started" in methods
    assert "item/autoApprovalReview/completed" in methods


@pytest.mark.asyncio
async def test_ephemeral_interrupted_thread_cannot_resume_in_new_process(tmp_path: Path):
    client = AsyncCodexClient(config=_config(tmp_path))
    thread_id = await client.start_thread({"sandbox_read_only": True})
    task = asyncio.create_task(
        _consume(client.run_turn(thread_id, "BLOCK", config={"sandbox_read_only": True}))
    )
    for _ in range(100):
        if await client.interrupt_active_turn(thread_id):
            break
        await asyncio.sleep(0.02)
    await asyncio.wait_for(task, timeout=2)
    await client.close()

    new_client = AsyncCodexClient(config=_config(tmp_path, reject_resume_id=thread_id))
    try:
        with pytest.raises(
            openai_codex.InvalidRequestError,
            match=rf"no rollout found for thread id {thread_id}",
        ):
            await new_client.resume_thread(thread_id, {"sandbox_read_only": True})
    finally:
        await new_client.close()


async def _consume(events) -> list[dict]:
    return [event async for event in events]


def test_client_surface_failure_reports_installed_version(monkeypatch):
    monkeypatch.delattr(openai_codex.AsyncTurnHandle, "interrupt")
    with pytest.raises(RuntimeError, match=r"0\.144\.4.*AsyncTurnHandle\.interrupt"):
        AsyncCodexClient(config=None)


@pytest.mark.asyncio
async def test_runtime_real_transport_stream_cancel_and_approval_drain(tmp_path: Path):
    client = AsyncCodexClient(config=_config(tmp_path))
    runtime = CodexRuntimeAdapter(client)
    handle = await runtime.start(StartRequest(input="BLOCK", user_id="u", session_id="s"))
    events = []

    async def consume() -> None:
        async for event in runtime.stream(handle):
            events.append(event)

    task = asyncio.create_task(consume())
    for _ in range(100):
        thread = runtime._threads.get(handle.run_id)
        if thread is not None and thread.pending_approvals == {"review_1"}:
            break
        await asyncio.sleep(0.02)
    result = await runtime.cancel(handle)
    await asyncio.wait_for(task, timeout=2)
    assert result in (CancelResult.INTERRUPTED_ACTIVE_TURN, CancelResult.PENDING_CANCEL_RECORDED)
    assert runtime.last_cancel_dropped_approvals == {"review_1"}
    assert any(getattr(event, "phase", None) == "commentary" for event in events)

    pid = int((tmp_path / "pid").read_text(encoding="utf-8"))
    assert _pid_exists(pid)
    await runtime.close(handle)
    await _wait_for_process_exit(pid)


@pytest.mark.asyncio
async def test_runtime_real_transport_same_thread_resume_uses_payload(tmp_path: Path):
    client = AsyncCodexClient(config=_config(tmp_path))
    runtime = CodexRuntimeAdapter(client)
    handle = await runtime.start(StartRequest(input="complete", user_id="u", session_id="s"))
    first = [event async for event in runtime.stream(handle)]
    await runtime.resume(
        handle,
        ResumeTarget(kind="thread_id", id=handle.run_id),
        ResumePayload(kind="free_text", data="resume payload"),
    )
    second = [event async for event in runtime.stream(handle)]
    await runtime.close(handle)

    assert any(getattr(event, "phase", None) == "commentary" for event in first)
    assert any(getattr(event, "phase", None) == "final_answer" for event in first)
    assert any(getattr(event, "phase", None) == "final_answer" for event in second)
    assert not any("must-not-become-final-text" in str(event.model_dump()) for event in first + second)
    requests = [
        json.loads(line)
        for line in (tmp_path / "requests.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    turns = [row for row in requests if row["method"] == "turn/start"]
    assert len(turns) == 2
    assert "resume payload" in json.dumps(turns[1]["params"]["input"])
    assert all(row["params"]["threadId"] == handle.run_id for row in turns)


@pytest.mark.asyncio
async def test_runtime_events_preserve_the_caller_scope(tmp_path: Path):
    client = AsyncCodexClient(config=_config(tmp_path))
    runtime = CodexRuntimeAdapter(client)
    request = StartRequest(
        input="complete",
        user_id="scope-user",
        session_id="scope-session",
        agent_id="scope-agent",
        metadata={"invocation_id": "scope-invocation"},
    )
    handle = await runtime.start(request)
    try:
        events = [event async for event in runtime.stream(handle)]
    finally:
        await runtime.close(handle)

    assert events
    # canonical 事件:调用方 scope 收敛进 source.metadata。
    assert {
        (
            event.source.metadata.get("agent_id"),
            event.source.metadata.get("user_id"),
            event.source.metadata.get("session_id"),
            event.source.metadata.get("invocation_id"),
        )
        for event in events
    } == {("scope-agent", "scope-user", "scope-session", "scope-invocation")}


@pytest.mark.asyncio
async def test_runtime_external_thread_uses_real_backend_resume(tmp_path: Path):
    client = AsyncCodexClient(config=_config(tmp_path))
    runtime = CodexRuntimeAdapter(client)
    handle = await runtime.start(
        StartRequest(
            input="complete",
            user_id="u",
            session_id="s",
            metadata={"thread_id": "019f0000-0000-7000-8000-000000000001"},
        )
    )
    events = [event async for event in runtime.stream(handle)]
    await runtime.close(handle)

    assert any(getattr(event, "phase", None) == "final_answer" for event in events)
    requests = [
        json.loads(line)
        for line in (tmp_path / "requests.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    resumed = [row for row in requests if row["method"] == "thread/resume"]
    assert resumed[0]["params"]["threadId"] == handle.run_id


@pytest.mark.asyncio
async def test_runtime_timeout_closes_real_transport_process(tmp_path: Path):
    client = AsyncCodexClient(config=_config(tmp_path))
    runtime = CodexRuntimeAdapter(client, turn_timeout_seconds=0.15)
    handle = await runtime.start(StartRequest(input="BLOCK", user_id="u", session_id="s"))
    pid = int((tmp_path / "pid").read_text(encoding="utf-8"))
    events = [event async for event in runtime.stream(handle)]
    await _wait_for_process_exit(pid)

    failed = [event for event in events if event.event_type == "run.failed"]
    assert failed and failed[0].error.message == "codex turn timed out"
    assert handle.run_id in runtime._do_not_persist

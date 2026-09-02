"""SubagentProvider/v1 lineage, policy and routing conformance."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from ksadk.plugins.subagents import (
    ChildHandle,
    SpawnSubagentRequest,
    SubagentEvent,
    SubagentProviderError,
    SubagentProviderRouter,
    SubagentResult,
    SubagentStatus,
)

PROVIDER_REF = "plugin://io.example.codex-child@1.0.0"


def _request(**updates: Any) -> SpawnSubagentRequest:
    payload = {
        "providerRef": PROVIDER_REF,
        "parentSessionId": "session-parent",
        "parentRunId": "run-parent",
        "task": "Review the repository without modifying it.",
        "depth": 1,
        "policy": {
            "maxDepth": 2,
            "timeoutSeconds": 30,
            "maxSteps": 4,
            "allowedTools": ["repo.read"],
            "allowedPermissions": ["filesystem:workspace-read"],
        },
    }
    payload.update(updates)
    return SpawnSubagentRequest.model_validate(payload)


def _handle(**updates: Any) -> ChildHandle:
    payload = {
        "handleId": "child-1",
        "providerRef": PROVIDER_REF,
        "parentSessionId": "session-parent",
        "parentRunId": "run-parent",
        "childSessionId": "session-child",
        "childRunId": "run-child",
        "depth": 1,
        "capabilities": ["streaming", "cancel"],
        "capabilityDigest": "sha256:" + "a" * 64,
        "createdAt": "2026-08-27T00:00:00Z",
        "resumable": False,
    }
    payload.update(updates)
    return ChildHandle.model_validate(payload)


class _Provider:
    def __init__(self, handle: ChildHandle | None = None) -> None:
        self.handle = handle or _handle()
        self.calls: list[str] = []

    async def describe(self):  # noqa: ANN201
        return {"name": "fixture"}

    async def available(self) -> bool:
        self.calls.append("available")
        return True

    async def spawn(self, request: SpawnSubagentRequest) -> ChildHandle:
        self.calls.append(f"spawn:{request.parent_run_id}")
        return self.handle

    async def followup(self, handle: ChildHandle, input: Any) -> None:
        self.calls.append(f"followup:{handle.handle_id}:{input}")

    async def status(self, handle: ChildHandle) -> SubagentStatus:
        self.calls.append(f"status:{handle.handle_id}")
        return SubagentStatus(
            handle_id=handle.handle_id,
            state="running",
            last_seq=1,
            updated_at=datetime.now(timezone.utc),
        )

    async def interrupt(self, handle: ChildHandle) -> None:
        self.calls.append(f"interrupt:{handle.handle_id}")

    async def cancel(self, handle: ChildHandle) -> None:
        self.calls.append(f"cancel:{handle.handle_id}")

    async def _events(self, handle: ChildHandle, after_seq: int) -> AsyncIterator[SubagentEvent]:
        self.calls.append(f"subscribe:{handle.handle_id}:{after_seq}")
        if after_seq < 1:
            yield SubagentEvent(
                handle_id=handle.handle_id,
                event_id="event-1",
                seq=1,
                kind="progress",
                payload={"message": "running"},
            )

    def subscribe(
        self, handle: ChildHandle, *, after_seq: int = 0
    ) -> AsyncIterator[SubagentEvent]:
        return self._events(handle, after_seq)

    async def result(self, handle: ChildHandle) -> SubagentResult:
        self.calls.append(f"result:{handle.handle_id}")
        return SubagentResult(handle_id=handle.handle_id, state="succeeded", output="done")

    async def dispose(self, handle: ChildHandle) -> None:
        self.calls.append(f"dispose:{handle.handle_id}")


@pytest.mark.asyncio
async def test_router_preserves_lineage_identity_and_routes_complete_lifecycle() -> None:
    provider = _Provider()
    router = SubagentProviderRouter({PROVIDER_REF: provider})

    handle = await router.spawn(_request())
    status = await router.status(handle)
    events = [event async for event in router.subscribe(handle)]
    await router.followup(handle, "continue")
    await router.interrupt(handle)
    await router.cancel(handle)
    result = await router.result(handle)
    await router.dispose(handle)

    assert handle.parent_session_id == "session-parent"
    assert handle.parent_run_id == "run-parent"
    assert handle.child_session_id == "session-child"
    assert status.handle_id == result.handle_id == handle.handle_id
    assert [(event.event_id, event.seq) for event in events] == [("event-1", 1)]
    assert provider.calls == [
        "available",
        "spawn:run-parent",
        "status:child-1",
        "subscribe:child-1:0",
        "followup:child-1:continue",
        "interrupt:child-1",
        "cancel:child-1",
        "result:child-1",
        "dispose:child-1",
    ]


@pytest.mark.asyncio
async def test_router_rejects_provider_handle_that_rewrites_parent_lineage() -> None:
    router = SubagentProviderRouter(
        {PROVIDER_REF: _Provider(_handle(parentRunId="run-other"))}
    )

    with pytest.raises(SubagentProviderError) as captured:
        await router.spawn(_request())

    assert captured.value.code == "subagent_handle_mismatch"


def test_contracts_reject_depth_escape_clear_secrets_and_false_recovery() -> None:
    with pytest.raises(ValidationError, match="exceeds"):
        _request(depth=3)
    with pytest.raises(ValidationError, match="secret reference"):
        _request(metadata={"apiKey": "clear-secret"})
    with pytest.raises(ValidationError, match="requires a resumeDescriptor"):
        _handle(resumable=True)
    with pytest.raises(ValidationError, match="cannot carry"):
        _handle(resumeDescriptor={"threadId": "thread-1"})


def test_failed_result_requires_typed_error_and_success_cannot_hide_one() -> None:
    with pytest.raises(ValidationError, match="requires errorCode"):
        SubagentResult(handle_id="child-1", state="failed")
    with pytest.raises(ValidationError, match="cannot carry an error"):
        SubagentResult(handle_id="child-1", state="succeeded", error_code="hidden")


@pytest.mark.asyncio
async def test_unknown_provider_and_negative_cursor_fail_closed() -> None:
    router = SubagentProviderRouter({})
    with pytest.raises(SubagentProviderError) as missing:
        await router.spawn(_request())
    assert missing.value.code == "subagent_provider_not_found"

    provider_router = SubagentProviderRouter({PROVIDER_REF: _Provider()})
    with pytest.raises(SubagentProviderError) as cursor:
        provider_router.subscribe(_handle(), after_seq=-1)
    assert cursor.value.code == "subagent_cursor_invalid"

# -*- coding: utf-8 -*-
"""Codex live approval 回包（Kernel Task 6 Step 2/6）。

断言 approve/reject 映射为 live JSON-RPC approval response，且：
- 使用**原 call_id**（requestApproval 的 approvalId），不是新 id；
- 送达的是 activation 持有的同一 client 实例（context.adapter 内的 client）；
- 流不重启、不伪造新 run（live_submit 语义）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Optional

from ksadk.codex.client import CodexClient
from ksadk.codex.runtime import CodexRuntimeAdapter
from ksadk.interaction.contracts import InteractionRecord, InteractionSubmission
from ksadk.interaction.provider import InteractionResolveContext
from ksadk.interaction.providers.codex import CodexInteractionProvider
from ksadk.runtime.adapter import RunHandle, StartRequest


class RecordingCodexClient(CodexClient):
    """只记录审批回包的假 client：真实 SDK 的 JSON-RPC 命令通道。"""

    def __init__(self) -> None:
        self.resolved_approvals: list[tuple[str, str]] = []
        self.resolved_interactions: list[tuple[str, dict]] = []
        self.turn_requests: list[Any] = []
        self.instance_id = id(self)

    async def start_thread(self, config: Optional[dict] = None) -> str:
        return "codex_thread_started"

    def run_turn(self, thread_id, prompt, *, config=None):  # pragma: no cover
        async def _gen():
            return
            yield  # pragma: no cover

        self.turn_requests.append(prompt)
        return _gen()

    async def interrupt_active_turn(self, thread_id: str) -> bool:  # pragma: no cover
        return True

    async def resume_thread(
        self, thread_id: str, config: Optional[dict] = None
    ) -> str:  # pragma: no cover
        return thread_id

    async def close(self) -> None:  # pragma: no cover
        return None

    async def resolve_approval(self, approval_id: str, decision: str) -> bool:
        self.resolved_approvals.append((approval_id, decision))
        return True

    async def resolve_interaction(self, interaction_id: str, data: dict) -> bool:
        self.resolved_interactions.append((interaction_id, data))
        return True


def _record(*, kind: str = "approval", native_target: dict | None = None) -> InteractionRecord:
    return InteractionRecord(
        interaction_id="it-codex-1",
        tenant_id="tenant-1",
        agent_instance_id="agent-1",
        session_id="s1",
        run_id="run-durable-1",
        kind=kind,  # type: ignore[arg-type]
        request_schema={"type": "object"},
        created_at=datetime.now(UTC).isoformat(),
        provider_id="codex",
        native_target=native_target
        or {"call_id": "call_9f2", "thread_id": "codex_thread_existing"},
    )


async def _context(adapter: CodexRuntimeAdapter, handle: RunHandle) -> InteractionResolveContext:
    return InteractionResolveContext(
        adapter=adapter,
        handle=handle,
        activation_id="act-1",
        fencing_token=7,
    )


async def _live_handle(adapter: CodexRuntimeAdapter) -> RunHandle:
    # metadata.thread_id = 接入既有 thread（不新建）。
    return await adapter.start(
        StartRequest(
            input="hi",
            user_id="u",
            session_id="s1",
            metadata={"thread_id": "codex_thread_existing"},
        )
    )


async def test_approve_reaches_original_call_id_on_same_client_instance():
    client = RecordingCodexClient()
    adapter = CodexRuntimeAdapter(client)
    handle = await _live_handle(adapter)
    provider = CodexInteractionProvider()
    submission = InteractionSubmission(
        interaction_id="it-codex-1",
        expected_revision=1,
        action="approve",
        response={"comment": "ship it"},
        idempotency_key="idem-1",
    )
    resolved_handle = await provider.resolve(
        await _context(adapter, handle), _record(), submission
    )
    # 原 call_id + codex 原生 decision，送达 context.adapter 持有的同一 client。
    assert client.resolved_approvals == [("call_9f2", "approve")]
    assert client.instance_id == id(client)  # 同一实例（防 fixture 重绑）
    # live_submit 不换 handle、不重启流。
    assert resolved_handle is handle


async def test_reject_maps_to_native_deny_decision():
    client = RecordingCodexClient()
    adapter = CodexRuntimeAdapter(client)
    handle = await _live_handle(adapter)
    submission = InteractionSubmission(
        interaction_id="it-codex-1",
        expected_revision=1,
        action="reject",
        response={"comment": "no"},
        idempotency_key="idem-2",
    )
    await CodexInteractionProvider().resolve(
        await _context(adapter, handle), _record(), submission
    )
    # codex client 词表把 reject 归一为 deny/decline；provider 送原生 "deny"。
    assert client.resolved_approvals == [("call_9f2", "deny")]


async def test_reject_with_public_decision_vocabulary_maps_to_deny():
    """The runtime advertises decision enum [approve, reject]; a client that
    echoes that public vocabulary (Response={"decision": "reject"}) must be
    normalized to the codex-native "deny", not rejected by the client's
    vocab check."""
    client = RecordingCodexClient()
    adapter = CodexRuntimeAdapter(client)
    handle = await _live_handle(adapter)
    submission = InteractionSubmission(
        interaction_id="it-codex-1",
        expected_revision=1,
        action="reject",
        response={"decision": "reject"},
        idempotency_key="idem-2b",
    )
    await CodexInteractionProvider().resolve(
        await _context(adapter, handle), _record(), submission
    )
    assert client.resolved_approvals == [("call_9f2", "deny")]


async def test_structured_input_uses_live_interaction_channel():
    client = RecordingCodexClient()
    adapter = CodexRuntimeAdapter(client)
    handle = await _live_handle(adapter)
    record = _record(kind="structured_input")
    submission = InteractionSubmission(
        interaction_id="it-codex-1",
        expected_revision=1,
        action="submit",
        response={"city": "Beijing"},
        idempotency_key="idem-3",
    )
    await CodexInteractionProvider().resolve(
        await _context(adapter, handle), record, submission
    )
    assert client.resolved_interactions == [("call_9f2", {"city": "Beijing"})]
    assert client.resolved_approvals == []

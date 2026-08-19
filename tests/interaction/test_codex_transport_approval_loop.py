# -*- coding: utf-8 -*-
"""P0-2 回归：真实 openai-codex SDK transport 上的 live approval 闭环。

fake app-server（JSON-RPC over stdio）下发真实
``item/commandExecution/requestApproval`` 请求，断言内核全链路：

1. canonical ``interaction.requested``（provider=codex、真实 call_id）落库；
2. 公网语义 ``submit_interaction`` approve → activation 持有的同一 client
   实例经**原 call_id** 回 JSON-RPC response；
3. 回包后 continuation 恢复，run 以 ``completed`` 收口
   （run.interrupted 事件不留下孤儿 open run）。

修复背景：bridge 旧版下发合成 ``item/approval/requested``，canonical
mapper 对该方法 fail-closed（unsupported_method），整个 run 直接失败。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

openai_codex = pytest.importorskip("openai_codex")  # noqa: F841

from ksadk.codex.client import AsyncCodexClient  # noqa: E402
from ksadk.codex.runtime import CodexRuntimeAdapter  # noqa: E402
from ksadk.kernel.state import InboxState, RunState  # noqa: E402
from ksadk.kernel.worker import AgentKernelWorker  # noqa: E402
from tests.kernel.control_harness import AGENT, command, kernel_stack  # noqa: E402

FAKE_SERVER = Path(__file__).with_name("fake_approval_app_server.py")


async def _wait_until(predicate, timeout: float = 20.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met within timeout")


@pytest.mark.asyncio
async def test_real_codex_transport_live_approval_closes_run():
    config = openai_codex.CodexConfig(
        launch_args_override=(sys.executable, str(FAKE_SERVER))
    )
    client = AsyncCodexClient(config=config)
    adapter = CodexRuntimeAdapter(client)
    # kernel admission 以真实 codex capability matrix 判定 submit_interaction。
    stack = await kernel_stack(adapter=adapter)
    lease = await stack.lease()
    worker = AgentKernelWorker(stack.store, adapter_factory=lambda: adapter)
    try:
        await stack.kernel.submit(
            command(idempotency_key="appr-1", content="run uname -a"),
            permit=stack.permit("enqueue"),
        )
        result = await worker.run_once(AGENT, lease)
        assert result.outcome == "completed", result
        run_id = result.run_id
        assert run_id is not None

        # 1) requestApproval → interaction.requested（真实 call_id）落库。
        await _wait_until(
            lambda: any(
                record.kind == "approval"
                for record in _interaction_records_sync(stack)
            )
        )
        record = next(
            r for r in _interaction_records_sync(stack) if r.kind == "approval"
        )
        assert record.provider_id == "codex"
        call_id = record.native_target["call_id"]
        assert call_id

        events = await stack.events.read("s1", 0, 200)
        requested = [e for e in events if e.event_type == "interaction.requested"]
        assert requested and requested[0].run_id == run_id

        # 2) approve → 原 call_id 经同一 client 实例回 JSON-RPC response。
        await stack.kernel.submit(
            command(
                "submit_interaction",
                idempotency_key="appr-resolve-1",
                payload={
                    "run_id": run_id,
                    "interaction_id": record.interaction_id,
                    "token_ref": "tok-appr",
                    "response": {"decision": "approve"},
                    "action": "approve",
                    "expected_revision": 1,
                },
            ),
            permit=stack.permit("submit_interaction"),
        )
        submit_result = await worker.run_once(AGENT, lease)
        assert submit_result.outcome == "completed", submit_result

        # 3) 回包送达后 continuation 恢复，run 以 completed 收口。
        await _wait_until(
            lambda: stack.store._runs.get(run_id) is not None
            and stack.store._runs[run_id].state == RunState.COMPLETED
        )
        events = await stack.events.read("s1", 0, 400)
        resolved = [e for e in events if e.event_type == "interaction.resolved"]
        assert resolved, "expected interaction.resolved after approve"
        record = await stack.store.get(record.interaction_id)
        assert record is not None and record.status == "resolved"
        for message in await stack.store.list_messages(AGENT, "s1"):
            assert message.status == InboxState.COMPLETED
    finally:
        await client.close()


def _interaction_records_sync(stack):
    """内存 store 的同步快照（避免在同步谓词里 await）。"""
    records = []
    for row in getattr(stack.store, "_interactions", {}).values():
        records.append(row["record"] if isinstance(row, dict) else row)
    return records

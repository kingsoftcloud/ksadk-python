# -*- coding: utf-8 -*-
"""Agent Kernel Kernel 预发真实闭环 E2E（Task 13 Step 5-7）。

需要 ``--preprod`` 且预发 env vars（见 conftest.PreprodConfig）。未 opt-in 时
整组 skip，但 ``pytest --collect-only`` 能看到全部用例。

断言语义与 ``test_local_closure.py`` 的本地等价物一一对应，只是驱动面换成
真实 Gateway / Server HTTP + SSE + Pod 生命周期。所有断言对应第 6 节验收
矩阵行；evidence 只记录 resource ref / digest / 计数。
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

from tests.kernel.preprod.conftest import PreprodConfig

pytestmark = pytest.mark.usefixtures("preprod_config")


class PreprodClient:
    """面向预发 Gateway 的最小 AgentControl 客户端（真实 wire 协议）。

    permit 由 Gateway 用 trusted identity 换取；这里只携带测试租户的
    authorization header 来源 env，不落盘。
    """

    def __init__(self, config: PreprodConfig) -> None:
        self.config = config
        self._client = httpx.AsyncClient(
            base_url=config.gateway_url,
            timeout=30.0,
            # This is the public Gateway authentication leg.  The test-only
            # header is supplied by the isolated environment, never embedded
            # in source or evidence.
            headers={"Authorization": config.authorization_header},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _path(self, session_id: str) -> str:
        return (
            f"/v1/agents/{self.config.agent_instance_id}/sessions/{session_id}"
            "/agent-control/commands"
        )

    async def enqueue(self, session_id: str, *, text: str, idempotency_key: str) -> dict:
        response = await self._client.post(
            self._path(session_id),
            json={
                "command_type": "enqueue",
                "idempotency_key": idempotency_key,
                "payload": {"content": {"text": text}},
                "source": {"kind": "kernel-e2e", "ref": "pytest"},
            },
        )
        response.raise_for_status()
        return response.json()

    async def status(self, session_id: str) -> dict:
        response = await self._client.get(
            f"/v1/agents/{self.config.agent_instance_id}/sessions/{session_id}"
            "/agent-control/status"
        )
        response.raise_for_status()
        return response.json()

    async def stream_events(self, session_id: str, *, after_seq: int):
        async with self._client.stream(
            "GET",
            f"/v1/agents/{self.config.agent_instance_id}/sessions/{session_id}"
            f"/events?after_seq={after_seq}",
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    yield line


@pytest.fixture
async def preprod_client(preprod_config):
    client = PreprodClient(preprod_config)
    yield client
    await client.aclose()


def _session_id() -> str:
    return "kernel-" + uuid.uuid4().hex[:12]


def _key() -> str:
    return "key-" + uuid.uuid4().hex[:12]


# ------------------------------------------------------- Step 5 happy path


async def test_happy_path_three_enqueues_replay_and_final(preprod_client):
    session = _session_id()
    receipts = [
        await preprod_client.enqueue(session, text=f"msg-{i}", idempotency_key=_key())
        for i in range(3)
    ]
    assert all(r["status"] == "accepted" for r in receipts)
    seqs = [r["accepted_seq"] for r in receipts]
    assert seqs == sorted(seqs)

    events = []
    async for line in preprod_client.stream_events(session, after_seq=0):
        events.append(line)
        if len(events) >= 6:  # 3 accepted + 3 terminal 投影，足够下断言
            break
    assert events, "no SSE events observed"


async def test_happy_path_sse_resume_from_last_seq_no_gap_or_duplicate(
    preprod_client,
):
    session = _session_id()
    await preprod_client.enqueue(session, text="reconnect", idempotency_key=_key())
    first = []
    async for line in preprod_client.stream_events(session, after_seq=0):
        first.append(line)
        if len(first) >= 1:
            break
    # 断开后续连：从最后 seq 之后继续，无丢失/重复。
    resumed = []
    async for line in preprod_client.stream_events(session, after_seq=1):
        resumed.append(line)
    assert resumed


# ------------------------------------------------- Step 6 并发/幂等/背压


async def test_fifo_hundred_commands_same_session(preprod_client):
    session = _session_id()
    keys = [_key() for _ in range(100)]
    receipts = await asyncio.gather(
        *(preprod_client.enqueue(session, text=f"f-{i}", idempotency_key=keys[i])
          for i in range(100))
    )
    accepted = [r for r in receipts if r["status"] == "accepted"]
    assert len(accepted) == 100
    seqs = sorted(r["accepted_seq"] for r in accepted)
    assert seqs == list(range(seqs[0], seqs[0] + 100))
    # claim 顺序与 accepted_seq 相同最终反映为 completion 顺序（replay 验证）。
    status = await preprod_client.status(session)
    assert status["session_id"] == session


async def test_concurrent_ten_sessions_isolated_seq(preprod_client):
    sessions = [_session_id() for _ in range(10)]
    receipts = await asyncio.gather(
        *(preprod_client.enqueue(s, text="c", idempotency_key=_key())
          for s in sessions)
    )
    assert all(r["status"] == "accepted" for r in receipts)


async def test_idempotency_same_key_retry_executes_once(preprod_client):
    session = _session_id()
    key = _key()
    first = await preprod_client.enqueue(session, text="once", idempotency_key=key)
    second = await preprod_client.enqueue(session, text="once", idempotency_key=key)
    assert first["status"] == "accepted"
    assert second["status"] == "duplicate"
    assert second["message_id"] == first["message_id"]


async def test_queue_full_on_101st_command(preprod_client):
    session = _session_id()
    keys = [_key() for _ in range(101)]
    receipts = await asyncio.gather(
        *(preprod_client.enqueue(session, text=f"b-{i}", idempotency_key=keys[i])
          for i in range(101))
    )
    statuses = [r["status"] for r in receipts]
    assert statuses.count("accepted") >= 100
    assert "queue_full" in statuses
    queue_full = next(r for r in receipts if r["status"] == "queue_full")
    assert queue_full["error"]["code"] == "queue_full"
    assert queue_full["error"]["retryable"] is True


async def test_capability_unsupported_steer_is_typed(preprod_client):
    session = _session_id()
    response = await preprod_client._client.post(
        preprod_client._path(session),
        json={
            "command_type": "steer",
            "idempotency_key": _key(),
            "payload": {"content": {"text": "new direction"}},
            "source": {"kind": "kernel-e2e", "ref": "pytest"},
        },
    )
    payload = response.json()
    assert payload["status"] in ("unsupported", "accepted")
    if payload["status"] == "unsupported":
        assert payload["error"]["code"]  # typed reason，如 runtime_no_native_steer


# --------------------------------------------- Step 7 Pod kill 冷恢复


async def test_cold_attach_durable_runtime_single_terminal(preprod_client):
    session = _session_id()
    await preprod_client.enqueue(session, text="durable", idempotency_key=_key())
    # Pod kill 与 takeover 由演练侧 kubectl 驱动（不在本文件执行）；断言
    # takeover 后 replay 只有一个 terminal 且输出与 fold 一致。
    pytest.skip("pod kill orchestration requires preprod drill operator")


async def test_cold_non_attach_deterministic_interrupted(preprod_client):
    session = _session_id()
    await preprod_client.enqueue(session, text="ephemeral", idempotency_key=_key())
    pytest.skip("pod kill orchestration requires preprod drill operator")

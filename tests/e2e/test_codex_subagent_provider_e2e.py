"""Real App Server conformance for isolated one-shot Codex children."""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Any

import openai_codex
import pytest

from ksadk.codex.client import AsyncCodexClient
from ksadk.plugins.subagent_providers.codex import (
    DEFAULT_CODEX_CHILD_PROVIDER_REF,
    CodexOneShotSubagentProvider,
)
from ksadk.plugins.subagents import SpawnSubagentRequest, SubagentPolicy
from tests.e2e.codex_responses_stub import DeterministicResponsesStub

pytestmark = pytest.mark.skipif(
    os.getenv("KSADK_CODEX_SUBAGENT_E2E") != "1",
    reason="set KSADK_CODEX_SUBAGENT_E2E=1 to exercise the real Codex App Server",
)


def _request(run_id: str) -> SpawnSubagentRequest:
    return SpawnSubagentRequest(
        provider_ref=DEFAULT_CODEX_CHILD_PROVIDER_REF,
        parent_session_id="parent-session",
        parent_run_id=run_id,
        task=f"Return the deterministic result for {run_id}.",
        policy=SubagentPolicy(timeout_seconds=10),
    )


class _ObservedClient:
    def __init__(self, delegate: AsyncCodexClient) -> None:
        self.delegate = delegate
        self.interrupt_results: list[bool] = []

    async def start_thread(self, config: dict[str, Any]) -> str:
        return await self.delegate.start_thread(config)

    def run_turn(self, thread_id: str, prompt: str, *, config: dict[str, Any]):  # noqa: ANN201
        return self.delegate.run_turn(thread_id, prompt, config=config)

    async def interrupt_active_turn(self, thread_id: str) -> bool:
        result = await self.delegate.interrupt_active_turn(thread_id)
        self.interrupt_results.append(result)
        return result

    async def close(self) -> None:
        await self.delegate.close()


class _RealFactory:
    def __init__(self, *, responses_url: str) -> None:
        self.responses_url = responses_url
        self.homes: list[Path] = []
        self.clients: list[_ObservedClient] = []
        self.processes: list[Any] = []

    def __call__(self, home: Path) -> _ObservedClient:
        self.homes.append(home)
        (home / "config.toml").write_text(
            f"""model = "ksadk-codex-plugin-stub"
model_provider = "ksadk_provider_stub"
approval_policy = "never"
sandbox_mode = "read-only"

[model_providers.ksadk_provider_stub]
name = "KsADK subagent deterministic E2E"
base_url = "{self.responses_url}"
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
requires_openai_auth = false
""",
            encoding="utf-8",
        )
        delegate = AsyncCodexClient(
            openai_codex.CodexConfig(
                env={
                    "CODEX_HOME": str(home),
                    "CODEX_APP_SERVER_DISABLE_MANAGED_CONFIG": "1",
                    "RUST_LOG": "warn",
                }
            )
        )
        transport = delegate._codex._client._sync
        original_close = transport.close

        def recording_close() -> None:
            process = transport._proc
            try:
                original_close()
            finally:
                if process is not None:
                    self.processes.append(process)

        transport.close = recording_close
        client = _ObservedClient(delegate)
        self.clients.append(client)
        return client


@pytest.mark.asyncio
async def test_real_children_use_distinct_app_servers_threads_and_cleanup(
    tmp_path: Path,
) -> None:
    with DeterministicResponsesStub() as responses:
        factory = _RealFactory(responses_url=responses.base_url)
        provider = CodexOneShotSubagentProvider(
            project_dir=tmp_path,
            model="ksadk-codex-plugin-stub",
            client_factory=factory,
        )
        first = await provider.spawn(_request("run-1"))
        second = await provider.spawn(_request("run-2"))
        first_result, second_result = await asyncio.gather(
            provider.result(first), provider.result(second)
        )

        assert first_result.state == second_result.state == "succeeded"
        assert first_result.output == second_result.output == "bridge skill received"
        assert first.child_session_id != second.child_session_id
        requests = responses.requests()
        assert len(requests) == 2
        assert {request.payload["client_metadata"]["thread_id"] for request in requests} == {
            first.child_session_id,
            second.child_session_id,
        }
        assert len(set(factory.homes)) == 2

        await provider.dispose(first)
        await provider.dispose(second)

    assert all(not home.exists() for home in factory.homes)
    assert len(factory.processes) == 2
    assert len({process.pid for process in factory.processes}) == 2
    assert all(process.poll() is not None for process in factory.processes)


@pytest.mark.asyncio
async def test_real_active_turn_is_interrupted_before_cancel_and_dispose(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    with DeterministicResponsesStub() as responses:
        original_events = responses._events

        def blocking_events(payload: dict[str, Any]):  # noqa: ANN202
            entered.set()
            if not release.wait(timeout=10):
                raise TimeoutError("test did not release blocking model response")
            return original_events(payload)

        responses._events = blocking_events  # type: ignore[method-assign]
        factory = _RealFactory(responses_url=responses.base_url)
        provider = CodexOneShotSubagentProvider(
            project_dir=tmp_path,
            model="ksadk-codex-plugin-stub",
            client_factory=factory,
        )
        handle = await provider.spawn(_request("run-cancel"))
        assert await asyncio.to_thread(entered.wait, 5)

        cancel_task = asyncio.create_task(provider.cancel(handle))
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.wait_for(cancel_task, timeout=5)

        result = await provider.result(handle)
        assert result.state == "cancelled"
        assert factory.clients[0].interrupt_results == [True]
        home = factory.homes[0]
        await provider.dispose(handle)

    assert not home.exists()
    assert len(factory.processes) == 1
    assert factory.processes[0].poll() is not None

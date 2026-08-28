"""Deployed Runtime data-plane contract tests (no network model)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from ksadk.harness.compiler import compile_revision_payload
from ksadk.harness.events import EventType
from ksadk.harness.reasoner import HarnessReasoningTurn
from ksadk.harness.runtime_server import (
    DeploymentRunStore,
    DeploymentRuntime,
    ResumeRequest,
    RunRequest,
    build_deployment_app,
)
from ksadk.runtime import CancelResult, RunHandle

from .test_lifecycle import _revision_payload


class _Reasoner:
    async def complete(self, **kwargs):
        return HarnessReasoningTurn(
            final_text=f"runtime:{kwargs['messages'][-1]['content']}",
            usage={"input_tokens": 5, "output_tokens": 2},
        )


class _CancelEngine:
    async def start(self, request, compiled):
        del compiled
        return RunHandle(
            run_id=request.metadata["invocation_id"],
            session_id=request.session_id,
            runtime_type="test",
            native_ref={"thread_id": "thread-1"},
        )

    async def cancel(self, handle):
        del handle
        return CancelResult.INTERRUPTED_ACTIVE_TURN


class _RecoverableEngine:
    def __init__(self) -> None:
        self.attached = False
        self.resumed = False

    async def compile(self, spec):
        del spec
        return object()

    def is_handle_attached(self, handle):
        del handle
        return self.attached

    async def attach(self, handle, compiled):
        del compiled
        self.attached = True
        return handle

    async def resume(self, handle, target, payload):
        del handle, target, payload
        self.resumed = True


def _app(*, state_dir=None):
    spec = compile_revision_payload(
        _revision_payload(), revision_ref="agent-revision://runtime-test@1"
    )
    return build_deployment_app(
        deployment_id="dep-test",
        route="studio://runtime-test/local",
        spec_payload=spec.model_dump(by_alias=True, mode="json"),
        build_id="bld-test",
        content_hash="sha256:test",
        reasoner=_Reasoner(),
        state_dir=state_dir,
    )


def _request(*, stream: bool = False) -> dict:
    return {
        "input": "hello",
        "userId": "user-1",
        "sessionId": "session-1",
        "agentId": "agent-1",
        "invocationId": "run-1" if not stream else "run-stream",
        "stream": stream,
    }


def test_run_is_rejected_until_deployment_is_activated():
    with TestClient(_app()) as client:
        response = client.post("/runs", json=_request())
        assert response.status_code == 409
        assert response.json()["detail"] == "deployment is not active"


def test_json_run_executes_harness_and_returns_v2_events():
    with TestClient(_app()) as client:
        activated = client.post("/control/activate")
        assert activated.json()["status"] == "active"

        response = client.post("/runs", json=_request())
        assert response.status_code == 200
        result = response.json()
        assert result["runId"] == "run-1"
        assert result["status"] == "completed"
        assert result["events"][-1]["event_type"] == EventType.RUN_COMPLETED
        assert all(event["schema_version"] == 2 for event in result["events"])
        assert all(event["run_id"] == "run-1" for event in result["events"])

        persisted = client.get("/runs/run-1").json()
        assert persisted == result


def test_completed_run_projection_survives_app_restart(tmp_path):
    """Run index is durable even after the in-process Runtime object is replaced."""
    state_dir = tmp_path / "runtime-state"
    with TestClient(_app(state_dir=state_dir)) as client:
        client.post("/control/activate")
        expected = client.post("/runs", json=_request()).json()

    with TestClient(_app(state_dir=state_dir)) as restarted:
        response = restarted.get("/runs/run-1")

    assert response.status_code == 200
    assert response.json() == expected
    assert (state_dir / "checkpoints.sqlite").is_file()
    assert (state_dir / "runs.json").stat().st_mode & 0o777 == 0o600


def test_duplicate_invocation_id_is_rejected_without_second_execution():
    with TestClient(_app()) as client:
        client.post("/control/activate")

        assert client.post("/runs", json=_request()).status_code == 200
        duplicate = client.post("/runs", json=_request())

        assert duplicate.status_code == 409
        assert duplicate.json()["detail"] == "duplicate invocationId: run-1"
        assert client.get("/runs/run-1").json()["status"] == "completed"


def test_sse_run_streams_named_runtime_events():
    with TestClient(_app()) as client:
        client.post("/control/activate")
        with client.stream("POST", "/runs", json=_request(stream=True)) as response:
            body = "".join(response.iter_text())

        assert response.headers["content-type"].startswith("text/event-stream")
        assert f"event: {EventType.RUN_STARTED}" in body
        assert f"event: {EventType.TEXT_COMPLETED}" in body
        assert f"event: {EventType.RUN_COMPLETED}" in body
        data_lines = [
            line.removeprefix("data: ") for line in body.splitlines() if line.startswith("data: ")
        ]
        events = [json.loads(line) for line in data_lines]
        assert all(event["schema_version"] == 2 for event in events)
        assert client.get("/runs/run-stream").json()["status"] == "completed"


@pytest.mark.asyncio
async def test_cancel_updates_the_queryable_run_status():
    spec = compile_revision_payload(
        _revision_payload(), revision_ref="agent-revision://runtime-cancel@1"
    )
    runtime = DeploymentRuntime(
        deployment_id="dep-cancel",
        spec_payload=spec.model_dump(by_alias=True, mode="json"),
        engine=_CancelEngine(),
        activated=True,
    )
    runtime._compiled = object()  # type: ignore[assignment]
    handle = await runtime.start(RunRequest.model_validate(_request()))

    result = await runtime.cancel(handle.run_id)

    assert result["cancelResult"] == CancelResult.INTERRUPTED_ACTIVE_TURN
    assert result["status"] == "canceled"
    assert runtime.get(handle.run_id)["status"] == "canceled"


@pytest.mark.asyncio
async def test_persisted_nonterminal_handle_is_attached_before_resume(tmp_path):
    state_dir = tmp_path / "runtime-state"
    handle = RunHandle(
        run_id="run-paused",
        session_id="session-1",
        runtime_type="managed-langgraph",
        native_ref={
            "thread_id": "thread-paused",
            "user_id": "user-1",
            "agent_id": "agent-1",
        },
    )
    DeploymentRunStore(state_dir).save(
        {
            handle.run_id: {
                "runId": handle.run_id,
                "status": "awaiting_approval",
                "events": [],
            }
        },
        {handle.run_id: handle},
    )
    spec = compile_revision_payload(
        _revision_payload(), revision_ref="agent-revision://runtime-resume@1"
    )
    engine = _RecoverableEngine()
    runtime = DeploymentRuntime(
        deployment_id="dep-resume",
        spec_payload=spec.model_dump(by_alias=True, mode="json"),
        engine=engine,
        activated=True,
        state_dir=state_dir,
    )

    recovered = await runtime.resume(
        handle.run_id,
        ResumeRequest(decision="approved", callId="call-1"),
    )

    assert recovered == handle
    assert engine.attached is True
    assert engine.resumed is True

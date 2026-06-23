#!/usr/bin/env python3
"""Validate KSADK checkpoint resume against a real PostgreSQL backend.

This script is intentionally self-contained so it can run inside a preprod Pod
with the current source tree copied in. It validates the W1 path:

1. LangGraph persists a checkpoint in PostgreSQL.
2. KSADK writes run_checkpoint events into the shared session backend.
3. ListSessionCheckpoints exposes the checkpoint.
4. ResumeRun resumes through LangGraphRunner without rerunning prior nodes.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import uuid
from types import SimpleNamespace
from typing import Any

import httpx

from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runners.base_runner import BaseRunner


AGENT_ID = "lt-w1-e2e-agent"
USER_ID = "lt-w1-e2e-user"
CANCEL_AGENT_ID = "lt-w25-cancel-agent"
CANCEL_USER_ID = "lt-w25-cancel-user"
CANCEL_RESUME_AGENT_ID = "lt-w25-cancel-resume-agent"
CANCEL_RESUME_USER_ID = "lt-w25-cancel-resume-user"
E2E_NODE_COUNTS: dict[str, int] = {}


class E2ELangGraphRunner(LangGraphRunner):
    def load_agent(self) -> None:
        return None


class CancellableStreamingRunner(BaseRunner):
    def __init__(self) -> None:
        super().__init__(
            detection_result=SimpleNamespace(
                name=CANCEL_AGENT_ID,
                type=SimpleNamespace(value="mock"),
            ),
            project_dir=".",
        )
        self.cancel_requests: list[str] = []

    def load_agent(self) -> None:
        return None

    async def invoke(self, input_data: dict[str, Any]) -> dict[str, Any]:
        return {"output": "should not be used"}

    async def stream(self, input_data: dict[str, Any]):
        yield {"type": "text", "delta": "started"}
        await asyncio.Event().wait()

    def request_cancel(self, invocation_id: str) -> str:
        self.cancel_requests.append(str(invocation_id))
        return "accepted"


class CancelThenResumeLangGraphRunner(E2ELangGraphRunner):
    def __init__(self, *args: Any, hold_after_checkpoint: bool = True, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.hold_after_checkpoint = hold_after_checkpoint
        self.cancel_requests: list[str] = []

    async def stream(self, input_data: dict[str, Any]):
        payload = dict(input_data)
        is_checkpoint_resume = bool(payload.get("checkpoint_resume"))
        if is_checkpoint_resume:
            async for chunk in super().stream(payload):
                yield chunk
            return

        result = await self.invoke(payload)
        metadata = result.get("metadata") if isinstance(result, dict) else None
        if isinstance(metadata, dict) and metadata.get("agentengine"):
            yield {"type": "checkpoint", "metadata": metadata}
        yield {"type": "text", "delta": "checkpoint persisted"}
        if self.hold_after_checkpoint:
            await asyncio.Event().wait()

    def request_cancel(self, invocation_id: str) -> str:
        self.cancel_requests.append(str(invocation_id))
        return "accepted"


async def _build_graph(*, dsn: str) -> Any:
    from typing import TypedDict

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.graph import END, StateGraph

    class VerifyState(TypedDict, total=False):
        input: str
        log: list[str]
        answer: str

    def _append(state: VerifyState, node: str) -> VerifyState:
        E2E_NODE_COUNTS[node] = E2E_NODE_COUNTS.get(node, 0) + 1
        log = list(state.get("log") or [])
        log.append(node)
        return {"log": log, "answer": ",".join(log)}

    def node_a(state: VerifyState) -> VerifyState:
        return _append(state, "a")

    def node_b(state: VerifyState) -> VerifyState:
        return _append(state, "b")

    def node_c(state: VerifyState) -> VerifyState:
        return _append(state, "c")

    saver_cm = AsyncPostgresSaver.from_conn_string(dsn)
    saver = await saver_cm.__aenter__()
    await saver.setup()
    graph = StateGraph(VerifyState)
    graph.add_node("a", node_a)
    graph.add_node("b", node_b)
    graph.add_node("c", node_c)
    graph.set_entry_point("a")
    graph.add_edge("a", "b")
    graph.add_edge("b", "c")
    graph.add_edge("c", END)
    app = graph.compile(checkpointer=saver, interrupt_before=["c"])
    app._ksadk_e2e_saver_cm = saver_cm
    return app


async def _build_runner(*, dsn: str) -> LangGraphRunner:
    runner = E2ELangGraphRunner(
        detection_result=SimpleNamespace(
            name=AGENT_ID,
            type=SimpleNamespace(value="langgraph"),
            entry_point="agent.py",
            agent_variable="app",
        ),
        project_dir=".",
    )
    runner._agent = await _build_graph(dsn=dsn)
    runner._module = SimpleNamespace()
    return runner


async def _build_cancel_then_resume_runner(*, dsn: str) -> CancelThenResumeLangGraphRunner:
    runner = CancelThenResumeLangGraphRunner(
        detection_result=SimpleNamespace(
            name=CANCEL_RESUME_AGENT_ID,
            type=SimpleNamespace(value="langgraph"),
            entry_point="agent.py",
            agent_variable="app",
        ),
        project_dir=".",
    )
    runner._agent = await _build_graph(dsn=dsn)
    runner._module = SimpleNamespace()
    return runner


async def _close_runner(runner: LangGraphRunner) -> None:
    agent = getattr(runner, "_agent", None)
    saver_cm = getattr(agent, "_ksadk_e2e_saver_cm", None)
    if saver_cm is not None:
        await saver_cm.__aexit__(None, None, None)


def _action_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("Data")
    if not isinstance(data, dict):
        raise AssertionError(f"Action payload missing Data: {payload}")
    return data


async def _list_events(client: httpx.AsyncClient, session_id: str) -> list[dict[str, Any]]:
    events_response = await client.post(
        "/agentengine/api/v1/ListSessionEvents",
        json={"SessionId": session_id},
    )
    events_response.raise_for_status()
    return _action_data(events_response.json())["Events"]


async def _checkpoint_state_values(runner: LangGraphRunner, checkpoint: dict[str, Any]) -> dict[str, Any]:
    framework_ref = checkpoint.get("FrameworkRef") if isinstance(checkpoint.get("FrameworkRef"), dict) else {}
    langgraph_ref = framework_ref.get("langgraph") if isinstance(framework_ref.get("langgraph"), dict) else {}
    configurable = {
        key: value
        for key, value in {
            "thread_id": langgraph_ref.get("thread_id"),
            "checkpoint_id": langgraph_ref.get("checkpoint_id"),
            "checkpoint_ns": langgraph_ref.get("checkpoint_ns"),
        }.items()
        if value
    }
    if not configurable:
        return {}
    state = await runner._agent.aget_state({"configurable": configurable})
    values = getattr(state, "values", None)
    return dict(values or {}) if isinstance(values, dict) else {}


def _summarize_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for event in events:
        metadata = event.get("Metadata") if isinstance(event.get("Metadata"), dict) else {}
        content = event.get("Content") if isinstance(event.get("Content"), dict) else {}
        summary.append(
            {
                "SeqId": event.get("SeqId"),
                "EventType": event.get("EventType"),
                "Author": event.get("Author"),
                "MetadataKeys": sorted(metadata.keys()),
                "AgentEngine": metadata.get("agentengine"),
                "Content": content,
            }
        )
    return summary


async def run_validation(*, dsn: str, keep_session: bool) -> dict[str, Any]:
    namespace = f"lt_w1_e2e_{uuid.uuid4().hex[:10]}"
    session_id = f"sess_{uuid.uuid4().hex}"
    thread_prefix = f"{namespace}:{AGENT_ID}:{session_id}"
    os.environ["KSADK_SESSION_BACKEND"] = "postgres"
    os.environ["KSADK_SESSION_DSN"] = dsn
    os.environ["KSADK_SESSION_NAMESPACE"] = namespace
    os.environ["KSADK_SESSION_TENANT_ID"] = "lt_w1_e2e_tenant"
    os.environ["KSADK_SESSION_WORKSPACE_ID"] = "lt_w1_e2e_workspace"
    os.environ["KSADK_E2E_LANGGRAPH_DSN"] = dsn
    E2E_NODE_COUNTS.clear()

    runner = await _build_runner(dsn=dsn)
    try:
        from ksadk.sessions import reset_session_service

        server_app_module = importlib.import_module("ksadk.server.app")
        await reset_session_service()
        server_app_module.set_runner(runner)
        transport = httpx.ASGITransport(app=server_app_module.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://ksadk.local",
            timeout=60,
        ) as client:
                run_response = await client.post(
                    "/agentengine/api/v1/RunAgent",
                    json={
                        "AgentId": AGENT_ID,
                        "UserId": USER_ID,
                        "SessionId": session_id,
                        "ApiFormat": "responses",
                        "Stream": False,
                        "ResponsesInput": [
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": "run until checkpoint",
                                    }
                                ],
                            }
                        ],
                    },
                )
                run_response.raise_for_status()
                run_payload = run_response.json()
                checkpoints_response = await client.post(
                    "/agentengine/api/v1/ListSessionCheckpoints",
                    json={"AgentId": AGENT_ID, "SessionId": session_id},
                )
                checkpoints_response.raise_for_status()
                checkpoints = _action_data(checkpoints_response.json())["Checkpoints"]
                if not checkpoints:
                    events = await _list_events(client, session_id)
                    raise AssertionError(
                        "ListSessionCheckpoints returned no checkpoints\n"
                        f"RunAgent payload: {json.dumps(run_payload, ensure_ascii=False)}\n"
                        f"Events: {json.dumps(_summarize_events(events), ensure_ascii=False)}"
                    )
                checkpoint = checkpoints[0]
                run_id = checkpoint["RunId"]
                checkpoint_id = checkpoint["CheckpointId"]
                checkpoint_state = await _checkpoint_state_values(runner, checkpoint)
                checkpoint_log = list(checkpoint_state.get("log") or [])
                if checkpoint_log != ["a", "b"]:
                    raise AssertionError(
                        "Checkpoint state before resume should contain exactly a,b; "
                        f"got {checkpoint_log!r}"
                    )
                node_counts_before_resume = dict(E2E_NODE_COUNTS)

                resume_response = await client.post(
                    "/agentengine/api/v1/ResumeRun",
                    json={
                        "AgentId": AGENT_ID,
                        "SessionId": session_id,
                        "RunId": run_id,
                        "CheckpointId": checkpoint_id,
                        "Stream": False,
                    },
                )
                resume_response.raise_for_status()
                resume_payload = resume_response.json()
                resume_data = _action_data(resume_payload)
                output_text = str(resume_data.get("output_text") or "")
                if output_text != "a,b,c":
                    raise AssertionError(
                        f"ResumeRun output should be 'a,b,c', got {output_text!r}"
                    )
                node_counts_after_resume = dict(E2E_NODE_COUNTS)
                if node_counts_after_resume != {"a": 1, "b": 1, "c": 1}:
                    raise AssertionError(
                        "ResumeRun should not rerun completed nodes; "
                        f"before={node_counts_before_resume}, after={node_counts_after_resume}"
                    )

                events = await _list_events(client, session_id)
                run_checkpoint_count = sum(
                    1 for event in events if event.get("EventType") == "run_checkpoint"
                )
                run_resume_count = sum(
                    1 for event in events if event.get("EventType") == "run_resume"
                )
                if run_checkpoint_count < 2:
                    raise AssertionError(
                        f"Expected at least two run_checkpoint events, got {run_checkpoint_count}"
                    )
                if run_resume_count < 1:
                    raise AssertionError("Expected a run_resume event")

                if not keep_session:
                    delete_response = await client.post(
                        "/agentengine/api/v1/DeleteSession",
                        json={"SessionId": session_id},
                    )
                    delete_response.raise_for_status()

                return {
                    "namespace": namespace,
                    "session_id": session_id,
                    "run_id": run_id,
                    "checkpoint_id": checkpoint_id,
                    "output_text": output_text,
                    "checkpoint_count": len(checkpoints),
                    "run_checkpoint_event_count": run_checkpoint_count,
                    "run_resume_event_count": run_resume_count,
                    "checkpoint_log_before_resume": checkpoint_log,
                    "node_counts_before_resume": node_counts_before_resume,
                    "node_counts_after_resume": node_counts_after_resume,
                    "resume_did_not_rerun_prior_nodes": True,
                    "kept_session": keep_session,
                    "run_action": run_payload.get("Code"),
                    "resume_action": resume_payload.get("Code"),
                }
    finally:
        await _close_runner(runner)


async def run_cancel_validation(*, dsn: str, keep_session: bool) -> dict[str, Any]:
    namespace = f"lt_w25_cancel_{uuid.uuid4().hex[:10]}"
    session_id = f"sess_{uuid.uuid4().hex}"
    invocation_id = f"run_{uuid.uuid4().hex}"
    os.environ["KSADK_SESSION_BACKEND"] = "postgres"
    os.environ["KSADK_SESSION_DSN"] = dsn
    os.environ["KSADK_SESSION_NAMESPACE"] = namespace
    os.environ["KSADK_SESSION_TENANT_ID"] = "lt_w25_cancel_tenant"
    os.environ["KSADK_SESSION_WORKSPACE_ID"] = "lt_w25_cancel_workspace"

    from ksadk.sessions import reset_session_service
    import ksadk.conversations as conversation

    server_app_module = importlib.import_module("ksadk.server.app")
    await reset_session_service()
    runner = CancellableStreamingRunner()
    server_app_module.set_runner(runner)
    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://ksadk.local",
        timeout=60,
    ) as client:
        server_app_module._detached_streaming_response(
            conversation.stream_responses_conversation_turn(
                runner=runner,
                agent_id=CANCEL_AGENT_ID,
                user_id=CANCEL_USER_ID,
                messages=[{"role": "user", "content": "start long streaming run"}],
                session_id=session_id,
                model=None,
                prepare_runner=lambda _runner, _model: None,
                invocation_id=invocation_id,
                session_service_provider=server_app_module.resolve_session_service,
            ),
            invocation_id=invocation_id,
        )

        for _ in range(50):
            events = await _list_events(client, session_id)
            statuses = [
                event.get("Content", {}).get("status")
                for event in events
                if event.get("EventType") == "run_status"
            ]
            if statuses == ["in_progress"]:
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("Cancel validation did not observe in_progress status")

        cancel_response = await client.post(
            "/agentengine/api/v1/CancelRun",
            json={"AgentId": CANCEL_AGENT_ID, "InvocationId": invocation_id},
        )
        cancel_response.raise_for_status()
        cancel_data = _action_data(cancel_response.json())
        if cancel_data.get("Found") is not True or cancel_data.get("Cancelled") is not True:
            raise AssertionError(f"CancelRun did not hit active run: {cancel_data}")

        for _ in range(50):
            events = await _list_events(client, session_id)
            statuses = [
                event.get("Content", {}).get("status")
                for event in events
                if event.get("EventType") == "run_status"
            ]
            if statuses and statuses[-1] == "cancelled":
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("Cancel validation did not observe cancelled status")

        event_count_at_cancel = len(events)
        await asyncio.sleep(3)
        final_events = await _list_events(client, session_id)
        unexpected_terminal = [
            event
            for event in final_events[event_count_at_cancel:]
            if event.get("EventType") in {"assistant_message", "run_checkpoint"}
            or (
                event.get("EventType") == "run_status"
                and event.get("Content", {}).get("status") == "completed"
            )
        ]
        if unexpected_terminal:
            raise AssertionError(
                "Cancel validation observed unexpected events after cancelled: "
                f"{json.dumps(_summarize_events(unexpected_terminal), ensure_ascii=False)}"
            )

        if not keep_session:
            delete_response = await client.post(
                "/agentengine/api/v1/DeleteSession",
                json={"SessionId": session_id},
            )
            delete_response.raise_for_status()

        return {
            "namespace": namespace,
            "session_id": session_id,
            "invocation_id": invocation_id,
            "cancel_action": cancel_response.json().get("Code"),
            "cancel_found": cancel_data.get("Found"),
            "cancel_status": cancel_data.get("Status"),
            "cancelled_event_count": sum(
                1
                for event in final_events
                if event.get("EventType") == "run_status"
                and event.get("Content", {}).get("status") == "cancelled"
            ),
            "post_cancel_extra_event_count": len(final_events) - event_count_at_cancel,
            "runner_cancel_requests": list(runner.cancel_requests),
            "kept_session": keep_session,
        }


async def _wait_for_status(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    status: str,
    attempts: int = 50,
) -> list[dict[str, Any]]:
    for _ in range(attempts):
        events = await _list_events(client, session_id)
        statuses = [
            event.get("Content", {}).get("status")
            for event in events
            if event.get("EventType") == "run_status"
        ]
        if statuses and statuses[-1] == status:
            return events
        await asyncio.sleep(0.1)
    raise AssertionError(f"Did not observe run_status={status!r}")


async def _wait_for_checkpoint(
    client: httpx.AsyncClient,
    *,
    agent_id: str,
    session_id: str,
    attempts: int = 50,
) -> dict[str, Any]:
    for _ in range(attempts):
        response = await client.post(
            "/agentengine/api/v1/ListSessionCheckpoints",
            json={"AgentId": agent_id, "SessionId": session_id},
        )
        response.raise_for_status()
        checkpoints = _action_data(response.json())["Checkpoints"]
        if checkpoints:
            return checkpoints[0]
        await asyncio.sleep(0.1)
    events = await _list_events(client, session_id)
    raise AssertionError(
        "Did not observe checkpoint before cancel\n"
        f"Events: {json.dumps(_summarize_events(events), ensure_ascii=False)}"
    )


async def run_cancel_then_resume_validation(*, dsn: str, keep_session: bool) -> dict[str, Any]:
    namespace = f"lt_w25_cancel_resume_{uuid.uuid4().hex[:10]}"
    session_id = f"sess_{uuid.uuid4().hex}"
    invocation_id = f"run_{uuid.uuid4().hex}"
    os.environ["KSADK_SESSION_BACKEND"] = "postgres"
    os.environ["KSADK_SESSION_DSN"] = dsn
    os.environ["KSADK_SESSION_NAMESPACE"] = namespace
    os.environ["KSADK_SESSION_TENANT_ID"] = "lt_w25_cancel_resume_tenant"
    os.environ["KSADK_SESSION_WORKSPACE_ID"] = "lt_w25_cancel_resume_workspace"
    os.environ["KSADK_E2E_LANGGRAPH_DSN"] = dsn
    E2E_NODE_COUNTS.clear()

    runner = await _build_cancel_then_resume_runner(dsn=dsn)
    try:
        from ksadk.sessions import reset_session_service
        import ksadk.conversations as conversation

        server_app_module = importlib.import_module("ksadk.server.app")
        await reset_session_service()
        server_app_module.set_runner(runner)
        transport = httpx.ASGITransport(app=server_app_module.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://ksadk.local",
            timeout=60,
        ) as client:
            server_app_module._detached_streaming_response(
                conversation.stream_responses_conversation_turn(
                    runner=runner,
                    agent_id=CANCEL_RESUME_AGENT_ID,
                    user_id=CANCEL_RESUME_USER_ID,
                    messages=[{"role": "user", "content": "start, checkpoint, then wait"}],
                    session_id=session_id,
                    model=None,
                    prepare_runner=lambda _runner, _model: None,
                    invocation_id=invocation_id,
                    session_service_provider=server_app_module.resolve_session_service,
                ),
                invocation_id=invocation_id,
            )

            await _wait_for_status(client, session_id=session_id, status="in_progress")
            checkpoint = await _wait_for_checkpoint(
                client,
                agent_id=CANCEL_RESUME_AGENT_ID,
                session_id=session_id,
            )
            run_id = checkpoint["RunId"]
            checkpoint_id = checkpoint["CheckpointId"]
            if run_id != invocation_id:
                raise AssertionError(
                    f"Checkpoint run_id should match cancelled invocation_id; {run_id!r} != {invocation_id!r}"
                )
            checkpoint_state = await _checkpoint_state_values(runner, checkpoint)
            checkpoint_log = list(checkpoint_state.get("log") or [])
            if checkpoint_log != ["a", "b"]:
                raise AssertionError(
                    "Checkpoint state before cancel should contain exactly a,b; "
                    f"got {checkpoint_log!r}"
                )

            cancel_response = await client.post(
                "/agentengine/api/v1/CancelRun",
                json={"AgentId": CANCEL_RESUME_AGENT_ID, "InvocationId": invocation_id},
            )
            cancel_response.raise_for_status()
            cancel_data = _action_data(cancel_response.json())
            if cancel_data.get("Found") is not True or cancel_data.get("Cancelled") is not True:
                raise AssertionError(f"CancelRun did not hit checkpointed active run: {cancel_data}")

            cancelled_events = await _wait_for_status(
                client,
                session_id=session_id,
                status="cancelled",
            )
            event_count_at_cancel = len(cancelled_events)
            await asyncio.sleep(1)
            post_cancel_events = await _list_events(client, session_id)
            unexpected_post_cancel = [
                event
                for event in post_cancel_events[event_count_at_cancel:]
                if event.get("EventType") in {"assistant_message", "run_checkpoint"}
                or (
                    event.get("EventType") == "run_status"
                    and event.get("Content", {}).get("status") == "completed"
                )
            ]
            if unexpected_post_cancel:
                raise AssertionError(
                    "Cancel then resume validation observed unexpected events after cancelled: "
                    f"{json.dumps(_summarize_events(unexpected_post_cancel), ensure_ascii=False)}"
                )

            node_counts_before_resume = dict(E2E_NODE_COUNTS)
            resume_response = await client.post(
                "/agentengine/api/v1/ResumeRun",
                json={
                    "AgentId": CANCEL_RESUME_AGENT_ID,
                    "SessionId": session_id,
                    "RunId": run_id,
                    "CheckpointId": checkpoint_id,
                    "Stream": False,
                },
            )
            resume_response.raise_for_status()
            resume_payload = resume_response.json()
            resume_data = _action_data(resume_payload)
            output_text = str(resume_data.get("output_text") or "")
            if output_text != "a,b,c":
                raise AssertionError(
                    f"ResumeRun after cancel should output 'a,b,c', got {output_text!r}"
                )
            node_counts_after_resume = dict(E2E_NODE_COUNTS)
            if node_counts_after_resume != {"a": 1, "b": 1, "c": 1}:
                raise AssertionError(
                    "ResumeRun after cancel should not rerun completed nodes; "
                    f"before={node_counts_before_resume}, after={node_counts_after_resume}"
                )

            final_events = await _list_events(client, session_id)
            run_checkpoint_count = sum(
                1 for event in final_events if event.get("EventType") == "run_checkpoint"
            )
            run_resume_count = sum(
                1 for event in final_events if event.get("EventType") == "run_resume"
            )
            cancelled_event_count = sum(
                1
                for event in final_events
                if event.get("EventType") == "run_status"
                and event.get("Content", {}).get("status") == "cancelled"
            )
            if run_checkpoint_count < 2:
                raise AssertionError(
                    f"Expected at least two run_checkpoint events after resume, got {run_checkpoint_count}"
                )
            if run_resume_count < 1:
                raise AssertionError("Expected a run_resume event after cancel")

            if not keep_session:
                delete_response = await client.post(
                    "/agentengine/api/v1/DeleteSession",
                    json={"SessionId": session_id},
                )
                delete_response.raise_for_status()

            return {
                "namespace": namespace,
                "session_id": session_id,
                "run_id": run_id,
                "invocation_id": invocation_id,
                "checkpoint_id": checkpoint_id,
                "cancel_action": cancel_response.json().get("Code"),
                "cancel_found": cancel_data.get("Found"),
                "cancel_status": cancel_data.get("Status"),
                "cancelled_event_count": cancelled_event_count,
                "post_cancel_extra_event_count": len(post_cancel_events) - event_count_at_cancel,
                "runner_cancel_requests": list(runner.cancel_requests),
                "output_text_after_resume": output_text,
                "run_checkpoint_event_count": run_checkpoint_count,
                "run_resume_event_count": run_resume_count,
                "checkpoint_log_before_cancel": checkpoint_log,
                "node_counts_before_resume": node_counts_before_resume,
                "node_counts_after_resume": node_counts_after_resume,
                "resume_after_cancel_did_not_rerun_prior_nodes": True,
                "kept_session": keep_session,
                "resume_action": resume_payload.get("Code"),
            }
    finally:
        await _close_runner(runner)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dsn",
        default=os.environ.get("KSADK_SESSION_DSN", ""),
        help="PostgreSQL DSN. Defaults to KSADK_SESSION_DSN.",
    )
    parser.add_argument(
        "--keep-session",
        action="store_true",
        help="Keep the generated KSADK session rows for debugging.",
    )
    parser.add_argument(
        "--include-cancel",
        action="store_true",
        help="Also validate W2.5 detached streaming CancelRun behavior.",
    )
    args = parser.parse_args()
    dsn = args.dsn.strip()
    if not dsn:
        raise SystemExit("--dsn or KSADK_SESSION_DSN is required")
    async def _run_all() -> dict[str, Any]:
        result: dict[str, Any] = {
            "checkpoint_resume": await run_validation(
                dsn=dsn,
                keep_session=args.keep_session,
            )
        }
        if args.include_cancel:
            result["runtime_cancel"] = await run_cancel_validation(
                dsn=dsn,
                keep_session=args.keep_session,
            )
            result["cancel_then_resume"] = await run_cancel_then_resume_validation(
                dsn=dsn,
                keep_session=args.keep_session,
            )
        return result

    result = asyncio.run(_run_all())
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

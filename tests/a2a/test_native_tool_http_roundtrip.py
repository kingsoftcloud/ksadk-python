"""Native ADK generates every canonical fact; only model responses are scripted."""

from types import SimpleNamespace

import pytest
from a2a.types import GetTaskRequest, TaskState
from fastapi import FastAPI
from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types
from google.protobuf.json_format import MessageToDict

from ksadk.a2a.presentation import ToolActivity
from ksadk.a2a.routes import A2AConfig, add_a2a_protocol_routes
from ksadk.a2a.task_adapter import A2ARuntimeTaskAdapter
from ksadk.a2a.tool_activity import PublicToolActivityPolicy, PublicToolRule
from ksadk.runners.adk_runner import ADKRunner
from ksadk.runtime.framework_adapters import ADKRuntimeAdapter
from tests.a2a.test_a2a_protocol_e2e import _client_for, _close, _send


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_text,second_text", [("first answer", "second answer"), ("same", "same")]
)
async def test_native_adk_tool_stream_and_reopened_get_task(tmp_path, first_text, second_text):
    from ksadk.sessions import bind_session_service
    from ksadk.sessions.local_service import LocalSessionService

    public_sessions = LocalSessionService(tmp_path / "public.db")
    await public_sessions.create_session(
        agent_id="tool_callee", user_id="anonymous", session_id="native-tool-session"
    )
    calls = []

    def lookup(city: str) -> dict:
        """Return a deterministic local reading."""
        calls.append(city)
        return {"value": 42, "accessToken": "fake-private-value", "hidden": "private"}

    class FixtureLlm(BaseLlm):
        model: str = "fixture"

        async def generate_content_async(self, llm_request, stream=False):
            has_result = any(
                p.function_response is not None for c in llm_request.contents for p in c.parts
            )
            if has_result:
                yield LlmResponse(
                    content=types.Content(role="model", parts=[types.Part(text=second_text)])
                )
            else:
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part(text=first_text),
                            types.Part(
                                function_call=types.FunctionCall(
                                    id="call_1", name="lookup", args={"city": "Paris"}
                                )
                            ),
                        ],
                    )
                )

    agent = LlmAgent(name="tool_callee", model=FixtureLlm(), tools=[lookup])
    native_app = App(name=agent.name, root_agent=agent)
    sessions = DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{tmp_path}/native.db")
    runner = ADKRunner(
        SimpleNamespace(entry_point="agent.py", agent_variable="root_agent", name=agent.name),
        ".",
    )
    runner._agent = agent
    runner._module = SimpleNamespace(app=native_app)
    runner._runner = Runner(app=native_app, session_service=sessions)
    runner._session_service = sessions
    runner._short_term_memory = SimpleNamespace(
        backend="database", session_service=sessions, create_session=sessions.create_session
    )
    await runner.prepare_runtime_capabilities()
    runtime = ADKRuntimeAdapter(runner)
    canonical = []
    original_stream = runtime.stream

    async def record_stream(handle):
        async for event in original_stream(handle):
            canonical.append(event)
            yield event

    runtime.stream = record_stream
    task_adapter = A2ARuntimeTaskAdapter(runtime, runtime_type="adk")
    policy = PublicToolActivityPolicy(
        {
            "lookup": PublicToolRule(
                "weather", frozenset({"city"}), frozenset({"value", "accessToken"})
            )
        }
    )
    task_id = None
    original = None
    live_activities = []
    live_order = []
    for stage in ("send", "reopen"):
        app = FastAPI()
        server = add_a2a_protocol_routes(
            app,
            A2AConfig(
                enabled=True,
                base_url="http://testserver",
                agent_name=agent.name,
                task_store_dsn=f"sqlite+aiosqlite:///{tmp_path}/tasks.db",
                public_tool_policy=policy,
            ),
            task_adapter=task_adapter,
        )
        client, http = await _client_for(app, card=server.agent_card)
        try:
            if stage == "send":
                send_request = _send("lookup")
                send_request.message.context_id = "native-tool-session"
                with bind_session_service(public_sessions):
                    async for response in client.send_message(send_request):
                        if response.task and response.task.id:
                            task_id = response.task.id
                        if response.HasField("artifact_update"):
                            for part in response.artifact_update.artifact.parts:
                                if part.HasField("text"):
                                    live_order.append(("text", part.text))
                                if part.HasField("data"):
                                    activity = ToolActivity.model_validate(MessageToDict(part.data))
                                    live_activities.append(activity)
                                    live_order.append((activity.item_kind, activity.operation))
                assert task_id
            task = await client.get_task(GetTaskRequest(id=task_id))
            assert task.status.state == TaskState.TASK_STATE_COMPLETED, [
                e.model_dump(mode="json") for e in canonical if e.event_type == "run.failed"
            ]
            assert calls == ["Paris"]
            text = "".join(p.text for a in task.artifacts for p in a.parts if p.HasField("text"))
            assert text == first_text + second_text, [
                e.model_dump(mode="json")
                for e in canonical
                if getattr(e, "item_kind", None) == "message"
            ]
            activities = [
                ToolActivity.model_validate(MessageToDict(p.data))
                for a in task.artifacts
                for p in a.parts
                if p.HasField("data")
            ]
            results = [
                a for a in activities if a.item_kind == "tool_result" and a.operation == "completed"
            ]
            assert len(results) == 1
            assert results[0].result == {"value": 42.0, "accessToken": "[REDACTED]"}
            assert "fake-private-value" not in str(MessageToDict(task))
            assert "hidden" not in str(MessageToDict(task))
            assert any(
                a.operation == "started" and a.execution_status == "running"
                for a in live_activities
            )
            assert any(a.event_id == results[0].event_id for a in live_activities)
            first_position = live_order.index(("text", first_text))
            call_position = live_order.index(("tool_call", "started"))
            result_position = live_order.index(("tool_result", "completed"))
            second_position = live_order.index(("text", second_text), result_position + 1)
            assert first_position < call_position < result_position < second_position
            assert len([a for a in task.artifacts if any(p.HasField("text") for p in a.parts)]) == 2
            current = MessageToDict(task)
            if original is None:
                original = current
            else:
                assert current == original
        finally:
            await _close(client, http)

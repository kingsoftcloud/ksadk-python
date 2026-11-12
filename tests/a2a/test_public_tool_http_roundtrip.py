"""Real FunctionTool execution survives HTTP serialization and durable GetTask."""

import pytest
from a2a.types import GetTaskRequest, TaskState
from fastapi import FastAPI
from google.protobuf.json_format import MessageToDict

from ksadk.a2a.presentation import ToolActivity
from ksadk.a2a.routes import A2AConfig, add_a2a_protocol_routes
from ksadk.a2a.tool_activity import PublicToolActivityPolicy, PublicToolRule
from ksadk.runtime import RunHandle
from tests.a2a.test_a2a_protocol_e2e import _client_for, _close, _send
from tests.a2a.test_public_tool_producer_r85 import ActualToolAdapter


class HTTPToolAdapter(ActualToolAdapter):
    async def start_task(self, *, task_id, context, input_data):
        return RunHandle(runtime_type="test", run_id=task_id, session_id=context.context_id)


@pytest.mark.asyncio
async def test_function_tool_public_facts_survive_reopened_taskstore(tmp_path):
    policy = PublicToolActivityPolicy(
        {
            "lookup": PublicToolRule(
                "weather", frozenset({"city"}), frozenset({"value", "access_token"})
            )
        }
    )
    task_id = None
    original = None
    for stage in ("send", "read"):
        adapter = HTTPToolAdapter()
        app = FastAPI()
        server = add_a2a_protocol_routes(
            app,
            A2AConfig(
                enabled=True,
                base_url="http://testserver",
                agent_name="tool-callee",
                task_store_dsn=f"sqlite+aiosqlite:///{tmp_path}/tasks.db",
                public_tool_policy=policy,
            ),
            task_adapter=adapter,
        )
        client, http = await _client_for(app, card=server.agent_card)
        try:
            if stage == "send":
                async for response in client.send_message(_send("lookup")):
                    if response.task and response.task.id:
                        task_id = response.task.id
                assert task_id
            task = await client.get_task(GetTaskRequest(id=task_id))
            assert task.status.state == TaskState.TASK_STATE_COMPLETED
            activities = [
                ToolActivity.model_validate(MessageToDict(part.data))
                for artifact in task.artifacts
                for part in artifact.parts
                if part.HasField("data")
            ]
            assert activities
            results = [
                value
                for value in activities
                if value.item_kind == "tool_result" and value.operation == "completed"
            ]
            assert len(results) == 1
            assert results[0].result == {"value": 42.0, "access_token": "[REDACTED]"}
            assert "private-value" not in str(MessageToDict(task))
            assert "hidden" not in str(MessageToDict(task))
            current = [value.model_dump(mode="json") for value in activities]
            if stage == "send":
                original = current
                assert adapter.calls == 1
            else:
                assert current == original
                assert adapter.calls == 0
                again = await client.get_task(GetTaskRequest(id=task_id))
                assert again == task and adapter.calls == 0
        finally:
            await _close(client, http)

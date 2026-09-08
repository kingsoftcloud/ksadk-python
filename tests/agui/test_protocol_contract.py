import inspect

from ag_ui.encoder import EventEncoder
from ag_ui_langgraph import add_langgraph_fastapi_endpoint
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ksadk.agui.config import (
    AGUI_LANGGRAPH_VERSION,
    AGUI_PROTOCOL_VERSION,
    COPILOTKIT_VERSION,
    agui_dependencies_available,
)


class _DuckAgent:
    name = "duck"
    clone_count = 0
    run_count = 0

    def clone(self):
        type(self).clone_count += 1
        return self

    async def run(self, input_data):
        from ag_ui.core import RunFinishedEvent, RunFinishedSuccessOutcome, RunStartedEvent

        type(self).run_count += 1
        yield RunStartedEvent(thread_id=input_data.thread_id, run_id=input_data.run_id)
        yield RunFinishedEvent(
            thread_id=input_data.thread_id,
            run_id=input_data.run_id,
            outcome=RunFinishedSuccessOutcome(),
        )


def _request_payload():
    return {
        "threadId": "thread-1",
        "runId": "run-1",
        "state": {},
        "messages": [{"id": "u1", "role": "user", "content": "hello"}],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }


def test_pinned_official_versions_are_available():
    from importlib.metadata import version

    assert agui_dependencies_available()
    assert version("ag-ui-protocol") == AGUI_PROTOCOL_VERSION
    assert version("ag-ui-langgraph") == AGUI_LANGGRAPH_VERSION
    assert version("copilotkit") == COPILOTKIT_VERSION


def test_official_helper_contract_remains_clone_run_and_event_encoder():
    source = inspect.getsource(add_langgraph_fastapi_endpoint)
    assert "agent.clone()" in source
    assert "request_agent.run(input_data)" in source
    assert "EventEncoder" in source
    assert "encoder.encode(event)" in source
    assert "StreamingResponse" in source


def test_official_helper_accepts_the_pinned_duck_seam_and_encodes_sse():
    app = FastAPI()
    agent = _DuckAgent()
    add_langgraph_fastapi_endpoint(app, agent, path="/agentengine/agui")

    response = TestClient(app).post(
        "/agentengine/agui",
        json=_request_payload(),
        headers={"accept": "text/event-stream"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(EventEncoder().get_content_type())
    assert '"type":"RUN_STARTED"' in response.text
    assert '"type":"RUN_FINISHED"' in response.text
    assert _DuckAgent.clone_count == 1
    assert _DuckAgent.run_count == 1

from pathlib import Path
from time import time

import pytest

from ksadk.events.reducer import StreamReducer
from ksadk.harness import HarnessServiceRuntimeAdapter
from ksadk.harness.service_runtime import _project
from ksadk.runtime.factory import create_runtime_adapter
from ksadk.runtime.launch import RuntimeLaunchContext


def test_default_harness_registry_uses_service_binding(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("KSADK_HARNESS_SERVICE_URL", "http://127.0.0.1:8787")
    monkeypatch.setenv("KSADK_HARNESS_SERVICE_TOKEN", "test-harness-service-token")
    context = RuntimeLaunchContext(runtime_type="harness", project_dir=tmp_path)
    adapter = create_runtime_adapter(context)
    assert isinstance(adapter, HarnessServiceRuntimeAdapter)
    assert adapter.capabilities().durable_restore.supported
    assert not adapter.capabilities().submit_interaction.supported
    monkeypatch.setenv("KSADK_HARNESS_SERVICE_WORKSPACE", str(tmp_path))
    assert isinstance(create_runtime_adapter(context), HarnessServiceRuntimeAdapter)
    monkeypatch.setenv("KSADK_HARNESS_SERVICE_WORKSPACE", str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="invalid Harness service workspace mount"):
        create_runtime_adapter(context)
    monkeypatch.delenv("KSADK_HARNESS_SERVICE_WORKSPACE")
    monkeypatch.delenv("KSADK_HARNESS_SERVICE_TOKEN")
    with pytest.raises(ValueError, match="invalid Harness service endpoint or credential"):
        create_runtime_adapter(context)


def test_projected_history_can_be_replayed_without_conflicting_event_ids():
    journal = [
        {"seq": 1, "kind": "run.started"},
        {"seq": 2, "kind": "model.completed", "usage": {"inputTokens": 12, "outputTokens": 5}},
        {"seq": 3, "kind": "run.terminal", "title": "completed", "detail": "Done."},
    ]
    reducer = StreamReducer()
    for _ in range(2):
        for row in journal:
            for event in _project("remote-run", row):
                reducer.apply(event)
    assert reducer.recent_event_count == 5


def test_public_harness_view_carries_only_display_fields():
    from ksadk.conversations.projector import project_conversation_item

    digest = "a" * 64
    event = {
        "schema": "agentkit.engine-event/v1", "seq": 2, "kind": "tool.completed",
        "category": "tool", "title": "write_workspace_file", "status": "ok",
        "arguments": {"credential": "fixture-should-not-project"},
        "result": "fixture-should-not-project",
    }
    runtime_event = _project("run", event, digest)[0]
    item = project_conversation_item(runtime_event, session_id="session")
    assert item.payload["harnessView"]["sha256"] == digest
    assert item.payload["harnessView"]["event"]["title"] == "write_workspace_file"
    assert "fixture-should-not-project" not in item.model_dump_json()


@pytest.mark.asyncio
async def test_service_start_only_sends_images_with_negotiated_support(monkeypatch):
    from ksadk.runtime.adapter import StartRequest

    adapter = HarnessServiceRuntimeAdapter(
        endpoint="http://127.0.0.1:8787",
        token="test-harness-service-token",
    )
    url = "data:image/png;base64,AAAA"
    request = StartRequest(
        input="",
        user_id="user",
        session_id="session",
        metadata={"invocation_id": "run-canonical", "conversation_request": {"messages": [
            {"role": "user", "content": [
                {"type": "input_image", "image_url": "data:image/png;base64,BBBB"},
            ]},
            {"role": "user", "content": [{"type": "input_image", "image_url": url}]},
        ]}},
    )
    calls = []
    capabilities = {}

    async def rpc(session, method, **params):
        calls.append((method, params))
        if method == "initialize":
            return {"protocolVersion": "agentkit.engine/v1", "capabilities": capabilities}
        return {}

    monkeypatch.setattr(adapter, "_rpc", rpc)
    with pytest.raises(ValueError, match="does not support image input"):
        await adapter.start(request)
    assert [method for method, _ in calls] == ["initialize"]

    capabilities["inputImages"] = {"maxCount": 8, "maxEncodedBytes": 1024}
    handle = await adapter.start(request)
    method, params = calls[-1]
    assert method == "run/start"
    assert params["runId"] == handle.run_id == "run-canonical"
    assert params["objective"]
    assert params["inputImages"] == [
        {"type": "image_url", "image_url": {"url": url, "detail": "auto"}},
    ]
    assert "BBBB" not in str(params)
    calls.clear()
    with pytest.raises(ValueError, match="Selected model does not match"):
        await adapter.start(request.model_copy(update={"model": "another-model"}))
    assert [method for method, _ in calls] == ["initialize"]


@pytest.mark.asyncio
async def test_service_host_callback_requires_original_run_and_activation():
    from ksadk.harness.service_runtime import ServiceHostTool
    from ksadk.runtime.adapter import RunHandle

    adapter = HarnessServiceRuntimeAdapter(
        endpoint="http://127.0.0.1:8787", token="test-harness-service-token",
    )
    calls = []

    async def call(arguments):
        calls.append(arguments)
        return "authorized"

    async def catalog():
        return (ServiceHostTool("bound_tool", "fixture", {"type": "object"}, call),)

    async def context(_request, _rows):
        return []

    adapter.bind_read_only_workspace(
        binding="original-binding", model="fixture", load_tools=catalog, context=context,
    )
    handle = RunHandle(
        run_id="original-run", session_id="session", runtime_type="harness",
        native_ref={"resource_binding": "original-binding"},
    )
    frame = {
        "method": "host/callTool", "expiresAt": int(time() * 1000) + 5000,
        "params": {
            "runId": handle.run_id, "callId": "call", "name": "bound_tool", "arguments": {},
        },
    }
    for rejected in (
        {**frame, "expiresAt": 0},
        {**frame, "params": {**frame["params"], "runId": "other-run"}},
        {**frame, "params": {**frame["params"], "name": "unbound_tool"}},
    ):
        assert (await adapter._execute_host_tool(handle, rejected))["status"] == "error"
    stale = handle.model_copy(update={"native_ref": {"resource_binding": "new-binding"}})
    assert (await adapter._execute_host_tool(stale, frame))["status"] == "error"
    assert calls == []
    assert await adapter._execute_host_tool(handle, frame) == {
        "status": "ok", "content": "authorized",
    }
    assert calls == [{}]


@pytest.mark.asyncio
async def test_service_binding_rejects_another_actor_or_session_before_rpc(monkeypatch):
    from ksadk.runtime.adapter import RunHandle, StartRequest

    adapter = HarnessServiceRuntimeAdapter(
        endpoint="http://127.0.0.1:8787", token="test-harness-service-token",
    )

    async def no_network(*args, **kwargs):
        pytest.fail("unbound request reached the Harness service")

    monkeypatch.setattr(adapter, "_rpc", no_network)
    adapter.bind_read_only_workspace(
        binding="fixture-binding", model="fixture", load_tools=no_network, context=no_network,
        scope={"agent_id": "agent", "user_id": "owner", "session_id": "session"},
    )
    for overrides in ({"user_id": "other"}, {"agent_id": "other"}, {"session_id": "other"}):
        fields = {"user_id": "owner", "agent_id": "agent", "session_id": "session", **overrides}
        with pytest.raises(ValueError, match="bound activation scope"):
            await adapter.start(StartRequest(input="hello", **fields))
    with pytest.raises(ValueError, match="bound activation scope"):
        await adapter.attach(RunHandle(
            run_id="run", session_id="session", runtime_type="harness",
            native_ref={
                "binding": "agentkit.engine-service/v1", "agent_id": "agent", "user_id": "other",
            },
        ))


@pytest.mark.asyncio
async def test_service_hook_keeps_the_original_bound_tool_identity():
    from ksadk.harness.service_runtime import ServiceHook, ServiceHostTool
    from ksadk.runtime.adapter import RunHandle

    adapter = HarnessServiceRuntimeAdapter(
        endpoint="http://127.0.0.1:8787", token="test-harness-service-token",
    )
    admitted = []

    async def invoke(handle, params):
        admitted.append((handle.run_id, params["input"]["call"]["name"]))
        return {"status": "failed", "reason": "policy_denied", "output": None}

    async def tool(_arguments):
        pytest.fail("a Hook callback must not call the tool")

    async def catalog():
        return (ServiceHostTool(
            "service_alias", "fixture", {"type": "object"}, tool, source_name="real_tool",
        ),)

    async def context(_request, _rows):
        return []

    adapter.bind_read_only_workspace(
        binding="original", model="fixture", load_tools=catalog, context=context,
        hooks=(ServiceHook({"hookId": "fixture", "toolNames": ["real_tool"]}, invoke),),
    )
    handle = RunHandle(
        run_id="run", session_id="session", runtime_type="harness",
        native_ref={"resource_binding": "original"},
    )
    params = {
        "contract": "agentkit.engine-hooks/v1", "runId": "run",
        "operationKey": "hook-op", "inputDigest": "hook-input-digest",
        "definition": {"hookId": "fixture", "toolNames": ["service_alias"]},
        "input": {"call": {"name": "service_alias"}},
    }
    frame = {"params": params, "expiresAt": int(time() * 1000) + 5000}
    forged = {**params, "definition": {"hookId": "fixture", "toolNames": ["wrong"]}}
    assert (await adapter._execute_host_hook(handle, {**frame, "params": forged})) == {
        "code": "ksadk_hook_unavailable",
    }
    assert not admitted
    result = await adapter._execute_host_hook(handle, frame)
    assert result["operationKey"] == "hook-op"
    assert result["reason"] == "policy_denied"
    assert admitted == [("run", "real_tool")]

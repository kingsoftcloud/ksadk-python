from ksadk.a2a.control_plane import InternalA2AControlPlaneClient
from ksadk.a2a.space_client import A2ASpaceClient


def test_managed_control_plane_precedes_legacy_service(monkeypatch):
    monkeypatch.setenv("KSADK_A2A_CONTROL_PLANE_URL", "https://control.example.test")
    monkeypatch.setenv("KSADK_A2A_SERVICE_URL", "https://legacy.example.test")
    monkeypatch.setattr("ksadk.a2a.platform._managed_http_verify", lambda: True)
    client = A2ASpaceClient.from_env(space_id="as-test")
    assert isinstance(client.control_plane, InternalA2AControlPlaneClient)
    assert client.control_plane._base_url == "https://control.example.test"


def test_explicit_backend_keeps_precedence(monkeypatch):
    monkeypatch.setenv("KSADK_A2A_CONTROL_PLANE_URL", "https://control.example.test")
    backend = object()
    assert A2ASpaceClient.from_env(space_id="as-test", backend=backend).control_plane is backend


async def test_hosted_owned_http_uses_gateway_ca_and_closes(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import ksadk.a2a.space_client as module

    sentinel = object()
    http = SimpleNamespace(aclose=AsyncMock())
    protocol = SimpleNamespace(close=AsyncMock())
    captured = {}

    def http_factory(**kwargs):
        captured.update(kwargs)
        return http

    monkeypatch.setattr(module.httpx, "AsyncClient", http_factory)
    monkeypatch.setattr("ksadk.a2a.platform._gateway_http_verify", lambda: sentinel)
    monkeypatch.setattr(module, "create_client", AsyncMock(return_value=protocol))
    dispatcher = SimpleNamespace(start=AsyncMock())
    client = A2ASpaceClient(
        "as-test", SimpleNamespace(gateway_token=lambda: "token"), event_dispatcher=dispatcher
    )
    monkeypatch.setattr(client, "_card_for_route", lambda card, route: card)
    route = SimpleNamespace(kind="hosted_gateway", interface=object())
    async with client._operation_client(
        SimpleNamespace(agent_card=object()), SimpleNamespace(route=route, call_permit="permit")
    ):
        assert captured == {"trust_env": False, "verify": sentinel}
    http.aclose.assert_awaited_once()
    protocol.close.assert_awaited_once()


async def test_managed_task_events_use_canonical_envelope():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from a2a.types import Task, TaskState, TaskStatus

    from ksadk.events.canonical import parse_runtime_event

    dispatcher = SimpleNamespace(enqueue=AsyncMock())
    client = A2ASpaceClient(
        "as-test", SimpleNamespace(task_event_format="canonical_v2"), event_dispatcher=dispatcher
    )
    task = Task(
        id="remote-task",
        context_id="remote-context",
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
    )
    await client._project_stream_item(
        "a2a-task-test",
        task,
        SimpleNamespace(agent_id="agent-test"),
        wire_position=0,
        operation_instance_id="operation-test",
    )
    events = dispatcher.enqueue.call_args.kwargs["events"]
    assert events
    for event in events:
        assert event["schema_version"] == 2
        assert event["event_id"] and event["run_id"] and event["scope_id"]
        assert "SourceEventId" not in event
        parse_runtime_event(event)


async def test_new_managed_observations_do_not_reuse_event_ids():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from a2a.types import Task, TaskState, TaskStatus

    ids = []
    for operation in ["query-one", "query-two", "query-one"]:
        dispatcher = SimpleNamespace(enqueue=AsyncMock())
        client = A2ASpaceClient(
            "as-test",
            SimpleNamespace(task_event_format="canonical_v2"),
            event_dispatcher=dispatcher,
        )
        task = Task(
            id="remote-task",
            context_id="context",
            status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
        )
        projected = await client._project_stream_item(
            "a2a-task-test",
            task,
            SimpleNamespace(agent_id="agent-test"),
            wire_position=0,
            operation_instance_id=operation,
        )
        queued = dispatcher.enqueue.call_args.kwargs["events"]
        assert [e.event_id for e in projected] == [e["event_id"] for e in queued]
        ids.append([e["event_id"] for e in queued])
    assert ids[0] != ids[1]
    assert ids[0] == ids[2]

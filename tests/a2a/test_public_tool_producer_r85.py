from types import SimpleNamespace

import pytest
from a2a.types import Artifact, TaskArtifactUpdateEvent
from google.adk.tools import FunctionTool
from google.protobuf.json_format import MessageToDict

from ksadk.a2a.executor import A2ARuntimeExecutor
from ksadk.a2a.tool_activity import PublicToolActivityPolicy, PublicToolRule
from ksadk.events.adapters.a2a import A2AAdapterContext, A2AEventAdapter
from ksadk.events.canonical import ContentSnapshot, ItemCompleted, SourceRef
from ksadk.events.content import ToolCallContent, ToolResultContent
from ksadk.runtime import RunHandle


class RecordingUpdater:
    def __init__(self):
        self.artifacts = []

    async def add_artifact(self, **kwargs):
        self.artifacts.append(kwargs)


@pytest.mark.parametrize(
    "payload",
    [
        '{"tools":{"lookup":{"name":"bad label"}}}',
        '{"tools":{"lookup":{"name":"weather","results":"*"}}}',
        '{"tools":{"lookup":{"name":"weather","allow_all":true}}}',
        '{"tools":{},"allow_all":true}',
    ],
)
def test_platform_public_tool_declaration_rejects_unsafe_or_ambiguous_policy(payload):
    with pytest.raises(ValueError):
        PublicToolActivityPolicy.from_json(payload)


def test_platform_public_tool_declaration_uses_explicit_fields_only():
    policy = PublicToolActivityPolicy.from_json(
        '{"tools":{"lookup":{"name":"weather","results":["value"]}}}'
    )
    assert policy.tools == {"lookup": PublicToolRule("weather", results=frozenset({"value"}))}


class ActualToolAdapter:
    def __init__(self):
        self.calls = 0

    async def stream_task(self, handle):
        def lookup(city: str) -> dict:
            """Look up a deterministic local test reading."""
            self.calls += 1
            return {"value": 42, "access_token": "private-value", "hidden": "private"}

        tool = FunctionTool(lookup)
        common = dict(
            schema_version=2,
            seq=0,
            timestamp=1.0,
            run_id=handle.run_id,
            scope_id="callee",
            source=SourceRef(framework="adk"),
        )
        yield ItemCompleted(
            **common,
            event_id="tool-call",
            item_id="tool-item",
            item_kind="tool_call",
            snapshot=ContentSnapshot(
                parts=(
                    ToolCallContent(
                        part_id="call", call_id="call_1", name="lookup", arguments={"city": "Paris"}
                    ),
                )
            ),
        )
        result = await tool.run_async(args={"city": "Paris"}, tool_context=None)
        yield ItemCompleted(
            **common,
            event_id="tool-result",
            item_id="result-item",
            item_kind="tool_result",
            snapshot=ContentSnapshot(
                parts=(ToolResultContent(part_id="result", call_id="call_1", result=result),)
            ),
        )

    def was_cancel_accepted(self, *_):
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize("restore_after_each", [False, True])
async def test_actual_adk_tool_executes_once_then_executor_wire_mapper_never_reexecutes(
    restore_after_each, monkeypatch,
):
    # Protobuf Struct maps have no ordering guarantee across stream/GetTask.
    import ksadk.events.adapters._a2a_tool_activity as tool_mapper

    decode = tool_mapper.MessageToDict
    reverse = False

    def reordered_decode(message):
        nonlocal reverse
        value = decode(message)
        if isinstance(value.get("result"), dict):
            reverse = not reverse
            pairs = sorted(value["result"].items(), reverse=reverse)
            value["result"] = dict(pairs)
        return value

    monkeypatch.setattr(tool_mapper, "MessageToDict", reordered_decode)
    adapter = ActualToolAdapter()
    policy = PublicToolActivityPolicy(
        {
            "lookup": PublicToolRule(
                "weather", frozenset({"city"}), frozenset({"value", "access_token"})
            )
        }
    )
    executor = A2ARuntimeExecutor(adapter, public_tool_policy=policy)
    updater = RecordingUpdater()
    await executor._run_runtime(
        SimpleNamespace(task_id="task"),
        updater,
        RunHandle(runtime_type="test", run_id="run", session_id="session"),
    )
    assert adapter.calls == 1
    assert len(updater.artifacts) == 3
    operations = [MessageToDict(value["parts"][0].data)["operation"] for value in updater.artifacts]
    assert operations == ["started", "completed", "completed"]
    assert [
        MessageToDict(value["parts"][0].data)["execution_status"] for value in updater.artifacts
    ] == ["running", "running", "completed"]
    projected = []
    mapper = A2AEventAdapter()
    context = A2AAdapterContext(
        run_id="root",
        context_id="context",
        task_id="task",
        fixed_scope_id="child",
        parent_scope_id="root-scope",
    )
    for value in updater.artifacts:
        artifact = Artifact(
            artifact_id=value["artifact_id"], parts=value["parts"], metadata=value["metadata"]
        )
        # Serialize and parse the real pinned A2A protobuf as a wire round trip.
        native = TaskArtifactUpdateEvent(
            task_id="task", context_id="context", artifact=artifact, append=False, last_chunk=True
        )
        native = TaskArtifactUpdateEvent.FromString(native.SerializeToString())
        projected.extend(mapper.map_event(native, context, timestamp=2.0))
        assert mapper.map_event(native, context, timestamp=3.0) == ()
        if restore_after_each:
            mapper, context = A2AEventAdapter.restore_checkpoint(mapper.checkpoint(context))
    results = [
        e for e in projected if isinstance(e, ItemCompleted) and e.item_kind == "tool_result"
    ]
    assert results[0].snapshot.parts[0].result == {"value": 42.0, "access_token": "[REDACTED]"}
    assert "hidden" not in str(projected)
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_tools_are_private_by_default():
    adapter = ActualToolAdapter()
    updater = RecordingUpdater()
    await A2ARuntimeExecutor(adapter)._run_runtime(
        SimpleNamespace(task_id="task"),
        updater,
        RunHandle(runtime_type="test", run_id="run", session_id="s"),
    )
    assert adapter.calls == 1
    assert updater.artifacts == []


@pytest.mark.asyncio
async def test_failed_public_write_can_be_retried_without_losing_the_event():
    from ksadk.a2a.tool_activity import PublicToolActivityProducer

    producer = PublicToolActivityProducer(
        PublicToolActivityPolicy({"lookup": PublicToolRule("weather")})
    )
    event = await anext(
        ActualToolAdapter().stream_task(
            RunHandle(runtime_type="test", run_id="run", session_id="s")
        )
    )

    class FailingUpdater:
        async def add_artifact(self, **kwargs):
            raise OSError("test store unavailable")

    with pytest.raises(OSError, match="test store unavailable"):
        await producer.emit(event, FailingUpdater())
    updater = RecordingUpdater()
    await producer.emit(event, updater)
    await producer.emit(event, updater)
    assert len(updater.artifacts) == 2
    assert [MessageToDict(value["parts"][0].data)["operation"] for value in updater.artifacts] == [
        "started",
        "completed",
    ]


def test_routes_and_server_share_public_tool_disclosure_policy():
    from a2a.server.tasks import InMemoryTaskStore
    from fastapi import FastAPI

    from ksadk.a2a.presentation import A2A_TOOL_ACTIVITY_EXTENSION_URI
    from ksadk.a2a.routes import A2AConfig, add_a2a_protocol_routes

    policy = PublicToolActivityPolicy({"lookup": PublicToolRule("weather")})
    server = add_a2a_protocol_routes(
        FastAPI(),
        A2AConfig(public_tool_policy=policy),
        task_adapter=ActualToolAdapter(),
        task_store=InMemoryTaskStore(),
    )
    assert server.executor.public_tool_policy is policy
    assert A2A_TOOL_ACTIVITY_EXTENSION_URI in [
        x.uri for x in server.agent_card.capabilities.extensions
    ]


@pytest.mark.asyncio
async def test_invocation_budget_emits_one_retryable_truncation_observation():
    from ksadk.a2a.presentation import A2A_TOOL_ACTIVITY_MAX_INVOCATION_BYTES, ToolActivity
    from ksadk.a2a.tool_activity import PublicToolActivityProducer

    producer = PublicToolActivityProducer(PublicToolActivityPolicy())
    updater = RecordingUpdater()
    failed_marker = False

    class FailMarkerOnce:
        async def add_artifact(self, **kwargs):
            nonlocal failed_marker
            if MessageToDict(kwargs["parts"][0].data)["truncated"] and not failed_marker:
                failed_marker = True
                raise OSError("marker write failed")
            await updater.add_artifact(**kwargs)

    writer = FailMarkerOnce()
    for index in range(2000):
        payload = {
            "event_id": f"event-{index}",
            "item_id": f"item-{index}",
            "call_id": f"call-{index}",
            "item_kind": "tool_result",
            "operation": "completed",
            "execution_status": "completed",
            "result": {"value": "x" * 3000},
        }
        arguments = dict(
            activity_payload=payload,
            public_item=payload["item_id"],
            public_event=payload["event_id"],
            updater=writer,
        )
        try:
            await producer._publish(**arguments)
        except OSError:
            await producer._publish(**arguments)
        if failed_marker:
            await producer._publish(**arguments)
            break
    assert failed_marker
    activities = [
        ToolActivity.model_validate(MessageToDict(a["parts"][0].data)) for a in updater.artifacts
    ]
    assert sum(a.truncated for a in activities) == 1
    assert activities[-1].result is None
    assert sum(len(a.model_dump_json().encode()) for a in activities) <= (
        A2A_TOOL_ACTIVITY_MAX_INVOCATION_BYTES
    )

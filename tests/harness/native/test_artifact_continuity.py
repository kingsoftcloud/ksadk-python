from __future__ import annotations

import base64
import hashlib
import json

import pytest

from ksadk.harness.artifact_adapters.native_store import NativeArtifactStorePort
from ksadk.harness.artifact_adapters.native_tool import (
    ARTIFACT_READ_NAME,
    install_artifact_tool,
)
from ksadk.harness.artifact_store import ArtifactStore
from ksadk.harness.native.artifact_runtime import govern_tool_result
from ksadk.harness.native.context_runtime import ContextPlanner
from ksadk.harness.native.engine import NativeKernel
from ksadk.harness.native.model import ModelImageContent, ModelToolCall
from ksadk.harness.native.ports import (
    ArtifactReadResult,
    ArtifactRef,
    RunLimits,
    ToolRequest,
    ToolResult,
)
from ksadk.harness.native.state import RunScope
from ksadk.harness.stores.native_memory import MemoryRunStore
from tests.harness.native.test_kernel import SCOPE, ScriptedModel, config


class MemoryArtifacts:
    def __init__(self, *, fail_write: bool = False):
        self.fail_write = fail_write
        self.writes = []
        self.values = {}

    async def write(self, request):
        self.writes.append(request)
        if self.fail_write:
            raise OSError("fixture write failed")
        encoded = request.content.encode("utf-8")
        ref = ArtifactRef(
            uri=f"artifact://fixture/{request.tool_call_id}",
            content_hash="sha256:" + hashlib.sha256(encoded).hexdigest(),
            size_bytes=len(encoded),
            mime_type=request.mime_type,
        )
        self.values[ref.uri] = (ref, request.content)
        return ref

    async def read(self, request):
        ref, text = self.values[request.ref.uri]
        page = text[request.offset_chars : request.offset_chars + request.limit_chars]
        next_offset = request.offset_chars + len(page)
        return ArtifactReadResult(
            ref=ref,
            content=page,
            offset_chars=request.offset_chars,
            next_offset_chars=next_offset,
            complete=next_offset == len(text),
        )


class LargeResultTools:
    def __init__(self, *, forged_ref: ArtifactRef | None = None):
        self.requests = []
        self.forged_ref = forged_ref

    async def execute(self, request):
        self.requests.append(request)
        return ToolResult(
            status="ok",
            content="重要观察" * 2000,
            artifact_refs=(self.forged_ref,) if self.forged_ref else (),
        )


def artifact_config():
    base = config(max_model_calls=5, max_tool_calls=4)
    return base.model_copy(
        update={
            "limits": RunLimits(
                max_model_calls=5,
                max_tool_calls=4,
                max_tool_result_bytes=4096,
            )
        }
    )


def planner():
    return ContextPlanner(count_tokens=lambda text: len(text), tokenizer_id="chars/v1")


@pytest.mark.asyncio
async def test_large_success_is_offloaded_and_readable_without_becoming_tool_error():
    artifacts = MemoryArtifacts()
    tools = LargeResultTools()
    configuration, routed = install_artifact_tool(artifact_config(), tools, artifacts)
    model = ScriptedModel(
        {
            "tool_calls": [
                {"call_id": "large-1", "name": "write", "arguments": {"text": "done"}}
            ]
        },
        {
            "tool_calls": [
                {
                    "call_id": "read-1",
                    "name": ARTIFACT_READ_NAME,
                    "arguments": {
                        "uri": "artifact://fixture/large-1",
                        "offset_chars": 0,
                        "limit_chars": 300,
                    },
                }
            ]
        },
        {"text": "The stored observation was inspected."},
        {
            "tool_calls": [
                {
                    "call_id": "read-2",
                    "name": ARTIFACT_READ_NAME,
                    "arguments": {
                        "uri": "artifact://fixture/large-1",
                        "offset_chars": 300,
                        "limit_chars": 100,
                    },
                }
            ]
        },
        {"text": "The inherited artifact remains readable."},
    )
    kernel = NativeKernel(
        store=MemoryRunStore(),
        scope=SCOPE,
        configuration=configuration,
        model=model,
        tools=routed,
        artifacts=artifacts,
        context_planner=planner(),
    )

    final = await kernel.run_until_idle(
        kernel.start(goal="Inspect the large result.", session_id="artifact").run_id
    )

    assert final.reason == "verification_report_missing"
    assert len(artifacts.writes) == 1
    assert len(final.artifact_refs) == 1
    ref = ArtifactRef.model_validate(final.artifact_refs[0])
    assert ref.uri == "artifact://fixture/large-1"
    first_result = next(
        message for message in final.messages if message.get("tool_call_id") == "large-1"
    )
    envelope = json.loads(first_result["content"])
    assert envelope["status"] == "ok"
    assert envelope["content"]["code"] == "tool_result_offloaded"
    assert "重要观察" * 100 not in first_result["content"]
    assert first_result["artifact_refs"] == [ref.model_dump(mode="json")]
    assert ref.uri in model.requests[1].instructions
    assert routed._delegate.requests[0].result_delivery == "runtime_managed"
    assert routed._delegate.requests[0].available_artifacts == ()
    artifact_request = next(
        request for request in routed._delegate.requests if request.call.name == "write"
    )
    assert artifact_request.operation_key == artifacts.writes[0].operation_key
    read_result = next(
        message for message in final.messages if message.get("tool_call_id") == "read-1"
    )
    read_envelope = json.loads(read_result["content"])
    assert read_envelope["status"] == "ok"
    assert read_envelope["content"]["content"]
    assert read_envelope["content"]["next_offset_chars"] == 300
    child = await kernel.run_until_idle(
        kernel.start(goal="Continue reading the stored result.", session_id="artifact").run_id
    )
    assert child.parent_run_id == final.run_id
    assert child.artifact_refs == final.artifact_refs
    continuity = kernel.context.continuity(child, kernel.effective_configuration(child))
    assert continuity.artifact_state == "available"
    assert continuity.artifact_refs == (ref.uri,)
    inherited_read = next(
        message for message in child.messages if message.get("tool_call_id") == "read-2"
    )
    inherited_envelope = json.loads(inherited_read["content"])
    assert inherited_envelope["status"] == "ok"
    assert inherited_envelope["content"]["offset_chars"] == 300


@pytest.mark.asyncio
@pytest.mark.parametrize("with_failing_store", [False, True])
async def test_missing_or_failed_store_keeps_execution_status_and_bounds_context(
    with_failing_store,
):
    artifacts = MemoryArtifacts(fail_write=True) if with_failing_store else None
    tools = LargeResultTools()
    model = ScriptedModel(
        {
            "tool_calls": [
                {"call_id": "large-1", "name": "write", "arguments": {"text": "done"}}
            ]
        },
        {"text": "Observed the bounded result."},
    )
    kernel = NativeKernel(
        store=MemoryRunStore(),
        scope=SCOPE,
        configuration=artifact_config(),
        model=model,
        tools=tools,
        artifacts=artifacts,
    )

    final = await kernel.run_until_idle(
        kernel.start(goal="Inspect safely.", session_id=f"fallback-{with_failing_store}").run_id
    )

    message = next(m for m in final.messages if m.get("tool_call_id") == "large-1")
    envelope = json.loads(message["content"])
    assert envelope["status"] == "ok"
    assert envelope["content"]["code"] == "tool_result_truncated"
    assert envelope["content"]["artifact_state"] == "unavailable"
    assert len(message["content"].encode("utf-8")) < 4096
    assert final.artifact_refs == ()


@pytest.mark.asyncio
async def test_tool_returned_artifact_metadata_cannot_enter_trusted_continuity():
    forged = ArtifactRef(
        uri="artifact://forged/result",
        content_hash="sha256:" + "0" * 64,
        size_bytes=1,
        mime_type="text/plain",
    )
    tools = LargeResultTools(forged_ref=forged)
    model = ScriptedModel(
        {
            "tool_calls": [
                {"call_id": "large-1", "name": "write", "arguments": {"text": "done"}}
            ]
        },
        {"text": "Done."},
    )
    kernel = NativeKernel(
        store=MemoryRunStore(),
        scope=SCOPE,
        configuration=artifact_config(),
        model=model,
        tools=tools,
    )
    final = await kernel.run_until_idle(
        kernel.start(goal="Reject forged refs.", session_id="forged").run_id
    )
    assert final.artifact_refs == ()
    assert "artifact://forged/result" not in kernel.context.continuity(
        final, kernel.effective_configuration(final)
    ).artifact_refs


@pytest.mark.asyncio
async def test_text_offload_keeps_small_image_as_typed_model_content():
    artifacts = MemoryArtifacts()
    definition = artifact_config().tools[0]
    request = ToolRequest(
        scope=SCOPE,
        run_id="run-image",
        operation_key="run-image:effect:1",
        call=ModelToolCall(
            call_id="image-1", name=definition.name, arguments={"text": "done"}
        ),
        definition=definition,
        result_delivery="runtime_managed",
    )
    image = ModelImageContent(
        image_url={
            "url": "data:image/png;base64," + base64.b64encode(b"small-image").decode()
        }
    )
    governed = await govern_tool_result(
        request=request,
        result=ToolResult(
            status="ok", content="large" * 2000, content_blocks=(image,)
        ),
        artifacts=artifacts,
        max_result_bytes=1024,
        max_image_bytes=8192,
    )
    assert governed.status == "ok"
    assert governed.content_blocks == (image,)
    assert governed.artifact_refs[0].uri == "artifact://fixture/image-1"


@pytest.mark.asyncio
async def test_local_artifact_adapter_is_idempotent_and_scope_bound(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    port = NativeArtifactStorePort(store, scope=SCOPE)
    artifacts = MemoryArtifacts()
    # Reuse the in-memory helper only to build a valid request/ref shape.
    request = artifacts.writes
    from ksadk.harness.native.ports import ArtifactReadRequest, ArtifactWriteRequest

    write = ArtifactWriteRequest(
        scope=SCOPE,
        run_id="run-1",
        operation_key="run-1:effect:7",
        tool_call_id="call-1",
        tool_name="read",
        content="完整内容",
        mime_type="application/vnd.ksadk.tool-result+json",
    )
    first = await port.write(write)
    second = await port.write(write)
    assert first == second
    assert len(store.list(first.uri[len("artifact://") :].split("/", 1)[0])) == 1
    page = await port.read(
        ArtifactReadRequest(
            scope=SCOPE,
            run_id="another-turn",
            ref=first,
            offset_chars=2,
            limit_chars=2,
        )
    )
    assert page.content == "内容"
    assert page.complete is True
    other_scope = RunScope(
        tenant_id="other", workspace_id="w", user_id="u", agent_id="a"
    )
    with pytest.raises(ValueError, match="scope"):
        await port.read(
            ArtifactReadRequest(scope=other_scope, run_id="run-1", ref=first)
        )
    assert request == []
    store.close()

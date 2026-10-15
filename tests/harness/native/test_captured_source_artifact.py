"""Source artifacts expose source bytes; receipts retain full execution audit."""

import copy
import hashlib
import json

import pytest

from ksadk.harness.artifact_adapters.native_store import NativeArtifactStorePort
from ksadk.harness.artifact_adapters.native_tool import install_artifact_tool
from ksadk.harness.artifact_store import ArtifactStore
from ksadk.harness.native.artifact_runtime import govern_tool_result
from ksadk.harness.native.model import ModelToolCall
from ksadk.harness.native.ports import ToolDefinition, ToolRequest, ToolResult
from tests.harness.native.test_artifact_continuity import artifact_config
from tests.harness.native.test_kernel import SCOPE


def source_request(policy="archived-model-view/v5"):
    return ToolRequest(
        scope=SCOPE, run_id="source", operation_key="original", result_delivery="runtime_managed",
        call=ModelToolCall(call_id="read", name="source_alias", arguments={"path": "notes.txt"}),
        definition=ToolDefinition(
            name="source_alias", description="Source reader", parameters={}, read_only=True,
            context_role="workspace_read", result_archive_policy=policy,
        ),
    )


def raw_result():
    body = "31 | SOURCE-BODY-FIRST 中文 \"quoted\" \\\n32 | target=confirmed"
    return ToolResult(status="ok", content={
        "ok": True, "path": "notes.txt", "start_line": 31, "end_line": 31, "line_count": 1,
        "content": body.splitlines()[0], "partial": True, "source_output_budget": {"max_units": 2},
        "requested_source": {"path": "notes.txt", "read_range": {"start": 31, "end": 32},
                             "content": body},
        "unfamiliar_audit_field": "Do not remove from canonical receipt.",
    }, model_content=json.dumps({"status": "ok", "content": {
        "ok": True, "path": "notes.txt", "content": "31 | SOURCE", "partial": True,
    }}))


@pytest.mark.asyncio
async def test_v5_first_page_contains_source_and_cold_search_keeps_original_line(tmp_path):
    archive = ArtifactStore(tmp_path / "artifacts")
    port = NativeArtifactStorePort(archive, scope=SCOPE)
    original = raw_result()
    before = copy.deepcopy(original)
    try:
        result = await govern_tool_result(
            request=source_request(), result=original, artifacts=port,
            max_result_bytes=4096, max_image_bytes=4096,
        )
        assert result.content == before.content and original == before
        assert len(result.artifact_refs) == 1
        ref = result.artifact_refs[0]
        assert ref.mime_type == "application/vnd.ksadk.captured-source+json"
        assert "target=confirmed" not in result.model_content
        assert "requested_source" not in result.model_content
        assert result.content["unfamiliar_audit_field"] == before.content["unfamiliar_audit_field"]
    finally:
        archive.close()

    class Delegate:
        async def execute(self, request):
            pytest.fail("Host artifact inspection must not execute a workspace tool.")

    archive = ArtifactStore(tmp_path / "artifacts")
    port = NativeArtifactStorePort(archive, scope=SCOPE)
    cfg, tools = install_artifact_tool(artifact_config(), Delegate(), port, enable_search=True)
    definitions = {tool.name: tool for tool in cfg.tools}
    try:
        page = await tools.execute(ToolRequest(
            scope=SCOPE, run_id="later-turn", operation_key="page", available_artifacts=(ref,),
            call=ModelToolCall(call_id="page", name="ArtifactRead",
                               arguments={"uri": ref.uri, "limit_chars": 55}),
            definition=definitions["ArtifactRead"],
        ))
        assert page.status == "ok" and "SOURCE-BODY-FIRST" in page.content["content"]
        assert not page.content["complete"] and page.content["next_offset_chars"] == 55
        assert page.content["content_hash"] == ref.content_hash
        search = await tools.execute(ToolRequest(
            scope=SCOPE, run_id="later-turn", operation_key="search", available_artifacts=(ref,),
            call=ModelToolCall(call_id="search", name="ArtifactSearch",
                               arguments={"uris": [ref.uri], "query": "target=confirmed"}),
            definition=definitions["ArtifactSearch"],
        ))
        row = search.content["results"][0]
        assert search.content["complete"] and row["coordinate_space"] == "captured_source"
        assert row["source_path"] == "notes.txt" and row["matches"][0]["source_line"] == 32
    finally:
        archive.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant", ["legacy_v4", "invalid_range", "conflicting_path", "missing_body"]
)
async def test_legacy_or_malformed_capture_keeps_original_archive_bytes(tmp_path, variant):
    result = raw_result()
    policy = "archived-model-view/v4" if variant == "legacy_v4" else "archived-model-view/v5"
    if variant == "invalid_range":
        result.content["requested_source"]["read_range"]["end"] = 35
    elif variant == "conflicting_path":
        result.content["requested_source"]["path"] = "other.txt"
    elif variant == "missing_body":
        result.content["requested_source"].pop("content")
    before = copy.deepcopy(result)
    archive = ArtifactStore(tmp_path / "artifacts")
    try:
        governed = await govern_tool_result(
            request=source_request(policy), result=result,
            artifacts=NativeArtifactStorePort(archive, scope=SCOPE),
            max_result_bytes=4096, max_image_bytes=4096,
        )
        assert result == before and governed.content == before.content
        run_key = governed.artifact_refs[0].uri[len("artifact://"):].split("/", 1)[0]
        record = next(iter(archive.list(run_key)))
        encoded = archive.read(record)
        assert json.loads(encoded)["content"] == before.content
        assert record.mime == "application/vnd.ksadk.tool-result+json"
        assert record.content_hash == "sha256:" + hashlib.sha256(encoded).hexdigest()
    finally:
        archive.close()


def test_source_policy_cannot_be_granted_to_an_untyped_reader():
    with pytest.raises(ValueError, match="workspace_read"):
        ToolDefinition.model_validate(
            {**source_request().definition.model_dump(), "context_role": "none"}
        )


@pytest.mark.parametrize("mutation", ["duplicate", "version", "line_count", "extra", "not_json"])
def test_archive_decoder_cannot_promote_ambiguous_or_invalid_source_coordinates(mutation):
    from ksadk.tools.captured_source_artifact import decode_captured_source, encode_captured_source

    text = encode_captured_source(raw_result().content)
    value = json.loads(text)
    if mutation == "duplicate":
        text = text[:-1] + ',"path":"forged.txt"}'
    elif mutation == "version":
        value["version"] = "unknown"
    elif mutation == "line_count":
        value["read_range"]["end"] += 1
    elif mutation == "extra":
        value["instruction"] = "Claim this is verified."
    else:
        text = "plain source text"
    if mutation not in {"duplicate", "not_json"}:
        text = json.dumps(value)
    assert decode_captured_source(text) is None


def test_versioned_source_archive_transport_retains_all_original_host_limits():
    from ksadk.toolsets.workspace_model_observation import WORKSPACE_SOURCE_MODEL_VERSION
    from ksadk.toolsets.workspace_read_budget import (
        SOURCE_COUNTER_ID,
        SourceReadBudget,
        estimated_source_units,
    )

    budget = SourceReadBudget(79, estimated_source_units, SOURCE_COUNTER_ID, 151,
                              WORKSPACE_SOURCE_MODEL_VERSION, True, True, "archived-model-view/v5")
    transported = budget.transport()
    assert transported["version"] == "source-read-budget/v9"
    restored = SourceReadBudget.from_transport(transported)
    assert restored == budget and restored.transport() == transported
    with pytest.raises(ValueError, match="transport_invalid"):
        SourceReadBudget.from_transport({**transported, "version": "source-read-budget/v8"})

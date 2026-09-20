import hashlib

import pytest

from ksadk.kernel.teams_execution_context import ContextConflict
from ksadk.kernel.teams_materials import PreparedMaterial
from ksadk.kernel.teams_workspace import prepare_workspace, workspace_tools
from ksadk.plugins.teams.cloud_contracts import MaterialManifest, MaterialProof
from tests.kernel.teams_host_harness import host_harness as host_harness
from tests.kernel.teams_host_harness import temporary_postgres as temporary_postgres


async def test_material_copy_is_private_and_replay_keeps_working_report(host_harness, tmp_path):
    h = host_harness
    source = tmp_path / "cache"
    source.mkdir()
    content = b"verified source"
    (source / "input.txt").write_bytes(content)
    manifest = MaterialManifest(
        kind="files",
        entries=[
            {
                "path": "input.txt",
                "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                "sizeBytes": len(content),
                "mediaType": "text/plain",
            }
        ],
    )
    context = h.context.model_copy(
        update={
            "material_manifest_ref": "material-original",
            "material_manifest_digest": manifest.manifest_digest,
        }
    )
    prepared = PreparedMaterial(
        MaterialProof(manifestRef="material-original", digest=manifest.manifest_digest), source
    )
    workspace = prepare_workspace(
        tmp_path / "work", context, prepared_material=prepared, manifest=manifest
    )
    seen = []

    async def authorize(ctx, operation):
        assert ctx == context
        seen.append(operation)

    tools = workspace_tools(workspace, context=context, authorize=authorize)
    await tools["write_file"].call(
        {"path": "input.txt", "content": "working copy"}, call_id="write"
    )
    assert (source / "input.txt").read_bytes() == content
    assert (
        prepare_workspace(tmp_path / "work", context, prepared_material=prepared, manifest=manifest)
        == workspace
    )
    assert (await tools["read_file"].call({"path": "input.txt"}))["content"] == "working copy"
    assert (await tools["list_files"].call({}))["files"] == [{"path": "input.txt", "sizeBytes": 12}]
    assert "read_file" in seen and seen.count("write_file") == 2


async def test_workspace_paths_and_revocation_fail_closed(host_harness, tmp_path):
    context = host_harness.context
    workspace = prepare_workspace(tmp_path / "work", context)
    allowed = True

    async def authorize(*args):
        if not allowed:
            raise PermissionError("revoked")

    tools = workspace_tools(workspace, context=context, authorize=authorize)
    outside = tmp_path / "outside"
    outside.write_text("secret")
    (workspace / "link").symlink_to(outside)
    with pytest.raises(OSError):
        await tools["read_file"].call({"path": "link"})
    with pytest.raises(ValueError):
        await tools["read_file"].call({"path": "../outside"})
    with pytest.raises(ContextConflict):
        await tools["write_file"].call(
            {"path": ".teams-workspace", "content": "overwrite"}, call_id="write"
        )
    with pytest.raises(ContextConflict):
        await tools["list_files"].call({})
    allowed = False
    with pytest.raises(PermissionError):
        await tools["write_file"].call({"path": "report", "content": "forbidden"}, call_id="write")
    assert not (workspace / "report").exists()
    assert outside.read_text() == "secret"
    with pytest.raises(ContextConflict):
        prepare_workspace(
            tmp_path / "work", context.model_copy(update={"context_digest": "sha256:" + "1" * 64})
        )

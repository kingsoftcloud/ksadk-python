import asyncio
import hashlib
import os
from contextlib import asynccontextmanager

import httpx
import pytest
from pydantic import ValidationError

from ksadk.kernel.teams_artifact_tools import TeamsArtifactTools
from ksadk.kernel.teams_execution_context import ContextConflict
from ksadk.kernel.teams_material_transport import (
    MaterialHTTPAuthorization,
    TeamsHTTPMaterialTransport,
)
from ksadk.plugins.teams.cloud_contracts import MAX_FILE_BYTES, digest
from tests.kernel.teams_host_harness import host_harness as host_harness
from tests.kernel.teams_host_harness import temporary_postgres as temporary_postgres


class ArtifactServer:
    """Byte/receipt boundary only; production HTTP/PG is tested in Server tests."""

    def __init__(self, context):
        self.context = context
        self.operations, self.objects, self.artifacts = {}, {}, {}
        self.invocations, self.create_requests, self.downloads = [], [], 0
        self.allowed, self.lose_finalize, self.corrupt = True, False, False
        self.wait = None
        self.on_upload = None
        self.on_read = None

    async def authorize(self, context, operation):
        assert context == self.context
        if not self.allowed:
            raise ContextConflict("current execution revoked")

    async def create_artifact(self, context, body):
        self.create_requests.append(dict(body))
        key, payload = (
            body["idempotencyKey"],
            {k: v for k, v in body.items() if k != "idempotencyKey"},
        )
        if key in self.operations and self.operations[key] != payload:
            raise ContextConflict("idempotency payload changed")
        self.operations[key] = payload
        ref = "tar_" + digest(payload)[7:]
        value = self.artifacts.setdefault(
            ref,
            {
                "artifactId": ref,
                "groupId": context.ref.groupId,
                "teamRunId": context.ref.teamRunId,
                **payload,
                "state": "pending",
                "producer": {
                    "contextRef": context.context_ref,
                    "ref": context.ref.model_dump(mode="json"),
                    "nativeRunId": "run-real",
                    "storeIncarnation": "original-store",
                },
            },
        )
        return dict(value)

    async def upload_artifact(self, context, artifact_id, chunks, *, size_bytes):
        if self.on_upload:
            self.on_upload()
        data = b"".join([chunk async for chunk in chunks])
        assert (
            len(data) == size_bytes
            and "sha256:" + hashlib.sha256(data).hexdigest()
            == self.artifacts[artifact_id]["digest"]
        )
        self.objects[artifact_id] = data

    async def finalize_artifact(self, context, artifact_id, *, idempotency_key):
        assert artifact_id in self.objects
        self.artifacts[artifact_id]["state"] = "ready"
        if self.lose_finalize:
            self.lose_finalize = False
            raise ConnectionError("finalize ACK lost after persistence")
        return dict(self.artifacts[artifact_id])

    async def invoke(self, operation, arguments, call_id):
        await self.authorize(self.context, operation)
        self.invocations.append((operation, dict(arguments), call_id))
        ref = arguments["path"] if operation == "team_publish_artifact" else arguments["artifactId"]
        raw = self.artifacts[ref]
        assert raw["state"] == "ready"
        ctx = self.context
        return {
            k: raw[k]
            for k in (
                "artifactId",
                "groupId",
                "teamRunId",
                "name",
                "mediaType",
                "digest",
                "sizeBytes",
                "state",
            )
        } | {
            "revision": 1,
            "uri": "https://untrusted.invalid/never-follow-response-uri",
            "source": {
                "authorityRef": ctx.ref.authorityId,
                "groupId": ctx.ref.groupId,
                "memberId": ctx.ref.memberId,
                "bindingRef": ctx.ref.bindingRef,
                "providerRef": ctx.ref.providerRef,
                "sessionId": ctx.ref.sessionId,
                "runId": "run-real",
            },
        }

    @asynccontextmanager
    async def open_artifact(self, context, group_id, artifact_id):
        assert group_id == context.ref.groupId and artifact_id in self.artifacts
        self.downloads += 1
        data = b"corrupt" if self.corrupt else self.objects[artifact_id]

        async def chunks():
            if self.wait:
                await self.wait.wait()
            if self.on_read:
                self.on_read()
            for start in range(0, len(data), 4096):
                await asyncio.sleep(0)
                yield data[start : start + 4096]

        yield chunks()


def fixture(h, tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    remote = ArtifactServer(h.context)
    helper = TeamsArtifactTools(
        h.context,
        workspace=workspace,
        transport=remote,
        invoke=remote.invoke,
        authorize=remote.authorize,
    )
    return helper, remote, workspace


async def publish(helper, workspace, *, value=b"verified file", call_id="stable-publish"):
    path = workspace / "report.md"
    path.write_bytes(value)
    return await helper.tools()["team_publish_artifact"].call(
        {"path": "report.md", "name": "Report"}, call_id=call_id
    )


async def test_publish_uploads_exact_bytes_and_invokes_only_ready_reference(host_harness, tmp_path):
    helper, remote, workspace = fixture(host_harness, tmp_path)
    result = await publish(helper, workspace)
    assert remote.objects[result["artifactId"]] == b"verified file"
    assert result["digest"] == "sha256:" + hashlib.sha256(b"verified file").hexdigest()
    assert remote.invocations == [
        (
            "team_publish_artifact",
            {"path": result["artifactId"], "name": "Report"},
            "stable-publish",
        )
    ]
    assert result["artifactId"].startswith("tar_")
    # Snapshot bytes cannot change underneath the upload after local hashing.
    remote.on_upload = lambda: (workspace / "report.md").write_bytes(b"later edit")
    second = await helper.publish({"path": "report.md", "name": "Report"}, "second")
    assert remote.objects[second["artifactId"]] == b"verified file"


async def test_lost_finalize_ack_is_recovered_with_same_keys_after_helper_restart(
    host_harness, tmp_path
):
    helper, remote, workspace = fixture(host_harness, tmp_path)
    remote.lose_finalize = True
    with pytest.raises(ConnectionError):
        await publish(helper, workspace)
    assert remote.invocations == []
    restarted = TeamsArtifactTools(
        host_harness.context,
        workspace=workspace,
        transport=remote,
        invoke=remote.invoke,
        authorize=remote.authorize,
    )
    result = await restarted.publish({"path": "report.md", "name": "Report"}, "stable-publish")
    assert len(remote.artifacts) == 1 and len(remote.operations) == 1
    assert remote.create_requests[0] == remote.create_requests[1]
    (workspace / "report.md").write_bytes(b"changed payload")
    with pytest.raises(ContextConflict, match="idempotency"):
        await restarted.publish({"path": "report.md", "name": "Report"}, "stable-publish")
    assert remote.artifacts[result["artifactId"]]["state"] == "ready"


async def test_publish_rejects_escaping_links_special_files_size_and_model_identity(
    host_harness, tmp_path
):
    helper, remote, workspace = fixture(host_harness, tmp_path)
    outside = tmp_path / "secret"
    outside.write_bytes(b"outside")
    (workspace / "soft").symlink_to(outside)
    (workspace / "parent").symlink_to(tmp_path, target_is_directory=True)
    os.link(outside, workspace / "hard")
    os.mkfifo(workspace / "fifo")
    with (workspace / "large").open("wb") as file:
        file.truncate(MAX_FILE_BYTES + 1)
    for path in ("../secret", str(outside), "soft", "parent/secret", "hard", "fifo", "large"):
        with pytest.raises((ContextConflict, ValidationError)):
            await helper.publish({"path": path}, "bad-" + path)
    for extra in (
        {"endpoint": "https://untrusted.invalid"},
        {"authorityId": "other"},
        {"contextRef": "other"},
        {"workspace": str(tmp_path)},
    ):
        with pytest.raises(ValidationError):
            await helper.publish({"path": "soft", **extra}, "extra")
    assert remote.create_requests == [] and outside.read_bytes() == b"outside"


async def test_read_atomic_digest_verified_copy_and_cache_does_not_follow_uri(
    host_harness, tmp_path
):
    helper, remote, workspace = fixture(host_harness, tmp_path)
    original = await publish(helper, workspace)
    first = await helper.read({"artifactId": original["artifactId"]}, "read-one")
    location = workspace / first["workspacePath"]
    assert location.read_bytes() == b"verified file"
    assert first["source"] == original["source"] and remote.downloads == 1
    second = await helper.read({"artifactId": original["artifactId"]}, "read-two")
    assert second["workspacePath"] == first["workspacePath"] and remote.downloads == 1
    os.chmod(location, 0o600)
    location.write_bytes(b"changed copy")
    with pytest.raises(ContextConflict):
        await helper.read({"artifactId": original["artifactId"]}, "read-changed")
    assert location.read_bytes() == b"changed copy"


async def test_download_digest_failure_and_cancel_leave_no_ready_or_partial_file(
    host_harness, tmp_path
):
    helper, remote, workspace = fixture(host_harness, tmp_path)
    artifact = await publish(helper, workspace)
    remote.corrupt = True
    with pytest.raises(ContextConflict):
        await helper.read({"artifactId": artifact["artifactId"]}, "corrupt")
    assert list((workspace / ".team-artifacts").iterdir()) == []
    remote.corrupt = False
    remote.wait = asyncio.Event()
    task = asyncio.create_task(helper.read({"artifactId": artifact["artifactId"]}, "cancelled"))
    for _ in range(100):
        if remote.downloads == 2:
            break
        await asyncio.sleep(0.001)
    assert remote.downloads == 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list((workspace / ".team-artifacts").iterdir()) == []


async def test_download_rechecks_authorization_and_parallel_read_is_non_overwriting(
    host_harness, tmp_path
):
    helper, remote, workspace = fixture(host_harness, tmp_path)
    artifact = await publish(helper, workspace)
    remote.on_read = lambda: setattr(remote, "allowed", False)
    with pytest.raises(ContextConflict, match="revoked"):
        await helper.read({"artifactId": artifact["artifactId"]}, "revoked")
    assert list((workspace / ".team-artifacts").iterdir()) == []
    remote.allowed, remote.on_read = True, None
    one, two = await asyncio.gather(
        helper.read({"artifactId": artifact["artifactId"]}, "one"),
        helper.read({"artifactId": artifact["artifactId"]}, "two"),
    )
    assert one["workspacePath"] == two["workspacePath"]
    assert len(list((workspace / ".team-artifacts").iterdir())) == 1


async def test_read_refuses_cross_scope_metadata_and_symlink_cache(host_harness, tmp_path):
    helper, remote, workspace = fixture(host_harness, tmp_path)
    artifact = await publish(helper, workspace)
    other = tmp_path / "outside"
    other.mkdir()
    (workspace / ".team-artifacts").symlink_to(other, target_is_directory=True)
    with pytest.raises(ContextConflict):
        await helper.read({"artifactId": artifact["artifactId"]}, "unsafe-root")
    assert remote.downloads == 0 and list(other.iterdir()) == []
    original = remote.invoke

    async def incorrect(*args):
        response = await original(*args)
        return {**response, "source": {**response["source"], "authorityRef": "other"}}

    helper.invoke = incorrect
    with pytest.raises(ContextConflict, match="authorized group"):
        await helper.read({"artifactId": artifact["artifactId"]}, "wrong-authority")


async def test_missing_preparation_and_replaced_workspace_fail_closed(host_harness, tmp_path):
    helper, remote, workspace = fixture(host_harness, tmp_path)
    with pytest.raises(ContextConflict):
        TeamsArtifactTools(
            host_harness.context,
            workspace=None,
            transport=remote,
            invoke=remote.invoke,
            authorize=remote.authorize,
        )
    context = host_harness.context.model_copy(
        update={"material_manifest_ref": "frozen", "material_manifest_digest": digest("manifest")}
    )
    with pytest.raises(ContextConflict, match="not prepared"):
        TeamsArtifactTools(
            context,
            workspace=workspace,
            transport=remote,
            invoke=remote.invoke,
            authorize=remote.authorize,
        )
    workspace.rename(tmp_path / "old")
    workspace.mkdir()
    (workspace / "report.md").write_bytes(b"replacement")
    with pytest.raises(ContextConflict, match="replaced"):
        await helper.publish({"path": "report.md"}, "replaced")
    assert remote.create_requests == []


async def test_stable_call_identity_required_before_any_upload(host_harness, tmp_path):
    helper, remote, workspace = fixture(host_harness, tmp_path)
    (workspace / "report.md").write_bytes(b"data")
    for call_id in (None, "", "x" * 513):
        with pytest.raises(ContextConflict, match="stable"):
            await helper.publish({"path": "report.md"}, call_id)
    assert remote.create_requests == []


async def test_helper_and_fixed_http_transport_upload_publish_download_bytes(
    host_harness, tmp_path
):
    import json

    context = host_harness.context
    remote, calls = ArtifactServer(context), []

    async def handler(request):
        calls.append(request)
        assert request.url.host == "teams.test.invalid"
        assert request.headers["x-teams-node-token"] == "fixture-node"
        prefix = "/teams/nodes/node-test"
        assert request.url.path.startswith(prefix)
        path = request.url.path[len(prefix) :]
        if path == "/contexts/invoke":
            body = json.loads(await request.aread())
            assert (
                body["contextRef"] == context.context_ref
                and body["commandId"] == context.ref.commandId
            )
            result = await remote.invoke(body["operation"], body["arguments"], body["callId"])
        else:
            assert request.url.params["contextRef"] == context.context_ref
            if path == "/artifacts":
                result = await remote.create_artifact(context, json.loads(await request.aread()))
            elif request.method == "PUT":
                ref = path.split("/")[2]
                data = await request.aread()

                async def chunks():
                    yield data

                await remote.upload_artifact(context, ref, chunks(), size_bytes=len(data))
                result = {"state": "ready"}
            elif path.endswith("/finalize"):
                result = await remote.finalize_artifact(
                    context,
                    path.split("/")[2],
                    idempotency_key=json.loads(await request.aread())["idempotencyKey"],
                )
            else:
                ref = path.split("/")[-2]
                assert path == f"/groups/{context.ref.groupId}/artifacts/{ref}/content"
                return httpx.Response(200, content=remote.objects[ref])
        return httpx.Response(200, json=result)

    transport_impl = httpx.MockTransport(handler)

    async def authorization(*args):
        return MaterialHTTPAuthorization({"X-Teams-Node-Token": "fixture-node"})

    transport = TeamsHTTPMaterialTransport(
        "https://teams.test.invalid/teams",
        node_id="node-test",
        authorization=authorization,
        transport=transport_impl,
    )
    workspace = tmp_path / "http-work"
    workspace.mkdir()
    async with httpx.AsyncClient(
        transport=transport_impl, base_url="https://teams.test.invalid"
    ) as client:

        async def invoke(operation, arguments, call_id):
            response = await client.post(
                "/teams/nodes/node-test/contexts/invoke",
                headers={"X-Teams-Node-Token": "fixture-node"},
                json={
                    "contextRef": context.context_ref,
                    "commandId": context.ref.commandId,
                    "operation": operation,
                    "arguments": arguments,
                    "callId": call_id,
                },
            )
            return response.json()

        helper = TeamsArtifactTools(
            context,
            workspace=workspace,
            transport=transport,
            invoke=invoke,
            authorize=remote.authorize,
        )
        artifact = await publish(helper, workspace)
        copy = await helper.read({"artifactId": artifact["artifactId"]}, "http-read")
        assert (workspace / copy["workspacePath"]).read_bytes() == b"verified file"
    await transport.close()
    assert len(calls) == 7 and len(remote.artifacts) == 1
    assert remote.invocations[0][1]["path"] == artifact["artifactId"]

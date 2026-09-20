import json
from types import SimpleNamespace

import httpx
import pytest

from ksadk.kernel.teams_material_transport import (
    MaterialHTTPAuthorization,
    TeamsHTTPMaterialTransport,
)
from ksadk.kernel.teams_materials import TeamsMaterializer
from ksadk.plugins.teams.errors import TeamsError
from tests.kernel.teams_host_harness import host_harness as host_harness
from tests.kernel.teams_host_harness import temporary_postgres as temporary_postgres
from tests.kernel.test_teams_materials import BytesTransport


@pytest.mark.parametrize("node", ["node-1", None])
async def test_http_transport_materializes_with_rotating_current_authorization(
    host_harness, tmp_path, node
):
    source = BytesTransport({"a.txt": b"a", "目录/notes.md": b"node bytes"})
    context = host_harness.context.model_copy(
        update={
            "material_manifest_ref": "material-1",
            "material_manifest_digest": source.body.manifest_digest,
        }
    )
    calls = []

    async def authorize(ctx, operation):
        assert ctx == context and operation == "read_material"
        token = "current-" + str(len(calls))
        return MaterialHTTPAuthorization(
            {"X-Teams-Node-Token": token}
            if node
            else {"X-Teams-Execution-Permit": json.dumps({"signed": token})},
            "original-prepare" if node else None,
        )

    async def handler(request):
        calls.append(request)
        assert (
            request.url.host == "localhost"
            and request.url.params["contextRef"] == context.context_ref
        )
        if node:
            assert request.url.params["nodeCommandId"] == "original-prepare"
            assert request.headers["x-teams-node-token"] == "current-" + str(len(calls) - 1)
        else:
            assert (
                "authorization" not in request.headers
                and "x-teams-node-token" not in request.headers
            )
        assert request.url.path.startswith("/teams/" + ("nodes/node-1" if node else "runtime"))
        if "/blobs/" not in request.url.path:
            return httpx.Response(200, json=await source.manifest(context, "material-1"))
        fingerprint = request.url.path.split("/blobs/")[1]
        async with source.blob(context, "material-1", fingerprint) as chunks:
            data = b"".join([chunk async for chunk in chunks])
        return httpx.Response(
            200, content=data, headers={"Content-Type": "application/octet-stream"}
        )

    transport = TeamsHTTPMaterialTransport(
        "http://localhost/teams",
        node_id=node,
        authorization=authorize,
        transport=httpx.MockTransport(handler),
    )
    try:
        client = TeamsMaterializer(
            tmp_path / "http-cache",
            fetch_manifest=transport.fetch_manifest,
            open_blob=transport.open_blob,
        )
        result = await client.materialize(context)
        assert (result.directory / "目录/notes.md").read_bytes() == b"node bytes"
        assert len(calls) == 4  # before/after manifest authorization plus two blobs
    finally:
        await transport.close()


async def test_artifact_upload_is_streamed_to_fixed_server_and_never_retried():
    ctx = SimpleNamespace(context_ref="context-1")
    calls, content = [], []

    async def authorize(context, operation):
        calls.append(operation)
        return MaterialHTTPAuthorization({"X-Teams-Callback-Permit": '{"signed":"test"}'})

    async def handler(request):
        assert request.url.params["contextRef"] == "context-1"
        if request.method == "PUT":
            assert request.url.path == "/teams/runtime/artifacts/artifact-1/content"
            assert request.headers["content-length"] == "4"
            content.append(await request.aread())
        return httpx.Response(200, json={"artifactId": "artifact-1", "state": "ready"})

    transport = TeamsHTTPMaterialTransport(
        "http://localhost/teams", authorization=authorize, transport=httpx.MockTransport(handler)
    )

    async def chunks():
        yield b"by"
        yield b"te"

    try:
        await transport.create_artifact(
            ctx,
            {
                "name": "report",
                "mediaType": "text/plain",
                "digest": "sha256:" + "0" * 64,
                "sizeBytes": 4,
                "idempotencyKey": "stable",
            },
        )
        await transport.upload_artifact(ctx, "artifact-1", chunks(), size_bytes=4)
        await transport.finalize_artifact(ctx, "artifact-1", idempotency_key="finalize")
        assert content == [b"byte"] and calls == [
            "create_artifact",
            "upload_artifact",
            "finalize_artifact",
        ]
    finally:
        await transport.close()


@pytest.mark.parametrize("status", [302, 307, 500, 401])
async def test_no_redirect_credentials_or_response_body_are_forwarded(status):
    requests = []

    async def auth(*args):
        return MaterialHTTPAuthorization({"X-Teams-Node-Token": "test-node-token"})

    async def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            headers={"Location": "https://untrusted.invalid/storage?secret=should-not-leak"},
            text="secret-response",
        )

    transport = TeamsHTTPMaterialTransport(
        "http://localhost/teams",
        node_id="node",
        authorization=auth,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(TeamsError) as failed:
            await transport.fetch_manifest(
                SimpleNamespace(context_ref="ctx", material_manifest_ref="m"), "m"
            )
        assert "secret" not in str(failed.value) and len(requests) == 1
    finally:
        await transport.close()


@pytest.mark.parametrize(
    "node,headers",
    [
        ("node", {"Authorization": "owner-bearer"}),
        ("node", {"X-Teams-Node-Token": "a", "X-Teams-Callback-Permit": "b"}),
        (None, {"X-Teams-Callback-Permit": "a", "X-Teams-Execution-Permit": "b"}),
        (None, {"X-Teams-Callback-Permit": "a\r\nb"}),
    ],
)
async def test_mixed_or_owner_authorization_rejected_before_network(node, headers):
    async def auth(*args):
        return MaterialHTTPAuthorization(headers)

    def handler(_request):
        pytest.fail("invalid authorization reached network")

    transport = TeamsHTTPMaterialTransport(
        "http://localhost/teams",
        node_id=node,
        authorization=auth,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(TeamsError):
            await transport.fetch_manifest(
                SimpleNamespace(context_ref="ctx", material_manifest_ref="m"), "m"
            )
    finally:
        await transport.close()


async def test_compressed_and_oversized_responses_fail_closed():
    current = "compressed"

    async def auth(*args):
        return MaterialHTTPAuthorization({"X-Teams-Node-Token": "test"})

    def handler(_request):
        if current == "compressed":
            # Empty valid gzip is still rejected: canonical transport is raw.
            import gzip

            return httpx.Response(
                200, content=gzip.compress(b"{}"), headers={"Content-Encoding": "gzip"}
            )
        return httpx.Response(200, content=b" " * (2 * 1024 * 1024 + 1))

    transport = TeamsHTTPMaterialTransport(
        "http://localhost/teams",
        node_id="node",
        authorization=auth,
        transport=httpx.MockTransport(handler),
    )
    try:
        for value in ("compressed", "oversized"):
            current = value
            with pytest.raises(TeamsError):
                await transport.fetch_manifest(
                    SimpleNamespace(context_ref="ctx", material_manifest_ref="m"), "m"
                )
    finally:
        await transport.close()


def test_remote_url_requires_https_and_no_embedded_credentials():
    for url in (
        "http://remote.invalid/teams",
        "https://user:secret@example.invalid/teams",
        "https://example.invalid/teams?token=secret",
    ):
        with pytest.raises(TeamsError):
            TeamsHTTPMaterialTransport(url, authorization=None)


async def test_artifact_download_uses_current_group_and_fixed_reference():
    async def auth(*args):
        return MaterialHTTPAuthorization({"X-Teams-Callback-Permit": '{"signed":"test"}'})

    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, content=b"artifact bytes")

    transport = TeamsHTTPMaterialTransport(
        "http://localhost/teams", authorization=auth, transport=httpx.MockTransport(handler)
    )
    ctx = SimpleNamespace(context_ref="ctx", ref=SimpleNamespace(groupId="group-1"))
    try:
        async with transport.open_artifact(ctx, "group-1", "artifact-1") as chunks:
            assert b"".join([value async for value in chunks]) == b"artifact bytes"
        assert paths == ["/teams/runtime/groups/group-1/artifacts/artifact-1/content"]
        with pytest.raises(TeamsError):
            async with transport.open_artifact(ctx, "group-other", "artifact-1"):
                pass
    finally:
        await transport.close()


@pytest.mark.parametrize("field", ["error", "detail"])
async def test_preserves_only_bounded_original_run_race_error(field):
    calls = []

    async def auth(*args):
        return MaterialHTTPAuthorization({"X-Teams-Callback-Permit": '{"signed":"fixture"}'})

    def handler(request):
        calls.append(request)
        return httpx.Response(
            409, json={field: {"code": "original_run_unverified", "message": "never expose secret"}}
        )

    transport = TeamsHTTPMaterialTransport(
        "http://localhost/teams", authorization=auth, transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(TeamsError) as caught:
            await transport.fetch_manifest(
                SimpleNamespace(context_ref="ctx", material_manifest_ref="material-1"), "material-1"
            )
        assert caught.value.code == "original_run_unverified"
        assert "secret" not in str(caught.value)
        assert len(calls) == 1
    finally:
        await transport.close()


@pytest.mark.parametrize(
    "status,body",
    [
        (403, {"error": {"code": "original_run_unverified"}}),
        (409, {"error": {"code": "secret-value"}}),
        (409, {"detail": {"code": "original_run_unverified"}, "padding": "x" * 4096}),
    ],
)
async def test_other_or_oversized_remote_errors_remain_redacted(status, body):
    async def auth(*args):
        return MaterialHTTPAuthorization({"X-Teams-Node-Token": "fixture"})

    transport = TeamsHTTPMaterialTransport(
        "http://localhost/teams",
        node_id="node-1",
        authorization=auth,
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body)),
    )
    try:
        with pytest.raises(TeamsError) as caught:
            await transport.fetch_manifest(
                SimpleNamespace(context_ref="ctx", material_manifest_ref="material-1"), "material-1"
            )
        assert caught.value.code == "material_request_rejected"
        assert "secret" not in str(caught.value)
    finally:
        await transport.close()

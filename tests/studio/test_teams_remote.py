import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import anyio
import httpx
import pytest
from fastapi import FastAPI

from ksadk.plugins.teams.errors import TeamsError
from ksadk.plugins.teams.transport import TeamsHTTPClient
from ksadk.studio.teams_remote import RemoteStudioTeamsInstallation


def installation(environment):
    studio = SimpleNamespace(
        workspace_plugins=Mock(),
        configuration=SimpleNamespace(environment=lambda: environment),
        cloud=SimpleNamespace(gateway=SimpleNamespace(client=Mock())),
    )
    return RemoteStudioTeamsInstallation(studio, "https://teams.example.test/teams")


@pytest.mark.asyncio
async def test_explicit_bearer_does_not_require_unrelated_ak_sk_configuration():
    value = installation({"KSADK_TEAMS_ACCESS_TOKEN": "test-only-token"})
    try:
        headers = await value._client().request_headers("GET", "/lifecycle")
        assert headers["Authorization"] == "Bearer test-only-token"
        value.studio.cloud.gateway.client._auth.sign_headers.assert_not_called()
    finally:
        await value.client.close()


@pytest.mark.asyncio
async def test_remote_proxy_rejects_encoded_path_escape_before_signing():
    value = installation({})
    value.client = SimpleNamespace(request_headers=AsyncMock())
    app = FastAPI()
    app.include_router(value._router())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/groups/%252e%252e/nodes")
        assert response.status_code == 400
        value.client.request_headers.assert_not_awaited()


@pytest.mark.asyncio
async def test_signing_failure_is_a_recoverable_response():
    value = installation({})
    value.client = SimpleNamespace(
        request_headers=AsyncMock(
            side_effect=TeamsError("teams_credentials_required", "missing", status=401)
        )
    )
    app = FastAPI()
    app.include_router(value._router())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/groups")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "teams_credentials_required"
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_credentials_are_resolved_for_each_signed_request():
    environment = {"KSADK_TEAMS_ACCESS_TOKEN": "first-owner"}
    value = installation(environment)
    try:
        client = value._client()
        assert (await client.request_headers("GET", "/lifecycle"))[
            "Authorization"
        ] == "Bearer first-owner"
        environment["KSADK_TEAMS_ACCESS_TOKEN"] = "second-owner"
        assert value._client() is client
        assert (await client.request_headers("GET", "/lifecycle"))[
            "Authorization"
        ] == "Bearer second-owner"
        environment.clear()
        value.studio.cloud.gateway.client = None
        with pytest.raises(TeamsError, match="请配置团队服务连接凭据"):
            await client.request_headers("GET", "/lifecycle")
    finally:
        await value.client.close()


@pytest.mark.asyncio
async def test_gateway_credentials_refresh_after_removing_bearer():
    environment = {"KSADK_TEAMS_ACCESS_TOKEN": "test-only-token"}
    value = installation(environment)
    old_gateway = value.studio.cloud.gateway.client
    gateway = Mock()
    gateway._build_headers.return_value = {"Content-Type": "application/json"}
    gateway._auth.sign_headers.return_value = {"Authorization": "fixture-signature"}
    try:
        client = value._client()
        await client.request_headers("GET", "/groups")
        environment.clear()
        value.studio.cloud.gateway.client = gateway
        headers = await client.request_headers("POST", "/groups", '{"name":"中文"}')
        assert headers["Authorization"] == "fixture-signature"
        old_gateway._auth.sign_headers.assert_not_called()
        gateway._auth.sign_headers.assert_called_once_with(
            method="POST",
            url="https://teams.example.test/teams/groups",
            headers={"Content-Type": "application/json", "Host": "teams.example.test"},
            body='{"name":"中文"}',
        )
    finally:
        await value.client.close()


@asynccontextmanager
async def proxy_client(handler):
    value = installation({})
    value.client = TeamsHTTPClient(
        value.base_url,
        access_token="test-only-token",
        transport=httpx.MockTransport(handler),
    )
    app = FastAPI()
    app.include_router(value._router())
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client
    finally:
        await value.client.close()


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self, chunks=(), error=None):
        self.chunks = chunks
        self.error = error
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error

    async def aclose(self):
        await anyio.sleep(0)
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "status", "code"),
    [(b"x" * (256 * 1024 + 1), 413, "request_too_large"), (b"\xff", 400, "invalid_request")],
)
async def test_invalid_request_body_never_reaches_upstream(body, status, code):
    handler = Mock(side_effect=AssertionError("invalid request reached upstream"))

    async def chunks():
        # Exercise a streamed request with no Content-Length header.
        yield body[:100]
        yield body[100:]

    async with proxy_client(handler) as client:
        response = await client.post("/groups", content=chunks())
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.headers["cache-control"] == "no-store"
    handler.assert_not_called()


@pytest.mark.asyncio
async def test_request_boundary_preserves_body_query_and_upstream_status():
    body = b"x" * (256 * 1024)
    captured = []
    stream = TrackedStream([b'{"error":{"code":"conflict"}}'])

    def handler(request):
        captured.append(request)
        return httpx.Response(409, stream=stream, headers={"Content-Type": "application/json"})

    async with proxy_client(handler) as client:
        response = await client.post(
            "/groups/group-one/messages?cursor=original",
            content=body,
            headers={"Authorization": "Bearer browser-forged"},
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert response.headers["cache-control"] == "no-store"
    assert captured[0].url.path == "/teams/groups/group-one/messages"
    assert captured[0].url.query == b"cursor=original"
    assert captured[0].content == body
    assert captured[0].headers["authorization"] == "Bearer test-only-token"
    assert stream.closed


@pytest.mark.asyncio
async def test_unexpected_redirect_is_not_forwarded_and_closes_upstream():
    stream = TrackedStream()
    handler = Mock(
        return_value=httpx.Response(
            307, stream=stream, headers={"Location": "https://other.example.test/target"}
        )
    )
    async with proxy_client(handler) as client:
        response = await client.get("/groups")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "teams_redirect_forbidden"
    assert "location" not in response.headers
    assert stream.closed
    handler.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("extra_byte", [False, True])
async def test_regular_response_has_a_bounded_buffer(extra_byte):
    chunks = [b"x" * (1024 * 1024)] * 2 + ([b"x"] if extra_byte else [])
    stream = TrackedStream(chunks)
    async with proxy_client(lambda request: httpx.Response(200, stream=stream)) as client:
        response = await client.get("/groups")
    assert stream.closed
    if extra_byte:
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "teams_response_too_large"
    else:
        assert response.status_code == 200
        assert response.content == b"".join(chunks)


@pytest.mark.asyncio
@pytest.mark.parametrize("download", [False, True])
async def test_sse_and_downloads_stream_beyond_the_json_buffer_limit(download):
    chunks = [b"data: " + b"x" * (1024 * 1024) + b"\n\n"] * 3
    stream = TrackedStream(chunks)
    media = "application/octet-stream" if download else "text/event-stream"
    headers = {"Content-Type": media, "ETag": '"fixture"'}
    if download:
        headers["Content-Disposition"] = 'attachment; filename="report.txt"'
    async with proxy_client(
        lambda request: httpx.Response(200, stream=stream, headers=headers)
    ) as client:
        response = await client.get("/groups/group-one/artifacts/file-one/download")
    assert response.status_code == 200
    assert response.content == b"".join(chunks)
    assert media in response.headers["content-type"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["etag"] == '"fixture"'
    if download:
        assert response.headers["content-disposition"] == headers["Content-Disposition"]
    assert stream.closed


@pytest.mark.asyncio
async def test_upstream_read_failure_closes_the_response():
    stream = TrackedStream([b"partial"], error=httpx.ReadError("fixture connection loss"))
    async with proxy_client(lambda request: httpx.Response(200, stream=stream)) as client:
        response = await client.get("/groups")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "teams_transport_unavailable"
    assert "fixture connection loss" not in response.text
    assert stream.closed


@pytest.mark.asyncio
async def test_cancellation_closes_upstream_and_is_not_an_error_response():
    stream = TrackedStream([b"partial"], error=asyncio.CancelledError())
    async with proxy_client(lambda request: httpx.Response(200, stream=stream)) as client:
        with pytest.raises(asyncio.CancelledError):
            await client.get("/groups")
    assert stream.closed


@pytest.mark.asyncio
async def test_browser_disconnect_allows_async_sse_cleanup():
    class OpenEventStream(TrackedStream):
        async def __aiter__(self):
            yield b"data: ready\n\n"
            await anyio.sleep_forever()

    stream = OpenEventStream()
    value = installation({})
    value.client = TeamsHTTPClient(
        value.base_url,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, stream=stream, headers={"Content-Type": "text/event-stream"}
            )
        ),
    )
    app = FastAPI()
    app.include_router(value._router())
    body_sent = anyio.Event()
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await body_sent.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            body_sent.set()

    try:
        with anyio.fail_after(2):
            await app(
                {
                    "type": "http",
                    "asgi": {"version": "3.0", "spec_version": "2.0"},
                    "http_version": "1.1",
                    "method": "GET",
                    "path": "/groups/group-one/events",
                    "query_string": b"",
                    "headers": [],
                    "scheme": "http",
                    "server": ("test", 80),
                    "client": ("127.0.0.1", 12345),
                },
                receive,
                send,
            )
        assert body_sent.is_set()
        assert stream.closed
    finally:
        await value.client.close()


@pytest.mark.asyncio
async def test_signer_failure_detail_is_not_exposed_to_the_browser():
    value = installation({})
    value.client = SimpleNamespace(
        request_headers=AsyncMock(
            side_effect=TeamsError("signing_failed", "private signer fixture detail", status=400)
        )
    )
    app = FastAPI()
    app.include_router(value._router())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/groups")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "signing_failed"
    assert "private signer fixture detail" not in response.text

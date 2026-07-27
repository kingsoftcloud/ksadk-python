from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import httpx
import pytest

from ksadk.a2a.control_plane import (
    A2AControlPlaneError,
    FileWorkloadTokenProvider,
    KopA2AControlPlane,
)
from ksadk.a2a.ids import require_a2a_resource_id

SPACE_ID = "a2a-space-00000000000040008000000000000001"
AGENT_ID = "a2a-agent-00000000000040008000000000000002"
VERSION_ID = "a2a-version-00000000000040008000000000000003"
TASK_ID = "a2a-task-00000000000040008000000000000004"
NEXT_VERSION_ID = "a2a-version-00000000000040008000000000000006"


def test_resource_ids_require_lowercase_uuid4_hex() -> None:
    assert require_a2a_resource_id(SPACE_ID, "a2a-space-", field_name="space_id") == SPACE_ID
    for invalid in (
        "a2a-space-test",
        "a2a-space-00000000000010008000000000000001",
        "a2a-space-00000000000040007000000000000001",
        "a2a-space-0000000000004000800000000000000A",
    ):
        with pytest.raises(ValueError, match="uuid4_hex"):
            require_a2a_resource_id(invalid, "a2a-space-", field_name="space_id")


def _card() -> dict:
    return {
        "name": "echo",
        "description": "echo",
        "version": "1.0.0",
        "supportedInterfaces": [
            {
                "url": "https://gateway.example/a2a/jsonrpc",
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
            }
        ],
        "capabilities": {},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [],
    }


def _jwt(*, audience: str = "a2a-registry", exp: int | None = None) -> str:
    def encode(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    payload = {"aud": audience, "exp": exp or int(time.time()) + 300}
    return f"{encode({'alg': 'none'})}.{encode(payload)}.sig"


def _write_token(token_dir: Path, audience: str, token: str | None = None) -> None:
    path = token_dir / f"{audience}.jwt"
    path.write_text(token or _jwt(audience=audience))
    path.chmod(0o400)


@pytest.mark.asyncio
async def test_list_space_agents_uses_internal_action_and_registry_token(tmp_path: Path) -> None:
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    _write_token(token_dir, "a2a-registry")
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["authorization"] = request.headers.get("authorization")
        seen["json"] = __import__("json").loads(request.content)
        return httpx.Response(
            200,
            json={
                "Code": 0,
                "RequestId": "req-1",
                "Action": "ListA2ASpaceAgents",
                "Data": {
                    "NotModified": False,
                    "A2ASpaceId": SPACE_ID,
                    "ETag": f'W/"{SPACE_ID}:4"',
                    "Agents": [
                        {
                            "A2AAgentId": AGENT_ID,
                            "VersionId": VERSION_ID,
                            "Source": "hosted",
                            "CardSha256": "a" * 64,
                            "AgentCard": _card(),
                            "Callable": True,
                            "BlockedReason": None,
                            "RouteKind": "hosted_gateway",
                        }
                    ],
                    "NextCursor": None,
                },
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://control")
    client = KopA2AControlPlane(
        "https://control",
        token_provider=FileWorkloadTokenProvider(token_dir),
        httpx_client=http,
    )
    try:
        page = await client.list_space_agents(SPACE_ID, skill_id="echo", page_size=25)
    finally:
        await http.aclose()

    assert seen["path"] == "/agentengine/internal/v1/a2a/ListA2ASpaceAgents"
    assert str(seen["authorization"]).startswith("Bearer ey")
    assert seen["json"] == {
        "A2ASpaceId": SPACE_ID,
        "SkillId": "echo",
        "PageSize": 25,
    }
    assert page.etag == f'W/"{SPACE_ID}:4"'
    assert page.agents[0].agent_id == AGENT_ID
    assert page.agents[0].callable is True


@pytest.mark.asyncio
async def test_prepare_call_parses_frozen_route_and_raises_domain_errors(tmp_path: Path) -> None:
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    _write_token(token_dir, "a2a-registry")
    responses = iter(
        [
            httpx.Response(
                200,
                json={
                    "Code": 0,
                    "Data": {
                        "A2ATaskId": TASK_ID,
                        "Target": {
                            "A2AAgentId": AGENT_ID,
                            "VersionId": VERSION_ID,
                            "CardSha256": "b" * 64,
                        },
                        "Route": {
                            "Kind": "external_public",
                            "Interface": {
                                "Url": "https://vendor.example/a2a/jsonrpc",
                                "ProtocolBinding": "JSONRPC",
                                "ProtocolVersion": "1.0",
                            },
                        },
                        "CallPermit": "permit-1",
                        "CallPermitExpiresAt": "2026-07-27T10:05:00Z",
                        "CredentialHandle": "credential-1",
                    },
                },
            ),
            httpx.Response(
                409,
                json={
                    "Code": 409,
                    "Message": "version changed",
                    "RequestId": "req-2",
                    "Action": "PrepareA2ACall",
                    "Data": {
                        "ErrorCode": "A2A_TARGET_VERSION_CHANGED",
                        "Retryable": True,
                        "Details": {"CurrentVersionId": NEXT_VERSION_ID},
                    },
                },
            ),
        ]
    )
    seen_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payloads.append(__import__("json").loads(request.content))
        return next(responses)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://control",
    )
    client = KopA2AControlPlane(
        "https://control",
        token_provider=FileWorkloadTokenProvider(token_dir),
        httpx_client=http,
    )
    try:
        prepared = await client.prepare_call(
            space_id=SPACE_ID,
            target_agent_id=AGENT_ID,
            expected_version_id=VERSION_ID,
            message_id="message-1",
            message_sha256="c" * 64,
            idempotency_token="idem-1234",
        )
        assert prepared.platform_task_id == TASK_ID
        assert prepared.route.interface.url == "https://vendor.example/a2a/jsonrpc"

        with pytest.raises(A2AControlPlaneError) as exc_info:
            await client.prepare_call(
                space_id=SPACE_ID,
                target_agent_id=AGENT_ID,
                expected_version_id=VERSION_ID,
                message_id="message-2",
                message_sha256="d" * 64,
                idempotency_token="idem-5678",
            )
    finally:
        await http.aclose()

    assert exc_info.value.error_code == "A2A_TARGET_VERSION_CHANGED"
    assert exc_info.value.retryable is True
    assert exc_info.value.details == {"CurrentVersionId": NEXT_VERSION_ID}
    assert seen_payloads[0]["A2ASpaceId"] == SPACE_ID


@pytest.mark.asyncio
async def test_control_plane_transport_failure_is_a_retryable_domain_error(tmp_path: Path) -> None:
    _write_token(tmp_path, "a2a-registry")

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    client = KopA2AControlPlane(
        "https://control",
        token_provider=FileWorkloadTokenProvider(tmp_path),
        httpx_client=http,
    )
    try:
        with pytest.raises(A2AControlPlaneError) as exc_info:
            await client.list_space_agents(SPACE_ID)
    finally:
        await http.aclose()

    assert exc_info.value.error_code == "A2A_CONTROL_PLANE_UNAVAILABLE"
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_control_plane_non_json_error_is_a_domain_error(tmp_path: Path) -> None:
    _write_token(tmp_path, "a2a-registry")
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503, text="unavailable"))
    )
    client = KopA2AControlPlane(
        "https://control",
        token_provider=FileWorkloadTokenProvider(tmp_path),
        httpx_client=http,
    )
    try:
        with pytest.raises(A2AControlPlaneError) as exc_info:
            await client.list_space_agents(SPACE_ID)
    finally:
        await http.aclose()

    assert exc_info.value.code == 503
    assert exc_info.value.error_code == "A2A_CONTROL_PLANE_UNAVAILABLE"
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_task_operation_and_credential_broker_contract(tmp_path: Path) -> None:
    registry_token = _jwt(audience="a2a-registry", exp=4_102_444_800)
    broker_token = _jwt(audience="credential-broker", exp=4_102_444_800)
    _write_token(tmp_path, "a2a-registry", registry_token)
    _write_token(tmp_path, "credential-broker", broker_token)
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "path": request.url.path,
                "authorization": request.headers.get("authorization"),
                "json": json.loads(request.content),
            }
        )
        if request.url.path.endswith("/PrepareA2ATaskOperation"):
            return httpx.Response(
                200,
                json={
                    "Code": 0,
                    "Data": {
                        "A2ATaskId": TASK_ID,
                        "Target": {
                            "A2AAgentId": AGENT_ID,
                            "VersionId": VERSION_ID,
                            "CardSha256": "b" * 64,
                        },
                        "Route": {
                            "Kind": "external_public",
                            "Interface": {
                                "Url": "https://vendor.example/a2a/jsonrpc",
                                "ProtocolBinding": "JSONRPC",
                                "ProtocolVersion": "1.0",
                            },
                        },
                        "RemoteTask": {
                            "RemoteTaskId": "vendor-task-1",
                            "RemoteContextId": "vendor-context-1",
                        },
                        "CallPermit": "permit-operation-1",
                        "CallPermitExpiresAt": "2026-07-27T10:05:00Z",
                        "CredentialHandle": None,
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "Code": 0,
                "Data": {
                    "Injection": {
                        "Headers": {"Authorization": "Bearer vendor-token"},
                        "Query": {"api_key": "query-token"},
                        "Cookies": {"session": "cookie-token"},
                    },
                    "ExpiresAt": "2026-07-27T10:04:00Z",
                },
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = KopA2AControlPlane(
        "https://control",
        token_provider=FileWorkloadTokenProvider(tmp_path),
        httpx_client=http,
    )
    try:
        prepared = await client.prepare_task_operation(
            platform_task_id=TASK_ID,
            operation="send_message",
            message_id="message-2",
            message_sha256="c" * 64,
            idempotency_token="idem-continue-1",
        )
        injection = await client.resolve_credential(
            platform_task_id=prepared.platform_task_id,
            credential_handle=prepared.credential_handle,
            call_permit=prepared.call_permit,
        )
    finally:
        await http.aclose()

    assert prepared.remote_task is not None
    assert prepared.remote_task.remote_task_id == "vendor-task-1"
    assert injection.headers == {"Authorization": "Bearer vendor-token"}
    assert injection.query == {"api_key": "query-token"}
    assert injection.cookies == {"session": "cookie-token"}
    assert seen == [
        {
            "path": "/agentengine/internal/v1/a2a/PrepareA2ATaskOperation",
            "authorization": f"Bearer {registry_token}",
            "json": {
                "A2ATaskId": TASK_ID,
                "Operation": "send_message",
                "MessageId": "message-2",
                "MessageSha256": "c" * 64,
                "IdempotencyToken": "idem-continue-1",
            },
        },
        {
            "path": "/agentengine/internal/v1/a2a/ResolveA2ACredential",
            "authorization": f"Bearer {broker_token}",
            "json": {
                "A2ATaskId": TASK_ID,
                "CredentialHandle": None,
                "CallPermit": "permit-operation-1",
            },
        },
    ]


@pytest.mark.asyncio
async def test_task_operation_rejects_non_contract_operation() -> None:
    client = KopA2AControlPlane("https://control")

    with pytest.raises(ValueError, match="unsupported A2A task operation"):
        await client.prepare_task_operation(
            platform_task_id=TASK_ID,
            operation="message/continue",  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "kwargs", "message"),
    [
        ("send_message", {}, "requires message_id"),
        ("cancel_task", {}, "requires idempotency_token"),
        ("get_task", {"idempotency_token": "idem-read"}, "does not accept idempotency_token"),
        ("subscribe_to_task", {"message_id": "message-1"}, "does not accept message fields"),
    ],
)
async def test_task_operation_validates_operation_specific_fields(
    operation: str,
    kwargs: dict[str, str],
    message: str,
) -> None:
    client = KopA2AControlPlane("https://control")

    with pytest.raises(ValueError, match=message):
        await client.prepare_task_operation(
            platform_task_id=TASK_ID,
            operation=operation,  # type: ignore[arg-type]
            **kwargs,
        )


def test_file_token_provider_fails_closed_for_missing_or_oversized_token(tmp_path: Path) -> None:
    provider = FileWorkloadTokenProvider(tmp_path)
    with pytest.raises(RuntimeError, match="a2a-gateway"):
        provider.get_token("a2a-gateway")

    (tmp_path / "a2a-gateway.jwt").write_text("x" * (16 * 1024 + 1))
    with pytest.raises(RuntimeError, match="16 KiB"):
        provider.get_token("a2a-gateway")


def test_file_token_provider_rejects_expired_or_permissive_token(tmp_path: Path) -> None:
    path = tmp_path / "a2a-registry.jwt"
    path.write_text(_jwt(exp=int(time.time()) - 1))
    path.chmod(0o400)
    provider = FileWorkloadTokenProvider(tmp_path)
    with pytest.raises(RuntimeError, match="expired"):
        provider.get_token("a2a-registry")

    path.chmod(0o600)
    path.write_text(_jwt())
    with pytest.raises(RuntimeError, match="mode 0400"):
        provider.get_token("a2a-registry")

    path.chmod(0o644)
    with pytest.raises(RuntimeError, match="mode 0400"):
        provider.get_token("a2a-registry")


def test_file_token_provider_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.jwt"
    target.write_text(_jwt())
    target.chmod(0o400)
    (tmp_path / "a2a-registry.jwt").symlink_to(target)

    with pytest.raises(RuntimeError, match="symlink"):
        FileWorkloadTokenProvider(tmp_path).get_token("a2a-registry")

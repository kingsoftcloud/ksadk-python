"""Typed client for the AgentEngine A2A runtime control-plane contract."""

from __future__ import annotations

import asyncio
import base64
import binascii
import errno
import json
import os
import stat
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args

import httpx
from a2a.types import AgentCard
from google.protobuf.json_format import ParseDict, ParseError

from ksadk.a2a.ids import require_a2a_resource_id
from ksadk.common.kop_client import KOPClient, KOPError

ENV_A2A_CONTROL_PLANE_URL = "KSADK_A2A_CONTROL_PLANE_URL"
ENV_A2A_TOKEN_DIR = "KSADK_A2A_TOKEN_DIR"
DEFAULT_A2A_TOKEN_DIR = "/var/run/secrets/agentengine/a2a"
MAX_WORKLOAD_TOKEN_BYTES = 16 * 1024

AUDIENCE_REGISTRY = "a2a-registry"
AUDIENCE_TASK_SINK = "a2a-task-sink"
AUDIENCE_CREDENTIAL_BROKER = "credential-broker"
AUDIENCE_GATEWAY = "a2a-gateway"

A2AOperation = Literal[
    "send_message",
    "get_task",
    "subscribe_to_task",
    "cancel_task",
]
A2A_OPERATIONS = frozenset(get_args(A2AOperation))

A2AInternalAction = Literal[
    "ListA2ASpaceAgents",
    "PrepareA2ACall",
    "PrepareA2ATaskOperation",
    "BindA2ARemoteTask",
    "AppendA2ATaskEvents",
    "ResolveA2ACredential",
]
A2A_INTERNAL_ACTIONS = frozenset(get_args(A2AInternalAction))
A2A_INTERNAL_PATH_PREFIX = "/agentengine/internal/v1/a2a"


def build_a2a_internal_action_path(action: A2AInternalAction) -> str:
    """Return the explicitly registered Runtime internal Action path."""

    if action not in A2A_INTERNAL_ACTIONS:
        raise ValueError(f"unsupported A2A internal Action: {action}")
    return f"{A2A_INTERNAL_PATH_PREFIX}/{action}"


class A2AControlPlaneError(RuntimeError):
    """Stable error returned by an AgentEngine A2A Action."""

    def __init__(
        self,
        *,
        code: int,
        message: str,
        error_code: str,
        retryable: bool = False,
        field: str | None = None,
        details: dict[str, Any] | None = None,
        request_id: str = "",
        action: str = "",
    ) -> None:
        super().__init__(f"{error_code}: {message}" if error_code else message)
        self.code = code
        self.message = message
        self.error_code = error_code
        self.retryable = retryable
        self.field = field
        self.details = dict(details or {})
        self.request_id = request_id
        self.action = action


class WorkloadTokenProvider(ABC):
    """Returns a short-lived workload token for one exact audience."""

    @abstractmethod
    def get_token(self, audience: str) -> str:
        raise NotImplementedError


class FileWorkloadTokenProvider(WorkloadTokenProvider):
    """Reads audience-specific projected JWT files before every request."""

    def __init__(self, token_dir: str | os.PathLike[str] | None = None) -> None:
        configured = token_dir or os.getenv(ENV_A2A_TOKEN_DIR) or DEFAULT_A2A_TOKEN_DIR
        self._token_dir = Path(configured)

    def get_token(self, audience: str) -> str:
        if not audience or "/" in audience or audience in {".", ".."}:
            raise ValueError(f"invalid workload token audience: {audience!r}")
        path = self._token_dir / f"{audience}.jwt"
        if path.is_symlink():
            raise RuntimeError(
                f"workload token for audience {audience!r} must not be a symlink: {path}"
            )
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise RuntimeError(
                    f"workload token for audience {audience!r} must not be a symlink: {path}"
                ) from exc
            raise RuntimeError(f"missing workload token for audience {audience!r}: {path}") from exc
        try:
            token_stat = os.fstat(descriptor)
            if not stat.S_ISREG(token_stat.st_mode):
                raise RuntimeError(
                    f"workload token for audience {audience!r} is not a regular file: {path}"
                )
            if token_stat.st_size > MAX_WORKLOAD_TOKEN_BYTES:
                raise RuntimeError(
                    f"workload token for audience {audience!r} exceeds 16 KiB: {path}"
                )
            if stat.S_IMODE(token_stat.st_mode) != 0o400:
                raise RuntimeError(
                    f"workload token for audience {audience!r} must have mode 0400: {path}"
                )
            with os.fdopen(descriptor, "rb", closefd=False) as token_file:
                raw_token = token_file.read(MAX_WORKLOAD_TOKEN_BYTES + 1)
            if len(raw_token) > MAX_WORKLOAD_TOKEN_BYTES:
                raise RuntimeError(
                    f"workload token for audience {audience!r} exceeds 16 KiB: {path}"
                )
            token = raw_token.decode("ascii").strip()
        except UnicodeError as exc:
            raise RuntimeError(
                f"workload token for audience {audience!r} must be ASCII: {path}"
            ) from exc
        finally:
            os.close(descriptor)
        if not token:
            raise RuntimeError(f"empty workload token for audience {audience!r}: {path}")
        self._validate_expiry(token, audience=audience, path=path)
        return token

    @staticmethod
    def _validate_expiry(token: str, *, audience: str, path: Path) -> None:
        parts = token.split(".")
        if len(parts) != 3:
            raise RuntimeError(f"workload token for audience {audience!r} is not a JWT: {path}")
        try:
            payload_segment = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_segment).decode("utf-8"))
            expires_at = int(payload["exp"])
        except (binascii.Error, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"workload token for audience {audience!r} has no valid exp claim: {path}"
            ) from exc
        token_audience = payload.get("aud")
        accepted_audiences = (
            {str(item) for item in token_audience}
            if isinstance(token_audience, list)
            else {str(token_audience)}
        )
        if audience not in accepted_audiences:
            raise RuntimeError(f"workload token audience mismatch for {audience!r}: {path}")
        if expires_at <= int(time.time()):
            raise RuntimeError(f"workload token for audience {audience!r} is expired: {path}")


@dataclass(frozen=True)
class A2ARouteInterface:
    url: str
    protocol_binding: str
    protocol_version: str


@dataclass(frozen=True)
class A2ARoute:
    kind: str
    interface: A2ARouteInterface


@dataclass(frozen=True)
class A2ATarget:
    agent_id: str
    version_id: str
    card_sha256: str


@dataclass(frozen=True)
class RemoteTaskReference:
    remote_task_id: str
    remote_context_id: str | None = None


@dataclass(frozen=True)
class PreparedA2AOperation:
    platform_task_id: str
    target: A2ATarget
    route: A2ARoute
    call_permit: str
    call_permit_expires_at: str
    credential_handle: str | None = None
    remote_task: RemoteTaskReference | None = None


@dataclass(frozen=True)
class CredentialInjection:
    headers: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    expires_at: str | None = None


@dataclass
class DiscoveredAgent:
    agent_id: str
    version_id: str
    source: str
    agent_card: AgentCard
    card_sha256: str = ""
    callable: bool = True
    blocked_reason: str | None = None
    route_kind: str = ""


@dataclass
class SpaceAgentPage:
    agents: list[DiscoveredAgent] = field(default_factory=list)
    etag: str | None = None
    next_cursor: str | None = None
    not_modified: bool = False


class A2AControlPlane(ABC):
    """Control-plane seam required by the product A2ASpaceClient."""

    @abstractmethod
    async def list_space_agents(
        self,
        space_id: str,
        *,
        prompt: str | None = None,
        skill_id: str | None = None,
        include_blocked: bool = False,
        if_none_match: str | None = None,
        cursor: str | None = None,
        page_size: int = 50,
    ) -> SpaceAgentPage:
        raise NotImplementedError

    @abstractmethod
    async def prepare_call(
        self,
        *,
        space_id: str,
        target_agent_id: str,
        expected_version_id: str | None,
        message_id: str,
        message_sha256: str,
        idempotency_token: str,
    ) -> PreparedA2AOperation:
        raise NotImplementedError

    @abstractmethod
    async def prepare_task_operation(
        self,
        *,
        platform_task_id: str,
        operation: A2AOperation,
        message_id: str | None = None,
        message_sha256: str | None = None,
        idempotency_token: str | None = None,
    ) -> PreparedA2AOperation:
        raise NotImplementedError

    @abstractmethod
    async def bind_remote_task(
        self,
        *,
        platform_task_id: str,
        remote_task_id: str,
        remote_context_id: str | None,
        observed_at: str,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def append_task_events(
        self,
        *,
        platform_task_id: str,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def resolve_credential(
        self,
        *,
        platform_task_id: str,
        credential_handle: str | None,
        call_permit: str,
    ) -> CredentialInjection:
        raise NotImplementedError

    @abstractmethod
    def gateway_token(self) -> str:
        raise NotImplementedError


class InternalA2AControlPlaneClient(A2AControlPlane):
    """Workload-authenticated client for Runtime internal A2A Actions."""

    def __init__(
        self,
        base_url: str,
        *,
        token_provider: WorkloadTokenProvider | None = None,
        httpx_client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
    ) -> None:
        if not base_url:
            raise ValueError(
                f"InternalA2AControlPlaneClient requires {ENV_A2A_CONTROL_PLANE_URL}"
            )
        self._base_url = base_url.rstrip("/")
        self._token_provider = token_provider or FileWorkloadTokenProvider()
        self._client = httpx_client
        self._timeout = timeout

    async def _post(
        self,
        action: A2AInternalAction,
        *,
        audience: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        token = self._token_provider.get_token(audience)
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        owns_client = self._client is None
        try:
            try:
                response = await client.post(
                    f"{self._base_url}{build_a2a_internal_action_path(action)}",
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                raise A2AControlPlaneError(
                    code=503,
                    message="A2A control plane is unavailable",
                    error_code="A2A_CONTROL_PLANE_UNAVAILABLE",
                    retryable=True,
                    action=action,
                ) from exc
            try:
                envelope = response.json()
            except ValueError:
                unavailable = response.status_code >= 500
                raise A2AControlPlaneError(
                    code=response.status_code if unavailable else 502,
                    message="A2A control plane returned a non-JSON response",
                    error_code=(
                        "A2A_CONTROL_PLANE_UNAVAILABLE"
                        if unavailable
                        else "A2A_CONTROL_PLANE_INVALID_RESPONSE"
                    ),
                    retryable=unavailable,
                    action=action,
                )
            if not isinstance(envelope, dict):
                raise A2AControlPlaneError(
                    code=response.status_code if response.is_error else 502,
                    message="A2A control plane response must be an object",
                    error_code="A2A_CONTROL_PLANE_INVALID_RESPONSE",
                    retryable=response.status_code >= 500,
                    action=action,
                )
            try:
                action_code = int(envelope.get("Code") or 0)
            except (TypeError, ValueError) as exc:
                raise A2AControlPlaneError(
                    code=502,
                    message="A2A control plane response Code must be an integer",
                    error_code="A2A_CONTROL_PLANE_INVALID_RESPONSE",
                    request_id=str(envelope.get("RequestId") or ""),
                    action=action,
                ) from exc
            if response.is_error or action_code != 0:
                self._raise_action_error(response, envelope, action)
            data = envelope.get("Data")
            if not isinstance(data, dict):
                raise A2AControlPlaneError(
                    code=response.status_code,
                    message="A2A control plane response Data must be an object",
                    error_code="A2A_CONTROL_PLANE_INVALID_RESPONSE",
                    request_id=str(envelope.get("RequestId") or ""),
                    action=action,
                )
            return data
        finally:
            if owns_client:
                await client.aclose()

    @staticmethod
    def _raise_action_error(
        response: httpx.Response,
        envelope: dict[str, Any],
        action: str,
    ) -> None:
        raw_data = envelope.get("Data")
        data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        raise A2AControlPlaneError(
            code=int(envelope.get("Code") or response.status_code),
            message=str(envelope.get("Message") or response.reason_phrase or "A2A action failed"),
            error_code=str(data.get("ErrorCode") or "A2A_CONTROL_PLANE_ERROR"),
            retryable=bool(data.get("Retryable", response.status_code >= 500)),
            field=str(data.get("Field")) if data.get("Field") is not None else None,
            details=data.get("Details") if isinstance(data.get("Details"), dict) else {},
            request_id=str(envelope.get("RequestId") or ""),
            action=str(envelope.get("Action") or action),
        )

    async def list_space_agents(
        self,
        space_id: str,
        *,
        prompt: str | None = None,
        skill_id: str | None = None,
        include_blocked: bool = False,
        if_none_match: str | None = None,
        cursor: str | None = None,
        page_size: int = 50,
    ) -> SpaceAgentPage:
        payload: dict[str, Any] = {"A2ASpaceId": space_id}
        if prompt:
            payload["Prompt"] = prompt
        if skill_id:
            payload["SkillId"] = skill_id
        if include_blocked:
            payload["IncludeBlocked"] = True
        if if_none_match:
            payload["IfNoneMatch"] = if_none_match
        if cursor:
            payload["Cursor"] = cursor
        if page_size != 50:
            payload["PageSize"] = page_size
        data = await self._post(
            "ListA2ASpaceAgents",
            audience=AUDIENCE_REGISTRY,
            payload=payload,
        )
        return SpaceAgentPage(
            agents=[_discovered_agent_from_wire(item) for item in data.get("Agents") or []],
            etag=_optional_str(data.get("ETag")),
            next_cursor=_optional_str(data.get("NextCursor")),
            not_modified=bool(data.get("NotModified", False)),
        )

    async def prepare_call(
        self,
        *,
        space_id: str,
        target_agent_id: str,
        expected_version_id: str | None,
        message_id: str,
        message_sha256: str,
        idempotency_token: str,
    ) -> PreparedA2AOperation:
        payload: dict[str, Any] = {
            "A2ASpaceId": space_id,
            "TargetA2AAgentId": target_agent_id,
            "MessageId": message_id,
            "MessageSha256": message_sha256,
            "IdempotencyToken": idempotency_token,
        }
        if expected_version_id:
            payload["ExpectedVersionId"] = expected_version_id
        data = await self._post("PrepareA2ACall", audience=AUDIENCE_REGISTRY, payload=payload)
        return _prepared_operation_from_wire(data)

    async def prepare_task_operation(
        self,
        *,
        platform_task_id: str,
        operation: A2AOperation,
        message_id: str | None = None,
        message_sha256: str | None = None,
        idempotency_token: str | None = None,
    ) -> PreparedA2AOperation:
        _validate_task_operation_request(
            operation=operation,
            message_id=message_id,
            message_sha256=message_sha256,
            idempotency_token=idempotency_token,
        )
        payload: dict[str, Any] = {"A2ATaskId": platform_task_id, "Operation": operation}
        if message_id:
            payload["MessageId"] = message_id
        if message_sha256:
            payload["MessageSha256"] = message_sha256
        if idempotency_token:
            payload["IdempotencyToken"] = idempotency_token
        data = await self._post(
            "PrepareA2ATaskOperation",
            audience=AUDIENCE_REGISTRY,
            payload=payload,
        )
        return _prepared_operation_from_wire(data)

    async def bind_remote_task(
        self,
        *,
        platform_task_id: str,
        remote_task_id: str,
        remote_context_id: str | None,
        observed_at: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "A2ATaskId": platform_task_id,
            "RemoteTaskId": remote_task_id,
            "ObservedAt": observed_at,
        }
        if remote_context_id:
            payload["RemoteContextId"] = remote_context_id
        return await self._post(
            "BindA2ARemoteTask",
            audience=AUDIENCE_TASK_SINK,
            payload=payload,
        )

    async def append_task_events(
        self,
        *,
        platform_task_id: str,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return await self._post(
            "AppendA2ATaskEvents",
            audience=AUDIENCE_TASK_SINK,
            payload={"A2ATaskId": platform_task_id, "Events": events},
        )

    async def resolve_credential(
        self,
        *,
        platform_task_id: str,
        credential_handle: str | None,
        call_permit: str,
    ) -> CredentialInjection:
        data = await self._post(
            "ResolveA2ACredential",
            audience=AUDIENCE_CREDENTIAL_BROKER,
            payload={
                "A2ATaskId": platform_task_id,
                "CredentialHandle": credential_handle,
                "CallPermit": call_permit,
            },
        )
        raw_injection = data.get("Injection")
        injection: dict[str, Any] = raw_injection if isinstance(raw_injection, dict) else {}
        return CredentialInjection(
            headers=_credential_string_map(
                injection.get("Headers"), field_name="Injection.Headers"
            ),
            query=_credential_string_map(injection.get("Query"), field_name="Injection.Query"),
            cookies=_credential_string_map(
                injection.get("Cookies"), field_name="Injection.Cookies"
            ),
            expires_at=_optional_str(data.get("ExpiresAt")),
        )

    def gateway_token(self) -> str:
        return self._token_provider.get_token(AUDIENCE_GATEWAY)


class A2AAgentCardClient(A2AControlPlane):
    """Card-driven control-plane client for hosted/external A2A discovery.

    发现面通过 KOP 调 server 对外 Action (ListAToASpaceAgents) 拿到 space 内
    每个 agent 的完整投影后 AgentCard（含 callable url）；调用面直接用
    card.url 发 A2A 请求，不回连 server 做 permit/credential。
    runtime 只需一个 service_url（KOP 对外 API，默认 aicp.api.ksyun.com），
    与 ksadk/skills 同款；card.url 由 server 投影，runtime 不需要知道 gateway 域名。

    KOP 鉴权（AICP AK/SK 签名）复用 ksadk.common.kop_client.KOPClient，
    和 skill/mcp 共用同一公共层。
    """

    def __init__(
        self,
        service_url: str,
        *,
        service_token: str = "",
        httpx_client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
    ) -> None:
        if not service_url:
            raise ValueError("A2AAgentCardClient requires a non-empty service_url")
        self._service_url = service_url.rstrip("/")
        self._service_token = service_token
        self._client = httpx_client
        self._timeout = timeout
        self._cards_by_agent: dict[str, tuple[AgentCard, str, str]] = {}
        self._kop = KOPClient(base_url=service_url, service_token=service_token, timeout=timeout)

    async def _post_action(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(self._kop.post_action, action, payload)
        except KOPError as exc:
            raise A2AControlPlaneError(
                code=exc.code,
                message=exc.message,
                error_code=f"KOP_{exc.code}",
                retryable=exc.code >= 500,
                action=action,  # type: ignore[arg-type]
            ) from exc

    async def list_space_agents(
        self,
        space_id: str,
        *,
        prompt: str | None = None,
        skill_id: str | None = None,
        include_blocked: bool = False,
        if_none_match: str | None = None,
        cursor: str | None = None,
        page_size: int = 50,
    ) -> SpaceAgentPage:
        _ = (prompt, skill_id, if_none_match)
        payload: dict[str, Any] = {
            "A2ASpaceId": space_id,
            "Status": "available" if not include_blocked else "",
            "PageSize": page_size,
        }
        if cursor:
            payload["PageNumber"] = int(cursor) if str(cursor).isdigit() else 1
        data = await self._post_action("ListAToASpaceAgents", payload)
        agents: list[DiscoveredAgent] = []
        for item in data.get("Agents") or []:
            if not isinstance(item, dict):
                continue
            a2a_agent_id = str(item.get("A2AAgentId") or "").strip()
            if not a2a_agent_id:
                continue
            # server 用纯 uuid,ksadk 内部要求 a2a-agent- 前缀;补前缀适配契约。
            if not a2a_agent_id.startswith("a2a-agent-"):
                a2a_agent_id = f"a2a-agent-{a2a_agent_id}"
            invocation_status = str(item.get("InvocationStatus") or "")
            if invocation_status and invocation_status != "available":
                continue
            card_payload = item.get("AgentCard")
            if not isinstance(card_payload, dict):
                continue
            try:
                card = _agent_card_from_wire(card_payload)
            except A2AControlPlaneError:
                continue
            sha = str(item.get("CardSha256") or "")
            version_id = str(item.get("VersionId") or "") or sha
            if version_id and not version_id.startswith("a2a-version-"):
                version_id = f"a2a-version-{version_id}"
            source = str(item.get("Source") or "hosted")
            self._cards_by_agent[a2a_agent_id] = (card, sha, version_id)
            agents.append(
                DiscoveredAgent(
                    agent_id=a2a_agent_id,
                    version_id=version_id,
                    source=source if source in {"hosted", "external"} else "hosted",
                    agent_card=card,
                    card_sha256=sha,
                    callable=True,
                    route_kind="hosted_gateway" if source == "hosted" else "external_public",
                )
            )
        return SpaceAgentPage(agents=agents)

    async def _resolve_card(self, target_agent_id: str) -> tuple[AgentCard, str, str]:
        cached = self._cards_by_agent.get(target_agent_id)
        if cached is not None:
            return cached
        await self.list_space_agents(os.getenv("KSADK_A2A_SPACE_ID", ""), page_size=100)
        cached = self._cards_by_agent.get(target_agent_id)
        if cached is None:
            raise A2AControlPlaneError(
                code=404,
                message=f"A2A agent {target_agent_id} not discoverable",
                error_code="A2A_AGENT_NOT_FOUND",
                action="PrepareA2ACall",  # type: ignore[arg-type]
            )
        return cached

    async def _direct_prepared(
        self,
        *,
        target_agent_id: str,
        version_id: str,
        card: AgentCard,
        card_sha256: str,
    ) -> PreparedA2AOperation:
        interfaces = list(card.supported_interfaces or [])
        interface = interfaces[0] if interfaces else None
        if interface is None or not interface.url:
            raise A2AControlPlaneError(
                code=502,
                message="discovered agent card has no callable interface url",
                error_code="A2A_CONTROL_PLANE_INVALID_RESPONSE",
                action="PrepareA2ACall",  # type: ignore[arg-type]
            )
        route = A2ARoute(
            kind="hosted_gateway",
            interface=A2ARouteInterface(
                url=str(interface.url),
                protocol_binding=str(interface.protocol_binding or "JSONRPC"),
                protocol_version=str(interface.protocol_version or "1.0"),
            ),
        )
        return PreparedA2AOperation(
            platform_task_id=f"a2a-task-{uuid.uuid4().hex}",
            target=A2ATarget(
                agent_id=target_agent_id,
                version_id=version_id,
                card_sha256=card_sha256,
            ),
            route=route,
            call_permit="",
            call_permit_expires_at="",
        )

    async def prepare_call(
        self,
        *,
        space_id: str,
        target_agent_id: str,
        expected_version_id: str | None,
        message_id: str,
        message_sha256: str,
        idempotency_token: str,
    ) -> PreparedA2AOperation:
        _ = (space_id, expected_version_id, message_id, message_sha256, idempotency_token)
        card, sha, version_id = await self._resolve_card(target_agent_id)
        return await self._direct_prepared(
            target_agent_id=target_agent_id,
            version_id=version_id,
            card=card,
            card_sha256=sha,
        )

    async def prepare_task_operation(
        self,
        *,
        platform_task_id: str,
        operation: A2AOperation,
        message_id: str | None = None,
        message_sha256: str | None = None,
        idempotency_token: str | None = None,
        agent_id: str | None = None,
    ) -> PreparedA2AOperation:
        _ = (operation, message_id, message_sha256, idempotency_token)
        target_id = agent_id or platform_task_id
        card, sha, version_id = await self._resolve_card(target_id)
        return await self._direct_prepared(
            target_agent_id=target_id,
            version_id=version_id,
            card=card,
            card_sha256=sha,
        )

    async def bind_remote_task(
        self,
        *,
        platform_task_id: str,
        remote_task_id: str,
        remote_context_id: str | None,
        observed_at: str,
    ) -> dict[str, Any]:
        _ = (platform_task_id, remote_context_id, observed_at)
        return {"RemoteTaskId": remote_task_id, "Bound": True}

    async def append_task_events(
        self,
        *,
        platform_task_id: str,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        _ = (platform_task_id, events)
        return {"Appended": len(events)}

    async def resolve_credential(
        self,
        *,
        platform_task_id: str,
        credential_handle: str | None,
        call_permit: str,
    ) -> CredentialInjection:
        _ = (platform_task_id, credential_handle, call_permit)
        return CredentialInjection()

    def gateway_token(self) -> str:
        return self._service_token


def _credential_string_map(value: Any, *, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()
    ):
        raise A2AControlPlaneError(
            code=502,
            message=f"control-plane response field {field_name} must be a string map",
            error_code="A2A_CONTROL_PLANE_INVALID_RESPONSE",
            field=field_name,
        )
    return dict(value)


def _validate_task_operation_request(
    *,
    operation: str,
    message_id: str | None,
    message_sha256: str | None,
    idempotency_token: str | None,
) -> None:
    if operation not in A2A_OPERATIONS:
        raise ValueError(f"unsupported A2A task operation: {operation!r}")
    if operation == "send_message":
        if not message_id or not message_sha256 or not idempotency_token:
            raise ValueError(
                "send_message requires message_id, message_sha256, and idempotency_token"
            )
        return
    if message_id or message_sha256:
        raise ValueError(f"{operation} does not accept message fields")
    if operation == "cancel_task":
        if not idempotency_token:
            raise ValueError("cancel_task requires idempotency_token")
        return
    if idempotency_token:
        raise ValueError(f"{operation} does not accept idempotency_token")


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _required_str(value: Any, *, field_name: str, prefix: str | None = None) -> str:
    result = str(value or "")
    if not result:
        raise A2AControlPlaneError(
            code=502,
            message=f"control-plane response field {field_name} is missing or invalid",
            error_code="A2A_CONTROL_PLANE_INVALID_RESPONSE",
            field=field_name,
        )
    if prefix is not None:
        try:
            require_a2a_resource_id(result, prefix, field_name=field_name)
        except ValueError as exc:
            raise A2AControlPlaneError(
                code=502,
                message=f"control-plane response field {field_name} has an invalid resource ID",
                error_code="A2A_CONTROL_PLANE_INVALID_RESPONSE",
                field=field_name,
            ) from exc
    return result


def _agent_card_from_wire(payload: Any) -> AgentCard:
    if not isinstance(payload, dict):
        raise A2AControlPlaneError(
            code=502,
            message="AgentCard must be an object",
            error_code="A2A_CONTROL_PLANE_INVALID_RESPONSE",
        )
    # 兼容 0.3 card：顶层 url/preferredTransport 折叠进 supportedInterfaces[0]。
    if not payload.get("supportedInterfaces") and payload.get("url"):
        payload = dict(payload)
        payload["supportedInterfaces"] = [{
            "url": payload["url"],
            "protocolBinding": payload.get("preferredTransport", "JSONRPC"),
            "protocolVersion": payload.get("protocolVersion", "0.3"),
        }]
    try:
        return ParseDict(payload, AgentCard(), ignore_unknown_fields=True)
    except (ParseError, TypeError, ValueError) as exc:
        raise A2AControlPlaneError(
            code=502,
            message="AgentCard does not match the A2A 1.0 schema",
            error_code="A2A_CONTROL_PLANE_INVALID_RESPONSE",
            field="AgentCard",
        ) from exc


def _discovered_agent_from_wire(item: dict[str, Any]) -> DiscoveredAgent:
    if not isinstance(item, dict):
        raise A2AControlPlaneError(
            code=502,
            message="ListA2ASpaceAgents item must be an object",
            error_code="A2A_CONTROL_PLANE_INVALID_RESPONSE",
            field="Agents",
        )
    route_kind = str(item.get("RouteKind") or "")
    if route_kind not in {"hosted_gateway", "external_public", "external_vpc"}:
        raise A2AControlPlaneError(
            code=502,
            message="ListA2ASpaceAgents returned an unknown RouteKind",
            error_code="A2A_CONTROL_PLANE_INVALID_RESPONSE",
            field="RouteKind",
        )
    source = _required_str(item.get("Source"), field_name="Source")
    if source not in {"hosted", "external"}:
        raise A2AControlPlaneError(
            code=502,
            message="ListA2ASpaceAgents returned an unknown Source",
            error_code="A2A_CONTROL_PLANE_INVALID_RESPONSE",
            field="Source",
        )
    return DiscoveredAgent(
        agent_id=_required_str(
            item.get("A2AAgentId"), field_name="A2AAgentId", prefix="a2a-agent-"
        ),
        version_id=_required_str(
            item.get("VersionId"), field_name="VersionId", prefix="a2a-version-"
        ),
        source=source,
        agent_card=_agent_card_from_wire(item.get("AgentCard")),
        card_sha256=str(item.get("CardSha256") or ""),
        callable=bool(item.get("Callable", False)),
        blocked_reason=_optional_str(item.get("BlockedReason")),
        route_kind=route_kind,
    )


def _prepared_operation_from_wire(data: dict[str, Any]) -> PreparedA2AOperation:
    raw_target = data.get("Target")
    target: dict[str, Any] = raw_target if isinstance(raw_target, dict) else {}
    raw_route = data.get("Route")
    route: dict[str, Any] = raw_route if isinstance(raw_route, dict) else {}
    raw_interface = route.get("Interface")
    interface: dict[str, Any] = raw_interface if isinstance(raw_interface, dict) else {}
    raw_remote_task = data.get("RemoteTask")
    if raw_remote_task is not None and not isinstance(raw_remote_task, dict):
        raise A2AControlPlaneError(
            code=502,
            message="control-plane response field RemoteTask must be an object or null",
            error_code="A2A_CONTROL_PLANE_INVALID_RESPONSE",
            field="RemoteTask",
        )
    remote_task: dict[str, Any] | None = (
        raw_remote_task if isinstance(raw_remote_task, dict) else None
    )
    route_kind = _required_str(route.get("Kind"), field_name="Route.Kind")
    if route_kind not in {"hosted_gateway", "external_public", "external_vpc"}:
        raise A2AControlPlaneError(
            code=502,
            message="prepared operation returned an unknown route kind",
            error_code="A2A_CONTROL_PLANE_INVALID_RESPONSE",
            field="Route.Kind",
        )
    protocol_binding = _required_str(
        interface.get("ProtocolBinding"), field_name="Route.Interface.ProtocolBinding"
    )
    if protocol_binding not in {"JSONRPC", "HTTP+JSON"}:
        raise A2AControlPlaneError(
            code=502,
            message="prepared operation returned an unsupported protocol binding",
            error_code="A2A_CONTROL_PLANE_INVALID_RESPONSE",
            field="Route.Interface.ProtocolBinding",
        )
    protocol_version = _required_str(
        interface.get("ProtocolVersion"), field_name="Route.Interface.ProtocolVersion"
    )
    if protocol_version != "1.0":
        raise A2AControlPlaneError(
            code=502,
            message="prepared operation returned an unsupported protocol version",
            error_code="A2A_CONTROL_PLANE_INVALID_RESPONSE",
            field="Route.Interface.ProtocolVersion",
        )
    return PreparedA2AOperation(
        platform_task_id=_required_str(
            data.get("A2ATaskId"), field_name="A2ATaskId", prefix="a2a-task-"
        ),
        target=A2ATarget(
            agent_id=_required_str(
                target.get("A2AAgentId"),
                field_name="Target.A2AAgentId",
                prefix="a2a-agent-",
            ),
            version_id=_required_str(
                target.get("VersionId"),
                field_name="Target.VersionId",
                prefix="a2a-version-",
            ),
            card_sha256=_required_str(target.get("CardSha256"), field_name="Target.CardSha256"),
        ),
        route=A2ARoute(
            kind=route_kind,
            interface=A2ARouteInterface(
                url=_required_str(interface.get("Url"), field_name="Route.Interface.Url"),
                protocol_binding=protocol_binding,
                protocol_version=protocol_version,
            ),
        ),
        call_permit=_required_str(data.get("CallPermit"), field_name="CallPermit"),
        call_permit_expires_at=_required_str(
            data.get("CallPermitExpiresAt"), field_name="CallPermitExpiresAt"
        ),
        credential_handle=_optional_str(data.get("CredentialHandle")),
        remote_task=(
            RemoteTaskReference(
                remote_task_id=_required_str(
                    remote_task.get("RemoteTaskId"), field_name="RemoteTask.RemoteTaskId"
                ),
                remote_context_id=_optional_str(remote_task.get("RemoteContextId")),
            )
            if remote_task is not None
            else None
        ),
    )


__all__ = [
    "A2AInternalAction",
    "A2A_INTERNAL_ACTIONS",
    "A2A_INTERNAL_PATH_PREFIX",
    "A2AAgentCardClient",
    "A2AControlPlane",
    "A2AControlPlaneError",
    "A2AOperation",
    "A2ARoute",
    "A2ARouteInterface",
    "A2ATarget",
    "CredentialInjection",
    "DiscoveredAgent",
    "ENV_A2A_CONTROL_PLANE_URL",
    "ENV_A2A_TOKEN_DIR",
    "FileWorkloadTokenProvider",
    "InternalA2AControlPlaneClient",
    "PreparedA2AOperation",
    "RemoteTaskReference",
    "SpaceAgentPage",
    "WorkloadTokenProvider",
    "build_a2a_internal_action_path",
]

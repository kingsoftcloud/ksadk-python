"""Security boundary for DeepSeek Harness UI plugins embedded by Studio.

This module deliberately has no FastAPI dependency.  Route composition may use
it to create a short-lived capability, render the credentialless iframe shell,
and authorize messages relayed by the trusted Studio host page.  The untrusted
client bundle never receives a Studio cookie or CSRF token and cannot issue
network requests from the frame.

``source_id`` is an opaque handle owned by the trusted host page.  A browser
integration must bind it to the concrete ``iframe.contentWindow`` and its
dedicated ``MessagePort``; it must never copy a source id out of an untrusted
message and call that verification.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import math
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Annotated, Any, Literal, Union, cast
from urllib.parse import parse_qs, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from ksadk.studio.errors import StudioError

DSH_UI_PROTOCOL_VERSION: Literal["agentkit.dsh-ui/v1"] = "agentkit.dsh-ui/v1"
DSH_UI_FRAME_ORIGIN = "null"
DSH_UI_IFRAME_SANDBOX = "allow-scripts"
DSH_UI_IFRAME_REFERRER_POLICY = "no-referrer"

_LOCAL_STUDIO_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testserver"})
_SESSION_ID_PATTERN = re.compile(r"^dshui_[A-Za-z0-9_-]{24,96}$")
_SOURCE_ID_PATTERN = re.compile(r"^frame_[A-Za-z0-9_-]{16,96}$")
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TOOL_ID_PATTERN = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9@._:/-]{0,255}$")
_PLUGIN_ID_PATTERN = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9@._/-]{0,255}$")
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_GENERATION_ID_PATTERN = re.compile(r"^dshgen_[A-Za-z0-9_-]{24,96}$")
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "authorization",
        "capability",
        "capabilitytoken",
        "cookie",
        "csrf",
        "csrftoken",
        "session",
        "sessionid",
        "token",
    }
)


class DshUiSandboxError(StudioError):
    """A stable, user-safe rejection at the UI sandbox boundary."""


def _sandbox_error(code: str, message: str, *, status_code: int) -> DshUiSandboxError:
    return DshUiSandboxError(code, message, status_code=status_code)


class _StrictEnvelope(BaseModel):
    model_config = ConfigDict(
        alias_generator=lambda value: value.split("_")[0]
        + "".join(part.capitalize() for part in value.split("_")[1:]),
        populate_by_name=True,
        extra="forbid",
        strict=True,
    )


class DshUiListToolsPayload(_StrictEnvelope):
    pass


class DshUiCallToolPayload(_StrictEnvelope):
    call_id: str = Field(pattern=_REQUEST_ID_PATTERN.pattern)
    tool_id: str = Field(pattern=_TOOL_ID_PATTERN.pattern)
    arguments: dict[str, Any] = Field(default_factory=dict)
    deadline_ms: int = Field(default=30_000, ge=1, le=120_000)


class DshUiCancelToolPayload(_StrictEnvelope):
    call_id: str = Field(pattern=_REQUEST_ID_PATTERN.pattern)


class _DshUiRequestEnvelope(_StrictEnvelope):
    protocol_version: Literal["agentkit.dsh-ui/v1"] = DSH_UI_PROTOCOL_VERSION
    kind: Literal["request"] = "request"
    session_id: str = Field(pattern=_SESSION_ID_PATTERN.pattern)
    capability_token: str = Field(min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    source_id: str = Field(pattern=_SOURCE_ID_PATTERN.pattern)
    request_id: str = Field(pattern=_REQUEST_ID_PATTERN.pattern)


class DshUiListToolsRequest(_DshUiRequestEnvelope):
    method: Literal["listTools"]
    payload: DshUiListToolsPayload = Field(default_factory=DshUiListToolsPayload)


class DshUiCallToolRequest(_DshUiRequestEnvelope):
    method: Literal["callTool"]
    payload: DshUiCallToolPayload


class DshUiCancelToolRequest(_DshUiRequestEnvelope):
    method: Literal["cancelTool"]
    payload: DshUiCancelToolPayload


DshUiRequest = Annotated[
    Union[DshUiListToolsRequest, DshUiCallToolRequest, DshUiCancelToolRequest],
    Field(discriminator="method"),
]
_REQUEST_ADAPTER: TypeAdapter[DshUiRequest] = TypeAdapter(DshUiRequest)


class DshUiReadyEnvelope(_StrictEnvelope):
    protocol_version: Literal["agentkit.dsh-ui/v1"] = DSH_UI_PROTOCOL_VERSION
    kind: Literal["ready"] = "ready"
    session_id: str = Field(pattern=_SESSION_ID_PATTERN.pattern)
    source_id: str = Field(pattern=_SOURCE_ID_PATTERN.pattern)


class DshUiResponseError(_StrictEnvelope):
    code: str = Field(min_length=1, max_length=128, pattern=r"^[A-Z0-9_]+$")
    message: str = Field(min_length=1, max_length=1024)


class DshUiSuccessResponse(_StrictEnvelope):
    protocol_version: Literal["agentkit.dsh-ui/v1"] = DSH_UI_PROTOCOL_VERSION
    kind: Literal["response"] = "response"
    session_id: str = Field(pattern=_SESSION_ID_PATTERN.pattern)
    request_id: str = Field(pattern=_REQUEST_ID_PATTERN.pattern)
    ok: Literal[True] = True
    result: Any = None


class DshUiErrorResponse(_StrictEnvelope):
    protocol_version: Literal["agentkit.dsh-ui/v1"] = DSH_UI_PROTOCOL_VERSION
    kind: Literal["response"] = "response"
    session_id: str = Field(pattern=_SESSION_ID_PATTERN.pattern)
    request_id: str = Field(pattern=_REQUEST_ID_PATTERN.pattern)
    ok: Literal[False] = False
    error: DshUiResponseError


@dataclass(frozen=True)
class DshUiSandboxLimits:
    """Server-enforced limits; browser checks are only an early rejection."""

    max_message_bytes: int = 64 * 1024
    max_requests_per_window: int = 60
    rate_window_seconds: float = 60.0
    max_concurrent_calls: int = 4
    max_sessions: int = 256
    max_tools_per_session: int = 256
    session_ttl_seconds: float = 15 * 60.0
    idle_ttl_seconds: float = 5 * 60.0
    max_deadline_ms: int = 120_000

    def __post_init__(self) -> None:
        values = (
            self.max_message_bytes,
            self.max_requests_per_window,
            self.rate_window_seconds,
            self.max_concurrent_calls,
            self.max_sessions,
            self.max_tools_per_session,
            self.session_ttl_seconds,
            self.idle_ttl_seconds,
            self.max_deadline_ms,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            for value in values
        ):
            raise ValueError("DSH UI sandbox limits must all be positive")
        if self.max_message_bytes > 1024 * 1024:
            raise ValueError("DSH UI messages may not exceed 1 MiB")
        if self.max_deadline_ms > 120_000:
            raise ValueError("DSH UI deadlines may not exceed 120 seconds")
        if self.idle_ttl_seconds > self.session_ttl_seconds:
            raise ValueError("idle TTL may not exceed the absolute session TTL")


@dataclass(frozen=True)
class DshUiSandboxGrant:
    """One-time server result consumed by the trusted Studio host page."""

    session_id: str
    source_id: str
    handshake_nonce: str
    plugin_id: str
    extension_id: str
    client_digest: str
    descriptor_digest: str
    generation_id: str
    parent_origin: str
    allowed_tool_ids: tuple[str, ...]
    expires_in_seconds: float
    agent_id: str | None = None
    protocol_version: str = DSH_UI_PROTOCOL_VERSION
    capability_token: str = field(repr=False, default="")

    def host_handshake(self) -> dict[str, Any]:
        """Return the exact init payload the trusted parent transfers with a port."""

        return {
            "protocolVersion": self.protocol_version,
            "kind": "init",
            "sessionId": self.session_id,
            "sourceId": self.source_id,
            "handshakeNonce": self.handshake_nonce,
            "capabilityToken": self.capability_token,
        }


@dataclass(frozen=True)
class DshUiAuthorizedRequest:
    """Sanitized dispatch input containing no bearer capability."""

    session_id: str
    plugin_id: str
    extension_id: str
    agent_id: str | None
    source_id: str
    request_id: str
    method: Literal["listTools", "callTool", "cancelTool"]
    allowed_tool_ids: tuple[str, ...]
    descriptor_digest: str
    generation_id: str
    tool_id: str | None = None
    call_id: str | None = None
    arguments: Mapping[str, Any] | None = None
    deadline_ms: int | None = None


@dataclass(frozen=True)
class DshUiSandboxDocument:
    """A no-store frame response plus the required parent iframe attributes."""

    html: str
    content_security_policy: str
    response_headers: Mapping[str, str]
    iframe_attributes: Mapping[str, str]


@dataclass
class _DshUiSession:
    session_id: str
    source_id: str
    handshake_nonce: str
    plugin_id: str
    extension_id: str
    client_digest: str
    descriptor_digest: str
    generation_id: str
    parent_origin: str
    allowed_tool_ids: tuple[str, ...]
    capability_digest: bytes
    created_at: float
    last_activity_at: float
    agent_id: str | None
    request_times: deque[float] = field(default_factory=deque)
    active_calls: set[str] = field(default_factory=set)
    revoked: bool = False


def _json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member: {key}")
        result[key] = value
    return result


def _encoded_json_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def parse_dsh_ui_request(
    raw: bytes | bytearray | str | Mapping[str, Any],
    *,
    max_message_bytes: int = 64 * 1024,
) -> DshUiRequest:
    """Size-check and strictly parse one untrusted postMessage request."""

    if max_message_bytes <= 0 or max_message_bytes > 1024 * 1024:
        raise ValueError("max_message_bytes must be between 1 byte and 1 MiB")
    try:
        if isinstance(raw, (bytes, bytearray)):
            encoded = bytes(raw)
            if len(encoded) > max_message_bytes:
                raise _sandbox_error(
                    "DSH_UI_MESSAGE_TOO_LARGE",
                    "DSH UI 消息超过大小限制",
                    status_code=413,
                )
            decoded = encoded.decode("utf-8")
            payload = json.loads(
                decoded,
                object_pairs_hook=_unique_object,
                parse_constant=_json_constant,
            )
        elif isinstance(raw, str):
            encoded = raw.encode("utf-8")
            if len(encoded) > max_message_bytes:
                raise _sandbox_error(
                    "DSH_UI_MESSAGE_TOO_LARGE",
                    "DSH UI 消息超过大小限制",
                    status_code=413,
                )
            payload = json.loads(
                raw,
                object_pairs_hook=_unique_object,
                parse_constant=_json_constant,
            )
        elif isinstance(raw, Mapping):
            payload = dict(raw)
            if _encoded_json_size(payload) > max_message_bytes:
                raise _sandbox_error(
                    "DSH_UI_MESSAGE_TOO_LARGE",
                    "DSH UI 消息超过大小限制",
                    status_code=413,
                )
        else:
            raise TypeError("message must be JSON text, bytes, or an object")
        if not isinstance(payload, dict):
            raise TypeError("message root must be an object")
        return cast(DshUiRequest, _REQUEST_ADAPTER.validate_python(payload))
    except DshUiSandboxError:
        raise
    except (UnicodeError, ValueError, TypeError, RecursionError, ValidationError) as exc:
        raise _sandbox_error(
            "DSH_UI_MESSAGE_INVALID",
            "DSH UI 消息不符合受支持的协议",
            status_code=422,
        ) from exc


def _normalize_parent_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("parent origin is invalid") from exc
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme not in {"http", "https"}
        or host not in _LOCAL_STUDIO_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("parent origin must be one loopback HTTP(S) origin")
    default_port = 80 if parsed.scheme == "http" else 443
    rendered_host = f"[{host}]" if ":" in host else host
    netloc = rendered_host if port in {None, default_port} else f"{rendered_host}:{port}"
    return urlunsplit((parsed.scheme, netloc, "", "", ""))


def _validated_identifier(value: str, *, name: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} is invalid")
    normalized = value.strip()
    if not pattern.fullmatch(normalized):
        raise ValueError(f"{name} is invalid")
    return normalized


class DshUiSandboxSessionStore:
    """In-memory, process-local capability authority for UI frame messages."""

    def __init__(
        self,
        *,
        limits: DshUiSandboxLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits or DshUiSandboxLimits()
        self._clock = clock
        self._sessions: dict[str, _DshUiSession] = {}
        self._pending_expired_cancellations: dict[str, tuple[str, ...]] = {}
        self._lock = threading.RLock()

    def create_session(
        self,
        *,
        plugin_id: str,
        extension_id: str,
        client_digest: str,
        descriptor_digest: str,
        generation_id: str,
        parent_origin: str,
        allowed_tool_ids: Sequence[str],
        agent_id: str | None = None,
    ) -> DshUiSandboxGrant:
        normalized_plugin = _validated_identifier(
            plugin_id, name="plugin_id", pattern=_PLUGIN_ID_PATTERN
        )
        normalized_extension = _validated_identifier(
            extension_id, name="extension_id", pattern=_TOOL_ID_PATTERN
        )
        if not _DIGEST_PATTERN.fullmatch(client_digest):
            raise ValueError("client_digest must be a lowercase sha256 digest")
        if not _DIGEST_PATTERN.fullmatch(descriptor_digest):
            raise ValueError("descriptor_digest must be a lowercase sha256 digest")
        if not _GENERATION_ID_PATTERN.fullmatch(generation_id):
            raise ValueError("generation_id must be a valid DSH generation")
        normalized_origin = _normalize_parent_origin(parent_origin)
        if isinstance(allowed_tool_ids, (str, bytes)):
            raise ValueError("allowed_tool_ids must be a sequence of identifiers")
        tools = tuple(
            sorted(
                {
                    _validated_identifier(tool, name="tool_id", pattern=_TOOL_ID_PATTERN)
                    for tool in allowed_tool_ids
                }
            )
        )
        if len(tools) > self.limits.max_tools_per_session:
            raise ValueError("allowed tool set exceeds the session limit")
        if agent_id is not None:
            agent_id = _validated_identifier(agent_id, name="agent_id", pattern=_TOOL_ID_PATTERN)

        now = self._clock()
        capability = secrets.token_urlsafe(32)
        session_id = f"dshui_{secrets.token_urlsafe(24)}"
        source_id = f"frame_{secrets.token_urlsafe(18)}"
        handshake_nonce = secrets.token_urlsafe(24)
        session = _DshUiSession(
            session_id=session_id,
            source_id=source_id,
            handshake_nonce=handshake_nonce,
            plugin_id=normalized_plugin,
            extension_id=normalized_extension,
            client_digest=client_digest,
            descriptor_digest=descriptor_digest,
            generation_id=generation_id,
            parent_origin=normalized_origin,
            allowed_tool_ids=tools,
            capability_digest=hashlib.sha256(capability.encode("ascii")).digest(),
            created_at=now,
            last_activity_at=now,
            agent_id=agent_id,
        )
        with self._lock:
            self._purge_expired_locked(now)
            if len(self._sessions) >= self.limits.max_sessions:
                raise _sandbox_error(
                    "DSH_UI_SESSION_LIMIT_REACHED",
                    "DSH UI 活跃会话已达到上限",
                    status_code=429,
                )
            while session_id in self._sessions:
                session_id = f"dshui_{secrets.token_urlsafe(24)}"
                session.session_id = session_id
            self._sessions[session_id] = session
        return DshUiSandboxGrant(
            session_id=session_id,
            source_id=source_id,
            handshake_nonce=handshake_nonce,
            plugin_id=normalized_plugin,
            extension_id=normalized_extension,
            client_digest=client_digest,
            descriptor_digest=descriptor_digest,
            generation_id=generation_id,
            parent_origin=normalized_origin,
            allowed_tool_ids=tools,
            expires_in_seconds=self.limits.session_ttl_seconds,
            agent_id=agent_id,
            capability_token=capability,
        )

    def authorize_message(
        self,
        raw: bytes | bytearray | str | Mapping[str, Any],
        *,
        parent_origin: str,
        source_id: str,
        frame_origin: str,
    ) -> DshUiAuthorizedRequest:
        """Authorize a message only after the host verified its frame/port binding."""

        request = parse_dsh_ui_request(raw, max_message_bytes=self.limits.max_message_bytes)
        try:
            normalized_origin = _normalize_parent_origin(parent_origin)
        except ValueError as exc:
            raise self._invalid_session() from exc
        now = self._clock()
        with self._lock:
            session = self._sessions.get(request.session_id)
            supplied_digest = hashlib.sha256(request.capability_token.encode("ascii")).digest()
            if session is None:
                hmac.compare_digest(supplied_digest, b"\0" * 32)
                raise self._invalid_session()
            if self._is_expired(session, now):
                self._expire_session_locked(session)
                raise self._invalid_session()
            valid_context = (
                frame_origin == DSH_UI_FRAME_ORIGIN
                and hmac.compare_digest(normalized_origin, session.parent_origin)
                and hmac.compare_digest(source_id, session.source_id)
                and hmac.compare_digest(request.source_id, session.source_id)
                and hmac.compare_digest(supplied_digest, session.capability_digest)
            )
            if session.revoked or not valid_context:
                raise self._invalid_session()

            self._admit_rate_locked(session, now, exempt=request.method == "cancelTool")
            session.last_activity_at = now
            if isinstance(request, DshUiListToolsRequest):
                return self._authorized(session, request)
            if isinstance(request, DshUiCallToolRequest):
                if request.payload.deadline_ms > self.limits.max_deadline_ms:
                    raise _sandbox_error(
                        "DSH_UI_DEADLINE_INVALID",
                        "DSH UI 工具调用超时值超过允许上限",
                        status_code=422,
                    )
                if request.payload.tool_id not in session.allowed_tool_ids:
                    raise _sandbox_error(
                        "DSH_UI_TOOL_FORBIDDEN",
                        "该 DSH UI 会话无权调用请求的工具",
                        status_code=403,
                    )
                if request.payload.call_id in session.active_calls:
                    raise _sandbox_error(
                        "DSH_UI_CALL_DUPLICATE",
                        "DSH UI 工具调用标识已存在",
                        status_code=409,
                    )
                if len(session.active_calls) >= self.limits.max_concurrent_calls:
                    raise _sandbox_error(
                        "DSH_UI_CALL_LIMIT_REACHED",
                        "DSH UI 并发工具调用已达到上限",
                        status_code=429,
                    )
                session.active_calls.add(request.payload.call_id)
                return self._authorized(session, request)
            if request.payload.call_id not in session.active_calls:
                raise _sandbox_error(
                    "DSH_UI_CALL_NOT_ACTIVE",
                    "待取消的 DSH UI 工具调用不存在",
                    status_code=404,
                )
            return self._authorized(session, request)

    def complete_call(self, session_id: str, call_id: str) -> bool:
        """Release an in-flight slot after result, failure, timeout, or cancellation."""

        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            existed = call_id in session.active_calls
            session.active_calls.discard(call_id)
            return existed

    def frame_grant(self, session_id: str) -> DshUiSandboxGrant:
        """Return token-free frame bootstrap state for one live session."""

        now = self._clock()
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise self._invalid_session()
            if self._is_expired(session, now):
                self._expire_session_locked(session)
                raise self._invalid_session()
            remaining = min(
                self.limits.session_ttl_seconds - (now - session.created_at),
                self.limits.idle_ttl_seconds - (now - session.last_activity_at),
            )
            return DshUiSandboxGrant(
                session_id=session.session_id,
                source_id=session.source_id,
                handshake_nonce=session.handshake_nonce,
                plugin_id=session.plugin_id,
                extension_id=session.extension_id,
                client_digest=session.client_digest,
                descriptor_digest=session.descriptor_digest,
                generation_id=session.generation_id,
                parent_origin=session.parent_origin,
                allowed_tool_ids=session.allowed_tool_ids,
                expires_in_seconds=max(0.0, remaining),
                agent_id=session.agent_id,
            )

    def revoke_session(self, session_id: str) -> tuple[str, ...]:
        """Revoke a session and return call ids that the host must cancel."""

        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return ()
            session.revoked = True
            active = tuple(sorted(session.active_calls))
            session.active_calls.clear()
            return active

    def revoke_plugin(self, plugin_id: str) -> dict[str, tuple[str, ...]]:
        """Atomically revoke every session owned by a disabled/uninstalled plugin."""

        with self._lock:
            session_ids = [
                session_id
                for session_id, session in self._sessions.items()
                if session.plugin_id == plugin_id
            ]
            return {session_id: self.revoke_session(session_id) for session_id in session_ids}

    def revoke_all(self) -> dict[str, tuple[str, ...]]:
        """Revoke every UI capability before a Profile refresh or shutdown."""

        with self._lock:
            session_ids = tuple(self._sessions)
            return {session_id: self.revoke_session(session_id) for session_id in session_ids}

    def purge_expired(self) -> dict[str, tuple[str, ...]]:
        """Remove expired sessions and return outstanding calls for cancellation."""

        now = self._clock()
        with self._lock:
            self._purge_expired_locked(now)
            pending = dict(self._pending_expired_cancellations)
            self._pending_expired_cancellations.clear()
            return pending

    @staticmethod
    def _invalid_session() -> DshUiSandboxError:
        return _sandbox_error(
            "DSH_UI_SESSION_INVALID",
            "DSH UI 会话无效、已过期或来源不匹配",
            status_code=403,
        )

    def _is_expired(self, session: _DshUiSession, now: float) -> bool:
        return (
            session.revoked
            or now - session.created_at >= self.limits.session_ttl_seconds
            or now - session.last_activity_at >= self.limits.idle_ttl_seconds
        )

    def _purge_expired_locked(self, now: float) -> dict[str, tuple[str, ...]]:
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if self._is_expired(session, now)
        ]
        result: dict[str, tuple[str, ...]] = {}
        for session_id in expired:
            result[session_id] = self._expire_session_locked(self._sessions[session_id])
        return result

    def _expire_session_locked(self, session: _DshUiSession) -> tuple[str, ...]:
        self._sessions.pop(session.session_id, None)
        session.revoked = True
        active = tuple(sorted(session.active_calls))
        session.active_calls.clear()
        self._pending_expired_cancellations[session.session_id] = active
        return active

    def _admit_rate_locked(self, session: _DshUiSession, now: float, *, exempt: bool) -> None:
        threshold = now - self.limits.rate_window_seconds
        while session.request_times and session.request_times[0] <= threshold:
            session.request_times.popleft()
        # Cancellation stays available under load so a noisy frame cannot pin
        # host resources after consuming its regular request budget.
        if not exempt and len(session.request_times) >= self.limits.max_requests_per_window:
            raise _sandbox_error(
                "DSH_UI_RATE_LIMITED",
                "DSH UI 请求过于频繁",
                status_code=429,
            )
        if not exempt:
            session.request_times.append(now)

    @staticmethod
    def _authorized(session: _DshUiSession, request: DshUiRequest) -> DshUiAuthorizedRequest:
        if isinstance(request, DshUiCallToolRequest):
            return DshUiAuthorizedRequest(
                session_id=session.session_id,
                plugin_id=session.plugin_id,
                extension_id=session.extension_id,
                agent_id=session.agent_id,
                source_id=session.source_id,
                request_id=request.request_id,
                method=request.method,
                allowed_tool_ids=session.allowed_tool_ids,
                descriptor_digest=session.descriptor_digest,
                generation_id=session.generation_id,
                tool_id=request.payload.tool_id,
                call_id=request.payload.call_id,
                arguments=MappingProxyType(dict(request.payload.arguments)),
                deadline_ms=request.payload.deadline_ms,
            )
        if isinstance(request, DshUiCancelToolRequest):
            return DshUiAuthorizedRequest(
                session_id=session.session_id,
                plugin_id=session.plugin_id,
                extension_id=session.extension_id,
                agent_id=session.agent_id,
                source_id=session.source_id,
                request_id=request.request_id,
                method=request.method,
                allowed_tool_ids=session.allowed_tool_ids,
                descriptor_digest=session.descriptor_digest,
                generation_id=session.generation_id,
                call_id=request.payload.call_id,
            )
        return DshUiAuthorizedRequest(
            session_id=session.session_id,
            plugin_id=session.plugin_id,
            extension_id=session.extension_id,
            agent_id=session.agent_id,
            source_id=session.source_id,
            request_id=request.request_id,
            method=request.method,
            allowed_tool_ids=session.allowed_tool_ids,
            descriptor_digest=session.descriptor_digest,
            generation_id=session.generation_id,
        )


def _validate_bundle_url(bundle_url: str, client_digest: str) -> str:
    parsed = urlsplit(bundle_url)
    if (
        parsed.scheme
        or parsed.netloc
        or not parsed.path.startswith("/api/v1/plugin-ecosystems/dsh/")
        or "\\" in parsed.path
        or parsed.fragment
    ):
        raise ValueError("client bundle URL must be a relative DSH API path")
    query = parse_qs(parsed.query, keep_blank_values=True)
    normalized_keys = {re.sub(r"[^a-z]", "", key.lower()) for key in query}
    if normalized_keys & _SENSITIVE_QUERY_KEYS:
        raise ValueError("client bundle URL must not contain credentials")
    digests = query.get("digest", [])
    if digests != [client_digest]:
        raise ValueError("client bundle URL must carry its exact digest")
    return bundle_url


def _digest_to_sri(digest: str) -> str:
    if not _DIGEST_PATTERN.fullmatch(digest):
        raise ValueError("client digest must be a lowercase sha256 digest")
    return "sha256-" + base64.b64encode(bytes.fromhex(digest.split(":", 1)[1])).decode("ascii")


_BOOTSTRAP_TEMPLATE = r"""(() => {
  "use strict";
  const config = Object.freeze(__CONFIG__);
  const protocol = "agentkit.dsh-ui/v1";
  const maxBytes = config.maxMessageBytes;
  const maxPending = config.maxConcurrentCalls + 4;
  const textEncoder = new TextEncoder();
  const pending = new Map();
  const activeCalls = new Set();
  let port = null;
  let capabilityToken = "";
  let sequence = 0;
  let resolveReady;
  let rejectReady;
  const ready = new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject; });
  const stopImmediate = Function.call.bind(Event.prototype.stopImmediatePropagation);

  const encodedSize = (value) => textEncoder.encode(JSON.stringify(value)).byteLength;
  const nextId = (prefix) => `${prefix}_${Date.now().toString(36)}_${(++sequence).toString(36)}`;
  const safeError = (code, message) => Object.assign(new Error(message), { code });

  function dispatch(method, payload) {
    if (!port) throw safeError("DSH_UI_NOT_READY", "DSH UI bridge is not ready");
    if (pending.size >= maxPending) {
      throw safeError("DSH_UI_CLIENT_LIMIT", "Too many pending DSH UI requests");
    }
    const requestId = nextId("req");
    const envelope = {
      protocolVersion: protocol,
      kind: "request",
      sessionId: config.sessionId,
      capabilityToken,
      sourceId: config.sourceId,
      requestId,
      method,
      payload
    };
    if (encodedSize(envelope) > maxBytes) {
      throw safeError("DSH_UI_MESSAGE_TOO_LARGE", "DSH UI request exceeds its size limit");
    }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(requestId);
        reject(safeError("DSH_UI_RESPONSE_TIMEOUT", "DSH UI host did not respond"));
      }, Math.min(payload.deadlineMs || 10000, config.maxDeadlineMs) + 1000);
      pending.set(requestId, { resolve, reject, timer });
      port.postMessage(envelope);
    });
  }

  function request(method, payload) {
    return ready.then(() => dispatch(method, payload));
  }

  const api = Object.freeze({
    protocolVersion: protocol,
    ready,
    listTools() {
      return request("listTools", {});
    },
    callTool(toolId, args = {}, options = {}) {
      if (typeof toolId !== "string" || !toolId || !args ||
          typeof args !== "object" || Array.isArray(args)) {
        return Promise.reject(safeError("DSH_UI_CALL_INVALID", "Invalid DSH UI tool call"));
      }
      const callId = options.callId || nextId("call");
      const deadlineMs = options.deadlineMs || 30000;
      if (!Number.isSafeInteger(deadlineMs) || deadlineMs < 1 ||
          deadlineMs > config.maxDeadlineMs) {
        return Promise.reject(safeError("DSH_UI_DEADLINE_INVALID", "Invalid DSH UI deadline"));
      }
      if (activeCalls.size >= config.maxConcurrentCalls || activeCalls.has(callId)) {
        return Promise.reject(safeError("DSH_UI_CLIENT_LIMIT", "Too many active DSH UI calls"));
      }
      activeCalls.add(callId);
      return request("callTool", { callId, toolId, arguments: args, deadlineMs })
        .finally(() => activeCalls.delete(callId));
    },
    cancelTool(callId) {
      if (typeof callId !== "string" || !activeCalls.has(callId)) {
        return Promise.reject(safeError("DSH_UI_CALL_NOT_ACTIVE", "DSH UI call is not active"));
      }
      return request("cancelTool", { callId });
    }
  });
  Object.defineProperty(window, "AgentKitDshUI", {
    value: api,
    configurable: false,
    enumerable: false,
    writable: false
  });

  function receivePortMessage(event) {
    const message = event.data;
    if (!message || typeof message !== "object" || encodedSize(message) > maxBytes) return;
    if (message.protocolVersion !== protocol || message.kind !== "response" ||
        message.sessionId !== config.sessionId || typeof message.requestId !== "string" ||
        typeof message.ok !== "boolean") return;
    const waiter = pending.get(message.requestId);
    if (!waiter) return;
    pending.delete(message.requestId);
    clearTimeout(waiter.timer);
    if (message.ok) waiter.resolve(message.result);
    else {
      const error = message.error;
      waiter.reject(safeError(
        error && typeof error.code === "string" ? error.code : "DSH_UI_HOST_ERROR",
        error && typeof error.message === "string" ? error.message : "DSH UI host rejected request"
      ));
    }
  }

  function receiveInit(event) {
    const message = event.data;
    if (port || event.source !== window.parent || event.origin !== config.parentOrigin ||
        !message || typeof message !== "object" || event.ports.length !== 1 ||
        message.protocolVersion !== protocol || message.kind !== "init" ||
        message.sessionId !== config.sessionId || message.sourceId !== config.sourceId ||
        message.handshakeNonce !== config.handshakeNonce ||
        typeof message.capabilityToken !== "string" || message.capabilityToken.length < 32) return;
    stopImmediate(event);
    capabilityToken = message.capabilityToken;
    port = event.ports[0];
    port.onmessage = receivePortMessage;
    port.start();
    port.postMessage({
      protocolVersion: protocol,
      kind: "ready",
      sessionId: config.sessionId,
      sourceId: config.sourceId
    });
    resolveReady();
  }
  window.addEventListener("message", receiveInit, true);
  window.addEventListener("pagehide", () => {
    rejectReady(safeError("DSH_UI_DISPOSED", "DSH UI frame was disposed"));
    for (const waiter of pending.values()) {
      clearTimeout(waiter.timer);
      waiter.reject(safeError("DSH_UI_DISPOSED", "DSH UI frame was disposed"));
    }
    pending.clear();
    activeCalls.clear();
    if (port) port.close();
    port = null;
    capabilityToken = "";
  }, { once: true });
})();"""


def render_dsh_ui_sandbox_document(
    grant: DshUiSandboxGrant,
    *,
    client_bundle_url: str | None = None,
    title: str = "DSH plugin",
    limits: DshUiSandboxLimits | None = None,
) -> DshUiSandboxDocument:
    """Render a digest-pinned, network-disabled iframe document.

    The capability token is intentionally absent from this document.  The
    trusted parent transfers it together with a fresh ``MessagePort`` after it
    verifies ``iframe.contentWindow``.  The document validates the exact parent
    origin and one-time handshake nonce before accepting that port.
    """

    active_limits = limits or DshUiSandboxLimits()
    if grant.protocol_version != DSH_UI_PROTOCOL_VERSION:
        raise ValueError("unsupported DSH UI sandbox protocol")
    _validated_identifier(grant.session_id, name="session_id", pattern=_SESSION_ID_PATTERN)
    _validated_identifier(grant.source_id, name="source_id", pattern=_SOURCE_ID_PATTERN)
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,128}", grant.handshake_nonce):
        raise ValueError("handshake nonce is invalid")
    _digest_to_sri(grant.client_digest)
    config = json.dumps(
        {
            "protocolVersion": DSH_UI_PROTOCOL_VERSION,
            "sessionId": grant.session_id,
            "sourceId": grant.source_id,
            "handshakeNonce": grant.handshake_nonce,
            "descriptorDigest": grant.descriptor_digest,
            "parentOrigin": _normalize_parent_origin(grant.parent_origin),
            "maxMessageBytes": active_limits.max_message_bytes,
            "maxConcurrentCalls": active_limits.max_concurrent_calls,
            "maxDeadlineMs": active_limits.max_deadline_ms,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    bootstrap = _BOOTSTRAP_TEMPLATE.replace("__CONFIG__", config)
    bootstrap_sri = "sha256-" + base64.b64encode(
        hashlib.sha256(bootstrap.encode("utf-8")).digest()
    ).decode("ascii")
    script_hashes = [bootstrap_sri]
    bundle_element = ""
    if client_bundle_url is not None:
        bundle_url = _validate_bundle_url(client_bundle_url, grant.client_digest)
        bundle_sri = _digest_to_sri(grant.client_digest)
        script_hashes.append(bundle_sri)
        bundle_element = (
            f'<script src="{html.escape(bundle_url, quote=True)}" '
            f'integrity="{bundle_sri}" crossorigin="anonymous"></script>'
        )
    script_policy = " ".join(f"'{item}'" for item in script_hashes)
    directives = (
        "default-src 'none'",
        "base-uri 'none'",
        "connect-src 'none'",
        "font-src 'none'",
        "form-action 'none'",
        "frame-src 'none'",
        "frame-ancestors 'self'",
        "img-src data: blob:",
        "manifest-src 'none'",
        "media-src 'none'",
        "object-src 'none'",
        f"script-src {script_policy}",
        "script-src-attr 'none'",
        f"script-src-elem {script_policy}",
        "style-src 'unsafe-inline'",
        "worker-src 'none'",
        "sandbox allow-scripts",
    )
    content_security_policy = "; ".join(directives)
    escaped_title = html.escape(title[:128], quote=False)
    document = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="referrer" content="no-referrer">'
        '<meta http-equiv="Content-Security-Policy" content="'
        f'{html.escape(content_security_policy, quote=True)}">'
        f"<title>{escaped_title}</title></head><body>"
        f"<script>{bootstrap}</script>{bundle_element}</body></html>"
    )
    headers = MappingProxyType(
        {
            "Cache-Control": "no-store",
            "Content-Security-Policy": content_security_policy,
            "Content-Type": "text/html; charset=utf-8",
            "Permissions-Policy": (
                "accelerometer=(), autoplay=(), camera=(), clipboard-read=(), "
                "clipboard-write=(), geolocation=(), gyroscope=(), magnetometer=(), "
                "microphone=(), payment=(), serial=(), usb=()"
            ),
            "Referrer-Policy": DSH_UI_IFRAME_REFERRER_POLICY,
            "X-Content-Type-Options": "nosniff",
        }
    )
    iframe_attributes = MappingProxyType(
        {
            "sandbox": DSH_UI_IFRAME_SANDBOX,
            "referrerpolicy": DSH_UI_IFRAME_REFERRER_POLICY,
            "credentialless": "",
        }
    )
    return DshUiSandboxDocument(
        html=document,
        content_security_policy=content_security_policy,
        response_headers=headers,
        iframe_attributes=iframe_attributes,
    )


def dsh_ui_client_bundle_headers(client_digest: str) -> Mapping[str, str]:
    """Headers for a public, immutable, digest-fenced sandbox bundle response."""

    _digest_to_sri(client_digest)
    return MappingProxyType(
        {
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=31536000, immutable",
            "Content-Type": "text/javascript; charset=utf-8",
            "Cross-Origin-Resource-Policy": "cross-origin",
            "ETag": f'"{client_digest}"',
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        }
    )


class DshClientBundleExecution(str, Enum):
    SANDBOX = "sandbox"
    LEGACY_TOP_LEVEL = "legacy-top-level"
    DENY = "deny"


def legacy_top_level_client_execution_allowed(*, explicit_opt_in: bool = False) -> bool:
    """Return false unless a trusted caller explicitly enables the unsafe path."""

    return explicit_opt_in is True


def select_dsh_client_bundle_execution(
    *,
    sandbox_compatible: bool,
    legacy_compatible: bool | None = None,
    explicit_legacy_opt_in: bool = False,
) -> DshClientBundleExecution:
    """Prefer the sandbox and default-deny incompatible legacy bundles."""

    if legacy_compatible is None:
        legacy_compatible = sandbox_compatible
    if legacy_compatible and legacy_top_level_client_execution_allowed(
        explicit_opt_in=explicit_legacy_opt_in
    ):
        return DshClientBundleExecution.LEGACY_TOP_LEVEL
    if sandbox_compatible:
        return DshClientBundleExecution.SANDBOX
    return DshClientBundleExecution.DENY


def request_to_wire(request: DshUiRequest) -> dict[str, Any]:
    """Canonical camelCase projection useful to browser/contract fixtures."""

    return cast(dict[str, Any], request.model_dump(by_alias=True, mode="json"))


__all__ = [
    "DSH_UI_FRAME_ORIGIN",
    "DSH_UI_IFRAME_REFERRER_POLICY",
    "DSH_UI_IFRAME_SANDBOX",
    "DSH_UI_PROTOCOL_VERSION",
    "DshClientBundleExecution",
    "DshUiAuthorizedRequest",
    "DshUiCallToolPayload",
    "DshUiCallToolRequest",
    "DshUiCancelToolPayload",
    "DshUiCancelToolRequest",
    "DshUiErrorResponse",
    "DshUiListToolsPayload",
    "DshUiListToolsRequest",
    "DshUiReadyEnvelope",
    "DshUiRequest",
    "DshUiResponseError",
    "DshUiSandboxDocument",
    "DshUiSandboxError",
    "DshUiSandboxGrant",
    "DshUiSandboxLimits",
    "DshUiSandboxSessionStore",
    "DshUiSuccessResponse",
    "dsh_ui_client_bundle_headers",
    "legacy_top_level_client_execution_allowed",
    "parse_dsh_ui_request",
    "render_dsh_ui_sandbox_document",
    "request_to_wire",
    "select_dsh_client_bundle_execution",
]

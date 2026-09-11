"""Private, generation-scoped IPC for Cordis-owned Python companions.

Only a complete live Cordis graph and a healthy Core activate a companion.
Invocation principals are retained in Python behind opaque, expiring handles;
neither plugin tool arguments nor browser payloads can choose that principal.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import secrets
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ksadk.plugins.companion_artifacts import DshCompanionArtifact

Callback = Callable[[], Awaitable[None] | None]
BindArtifact = Callable[[DshCompanionArtifact], Awaitable[None] | None]
Invoke = Callable[[Any, str, dict[str, Any], str], Awaitable[dict[str, Any]] | dict[str, Any]]
_MAX_BYTES = 1024 * 1024


class CompanionError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DshCompanionDefinition:
    plugin_id: str
    components: Mapping[str, str]
    operations: frozenset[str]
    start: Callback
    revoke: Callback
    close: Callback
    invoke: Invoke
    profile: str = "web"
    bind_artifact: BindArtifact | None = None
    artifact_sources: tuple[Path, ...] = ()


@dataclass(frozen=True)
class _Grant:
    plugin_id: str
    principal: Any
    operations: frozenset[str]
    expires_at: float


@dataclass
class _Companion:
    definition: DshCompanionDefinition
    connections: dict[str, asyncio.StreamWriter] = field(default_factory=dict)
    running: bool = False


async def _call(callback: Callback) -> None:
    result = callback()
    if inspect.isawaitable(result):
        await result


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


class DshPluginCompanionManager:
    def __init__(
        self,
        *,
        profile: str,
        generation_id: str,
        definitions: tuple[DshCompanionDefinition, ...],
        artifacts: Mapping[str, DshCompanionArtifact] | None = None,
    ) -> None:
        if any(item.profile != profile for item in definitions):
            raise CompanionError("COMPANION_PROFILE_MISMATCH")
        self.profile = profile
        self.generation_id = generation_id
        self._companions = {item.plugin_id: _Companion(item) for item in definitions}
        self._artifacts = dict(artifacts or {})
        if any(
            item.bind_artifact and item.plugin_id not in self._artifacts for item in definitions
        ):
            raise CompanionError("COMPANION_ARTIFACT_REQUIRED")
        if len(self._companions) != len(definitions):
            raise CompanionError("COMPANION_DUPLICATE")
        self._secret = secrets.token_urlsafe(32)
        self._grants: dict[str, _Grant] = {}
        self._pending: dict[asyncio.Task[Any], str] = {}
        self._connections: set[asyncio.StreamWriter] = set()
        self._lock = asyncio.Lock()
        self._server: asyncio.Server | None = None
        self._root: Path | None = None
        self._healthy = False
        self._closed = False

    @property
    def active_plugins(self) -> frozenset[str]:
        return frozenset(key for key, value in self._companions.items() if value.running)

    @property
    def configuration(self) -> dict[str, Any]:
        if self._root is None:
            raise CompanionError("COMPANION_BROKER_NOT_STARTED")
        return {
            "socketPath": str(self._root / "host.sock"),
            "generationId": self.generation_id,
            "secret": self._secret,
            "plugins": {
                key: {"components": sorted(value.definition.components)}
                for key, value in self._companions.items()
            },
        }

    async def start_broker(self) -> Path:
        if self._closed:
            raise CompanionError("COMPANION_CLOSED")
        if self._root is None:
            self._root = Path(tempfile.mkdtemp(prefix="ksadk-companion-"))
            self._root.chmod(0o700)
            path = self._root / "host.sock"
            self._server = await asyncio.start_unix_server(self._connection, path=path)
            path.chmod(0o600)
        return self._root / "host.sock"

    async def confirm_core_ready(self) -> None:
        """Called by the trusted supervisor only after actual Core health passes."""
        async with self._lock:
            if self._closed:
                raise CompanionError("COMPANION_CLOSED")
            for value in self._companions.values():
                if set(value.connections) != set(value.definition.components):
                    raise CompanionError("COMPANION_GRAPH_NOT_READY")
            self._healthy = True
            try:
                for value in self._companions.values():
                    await self._start(value)
            except BaseException:
                self._healthy = False
                for value in self._companions.values():
                    await self._stop(value)
                raise

    def issue_invocation(
        self,
        plugin_id: str,
        principal: Any,
        *,
        operations: frozenset[str],
        ttl_seconds: float = 60,
    ) -> str:
        value = self._companions.get(plugin_id)
        if self._closed or not self._healthy or value is None or not value.running:
            raise CompanionError("COMPANION_UNAVAILABLE")
        if (
            not operations
            or not operations <= value.definition.operations
            or not 0 < ttl_seconds <= 120
        ):
            raise CompanionError("COMPANION_GRANT_INVALID")
        now = time.monotonic()
        self._grants = {key: grant for key, grant in self._grants.items() if grant.expires_at > now}
        if len(self._grants) >= 1024:
            raise CompanionError("COMPANION_BUSY")
        handle = secrets.token_urlsafe(32)
        self._grants[handle] = _Grant(plugin_id, principal, operations, now + ttl_seconds)
        return handle

    def revoke_invocation(self, handle: str) -> None:
        self._grants.pop(handle, None)

    async def _start(self, value: _Companion) -> None:
        if (
            not value.running
            and self._healthy
            and set(value.connections) == set(value.definition.components)
        ):
            try:
                if value.definition.bind_artifact is not None:
                    bound = value.definition.bind_artifact(
                        self._artifacts[value.definition.plugin_id]
                    )
                    if inspect.isawaitable(bound):
                        await bound
                await _call(value.definition.start)
            except BaseException:
                await _call(value.definition.revoke)
                await _call(value.definition.close)
                raise
            value.running = True

    async def _stop(self, value: _Companion) -> None:
        plugin_id = value.definition.plugin_id
        self._grants = {
            key: grant for key, grant in self._grants.items() if grant.plugin_id != plugin_id
        }
        if value.running:
            value.running = False
            # Revoke host dispatch and tool authority before closing the domain.
            try:
                await _call(value.definition.revoke)
            finally:
                for task, owner in tuple(self._pending.items()):
                    if owner == plugin_id and task is not asyncio.current_task():
                        task.cancel()
                await _call(value.definition.close)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            self._healthy = False
            try:
                for value in self._companions.values():
                    await self._stop(value)
            finally:
                if self._server:
                    self._server.close()
                for writer in tuple(self._connections):
                    writer.close()
                if self._server:
                    await self._server.wait_closed()
                self._grants.clear()
                if self._root:
                    shutil.rmtree(self._root, ignore_errors=True)

    async def _read(self, reader: asyncio.StreamReader) -> dict[str, Any]:
        size = int.from_bytes(await reader.readexactly(4), "big")
        if not 0 < size <= _MAX_BYTES:
            raise CompanionError("COMPANION_REQUEST_INVALID")
        raw = await reader.readexactly(size)
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        if not isinstance(value, dict) or value.get("v") != 1:
            raise CompanionError("COMPANION_REQUEST_INVALID")
        return value

    async def _write(self, writer: asyncio.StreamWriter, value: dict[str, Any]) -> None:
        body = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
        if len(body) > _MAX_BYTES:
            raise CompanionError("COMPANION_RESPONSE_INVALID")
        writer.write(len(body).to_bytes(4, "big") + body)
        await writer.drain()

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        component: str | None = None
        companion: _Companion | None = None
        request_id = ""
        if len(self._connections) >= 128 or self._closed:
            writer.close()
            return
        self._connections.add(writer)
        try:
            request = await asyncio.wait_for(self._read(reader), timeout=5)
            request_id = request.get("requestId")
            if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
                raise CompanionError("COMPANION_REQUEST_INVALID")
            companion = self._companions.get(request.get("pluginId"))
            if companion is None:
                raise CompanionError("COMPANION_UNAVAILABLE")
            if request.get("type") == "register":
                component = request.get("component")
                if (
                    request.get("generationId") != self.generation_id
                    or not isinstance(request.get("secret"), str)
                    or not secrets.compare_digest(request["secret"], self._secret)
                    or component not in companion.definition.components
                ):
                    component = None
                    raise CompanionError("COMPANION_REGISTRATION_INVALID")
                async with self._lock:
                    if component in companion.connections:
                        component = None
                        raise CompanionError("COMPANION_COMPONENT_DUPLICATE")
                    companion.connections[component] = writer
                    await self._start(companion)
                await self._write(
                    writer, {"v": 1, "requestId": request_id, "result": {"registered": True}}
                )
                # A live connection is the component's lifecycle lease. Any
                # data or EOF retires it; no caller-controlled heartbeat state.
                await reader.read(1)
            elif request.get("type") == "invoke":
                operation = request.get("operation")
                grant = self._grants.get(request.get("handle"))
                arguments = request.get("arguments")
                deadline = request.get("deadline")
                call_id = request.get("callId")
                if (
                    not self._healthy
                    or not companion.running
                    or grant is None
                    or grant.plugin_id != companion.definition.plugin_id
                    or grant.expires_at <= time.monotonic()
                    or operation not in grant.operations
                ):
                    raise CompanionError("COMPANION_INVOCATION_DENIED")
                if (
                    not isinstance(arguments, dict)
                    or not isinstance(call_id, str)
                    or not 1 <= len(call_id) <= 128
                    or isinstance(deadline, bool)
                    or not isinstance(deadline, int)
                    or deadline <= time.time() * 1000
                    or deadline > time.time() * 1000 + 120_000
                ):
                    raise CompanionError("COMPANION_REQUEST_INVALID")
                task = asyncio.current_task()
                assert task is not None
                self._pending[task] = companion.definition.plugin_id
                result = companion.definition.invoke(
                    grant.principal, operation, arguments, call_id
                )
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(
                        result,
                        timeout=min(
                            grant.expires_at - time.monotonic(), deadline / 1000 - time.time()
                        ),
                    )
                if not isinstance(result, dict):
                    raise CompanionError("COMPANION_RESPONSE_INVALID")
                await self._write(writer, {"v": 1, "requestId": request_id, "result": result})
            else:
                raise CompanionError("COMPANION_REQUEST_INVALID")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code = error.code if isinstance(error, CompanionError) else "COMPANION_REQUEST_FAILED"
            try:
                await self._write(
                    writer, {"v": 1, "requestId": request_id, "error": {"code": code}}
                )
            except Exception:
                pass
        finally:
            self._pending.pop(asyncio.current_task(), None)
            if companion is not None and component is not None:
                async with self._lock:
                    if companion.connections.get(component) is writer:
                        companion.connections.pop(component)
                        await self._stop(companion)
            self._connections.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

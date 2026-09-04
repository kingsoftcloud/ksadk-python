"""Studio-owned lifecycle for one profile-level DSH MCP capability host.

The service is intentionally ephemeral.  It caches one host for the current
immutable Profile projection, keeps its loopback endpoint and bearer lease in
memory only, and exposes only descriptor/tool/result projections to routes.
Profile refresh and shutdown cancel all tracked calls before disposing the
sidecar.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any, Literal, cast

import httpx
from pydantic import ValidationError

from ksadk.plugins.bridges.dsh import DshProfilePluginBridge, DshProfileProjection
from ksadk.plugins.dsh_toolchain import DshToolchainManager
from ksadk.plugins.host import PluginHostError
from ksadk.plugins.providers.dsh_capabilities import (
    DSH_CAPABILITY_BUNDLE_PACKAGE,
    DSH_CAPABILITY_HOST_VERSION,
    DSH_CAPABILITY_MCP_PROTOCOL,
    DshCapabilityTool,
    DshMcpConnectorLease,
    DshProfileCapabilityDescriptor,
    DshProfileCapabilityHost,
    DshProfileCapabilityInventory,
)
from ksadk.studio.errors import StudioError

_CALL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_GENERATION_ID = re.compile(r"^dshgen_[A-Za-z0-9_-]{24,96}$")
_MAX_ARGUMENT_BYTES = 256 * 1024
_MAX_RESPONSE_BYTES = 1024 * 1024
_JSON_RPC_ENVELOPE_BYTES = 16 * 1024

BridgeFactory = Callable[..., DshProfilePluginBridge]
HostFactory = Callable[..., DshProfileCapabilityHost]


def dsh_ui_mcp_call_id(session_id: str, call_id: str) -> str:
    """Derive a bounded sidecar call id without exposing either UI identifier."""

    material = f"{session_id}\0{call_id}".encode("utf-8")
    return f"ui-{hashlib.sha256(material).hexdigest()}"


@dataclass(frozen=True)
class _ActiveCall:
    lease: DshMcpConnectorLease
    tool_name: str


@dataclass(frozen=True)
class DshCapabilityRuntimeSnapshot:
    """One atomically fenced descriptor/lease pair for PluginHost."""

    descriptor: DshProfileCapabilityDescriptor
    lease: DshMcpConnectorLease = dataclass_field(repr=False)


@dataclass(frozen=True)
class DshCapabilitySnapshot:
    """One generation-consistent public capability inventory."""

    descriptor: DshProfileCapabilityDescriptor
    tools: tuple[DshCapabilityTool, ...]
    inventory: DshProfileCapabilityInventory
    generation_id: str


def _strict_json_size(value: Any) -> int:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise StudioError(
            "DSH_CAPABILITY_ARGUMENTS_INVALID",
            "DSH 工具参数必须是严格 JSON 对象",
            status_code=422,
        ) from error
    return len(encoded)


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON member")
        value[key] = item
    return value


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


class StudioDshCapabilityService:
    """Lazy, generation-fenced access to a DSH Profile's real MCP lease."""

    def __init__(
        self,
        workspace: Path,
        *,
        dsh_home: Path,
        profile: str = "studio",
        dsh_command: Sequence[str] | None = None,
        bridge_factory: BridgeFactory = DshProfilePluginBridge,
        host_factory: HostFactory = DshProfileCapabilityHost,
        max_argument_bytes: int = _MAX_ARGUMENT_BYTES,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
    ) -> None:
        self._workspace = workspace.resolve()
        self._dsh_home = dsh_home.expanduser().resolve()
        self._profile = profile
        if isinstance(dsh_command, (str, bytes)):
            raise ValueError("dsh_command must be a one-item argv sequence")
        normalized_command = tuple(dsh_command) if dsh_command is not None else ()
        if len(normalized_command) > 1:
            raise ValueError("dsh_command must contain only the DSH executable")
        self._explicit_dsh_executable = normalized_command[0] if normalized_command else None
        self._bridge_factory = bridge_factory
        self._host_factory = host_factory
        if (
            isinstance(max_argument_bytes, bool)
            or max_argument_bytes < 1
            or max_argument_bytes > 8 * 1024 * 1024
            or isinstance(max_response_bytes, bool)
            or max_response_bytes < 1
            or max_response_bytes > 8 * 1024 * 1024
        ):
            raise ValueError("DSH capability size limits are invalid")
        self._max_argument_bytes = max_argument_bytes
        self._max_result_bytes = max_response_bytes
        self._max_wire_response_bytes = max_response_bytes + _JSON_RPC_ENVELOPE_BYTES
        self._lock = asyncio.Lock()
        self._host: DshProfileCapabilityHost | None = None
        self._lease: DshMcpConnectorLease | None = None
        self._projection: DshProfileProjection | None = None
        self._generation_id: str | None = None
        self._active_calls: dict[str, _ActiveCall] = {}
        self._closed = False

    @classmethod
    def discover_or_create_workspace_default(cls, workspace: Path) -> "StudioDshCapabilityService":
        root = workspace.resolve()
        configured_home = os.environ.get("KSADK_DSH_HOME", "").strip()
        dsh_home = (
            Path(configured_home).expanduser()
            if configured_home
            else root / ".agentkit" / "dsh-home"
        )
        profile = os.environ.get("KSADK_DSH_PROFILE", "").strip() or "studio"
        configured_bin = os.environ.get("KSADK_DSH_BIN", "").strip()
        command = (str(Path(configured_bin).expanduser()),) if configured_bin else None
        return cls(
            root,
            dsh_home=dsh_home,
            profile=profile,
            dsh_command=command,
        )

    @property
    def profile(self) -> str:
        return self._profile

    async def has_enabled_profile_plugins(self) -> bool:
        """Inspect Profile metadata without starting the capability sidecar."""

        async with self._lock:
            if self._closed:
                raise StudioError(
                    "DSH_CAPABILITY_SERVICE_CLOSED",
                    "DSH capability service 已关闭",
                    status_code=503,
                )
            command = await asyncio.to_thread(self._resolve_command)
            return await asyncio.to_thread(self._profile_has_enabled_plugins, command)

    async def describe(self) -> DshProfileCapabilityDescriptor:
        host, _lease = await self._ready_generation()
        return host.descriptor

    async def runtime_snapshot(self) -> DshCapabilityRuntimeSnapshot:
        """Fence descriptor and credential to the same supervised generation."""

        async with self._lock:
            host, lease = await self._ensure_ready_locked()
            descriptor = host.descriptor
            self._validate_lease(descriptor, lease)
            return DshCapabilityRuntimeSnapshot(descriptor=descriptor, lease=lease)

    async def capability_snapshot(self) -> DshCapabilitySnapshot:
        """Read descriptor, tools and health without crossing a refresh."""

        async with self._lock:
            host, lease = await self._ensure_ready_locked()
            descriptor = host.descriptor
            self._validate_lease(descriptor, lease)
            tools = await self._list_tools_for_generation(host, lease)
            inventory = await host.inventory()
            if (
                inventory.state != "ready"
                or inventory.profile != descriptor.profile
                or inventory.profile_digest != descriptor.profile_digest
                or inventory.descriptor_digest != descriptor.descriptor_digest
                or inventory.inventory_digest != descriptor.inventory_digest
                or inventory.tool_count != len(descriptor.tools)
            ):
                raise self._protocol_error()
            return DshCapabilitySnapshot(
                descriptor=descriptor,
                tools=tools,
                inventory=inventory,
                generation_id=self._require_generation_id(),
            )

    async def descriptor_generation(self) -> tuple[DshProfileCapabilityDescriptor, str]:
        """Return a descriptor and non-secret lease epoch under one lock."""

        async with self._lock:
            host, _lease = await self._ensure_ready_locked()
            return host.descriptor, self._require_generation_id()

    async def connector_lease(self) -> DshMcpConnectorLease:
        """Return the current in-memory lease to a trusted PluginHost adapter."""

        host, lease = await self._ready_generation()
        descriptor = host.descriptor
        self._validate_lease(descriptor, lease)
        return lease

    async def inventory(self) -> DshProfileCapabilityInventory:
        host, _lease = await self._ready_generation()
        return await host.inventory()

    async def list_tools(
        self,
        *,
        expected_descriptor_digest: str | None = None,
        expected_generation_id: str | None = None,
    ) -> tuple[DshCapabilityTool, ...]:
        self._validate_expected_generation(
            expected_descriptor_digest, expected_generation_id
        )
        async with self._lock:
            host, lease = await self._ensure_ready_locked()
            self._require_expected_generation(
                host.descriptor,
                expected_descriptor_digest,
                expected_generation_id,
            )
            return await self._list_tools_for_generation(host, lease)

    async def _list_tools_for_generation(
        self,
        host: DshProfileCapabilityHost,
        lease: DshMcpConnectorLease,
    ) -> tuple[DshCapabilityTool, ...]:
        request_id = f"list-{os.urandom(12).hex()}"
        result = await self._mcp_request(
            lease,
            request_id=request_id,
            method="tools/list",
            params={},
            timeout_seconds=5.0,
        )
        raw_tools = result.get("tools")
        if not isinstance(raw_tools, list):
            raise self._protocol_error()
        try:
            tools = tuple(DshCapabilityTool.model_validate(item) for item in raw_tools)
        except (TypeError, ValidationError, ValueError) as error:
            raise self._protocol_error() from error
        if tools != host.descriptor.tools:
            raise StudioError(
                "DSH_CAPABILITY_INVENTORY_CHANGED",
                "DSH 工具清单已变化，请刷新插件 Profile 后重试",
                status_code=409,
            )
        return tools

    @staticmethod
    def _validate_lease(
        descriptor: DshProfileCapabilityDescriptor,
        lease: DshMcpConnectorLease,
    ) -> None:
        if (
            lease.profile != descriptor.profile
            or lease.profile_digest != descriptor.profile_digest
            or lease.descriptor_digest != descriptor.descriptor_digest
        ):
            raise StudioDshCapabilityService._protocol_error()

    async def call_tool(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        deadline_ms: int,
        expected_descriptor_digest: str | None = None,
        expected_generation_id: str | None = None,
    ) -> dict[str, Any]:
        self._validate_expected_generation(
            expected_descriptor_digest, expected_generation_id
        )
        if not _CALL_ID.fullmatch(call_id):
            raise StudioError(
                "DSH_CAPABILITY_CALL_ID_INVALID",
                "DSH 工具调用标识无效",
                status_code=422,
            )
        if not isinstance(tool_name, str) or not _TOOL_NAME.fullmatch(tool_name):
            raise StudioError(
                "DSH_CAPABILITY_TOOL_INVALID",
                "DSH 工具名称无效",
                status_code=422,
            )
        if not isinstance(arguments, Mapping):
            raise StudioError(
                "DSH_CAPABILITY_ARGUMENTS_INVALID",
                "DSH 工具参数必须是 JSON 对象",
                status_code=422,
            )
        detached_arguments = dict(arguments)
        if _strict_json_size(detached_arguments) > self._max_argument_bytes:
            raise StudioError(
                "DSH_CAPABILITY_ARGUMENTS_TOO_LARGE",
                "DSH 工具参数超过大小限制",
                status_code=413,
            )
        if (
            isinstance(deadline_ms, bool)
            or not isinstance(deadline_ms, int)
            or deadline_ms < 1
            or deadline_ms > 120_000
        ):
            raise StudioError(
                "DSH_CAPABILITY_DEADLINE_INVALID",
                "DSH 工具调用超时值无效",
                status_code=422,
            )

        async with self._lock:
            host, lease = await self._ensure_ready_locked()
            self._require_expected_generation(
                host.descriptor,
                expected_descriptor_digest,
                expected_generation_id,
            )
            if tool_name not in {tool.name for tool in host.descriptor.tools}:
                raise StudioError(
                    "DSH_CAPABILITY_TOOL_FORBIDDEN",
                    "请求的 DSH 工具不在当前能力清单中",
                    status_code=403,
                )
            if call_id in self._active_calls:
                raise StudioError(
                    "DSH_CAPABILITY_CALL_DUPLICATE",
                    "DSH 工具调用标识已在使用",
                    status_code=409,
                )
            self._active_calls[call_id] = _ActiveCall(lease=lease, tool_name=tool_name)

        try:
            try:
                return await asyncio.wait_for(
                    self._mcp_request(
                        lease,
                        request_id=call_id,
                        method="tools/call",
                        params={"name": tool_name, "arguments": detached_arguments},
                        timeout_seconds=(deadline_ms / 1000.0) + 2.0,
                    ),
                    timeout=(deadline_ms / 1000.0) + 2.0,
                )
            except asyncio.TimeoutError as error:
                await self._send_cancel(lease, call_id)
                raise StudioError(
                    "DSH_CAPABILITY_CALL_TIMEOUT",
                    "DSH 工具调用超时",
                    status_code=504,
                ) from error
            except asyncio.CancelledError:
                await self._send_cancel(lease, call_id)
                raise
        finally:
            async with self._lock:
                current = self._active_calls.get(call_id)
                if current is not None and current.lease is lease:
                    self._active_calls.pop(call_id, None)

    async def cancel(self, call_id: str) -> bool:
        async with self._lock:
            active = self._active_calls.get(call_id)
        if active is None:
            return False
        await self._send_cancel(active.lease, call_id)
        return True

    async def revoke_runtime_token(
        self,
        lease: DshMcpConnectorLease,
        token: str,
    ) -> None:
        """Revoke one activation-scoped token or kill its generation on failure."""

        if not isinstance(token, str) or not token.startswith("ks1.") or len(token) > 12 * 1024:
            raise StudioError(
                "DSH_CAPABILITY_SCOPE_INVALID",
                "DSH runtime credential 无效",
                status_code=422,
            )
        async with self._lock:
            if self._closed or self._lease is not lease:
                # A replaced/disposed process has already invalidated its HMAC
                # root, so no live credential remains to revoke.
                return
            try:
                result = await self._mcp_request(
                    lease,
                    request_id=f"revoke-{os.urandom(12).hex()}",
                    method="io.ksadk/scopes/revoke",
                    params={"token": token},
                    timeout_seconds=2.0,
                )
                if result.get("revoked") is not True:
                    raise self._protocol_error()
            except BaseException:
                # Revocation is a security boundary. If it cannot be proven,
                # terminating the generation invalidates every derived token.
                await self._dispose_generation_locked()
                raise

    async def refresh(self) -> None:
        """Cancel the cached generation and allow the next call to start afresh."""

        async with self._lock:
            if self._closed:
                return
            await self._dispose_generation_locked()

    async def aclose(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            await self._dispose_generation_locked()

    async def _ready_generation(
        self,
    ) -> tuple[DshProfileCapabilityHost, DshMcpConnectorLease]:
        async with self._lock:
            return await self._ensure_ready_locked()

    async def _ensure_ready_locked(
        self,
    ) -> tuple[DshProfileCapabilityHost, DshMcpConnectorLease]:
        if self._closed:
            raise StudioError(
                "DSH_CAPABILITY_SERVICE_CLOSED",
                "DSH capability service 已关闭",
                status_code=503,
            )
        host = self._host
        projection = self._projection
        if host is not None:
            if projection is None:  # pragma: no cover - internal invariant guard
                await self._dispose_generation_locked()
                raise self._protocol_error()
            if self._lease is not None:
                try:
                    if await host.health():
                        return host, self._lease
                except (PluginHostError, OSError):
                    pass
                self._lease = None
            return await self._start_host_locked(host, projection)

        try:
            command = await asyncio.to_thread(self._resolve_command)
            projection = await asyncio.to_thread(self._project_profile, command)
            host = self._host_factory(
                command,
                projection=projection,
                dsh_home=self._dsh_home,
                cwd=self._workspace,
                max_argument_bytes=self._max_argument_bytes,
                max_result_bytes=self._max_result_bytes,
                max_request_bytes=self._max_argument_bytes + 16 * 1024,
            )
        except StudioError:
            raise
        except (PluginHostError, OSError, ValueError) as error:
            raise self._unavailable(error) from error

        # Retain one Host across transient start failures.  Its circuit breaker
        # is generation state; replacing it here would make repeated crashes
        # look like unrelated first failures and prevent the breaker opening.
        self._projection = projection
        self._host = host
        return await self._start_host_locked(host, projection)

    async def _start_host_locked(
        self,
        host: DshProfileCapabilityHost,
        projection: DshProfileProjection,
    ) -> tuple[DshProfileCapabilityHost, DshMcpConnectorLease]:
        try:
            lease = await host.lease()
            descriptor = host.descriptor
            if (
                lease.profile != projection.profile
                or lease.profile_digest != projection.config_digest
                or lease.descriptor_digest != descriptor.descriptor_digest
            ):
                raise self._protocol_error()
            await self._initialize_lease(lease)
        except StudioError as error:
            self._lease = None
            if error.code == "DSH_CAPABILITY_PROTOCOL_INVALID":
                await self._dispose_generation_locked()
            raise
        except (PluginHostError, OSError, ValueError) as error:
            self._lease = None
            raise self._unavailable(error) from error
        self._lease = lease
        self._generation_id = f"dshgen_{secrets.token_urlsafe(24)}"
        return host, lease

    def _resolve_command(self) -> tuple[str, ...]:
        try:
            return tuple(DshToolchainManager().require_command(self._explicit_dsh_executable))
        except Exception as error:
            raise StudioError(
                "DSH_CAPABILITY_HOST_UNAVAILABLE",
                "DSH capability host 未安装、版本不匹配或不可用",
                status_code=503,
            ) from error

    def _project_profile(self, command: Sequence[str]) -> DshProfileProjection:
        try:
            with self._bridge_factory(
                dsh_home=self._dsh_home,
                profile=self._profile,
                dsh_command=command,
                cwd=self._workspace,
            ) as bridge:
                return bridge.project_profile()
        except Exception as error:
            raise self._unavailable(error) from error

    def _profile_has_enabled_plugins(self, command: Sequence[str]) -> bool:
        try:
            with self._bridge_factory(
                dsh_home=self._dsh_home,
                profile=self._profile,
                dsh_command=command,
                cwd=self._workspace,
            ) as bridge:
                return any(item.enabled for item in bridge.list_plugins())
        except Exception as error:
            raise self._unavailable(error) from error

    @staticmethod
    def _validate_expected_generation(
        descriptor_digest: str | None,
        generation_id: str | None,
    ) -> None:
        if descriptor_digest is not None and (
            not isinstance(descriptor_digest, str)
            or not _DIGEST.fullmatch(descriptor_digest)
        ):
            raise StudioError(
                "DSH_CAPABILITY_DESCRIPTOR_INVALID",
                "DSH capability descriptor 摘要无效",
                status_code=422,
            )
        if generation_id is not None and (
            not isinstance(generation_id, str)
            or not _GENERATION_ID.fullmatch(generation_id)
        ):
            raise StudioError(
                "DSH_CAPABILITY_GENERATION_INVALID",
                "DSH capability generation 标识无效",
                status_code=422,
            )

    def _require_expected_generation(
        self,
        descriptor: DshProfileCapabilityDescriptor,
        expected_descriptor_digest: str | None,
        expected_generation_id: str | None,
    ) -> None:
        if (
            (
                expected_descriptor_digest is not None
                and descriptor.descriptor_digest != expected_descriptor_digest
            )
            or (
                expected_generation_id is not None
                and expected_generation_id != self._generation_id
            )
        ):
            raise StudioError(
                "DSH_CAPABILITY_GENERATION_CHANGED",
                "DSH capability generation 已变化，请刷新后重试",
                status_code=409,
            )

    def _require_generation_id(self) -> str:
        generation_id = self._generation_id
        if generation_id is None:  # pragma: no cover - internal invariant guard
            raise self._protocol_error()
        return generation_id

    async def _dispose_generation_locked(self) -> None:
        await self._finish_cleanup(self._dispose_generation_owned_locked())

    async def _dispose_generation_owned_locked(self) -> None:
        """Finish generation teardown before propagating caller cancellation."""

        host = self._host
        active = tuple(self._active_calls.items())
        self._host = None
        self._lease = None
        self._projection = None
        self._generation_id = None
        self._active_calls.clear()
        if active:
            await asyncio.gather(
                *(self._send_cancel(call.lease, call_id) for call_id, call in active),
                return_exceptions=True,
            )
        if host is not None:
            await host.dispose()

    @staticmethod
    async def _finish_cleanup(cleanup: Coroutine[Any, Any, None]) -> None:
        """Defer caller cancellation until an owned-effect cleanup finishes."""

        cleanup_task = asyncio.create_task(cleanup)
        interrupted = False
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                interrupted = True
        cleanup_task.result()
        if interrupted:
            raise asyncio.CancelledError

    async def _mcp_request(
        self,
        lease: DshMcpConnectorLease,
        *,
        request_id: str,
        method: Literal[
            "initialize",
            "io.ksadk/scopes/revoke",
            "tools/list",
            "tools/call",
        ],
        params: Mapping[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": dict(params),
        }
        headers = {**lease.headers(), "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(
                headers=headers,
                timeout=timeout_seconds,
                trust_env=False,
            ) as client:
                response = await client.post(lease.endpoint, json=payload)
        except httpx.TimeoutException as error:
            raise asyncio.TimeoutError from error
        except httpx.HTTPError as error:
            raise StudioError(
                "DSH_CAPABILITY_HOST_UNAVAILABLE",
                "DSH capability MCP 连接失败",
                status_code=503,
                details={"errorType": type(error).__name__},
            ) from error
        if response.status_code != 200 or len(response.content) > self._max_wire_response_bytes:
            raise self._protocol_error()
        try:
            envelope = json.loads(
                response.content,
                object_pairs_hook=_duplicate_rejecting_object,
                parse_constant=_reject_non_finite,
            )
        except (UnicodeError, ValueError, TypeError, RecursionError) as error:
            raise self._protocol_error() from error
        if (
            not isinstance(envelope, dict)
            or envelope.get("jsonrpc") != "2.0"
            or envelope.get("id") != request_id
        ):
            raise self._protocol_error()
        if "error" in envelope:
            rpc_error = envelope.get("error")
            code = rpc_error.get("code") if isinstance(rpc_error, dict) else None
            raise StudioError(
                "DSH_CAPABILITY_REQUEST_FAILED",
                "DSH capability MCP 拒绝了请求",
                status_code=502,
                details={"rpcCode": code if isinstance(code, int) else None},
            )
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise self._protocol_error()
        try:
            result_bytes = len(
                json.dumps(
                    result,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        except (TypeError, ValueError, RecursionError) as error:
            raise self._protocol_error() from error
        if result_bytes > self._max_result_bytes:
            raise self._protocol_error()
        return cast(dict[str, Any], result)

    async def _initialize_lease(self, lease: DshMcpConnectorLease) -> None:
        request_id = f"initialize-{os.urandom(12).hex()}"
        result = await self._mcp_request(
            lease,
            request_id=request_id,
            method="initialize",
            params={
                "protocolVersion": DSH_CAPABILITY_MCP_PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "ksadk-studio", "version": "1.0.0"},
            },
            timeout_seconds=5.0,
        )
        server_info = result.get("serverInfo")
        capabilities = result.get("capabilities")
        if (
            result.get("protocolVersion") != DSH_CAPABILITY_MCP_PROTOCOL
            or not isinstance(server_info, dict)
            or server_info.get("name") != DSH_CAPABILITY_BUNDLE_PACKAGE
            or server_info.get("version") != DSH_CAPABILITY_HOST_VERSION
            or not isinstance(capabilities, dict)
            or not isinstance(capabilities.get("tools"), dict)
        ):
            raise self._protocol_error()
        await self._send_notification(
            lease,
            method="notifications/initialized",
            params={},
            tolerate_failure=False,
        )

    async def _send_cancel(self, lease: DshMcpConnectorLease, call_id: str) -> None:
        await self._send_notification(
            lease,
            method="notifications/cancelled",
            params={"requestId": call_id, "reason": "Studio UI cancelled the call"},
            tolerate_failure=True,
        )

    async def _send_notification(
        self,
        lease: DshMcpConnectorLease,
        *,
        method: Literal["notifications/initialized", "notifications/cancelled"],
        params: Mapping[str, Any],
        tolerate_failure: bool,
    ) -> None:
        headers = {**lease.headers(), "Content-Type": "application/json"}
        payload = {"jsonrpc": "2.0", "method": method, "params": dict(params)}
        try:
            async with httpx.AsyncClient(headers=headers, timeout=2.0, trust_env=False) as client:
                response = await client.post(lease.endpoint, json=payload)
            if response.status_code not in {200, 202, 204}:
                raise self._protocol_error()
        except (httpx.HTTPError, StudioError):
            # Disposal still kills the process and aborts all work.  Cancellation
            # is best effort here so refresh/close cannot be held hostage by a
            # broken loopback listener.
            if tolerate_failure:
                return
            raise self._protocol_error() from None

    @staticmethod
    def _protocol_error() -> StudioError:
        return StudioError(
            "DSH_CAPABILITY_PROTOCOL_INVALID",
            "DSH capability MCP 返回了无效响应",
            status_code=502,
        )

    @staticmethod
    def _unavailable(error: BaseException) -> StudioError:
        reason = getattr(error, "code", None)
        return StudioError(
            "DSH_CAPABILITY_HOST_UNAVAILABLE",
            "DSH capability host 当前不可用",
            status_code=503,
            details={"reason": reason} if isinstance(reason, str) else {},
        )


__all__ = [
    "DshCapabilityRuntimeSnapshot",
    "DshCapabilitySnapshot",
    "StudioDshCapabilityService",
    "dsh_ui_mcp_call_id",
]

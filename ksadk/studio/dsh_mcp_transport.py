"""HTTP/JSON-RPC mechanics for Studio's generation-bound DSH MCP lease.

The service retains lifecycle, cancellation ownership, limits and generation
locks. These overridable methods only perform requests on its existing lease.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from typing import Any, Literal, cast

import httpx

from ksadk.plugins.providers.dsh_capabilities import (
    DSH_CAPABILITY_BUNDLE_PACKAGE,
    DSH_CAPABILITY_HOST_VERSION,
    DSH_CAPABILITY_MCP_PROTOCOL,
    DshMcpConnectorLease,
)
from ksadk.studio.errors import StudioError


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON member")
        value[key] = item
    return value


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


class DshMcpTransport:
    _max_wire_response_bytes: int
    _max_result_bytes: int

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

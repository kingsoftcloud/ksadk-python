from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Callable

import httpx


class BrokerError(RuntimeError):
    transient_codes = frozenset(
        {
            "BROKER_UNAVAILABLE",
            "MCP_CONTROL_UNAVAILABLE",
            "MCP_CONTROL_TIMEOUT",
            "MCP_CONTROL_RATE_LIMITED",
            "MCP_CONTROL_LIMITER_UNAVAILABLE",
        }
    )

    def __init__(self, code, *, retry_after=None, rejected_grant_version=None):
        self.code = code
        self.retry_after = retry_after
        self.rejected_grant_version = rejected_grant_version
        super().__init__(code)


class MCPToolOutcomeUnknown(RuntimeError):
    """Dispatch started, but its result was not durably confirmed."""


class BrokerClient:
    """Workload tokens are reread on every call and never persisted in a graph."""

    def __init__(self, url: str, token_source: Callable[[], str], *, http=None, project_dir: Path | None = None):
        self.url, self.token_source = url.rstrip("/"), token_source
        self.http = http or httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False)
        self.project_dir = project_dir
        self._runtime_executor = None
        self._provider_io = None

    @classmethod
    def from_token_file(cls, url, path, **kwargs):
        return cls(url, lambda: Path(path).read_text().strip(), **kwargs)

    async def call(self, operation, **body):
        for attempt in range(31):
            try:
                response = await self.http.post(
                    self.url + "/agentengine/internal/mcp-oauth/" + operation,
                    json=body,
                    headers={"Authorization": "Bearer " + self.token_source()},
                )
            except (httpx.TransportError, OSError):
                raise BrokerError("BROKER_UNAVAILABLE") from None
            try:
                data = response.json()
            except ValueError:
                data = None
            if response.status_code == 200:
                if not isinstance(data, dict):
                    raise BrokerError("BROKER_UNAVAILABLE")
                if operation == "GetMcpAuthorizationCapabilities":
                    self._provider_io = response.headers.get("X-MCP-Provider-IO") == "runtime/v1"
                return data
            code = data.get("ReasonCode") if isinstance(data, dict) else None
            if not code:
                code = {
                    429: "MCP_CONTROL_RATE_LIMITED",
                    502: "MCP_CONTROL_UNAVAILABLE",
                    503: "MCP_CONTROL_UNAVAILABLE",
                    504: "MCP_CONTROL_TIMEOUT",
                }.get(response.status_code, "BROKER_REJECTED")
            # Only a missing name on a binding request can use the uploaded project's
            # declaration. Existing published policies and explicit bindings win.
            if (
                code == "MCP_CONFIGURATION_NOT_FOUND"
                and self.project_dir is not None
                and body.get("config_id")
                and "declaration" not in body
                and (operation in {"LookupMcpCredential", "EnsureMcpAuthorization"}
                     or (operation == "RuntimeMcpOAuthIO" and body.get("io_action") == "claim"))
            ):
                from .code_config import project_declaration

                declaration = await asyncio.to_thread(project_declaration, self.project_dir, body["config_id"])
                if declaration is not None:
                    return await self.call(operation, **body, declaration=declaration)
            # Another runtime owns a durable refresh claim. Wait for it rather
            # than rotating the same refresh token or replaying a business run.
            if code not in {"CREDENTIAL_REFRESHING", "TASK_ADMISSION_NOT_VISIBLE"} or attempt == 30:
                # The workload Gateway supplies Retry-After as delta seconds.
                retry_after = response.headers.get("Retry-After", "").strip()
                delay = None
                if retry_after.isascii() and retry_after.isdecimal():
                    delay = int((retry_after.lstrip("0") or "0")[:9])
                raise BrokerError(code, retry_after=delay)
            await asyncio.sleep(0.5)

    async def capabilities(self):
        from .contract import CAPABILITY

        result = await self.call("GetMcpAuthorizationCapabilities")
        if result != CAPABILITY:
            raise BrokerError("MCP_OAUTH_CONTRACT_MISMATCH")
        return result

    async def prepare(self, task_id, config_id, **binding):
        await self.drive_runtime_io(task_id=task_id, config_id=config_id, **binding)

    async def drive_runtime_io(self, **binding):
        from .local.protocol import OAuthError
        from .runtime_io import RuntimeOAuthExecutor

        if self._provider_io is None:
            await self.capabilities()
        if not self._provider_io:
            return
        if self._runtime_executor is None:
            self._runtime_executor = RuntimeOAuthExecutor()
        for _ in range(240):
            # The Broker grants at most 120 seconds and rejects expired claims itself.
            delivery_deadline = time.monotonic() + 120
            operation = await self.call("RuntimeMcpOAuthIO", io_action="claim", **binding)
            if operation.get("schema") != "mcp.runtime-io/v1":
                raise BrokerError("MCP_RUNTIME_IO_CONTRACT_MISMATCH")
            if operation["status"] == "ready":
                return
            if operation["status"] == "pending":
                await asyncio.sleep(0.5)
                continue
            result, error = None, None
            try:
                result = await self._runtime_executor.execute(operation)
            except OAuthError as exc:
                error = exc.code
            # Retry delivery of the same result, never the external operation.
            backoff = 0.5
            while True:
                try:
                    await self.call(
                        "RuntimeMcpOAuthIO",
                        io_action="complete",
                        task_id=operation["task_id"],
                        operation_id=operation["operation_id"],
                        claim=operation["claim"],
                        result=result,
                        error=error,
                    )
                    break
                except BrokerError as exc:
                    delay = max(backoff, exc.retry_after or 0)
                    if (
                        exc.code not in BrokerError.transient_codes
                        or time.monotonic() + delay >= delivery_deadline
                    ):
                        raise
                    await asyncio.sleep(delay)
                    backoff = min(5, 2 * backoff)
            if error:
                if operation["kind"] == "refresh":
                    # The Broker acknowledged the failed refresh and updated
                    # the grant. The caller's lookup can now request consent.
                    return
                raise BrokerError(error)
        raise BrokerError("MCP_RUNTIME_IO_BUSY")

    async def ensure(self, task_id, config_id, request_key, *, subject_digest, **binding):
        return await self.call(
            "EnsureMcpAuthorization",
            task_id=task_id,
            config_id=config_id,
            request_key=request_key,
            subject_digest=subject_digest,
            **binding,
        )

    async def get(self, wait_id):
        await self.drive_runtime_io(wait_id=wait_id)
        return await self.call("GetMcpAuthorizationWait", wait_id=wait_id)

    async def confirm(
        self, wait_id, runtime_ref=None, *, phase=None, error=None, rejected_grant_version=None,
    ):
        return await self.call(
            "ConfirmMcpAuthorizationWait",
            wait_id=wait_id,
            runtime_ref=runtime_ref,
            phase=phase,
            error=error,
            **({"rejected_grant_version": rejected_grant_version} if rejected_grant_version is not None else {}),
        )

    async def credential(self, wait_id):
        await self.drive_runtime_io(wait_id=wait_id)
        return await self.call("ResolveMcpCredential", wait_id=wait_id)

    async def close(self):
        await self.http.aclose()

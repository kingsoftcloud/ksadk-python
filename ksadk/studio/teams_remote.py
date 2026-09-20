"""Studio projection of a server-owned Teams authority and local node.

Enabled explicitly with KSADK_TEAMS_SERVER_URL. Cloud coordination outlives
this adapter; shutting it down never revokes a team or a cloud execution.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from urllib.parse import urlsplit
from uuid import uuid4

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ksadk.plugins.teams.contracts import API_VERSION
from ksadk.plugins.teams.errors import TeamsError
from ksadk.plugins.teams.transport import TeamsHTTPClient
from ksadk.studio.teams_catalog import StudioTeamsCatalog
from ksadk.studio.teams_node import TeamsExecutionNode
from ksadk.studio.workspace_plugins import WorkspacePlugin

_MAX_PROXY_REQUEST_BYTES = 256 * 1024
_MAX_PROXY_RESPONSE_BYTES = 2 * 1024 * 1024
_PROXY_FAILURES = {
    "request_too_large": (413, "请求内容超过大小限制"),
    "invalid_request": (400, "请求编码无效"),
    "teams_scope_invalid": (403, "团队身份条件无效"),
    "teams_scope_changed": (403, "团队身份已发生变化，请返回原账号核查操作"),
    "teams_response_invalid": (502, "团队服务状态无效"),
    "teams_redirect_forbidden": (502, "团队服务返回了意外重定向"),
    "teams_response_too_large": (502, "团队响应超过大小限制"),
}


async def _close_proxy_response(response):
    # Starlette cancels the response task group when the browser disconnects.
    # Let the HTTP stream release its connection even inside that cancel scope.
    with anyio.CancelScope(shield=True):
        await response.aclose()


class RemoteStudioTeamsInstallation:
    def __init__(self, studio, base_url: str, *, node_v1_factory=None):
        self.studio, self.base_url = studio, base_url
        self.authority_ref = ""
        self.available = True
        self._enabled = False
        self._failure = None
        self.client = None
        self.node = None
        self.registry = None
        self.host = None
        self._prior_resolver = None
        self.node_v1_factory = node_v1_factory
        self._server_node_protocol = "legacy"
        self._node_transport_available = False
        self._cloud_status = {}
        self._status_lock = asyncio.Lock()
        studio.workspace_plugins.register(
            WorkspacePlugin(
                "teams",
                API_VERSION,
                self.enable,
                self.disable,
                self.status,
                shutdown=self._close,
                refresh_status=self.refresh_status,
            )
        )
        # Reading remote history does not depend on local node startup.
        studio.workspace_plugins.contribute_routes("teams", self._router())

    def status(self):
        return {
            "enabled": self._enabled,
            "configured": True,
            "available": True,
            "mode": "server",
            "authorityLocation": "server",
            "authorityRef": self.authority_ref,
            "authorityId": self._cloud_status.get("authorityId"),
            "ownerScopeRef": self._cloud_status.get("ownerScopeRef"),
            "features": self._cloud_status.get("features", []) if self._enabled else [],
            "health": self._cloud_status.get("health", "degraded")
            if self._enabled
            else "error"
            if self._failure
            else "disabled",
            "stage": "connection",
            "reason": self._failure,
            "nodeReason": self.node.last_error if self.node else self._failure,
            "recoveryUrl": "/studio-recovery/",
        }

    def _client(self):
        if self.client is None:

            async def signed(method, url, body):
                # The HTTP client survives Studio configuration updates. Resolve
                # credentials per request instead of retaining the first owner.
                token = self.studio.configuration.environment().get("KSADK_TEAMS_ACCESS_TOKEN")
                if token:
                    return {"Authorization": "Bearer " + token}
                gateway_client = getattr(
                    getattr(getattr(self.studio, "cloud", None), "gateway", None), "client", None
                )
                if gateway_client is None:
                    raise TeamsError(
                        "teams_credentials_required", "请配置团队服务连接凭据", status=401
                    )

                def create():
                    headers = gateway_client._build_headers()
                    if isinstance(body, bytes):
                        headers["Content-Type"] = "application/octet-stream"
                    headers["Host"] = urlsplit(url).netloc
                    return gateway_client._auth.sign_headers(
                        method=method, url=url, headers=headers, body=body
                    )

                return await asyncio.to_thread(create)

            self.client = TeamsHTTPClient(self.base_url, headers=signed)
        return self.client

    async def _read_cloud_status(self):
        status = await self._client().request("GET", "/lifecycle")
        if not isinstance(status, dict):
            raise TeamsError("teams_response_invalid", "团队服务状态无效", status=502)
        if status.get("mode") == "cloud":
            if (
                status.get("apiVersion") != API_VERSION
                or not isinstance(status.get("authorityId"), str)
                or not status["authorityId"]
                or not isinstance(status.get("ownerScopeRef"), str)
                or not status["ownerScopeRef"]
                or not isinstance(status.get("features"), list)
                or not all(isinstance(value, str) for value in status["features"])
                or status.get("health")
                not in {"ready", "degraded", "initialization_required", "provisioning"}
            ):
                raise TeamsError("teams_response_invalid", "团队服务身份或能力声明无效", status=502)
        prior = (self.authority_ref, self._cloud_status.get("ownerScopeRef"))
        current = (
            status.get("authorityId") or status.get("authorityRef") or "",
            status.get("ownerScopeRef"),
        )
        if prior[0] and current != prior:
            await self._close_node()
        self.authority_ref = current[0]
        self._cloud_status = status
        self._server_node_protocol = "teams-node/v1" if status.get("mode") == "cloud" else "legacy"
        self._node_transport_available = bool((status.get("services") or {}).get("nodeTransport"))
        return status

    async def refresh_status(self):
        if not self._enabled:
            return self.status()
        async with self._status_lock:
            try:
                await self._read_cloud_status()
            except Exception as error:
                self._failure = getattr(error, "code", "teams_transport_unavailable")
                self._cloud_status = {**self._cloud_status, "health": "degraded", "features": []}
        return self.status()

    async def enable(self):
        if self._enabled:
            await self.refresh_status()
            if self.node is None:
                await self._start_node()
                self._failure = None
            return
        try:
            await self._read_cloud_status()
            self._enabled = True
            self._failure = None
        except TeamsError as error:
            self._failure = error.code
            raise
        # Server teams remain accessible if this particular device cannot run.
        try:
            await self._start_node()
        except Exception as error:
            self._failure = getattr(error, "code", "local_node_unavailable")

    async def _start_node(self):
        if self.node is not None:
            return
        if self._server_node_protocol == "teams-node/v1":
            await self._start_node_v1()
            return
        from ksadk.studio.execution_host import StudioExecutionHost
        from ksadk.studio.kernel_registry import StudioBuildKernelRegistry

        await self.studio.start()
        state = self.studio.workspace.resolve(".agentkit/teams-node")
        state.mkdir(parents=True, exist_ok=True)
        identity_path = state / "identity.json"
        if identity_path.exists():
            identity = json.loads(identity_path.read_text())
        else:
            identity = {"nodeId": "node_" + uuid4().hex}
            with os.fdopen(
                os.open(identity_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
            ) as stream:
                json.dump(identity, stream)
        catalog = StudioTeamsCatalog(self.studio, authority_ref=self.authority_ref)
        bindings = catalog._local()
        environment = self.studio.configuration.environment()
        kind = environment.get("KSADK_TEAMS_NODE_KIND", "local")
        name = environment.get("KSADK_TEAMS_NODE_NAME") or socket.gethostname()
        registration = await self._client().request(
            "POST",
            "/nodes/register",
            {
                "nodeId": identity["nodeId"],
                "name": name,
                "kind": kind,
                "bindings": bindings,
                "idempotencyKey": TeamsExecutionNode.registration_key(
                    identity["nodeId"], name, kind, bindings
                ),
            },
        )
        self.registry = StudioBuildKernelRegistry(
            resolve_build=self.studio.resolve_run_spec,
            resolve_adapter_provider=self.studio._scheduler_adapter_provider,
            session_service=self.studio.session_service,
            runtime_executor=self.studio.runtime_executor,
            state_dir=state / "runtime",
            tenant_id=registration["tenantId"],
            workspace_id=registration["nodeId"],
        )
        try:
            await self.registry.start()
            self.host = StudioExecutionHost(self.registry, state_path=state / "host.sqlite")
            self.node = TeamsExecutionNode(
                self._client(),
                self.host,
                state_dir=state,
                name=name,
                kind=kind,
                bindings=bindings,
                allowed_roots=(self.studio.workspace.root,),
                list_bindings=catalog._local,
            )
            self._prior_resolver = self.studio.plugin_runs.execution_policy_resolver
            self.studio.plugin_runs.execution_policy_resolver = self.host
            await self.node.start()
        except BaseException:
            await self._close_node()
            raise

    async def _start_node_v1(self):
        """New servers never fall back to the legacy policy/registration path."""
        from ksadk.studio.teams_node_v1 import TeamsNodeV1

        factory = self.node_v1_factory
        if not self._node_transport_available or factory is None:
            raise TeamsError(
                "node_host_bootstrap_required", "受控节点宿主尚未装配，仍可查看云端团队", status=503
            )
        environment = self.studio.configuration.environment()
        node = await factory(
            studio=self.studio,
            owner_client=self._client(),
            state_dir=self.studio.workspace.resolve(".agentkit/teams-node"),
            authority_id=self.authority_ref,
            name=environment.get("KSADK_TEAMS_NODE_NAME") or socket.gethostname(),
        )
        if not isinstance(node, TeamsNodeV1):
            raise TypeError("the node bootstrap must return TeamsNodeV1")
        self.node = node
        try:
            await node.start()
        except BaseException:
            await self._close_node()
            raise

    def _router(self):
        router = APIRouter()
        transfer_slots = asyncio.Semaphore(4)

        @router.api_route("/groups", methods=["GET", "POST"])
        @router.api_route("/groups/{rest:path}", methods=["GET", "POST", "PATCH", "DELETE"])
        async def proxy(request: Request, rest: str = ""):
            return await forward(request, "/groups" + ("/" + rest if rest else ""))

        @router.api_route("/teams/{control:path}", methods=["GET", "POST", "PUT"])
        async def control_proxy(request: Request, control: str):
            import re

            material = re.fullmatch(
                r"materials(?:/[A-Za-z0-9_-]+(?:/(?:finalize|blobs/sha256:[0-9a-f]{64}))?)?",
                control,
            )
            material_allowed = bool(material) and (
                (
                    request.method == "POST"
                    and (control == "materials" or control.endswith("/finalize"))
                )
                or (
                    request.method == "GET"
                    and control != "materials"
                    and not control.endswith("/finalize")
                )
                or (request.method == "PUT" and "/blobs/" in control)
            )
            allowed = (request.method, control) in {
                ("GET", "lifecycle"),
                ("POST", "authorities/provision"),
                ("POST", "operations/lookup"),
            } or (
                request.method == "GET"
                and control.startswith("operations/")
                and len(control.split("/")) == 2
            )
            if not allowed and not material_allowed:
                return JSONResponse(
                    {"error": {"code": "teams_path_forbidden", "message": "团队接口路径无效"}},
                    status_code=404,
                )
            if material_allowed:
                async with transfer_slots:
                    return await forward(
                        request,
                        "/" + control,
                        binary=request.method == "PUT",
                        max_json=2 * 1024 * 1024,
                    )
            return await forward(request, "/" + control)

        async def forward(
            request: Request, path: str, *, binary=False, max_json=_MAX_PROXY_REQUEST_BYTES
        ):
            rest = path
            if any(part in {".", ".."} for part in rest.split("/")) or any(
                char in rest for char in "\\%?#\x00"
            ):
                return JSONResponse(
                    {"error": {"code": "teams_path_forbidden", "message": "团队接口路径无效"}},
                    status_code=400,
                    headers={"Cache-Control": "no-store"},
                )
            if request.url.query:
                path += "?" + request.url.query
            client = self._client()
            upstream = None
            try:
                parts, size = [], 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > (20 * 1024 * 1024 if binary else max_json):
                        raise TeamsError("request_too_large", "请求内容超过大小限制", status=413)
                    parts.append(chunk)
                body = b"".join(parts)
                try:
                    text = body if binary else body.decode("utf-8")
                except UnicodeDecodeError:
                    raise TeamsError("invalid_request", "请求编码无效", status=400) from None
                expected = {}
                for header, query in (
                    ("X-Teams-Authority-Id", "expectAuthorityId"),
                    ("X-Teams-Owner-Scope-Ref", "expectOwnerScopeRef"),
                ):
                    values = request.headers.getlist(header)
                    query_values = request.query_params.getlist(query)
                    if (
                        len(values) > 1
                        or len(query_values) > 1
                        or any(not v or len(v) > 256 for v in values + query_values)
                    ):
                        raise TeamsError("teams_scope_invalid", "团队身份条件无效", status=403)
                    if values and query_values and values != query_values:
                        raise TeamsError("teams_scope_changed", "团队身份已发生变化", status=403)
                    if values or query_values:
                        expected[header] = (values or query_values)[0]
                if expected or self._server_node_protocol == "teams-node/v1":
                    status = await self._read_cloud_status()
                    for header, field in (
                        ("X-Teams-Authority-Id", "authorityId"),
                        ("X-Teams-Owner-Scope-Ref", "ownerScopeRef"),
                    ):
                        if expected.get(header) != status.get(field):
                            raise TeamsError(
                                "teams_scope_changed",
                                "团队身份已发生变化，请返回原账号核查操作",
                                status=403,
                            )
                headers = await client.request_headers(request.method, path, text)
                # Forward only scope preconditions. Browser authentication,
                # node tokens and internal identity carriers are never copied.
                headers.update(expected)
                upstream = await client.http.send(
                    client.http.build_request(
                        request.method, client.base_url + path, content=body, headers=headers
                    ),
                    stream=True,
                )
                if upstream.is_redirect:
                    raise TeamsError(
                        "teams_redirect_forbidden", "团队服务返回了意外重定向", status=502
                    )
                media = upstream.headers.get("content-type", "application/json")
                output_headers = {
                    key: value
                    for key, value in upstream.headers.items()
                    if key.lower() in {"content-disposition", "etag", "x-teams-request-id"}
                }
                output_headers["Cache-Control"] = "no-store"
                if "text/event-stream" in media or (
                    "content-disposition" in upstream.headers and upstream.is_success
                ):

                    async def stream():
                        try:
                            async for chunk in upstream.aiter_bytes():
                                yield chunk
                        finally:
                            await _close_proxy_response(upstream)

                    return StreamingResponse(
                        stream(),
                        status_code=upstream.status_code,
                        media_type=media,
                        headers={**output_headers, "X-Accel-Buffering": "no"},
                    )
                chunks, size = [], 0
                async for chunk in upstream.aiter_bytes():
                    size += len(chunk)
                    if size > _MAX_PROXY_RESPONSE_BYTES:
                        raise TeamsError(
                            "teams_response_too_large", "团队响应超过大小限制", status=502
                        )
                    chunks.append(chunk)
                content = b"".join(chunks)
                await _close_proxy_response(upstream)
                return Response(
                    content,
                    status_code=upstream.status_code,
                    media_type=media,
                    headers=output_headers,
                )
            except asyncio.CancelledError:
                if upstream is not None:
                    await _close_proxy_response(upstream)
                raise
            except Exception as error:
                if upstream is not None:
                    await _close_proxy_response(upstream)
                failure = _PROXY_FAILURES.get(error.code) if isinstance(error, TeamsError) else None
                return JSONResponse(
                    {
                        "error": {
                            "code": error.code
                            if isinstance(error, TeamsError)
                            else "teams_transport_unavailable",
                            "message": failure[1]
                            if failure
                            else "团队服务连接中断，请重试核对原操作",
                        }
                    },
                    status_code=failure[0] if failure else 503,
                    headers={"Cache-Control": "no-store"},
                )

        return router

    async def _close_node(self):
        if self.node:
            await self.node.close()
            self.node = None
        if self.registry:
            await self.registry.close()
            self.registry = None
        if self.host:
            if self.studio.plugin_runs.execution_policy_resolver is self.host:
                self.studio.plugin_runs.execution_policy_resolver = self._prior_resolver
            await self.host.close()
            self.host = None

    async def disable(self):
        await self._close_node()
        self._enabled = False

    async def _close(self):
        await self.disable()
        if self.client:
            await self.client.close()
            self.client = None

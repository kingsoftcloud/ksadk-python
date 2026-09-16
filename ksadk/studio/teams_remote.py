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

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ksadk.plugins.teams.contracts import API_VERSION
from ksadk.plugins.teams.errors import TeamsError
from ksadk.plugins.teams.transport import TeamsHTTPClient
from ksadk.studio.teams_catalog import StudioTeamsCatalog
from ksadk.studio.teams_node import TeamsExecutionNode
from ksadk.studio.workspace_plugins import WorkspacePlugin


class RemoteStudioTeamsInstallation:
    def __init__(self, studio, base_url: str):
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
        studio.workspace_plugins.register(
            WorkspacePlugin(
                "teams", API_VERSION, self.enable, self.disable, self.status, shutdown=self._close
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
            "health": "degraded"
            if self._enabled and self._failure
            else "ready"
            if self._enabled
            else "error"
            if self._failure
            else "disabled",
            "stage": "connection",
            "reason": self._failure,
            "nodeReason": self.node.last_error if self.node else None,
            "recoveryUrl": "/studio-recovery/",
        }

    def _client(self):
        if self.client is None:
            environment = self.studio.configuration.environment()
            token = environment.get("KSADK_TEAMS_ACCESS_TOKEN") or None
            gateway_client = getattr(
                getattr(getattr(self.studio, "cloud", None), "gateway", None), "client", None
            )

            async def signed(method, url, body):
                if token:
                    return {}
                if gateway_client is None:
                    if not token:
                        raise TeamsError(
                            "teams_credentials_required", "请配置团队服务连接凭据", status=401
                        )
                    return {}

                def create():
                    headers = gateway_client._build_headers()
                    headers["Host"] = urlsplit(url).netloc
                    return gateway_client._auth.sign_headers(
                        method=method, url=url, headers=headers, body=body
                    )

                return await asyncio.to_thread(create)

            self.client = TeamsHTTPClient(self.base_url, access_token=token, headers=signed)
        return self.client

    async def enable(self):
        if self._enabled:
            if self.node is None:
                await self._start_node()
                self._failure = None
            return
        try:
            status = await self._client().request("GET", "/lifecycle")
            self.authority_ref = status["authorityRef"]
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

    def _router(self):
        router = APIRouter()

        @router.api_route("/groups", methods=["GET", "POST"])
        @router.api_route("/groups/{rest:path}", methods=["GET", "POST", "PATCH", "DELETE"])
        async def proxy(request: Request, rest: str = ""):
            if any(part in {".", ".."} for part in rest.split("/")) or any(
                char in rest for char in "\\%?#\x00"
            ):
                return JSONResponse(
                    {"error": {"code": "teams_path_forbidden", "message": "团队接口路径无效"}},
                    status_code=400,
                )
            path = "/groups" + ("/" + rest if rest else "")
            if request.url.query:
                path += "?" + request.url.query
            client = self._client()
            body = await request.body()
            text = body.decode("utf-8") if body else ""
            upstream = None
            try:
                headers = await client.request_headers(request.method, path, text)
                upstream = await client.http.send(
                    client.http.build_request(
                        request.method, client.base_url + path, content=body, headers=headers
                    ),
                    stream=True,
                )
                media = upstream.headers.get("content-type", "application/json")
                if "text/event-stream" in media:

                    async def stream():
                        try:
                            async for chunk in upstream.aiter_bytes():
                                yield chunk
                        finally:
                            await upstream.aclose()

                    return StreamingResponse(
                        stream(), status_code=upstream.status_code, media_type="text/event-stream"
                    )
                content = await upstream.aread()
                await upstream.aclose()
                return Response(
                    content,
                    status_code=upstream.status_code,
                    media_type=media,
                    headers={
                        key: value
                        for key, value in upstream.headers.items()
                        if key.lower() in {"content-disposition", "etag"}
                    },
                )
            except Exception as error:
                if upstream:
                    await upstream.aclose()
                return JSONResponse(
                    {
                        "error": {
                            "code": error.code
                            if isinstance(error, TeamsError)
                            else "teams_transport_unavailable",
                            "message": "团队服务连接中断，请重试核对原操作",
                        }
                    },
                    status_code=503,
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

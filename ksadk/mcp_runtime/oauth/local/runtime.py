"""Loopback listener and app-owned composition for local Web debugging only."""

import asyncio
import errno
import ipaddress
import json
import os
import socket
from contextlib import AsyncExitStack, suppress
from urllib.parse import urlsplit

import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..client import BrokerError
from ..http import AuthorizationAction
from ..kernel import MCPAuthorizationExtension, SQL
from .broker import LocalBroker
from .persistence import SQLiteAuthorizationStore, open_project_vault, project_vault_directory


class LocalAuthorizationExtension(MCPAuthorizationExtension):
    def __init__(self, store, client):
        self._publication_lock = asyncio.Lock()
        self.signing_key = Ed25519PrivateKey.generate()
        super().__init__(
            SQLiteAuthorizationStore(store),
            client,
            public_key=self.signing_key.public_key(),
            issuer="ksadk-local-mcp-oauth",
        )

    async def ensure_schema(self):
        # LocalVault owns the project lock; this adapter stores its waits in SQLite.
        async with self.pool.acquire() as conn:
            await conn.execute(SQL)
            if self.store.kernel._session_schema == "main":
                from ksadk.kernel.sqlite_session_cleanup import install_wait_delete_trigger

                await install_wait_delete_trigger(conn.raw)
        self._schema_ready = True

    async def publish(self, worker, run, snapshot, guard):
        # Local event history and the Kernel can be separate databases. Append
        # the stable event first so an interrupted write can be safely repeated.
        from ksadk.events.session_event import session_event_storage_id, session_event_to_envelope

        async with self._publication_lock:
            previous = await self.row(snapshot["wait_id"])
            if previous["snapshot"].get("revision", 0) >= snapshot["revision"]:
                return
            envelope = self._authorization_event(
                run, snapshot, not previous["snapshot"].get("requires_resume")
            )
            history = worker._session_events
            stored_id = session_event_storage_id(run.session_id, str(envelope.event_id))
            existing = await history.session_service.get_event_by_id(run.session_id, stored_id)
            if existing is not None:
                envelope = envelope.model_copy(
                    update={"timestamp": session_event_to_envelope(existing).timestamp}
                )
            await history.append(envelope, guard=guard)
            async with self.pool.acquire() as conn:
                await self.store._assert_fence(
                    conn, run.agent_instance_id, run.session_id, guard.fencing_token
                )
                await conn.execute(
                    "UPDATE mcp_authorization_waits SET snapshot=$2 WHERE wait_id=$1 AND run_id=$3",
                    snapshot["wait_id"],
                    json.dumps(snapshot),
                    run.run_id,
                )

    async def ensure_authorization(self, run_id, config_id, request_key):
        # The local Kernel, not HTTP identity headers, owns the binding.
        from ..langgraph import current_declaration, require_wait_checkpoint

        require_wait_checkpoint(hosted=False)
        return await self.client.ensure(
            run_id, config_id, request_key, declaration=current_declaration(config_id)
        )

    async def lookup_credential(
        self, run_id, config_id, request_key, *, rejected_grant_version=None
    ):
        from ..langgraph import current_declaration

        return await self.client.ensure(
            run_id,
            config_id,
            request_key,
            declaration=current_declaration(config_id),
            lookup=True,
            rejected_grant_version=rejected_grant_version,
        )


def _bind_callback_socket(listener: socket.socket, port: int) -> None:
    # Windows SO_REUSEADDR permits a second listener on the same port.
    option = getattr(socket, "SO_EXCLUSIVEADDRUSE", socket.SO_REUSEADDR)
    listener.setsockopt(socket.SOL_SOCKET, option, 1)
    try:
        listener.bind(("127.0.0.1", port))
    except OSError as exc:
        # Winsock can report WSAEACCES for an exclusively held port.
        windows_conflict = port and getattr(exc, "winerror", None) in (10013, 10048)
        if exc.errno == errno.EADDRINUSE or windows_conflict:
            raise BrokerError("LOCAL_OAUTH_CALLBACK_PORT_IN_USE") from None
        raise


def require_loopback(request, *, same_origin=False, authority=None):
    host = request.url.hostname
    try:
        peer_ok = (
            request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
        )
    except ValueError:
        peer_ok = False
    if not peer_ok or host not in {"127.0.0.1", "localhost", "::1"}:
        raise BrokerError("LOCAL_OAUTH_LOOPBACK_REQUIRED")
    if authority and request.headers.get("host") != authority:
        raise BrokerError("LOCAL_OAUTH_LOOPBACK_REQUIRED")
    origin = request.headers.get("origin")
    if same_origin and origin and origin != str(request.base_url).rstrip("/"):
        raise BrokerError("LOCAL_OAUTH_ORIGIN_REJECTED")
    if same_origin and request.headers.get("sec-fetch-site") == "cross-site":
        raise BrokerError("LOCAL_OAUTH_ORIGIN_REJECTED")


class LocalOAuthRuntime:
    def __init__(self, local, *, directory=None, http=None):
        self.local = local
        self.launch = local.launch
        self.directory = directory or project_vault_directory(self.launch.project_dir)
        self.http = http
        self._activation_lock = asyncio.Lock()
        self.runtime = None
        self.broker = self.vault = self.server = self.task = self.socket = None

    async def prepare_adapter(self, adapter):
        """Opt this app into the existing saver without changing process env.

        Never replace an Agent-owned saver or an explicit checkpoint selection.
        An implicit saver only restores a run when it resumes an interrupt.
        """
        from ksadk.runners.langgraph_runner import LangGraphRunner

        runner = getattr(adapter, "_runner", None)
        if not isinstance(runner, LangGraphRunner):
            return
        if runner._agent is None:
            runner.load_agent()
        saver = getattr(runner._agent, "checkpointer", None)
        if saver is None:
            saver = getattr(runner._agent, "_checkpointer", None)
        if saver is not None:
            return
        if os.getenv("KSADK_LANGGRAPH_AUTO_CHECKPOINT") is not None:
            return  # Explicit enable/disable and Session fallback belong to the runner.
        from ksadk.sessions.topology import resolve_persistence_topology

        topology = resolve_persistence_topology(
            framework="langgraph", effective_session=runner._effective_session_target,
        )
        if topology.checkpoint.dsn or os.getenv("KSADK_CHECKPOINT_BACKEND"):
            return
        from langgraph.graph.state import CompiledStateGraph

        if not isinstance(runner._agent, CompiledStateGraph) and not callable(
            runner._graph_factory()
        ):
            return
        from .checkpoint import LocalInterruptSaver

        await runner._bind_local_checkpoint(backend="sqlite", sqlite_saver_type=LocalInterruptSaver)

    async def start(self, runtime):
        self.runtime = runtime
        pending = False
        if (self.directory / "vault.sqlite3").exists():
            async with SQLiteAuthorizationStore(runtime.kernel_store).acquire() as db:
                if await db.fetchrow(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mcp_authorization_waits'"
                ):
                    pending = bool(
                        await db.fetchrow(
                            "SELECT 1 FROM mcp_authorization_waits WHERE status IN ('pending','resolving','consumed') "
                            "UNION ALL SELECT 1 FROM kernel_runs WHERE state='waiting' "
                            "AND json_type(metadata_json, '$.mcp_authorization')='object' LIMIT 1"
                        )
                    )
        if pending:
            await self.activate()
        else:
            # Normal chats use no OAuth listener or vault. An actual tool call
            # switches the worker to its durable extension before interrupt().
            runtime.worker._mcp_authorization = self

    async def activate(self):
        async with self._activation_lock:
            if self.broker is None:
                await self._start(self.runtime)
            return self.broker.extension

    async def ensure_authorization(self, run_id, config_id, request_key):
        extension = await self.activate()
        return await extension.ensure_authorization(run_id, config_id, request_key)

    async def lookup_credential(self, *args, **kwargs):
        return await (await self.activate()).lookup_credential(*args, **kwargs)

    async def claim_dispatch(self, *args):
        return await (await self.activate()).claim_dispatch(*args)

    async def release_declined_dispatch(self, *args):
        return await (await self.activate()).release_declined_dispatch(*args)

    async def finish_dispatch(self, *args):
        return await (await self.activate()).finish_dispatch(*args)

    async def require_completion(self, *args):
        return await (await self.activate()).require_completion(*args)

    @property
    def client(self):
        return self.broker or self

    async def pending(self, run_id):
        return []

    async def cancellation_targets(self, run_id):
        return []

    async def pending_session_ids(self, instance_id):
        return set()

    async def consumed(self, worker, run, event, guard):
        # Ordinary input/approval must finish without creating an OAuth vault.
        if self.broker is not None:
            await self.broker.extension.consumed(worker, run, event, guard)

    async def tick(self, worker, activation, session_id):
        return False

    async def cancel_run(self, worker, run, guard):
        return None

    async def _start(self, runtime):
        from ksadk.deployment.mcp import component_mcp_payload, project_mcp_declarations

        if not self.local._eligible():
            raise BrokerError("LOCAL_OAUTH_NOT_AVAILABLE")
        declarations = component_mcp_payload(
            project_mcp_declarations(self.launch.config, project_dir=self.launch.project_dir) or []
        )
        if any(d["oauth"].get("client_ref") for d in declarations):
            raise BrokerError("LOCAL_OAUTH_PLATFORM_CLIENT_REF_UNSUPPORTED")
        try:
            self.vault = open_project_vault(self.launch.project_dir, directory=self.directory)
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            _bind_callback_socket(self.socket, self.vault.data["port"])
            self.socket.listen(16)
            self.socket.setblocking(False)
            port = self.socket.getsockname()[1]
            self.vault.data["port"] = port
            self.vault.save()
            self.broker = LocalBroker(self.vault, runtime, declarations, http=self.http)
            self.broker.base_url = f"http://127.0.0.1:{port}"
            extension = LocalAuthorizationExtension(runtime.kernel_store, self.broker)
            self.broker.extension = extension
            await extension.ensure_schema()
            app = self.callback_app()
            self.server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    log_level="error",
                    log_config=None,
                    access_log=False,
                    lifespan="off",
                    proxy_headers=False,
                )
            )
            # The enclosing CLI owns process signals, not this auxiliary listener.
            from contextlib import nullcontext

            self.server.capture_signals = nullcontext
            self.task = asyncio.create_task(self.server.serve(sockets=[self.socket]))
            async with asyncio.timeout(5):
                while not self.server.started:
                    if self.task.done():
                        await self.task
                        raise BrokerError("LOCAL_OAUTH_CALLBACK_START_FAILED")
                    await asyncio.sleep(0.01)
            runtime.worker._mcp_authorization = extension
        except BaseException:
            await self.close()
            raise

    def callback_app(self):
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        headers = {
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
        }
        authority = urlsplit(self.broker.base_url).netloc

        @app.get("/open/{ticket}")
        async def open_authorization(ticket: str, request: Request):
            try:
                require_loopback(request, authority=authority)
                url = await self.broker.open_ticket(ticket)
                return RedirectResponse(url, status_code=303, headers=headers)
            except BrokerError:
                return HTMLResponse(
                    "授权入口已失效，请回到 Agent 页面重新打开。", status_code=400, headers=headers
                )

        @app.get("/agentengine/oauth/mcp/callback")
        async def callback(request: Request):
            try:
                require_loopback(request, authority=authority)
                query = request.query_params
                if any(len(query.getlist(k)) > 1 for k in ("state", "code", "error", "iss")):
                    raise BrokerError("INVALID_OAUTH_STATE")
                if not query.get("state") or len(str(request.url)) > 16384:
                    raise BrokerError("INVALID_OAUTH_STATE")
                if query.get("code") and query.get("error"):
                    raise BrokerError("INVALID_OAUTH_STATE")
                wait = await self.broker.callback(
                    state=query["state"],
                    code=query.get("code"),
                    error=query.get("error"),
                    issuer=query.get("iss"),
                )
                message = (
                    "授权已接收，请回到 Agent 页面查看任务。"
                    if wait["status"] in {"credential_ready", "connecting", "resumed"}
                    else "本次授权未完成，请回到 Agent 页面查看状态。"
                )
                return HTMLResponse('<meta charset="utf-8"><p>' + message + "</p>", headers=headers)
            except BrokerError:
                return HTMLResponse(
                    "授权回调无效，请回到 Agent 页面查看状态。", status_code=400, headers=headers
                )

        return app

    def install_routes(self, app):
        for name in (
            "GetMcpAuthorizationWait",
            "OpenMcpAuthorization",
            "RetryMcpAuthorization",
            "RetryMcpConnection",
            "CancelMcpAuthorizationWait",
        ):

            def make_action(operation):
                async def action(request: Request, body: AuthorizationAction):
                    try:
                        require_loopback(request, same_origin=True)
                        await self.local.ensure()
                        if self.broker is None:
                            if not (self.directory / "vault.sqlite3").exists():
                                raise BrokerError("WAIT_NOT_FOUND")
                            await self.activate()
                        data = await self.broker.action(
                            operation,
                            body.WaitId,
                            body.ExpectedRevision,
                            agent_id=body.AgentId,
                            session_id=body.SessionId,
                        )
                        return JSONResponse(
                            {"Code": 0, "Message": "Success", "Data": data},
                            headers={"Cache-Control": "no-store"},
                        )
                    except BrokerError as exc:
                        return JSONResponse(
                            {"Code": 409, "ReasonCode": exc.code, "Message": exc.code},
                            status_code=409,
                            headers={"Cache-Control": "no-store"},
                        )

                return action

            app.add_api_route(
                "/agentengine/api/v1/" + name, make_action(name), methods=["POST"], name=name
            )

    async def close(self):
        try:
            async with AsyncExitStack() as cleanup:
                if self.vault is not None:
                    cleanup.callback(self.vault.close)
                if self.broker is not None:
                    cleanup.push_async_callback(self.broker.close)
                if self.socket is not None:
                    cleanup.callback(self.socket.close)
                if self.server is not None:
                    self.server.should_exit = True
                if self.task is not None:
                    with suppress(asyncio.CancelledError):
                        try:
                            await asyncio.wait_for(self.task, 5)
                        except TimeoutError:
                            self.task.cancel()
                            await self.task
        finally:
            self.broker = self.vault = self.server = self.task = self.socket = None

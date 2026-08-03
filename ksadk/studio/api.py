"""FastAPI application for the loopback-only AgentKit Studio control plane."""

from __future__ import annotations

import hmac
import json
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import FastAPI, File, Header, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]
from pydantic import Field, SecretStr

from ksadk.studio.contracts import (
    AgentBindings,
    AgentSpec,
    AgentTemplateComposeRequest,
    ContractModel,
    DeploymentRequest,
    MCPServerRef,
    ModelSpec,
    ToolContract,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.service import StudioService
from ksadk.studio.shared_web import StudioSharedWebBridge, shared_web_static_root

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_PUBLIC_API_PATHS = {
    "/api/v1/system/health",
    "/api/v1/system/session",
}
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testserver"}
_SHARED_CHAT_THEME = (
    '<link id="agentkitStudioSharedChatTheme" rel="stylesheet" '
    'href="/static/shared-chat.css">'
)


def _themed_shared_chat_document(path: Path) -> str:
    document = path.read_text(encoding="utf-8")
    if "data-agentkit-studio-chat" not in document:
        document = document.replace(
            "<html",
            '<html data-agentkit-studio-chat="workbench"',
            1,
        )
    if "agentkitStudioSharedChatTheme" not in document:
        if "</head>" in document:
            document = document.replace(
                "</head>",
                f"  {_SHARED_CHAT_THEME}\n  </head>",
                1,
            )
        else:
            document = document.replace(
                "<body",
                f"<head>{_SHARED_CHAT_THEME}</head><body",
                1,
            )
    return document


class SessionExchangeRequest(ContractModel):
    token: str = Field(min_length=16, max_length=512)


class WorkspaceOpenRequest(ContractModel):
    path: str
    create: bool = False


class CreateAgentRequest(ContractModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{2,62}$")
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=1024)
    template: str = "blank"
    spec: AgentSpec | None = None


class ValidationRequest(ContractModel):
    revision: int = Field(ge=1)
    level: Literal["schema", "build", "release"] = "build"


class BuildRequest(ContractModel):
    revision: int = Field(ge=1)
    run_evaluation: bool = False
    evaluation_suite_refs: list[str] = Field(default_factory=list)


class MessageInput(ContractModel):
    role: str = "user"
    content: str = Field(min_length=1, max_length=1_000_000)


class RunRequest(ContractModel):
    session_id: str | None = None
    input: MessageInput
    environment: str = "local"
    stream: bool = True


class EvaluationRequest(ContractModel):
    suite_refs: list[str] = Field(min_length=1)
    concurrency: int = Field(default=1, ge=1, le=4)
    fail_fast: bool = False


class SecretReferenceCheckRequest(ContractModel):
    ref: str


class CredentialPutRequest(ContractModel):
    value: SecretStr = Field(min_length=1, max_length=16_384)
    persistence: Literal["session"] = "session"


class RollbackRequest(ContractModel):
    target_build_id: str


class ModelProfileCreateRequest(ContractModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9._-]{1,127}$")
    display_name: str = Field(min_length=1, max_length=128)
    version: str = Field(default="1.0.0", min_length=1, max_length=64)
    description: str = Field(default="", max_length=4096)
    spec: ModelSpec


class MCPResourceCreateRequest(ContractModel):
    display_name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=4096)
    server: MCPServerRef


class ToolResourceCreateRequest(ContractModel):
    display_name: str = Field(min_length=1, max_length=128)
    category: str = Field(default="custom", max_length=64)
    contract: ToolContract


class ToolSchemaValidationRequest(ContractModel):
    schema_definition: dict[str, Any] = Field(alias="schema")
    sample: Any = None


class ToolPolicyPreviewRequest(ContractModel):
    bindings: AgentBindings


def create_studio_app(
    root: Path | str,
    *,
    service: StudioService | None = None,
    session_token: str | None = None,
    csrf_token: str | None = None,
    security_enabled: bool = True,
) -> FastAPI:
    studio = service or StudioService(root)
    session_secret = session_token or secrets.token_urlsafe(32)
    csrf_secret = csrf_token or secrets.token_urlsafe(24)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            studio.credentials.clear_session()

    app = FastAPI(
        title="AgentKit Local Studio",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
        lifespan=lifespan,
    )
    app.state.studio_service = studio
    app.state.session_token = session_secret
    app.state.csrf_token = csrf_secret
    static_root = Path(__file__).with_name("static")
    shared_static_root = shared_web_static_root()
    shared_chat_document = (
        _themed_shared_chat_document(shared_static_root / "index.html")
        if shared_static_root is not None
        else None
    )
    shared_web = StudioSharedWebBridge(studio)
    app.state.shared_web_bridge = shared_web
    app.mount("/static", StaticFiles(directory=static_root), name="studio-static")
    if shared_static_root is not None:
        app.mount(
            "/chat/assets",
            StaticFiles(directory=shared_static_root / "assets"),
            name="shared-chat-assets",
        )

    @app.middleware("http")
    async def local_security(request: Request, call_next):
        request.state.request_id = request.headers.get("X-Request-Id") or (
            f"req_{secrets.token_hex(12)}"
        )
        host = (request.url.hostname or "").lower()
        if host not in _LOCAL_HOSTS:
            return _error_response(
                StudioError(
                    "LOCAL_HOST_FORBIDDEN",
                    "Studio 只接受 loopback Host",
                    status_code=403,
                ),
                request,
            )
        origin = request.headers.get("Origin")
        if origin and not _is_local_origin(origin):
            return _error_response(
                StudioError(
                    "LOCAL_ORIGIN_FORBIDDEN",
                    "请求 Origin 不是本地 Studio",
                    status_code=403,
                ),
                request,
            )
        content_length = request.headers.get("Content-Length")
        request_limit = (
            52 * 1024 * 1024
            if request.url.path == "/api/v1/catalog/skills:import"
            else 2 * 1024 * 1024
        )
        if content_length and int(content_length) > request_limit:
            return _error_response(
                StudioError(
                    "REQUEST_TOO_LARGE",
                    "请求体超过 2 MiB 限制",
                    status_code=413,
                ),
                request,
            )
        studio_api = request.url.path.startswith("/api/v1")
        shared_web_api = request.url.path.startswith("/agentengine/api/v1")
        if security_enabled and (
            (studio_api and request.url.path not in _PUBLIC_API_PATHS)
            or shared_web_api
        ):
            supplied = request.cookies.get("agentkit_studio_session") or request.headers.get(
                "X-AgentKit-Session"
            )
            if not supplied or not hmac.compare_digest(supplied, session_secret):
                return _error_response(
                    StudioError(
                        "LOCAL_SESSION_REQUIRED",
                        "缺少有效的 Studio 本地会话",
                        status_code=401,
                    ),
                    request,
                )
            if studio_api and request.method in _WRITE_METHODS:
                csrf = request.headers.get("X-CSRF-Token")
                if not csrf or not hmac.compare_digest(csrf, csrf_secret):
                    return _error_response(
                        StudioError(
                            "CSRF_TOKEN_INVALID",
                            "缺少有效的 CSRF token",
                            status_code=403,
                        ),
                        request,
                    )
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        response.headers["Cache-Control"] = (
            "no-store"
            if request.url.path.startswith("/api/")
            else response.headers.get("Cache-Control", "no-cache")
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = (
            "SAMEORIGIN" if request.url.path.startswith("/chat") else "DENY"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(StudioError)
    async def studio_error_handler(request: Request, exc: StudioError):
        return _error_response(exc, request)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(value) for value in first.get("loc", [])[1:])
        return _error_response(
            StudioError(
                "REQUEST_VALIDATION_FAILED",
                str(first.get("msg") or "请求参数无效"),
                status_code=422,
                field=field or None,
            ),
            request,
        )

    @app.get("/")
    async def index():
        path = static_root / "index.html"
        response = FileResponse(path, media_type="text/html")
        if security_enabled:
            response.set_cookie(
                "agentkit_studio_session",
                session_secret,
                httponly=True,
                samesite="strict",
                secure=False,
                path="/",
            )
        return response

    @app.get("/favicon.ico")
    async def favicon():
        return Response(status_code=204)

    if shared_static_root is not None:

        @app.get("/chat")
        @app.get("/chat/")
        async def shared_chat(request: Request):
            requested = request.query_params.get("agentId")
            agent_id = shared_web.resolve_agent_id(requested)
            response = HTMLResponse(shared_chat_document)
            response.set_cookie(
                "agentkit_studio_chat_agent",
                agent_id,
                httponly=True,
                samesite="strict",
                secure=False,
                path="/",
            )
            return response

    @app.post("/agentengine/api/v1/{action}")
    async def shared_chat_action(
        action: str,
        request: Request,
        payload: dict[str, Any],
    ):
        try:
            cookie_agent_id = request.cookies.get("agentkit_studio_chat_agent")
            requested_agent_id = str(payload.get("AgentId") or cookie_agent_id or "")
            if action == "GetAgentUiBootstrap":
                data = shared_web.bootstrap(
                    shared_web.resolve_agent_id(requested_agent_id or None)
                )
            elif action == "ListAgentModels":
                data = shared_web.list_models(
                    shared_web.resolve_agent_id(requested_agent_id or None)
                )
            elif action == "ListSessions":
                data = shared_web.list_sessions(
                    shared_web.resolve_agent_id(requested_agent_id or None),
                    page=int(payload.get("Page") or 1),
                    page_size=int(payload.get("PageSize") or 30),
                )
            elif action == "CreateSession":
                data = shared_web.create_session(
                    shared_web.resolve_agent_id(requested_agent_id or None)
                )
            elif action == "GetSession":
                data = shared_web.get_session(str(payload.get("SessionId") or ""))
            elif action == "DeleteSession":
                data = shared_web.delete_session(str(payload.get("SessionId") or ""))
            elif action == "ListSessionMessages":
                data = shared_web.list_messages(
                    str(payload.get("SessionId") or ""),
                    after_seq_id=_optional_int(payload.get("AfterSeqId")),
                    before_seq_id=_optional_int(payload.get("BeforeSeqId")),
                    limit=int(payload.get("Limit") or 50),
                )
            elif action == "ListSessionEvents":
                data = shared_web.list_session_events(
                    str(payload.get("SessionId") or "")
                )
            elif action == "RunAgent":
                return StreamingResponse(
                    shared_web.stream_run(payload),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-store",
                        "X-Accel-Buffering": "no",
                    },
                )
            elif action == "CancelRun":
                data = shared_web.cancel_run(
                    str(payload.get("InvocationId") or "")
                )
            elif action in {
                "GetResponseFeedback",
                "UpsertResponseFeedback",
                "DeleteResponseFeedback",
            }:
                data = {"Feedback": None} if action == "GetResponseFeedback" else {}
            elif action == "ListSessionCheckpoints":
                data = {"Checkpoints": []}
            elif action == "ListToolReceipts":
                data = {"ToolReceipts": []}
            else:
                return JSONResponse(
                    status_code=404,
                    content={
                        "Code": 404,
                        "Message": f"Studio 尚未实现共享 Web 动作：{action}",
                        "Data": {},
                    },
                )
            return {"Code": 0, "Message": "OK", "Data": data}
        except StudioError as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "Code": exc.status_code,
                    "Message": exc.message,
                    "Data": {"errorCode": exc.code},
                },
            )

    @app.get("/agentengine/api/v1/SubscribeRunEvents")
    async def shared_chat_subscribe_run_events():
        async def completed_stream():
            yield "event: done\ndata: [DONE]\n\n"

        return StreamingResponse(
            completed_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/v1/system/health")
    async def health():
        return {
            "status": "ok",
            "version": "1.0.0",
            "workspaceReady": studio.workspace.root.is_dir(),
        }

    @app.post("/api/v1/system/session")
    async def exchange_session(payload: SessionExchangeRequest, response: Response):
        if security_enabled and not hmac.compare_digest(payload.token, session_secret):
            raise StudioError(
                "LOCAL_SESSION_INVALID",
                "Studio 启动会话无效",
                status_code=401,
            )
        response.set_cookie(
            "agentkit_studio_session",
            session_secret,
            httponly=True,
            samesite="strict",
            secure=False,
            path="/",
        )
        return {"csrfToken": csrf_secret}

    @app.get("/api/v1/system/bootstrap")
    async def bootstrap():
        return {
            "serverVersion": "1.0.0",
            "apiVersion": "v1",
            "sessionTokenRequired": security_enabled,
            "csrfToken": csrf_secret,
            "workspace": {
                "name": studio.workspace.root.name,
                "path": str(studio.workspace.root),
            },
            "features": {
                "build": True,
                "run": True,
                "evaluation": True,
                "deployment": True,
                "cloudRebuild": False,
                "sharedChat": shared_static_root is not None,
            },
        }

    @app.post("/api/v1/workspaces:open")
    async def open_workspace(payload: WorkspaceOpenRequest):
        requested = Path(payload.path).expanduser().resolve()
        if requested != studio.workspace.root:
            raise StudioError(
                "WORKSPACE_PATH_FORBIDDEN",
                "当前 Daemon 不允许切换到启动 root 之外的工作区",
                status_code=403,
            )
        return {
            "name": studio.workspace.root.name,
            "path": str(studio.workspace.root),
        }

    @app.post("/api/v1/agents", status_code=201)
    async def create_agent(payload: CreateAgentRequest):
        return studio.create_agent(
            agent_id=payload.id,
            name=payload.name,
            description=payload.description,
            template=payload.template,
            spec=payload.spec,
        )

    @app.get("/api/v1/agent-templates")
    async def agent_templates():
        return {"items": studio.list_agent_templates()}

    @app.post("/api/v1/agent-templates/{template_id}:compose")
    async def compose_agent_template(
        template_id: str,
        payload: AgentTemplateComposeRequest,
    ):
        return studio.compose_agent_template(template_id, payload)

    @app.get("/api/v1/agents")
    async def list_agents(
        limit: int = Query(default=50, ge=1, le=200),
        query: str = "",
    ):
        return {
            "items": studio.drafts.list(query=query, limit=limit),
            "nextCursor": None,
        }

    @app.get("/api/v1/agents/{agent_id}")
    async def get_agent(agent_id: str):
        draft = studio.drafts.get(agent_id)
        builds = studio.builds.list_for_agent(agent_id)
        return {
            "draft": draft,
            "builds": builds[:10],
            "validation": studio.validator.validate(draft),
        }

    @app.put("/api/v1/agents/{agent_id}")
    async def update_agent(
        agent_id: str,
        spec: AgentSpec,
        if_match: str | None = Header(default=None, alias="If-Match"),
    ):
        if not if_match:
            raise StudioError(
                "AGENT_REVISION_REQUIRED",
                "更新 Agent 必须提供 If-Match revision",
                status_code=428,
            )
        try:
            revision = int(if_match.strip().strip('"'))
        except ValueError as exc:
            raise StudioError(
                "AGENT_REVISION_INVALID",
                "If-Match 必须是整数 revision",
                status_code=400,
            ) from exc
        return studio.update_agent(
            agent_id,
            spec,
            expected_revision=revision,
        )

    @app.put("/api/v1/agents/{agent_id}/bindings")
    async def update_agent_bindings(
        agent_id: str,
        bindings: AgentBindings,
        if_match: str | None = Header(default=None, alias="If-Match"),
    ):
        revision = _parse_revision(if_match)
        return studio.update_agent_bindings(
            agent_id,
            bindings,
            expected_revision=revision,
        )

    @app.delete("/api/v1/agents/{agent_id}", status_code=204)
    async def delete_agent(agent_id: str, purge: bool = False):
        studio.drafts.delete(agent_id, purge=purge)
        return Response(status_code=204)

    @app.post("/api/v1/agents/{agent_id}/validations")
    async def validate_agent(agent_id: str, payload: ValidationRequest):
        draft = studio.drafts.get(agent_id)
        if draft.metadata.revision != payload.revision:
            raise StudioError(
                "AGENT_REVISION_CONFLICT",
                "Validation revision 与当前 Agent 不一致",
                status_code=409,
            )
        return studio.validate_agent(agent_id, level=payload.level)

    @app.post("/api/v1/agents/{agent_id}/builds", status_code=202)
    async def create_build(
        agent_id: str,
        payload: BuildRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        return studio.submit_build(
            agent_id,
            revision=payload.revision,
            idempotency_key=_require_idempotency_key(idempotency_key),
        )

    @app.get("/api/v1/builds/{build_id}")
    async def get_build(build_id: str):
        return studio.builds.get(build_id)

    @app.get("/api/v1/builds/{build_id}/manifest")
    async def get_build_manifest(build_id: str):
        build = studio.builds.get(build_id)
        if not build.artifact_path:
            raise StudioError("BUILD_NOT_READY", "Build 尚未生成制品", status_code=409)
        archive = studio.workspace.resolve(build.artifact_path)
        manifest = archive.parent / "agent-bundle" / "manifest.json"
        return json.loads(manifest.read_text(encoding="utf-8"))

    @app.post("/api/v1/builds/{build_id}/runs", status_code=202)
    async def create_run(
        build_id: str,
        payload: RunRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        if payload.environment != "local":
            raise StudioError(
                "RUN_ENVIRONMENT_UNSUPPORTED",
                "一期 Local Run 只支持 environment=local",
                status_code=422,
            )
        return studio.submit_run(
            build_id,
            payload.input.content,
            session_id=payload.session_id,
            idempotency_key=_require_idempotency_key(idempotency_key),
        )

    @app.get("/api/v1/runs/{run_id}")
    async def get_run(run_id: str):
        return studio.event_store.get(run_id)

    @app.get("/api/v1/runs")
    async def list_runs(session_id: str | None = Query(default=None, alias="sessionId")):
        return {"items": studio.event_store.list_runs(session_id=session_id)}

    @app.get("/api/v1/runs/{run_id}/events")
    async def run_events(
        run_id: str,
        request: Request,
        after: int = Query(default=0, ge=0),
    ):
        last = request.headers.get("Last-Event-ID")
        cursor = int(last) if last and last.isdigit() else after
        events = studio.event_store.events(run_id, after=cursor)
        return _sse(events)

    @app.get("/api/v1/traces/{trace_id}")
    async def get_trace(trace_id: str):
        return studio.event_store.trace(trace_id)

    @app.post("/api/v1/builds/{build_id}/evaluations", status_code=202)
    async def create_evaluation(
        build_id: str,
        payload: EvaluationRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        return studio.submit_evaluation(
            build_id,
            payload.suite_refs,
            fail_fast=payload.fail_fast,
            idempotency_key=_require_idempotency_key(idempotency_key),
        )

    @app.get("/api/v1/evaluations/{evaluation_id}")
    async def get_evaluation(evaluation_id: str):
        return studio.evaluations.get(evaluation_id)

    @app.post("/api/v1/builds/{build_id}/deployments", status_code=202)
    async def create_deployment(
        build_id: str,
        payload: DeploymentRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        return studio.submit_deployment(
            build_id,
            payload,
            idempotency_key=_require_idempotency_key(idempotency_key),
        )

    @app.get("/api/v1/deployments/{deployment_id}")
    async def get_deployment(deployment_id: str):
        return studio.cloud.get(deployment_id)

    @app.post("/api/v1/deployments/{deployment_id}:rollback", status_code=202)
    async def rollback_deployment(
        deployment_id: str,
        payload: RollbackRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        return studio.submit_rollback(
            deployment_id,
            target_build_id=payload.target_build_id,
            idempotency_key=_require_idempotency_key(idempotency_key),
        )

    @app.get("/api/v1/operations/{operation_id}")
    async def get_operation(operation_id: str):
        return studio.operations.get(operation_id)

    @app.post("/api/v1/operations/{operation_id}:cancel")
    async def cancel_operation(operation_id: str):
        return studio.operations.cancel(operation_id)

    @app.get("/api/v1/operations/{operation_id}/events")
    async def operation_events(
        operation_id: str,
        request: Request,
        after: int = Query(default=0, ge=0),
    ):
        last = request.headers.get("Last-Event-ID")
        cursor = int(last) if last and last.isdigit() else after
        return _sse(studio.operations.events(operation_id, after=cursor))

    @app.get("/api/v1/capabilities")
    async def capabilities(kind: str | None = None, query: str = ""):
        return {"items": studio.list_capabilities(kind=kind, query=query)}

    @app.get("/api/v1/catalog/resources")
    async def catalog_resources(
        kind: Literal["model", "tool", "mcp", "skill"] | None = None,
        query: str = "",
        source: Literal["builtin", "local", "market"] | None = None,
        status: str | None = None,
        installed: bool | None = None,
        limit: int = Query(default=50, ge=1, le=200),
    ):
        return {
            "items": studio.catalog.list(
                kind=kind,
                query=query,
                source=source,
                status=status,
                installed=installed,
                limit=limit,
            ),
            "nextCursor": None,
        }

    @app.get("/api/v1/catalog/resources/{resource_id}")
    async def get_catalog_resource(resource_id: str):
        return studio.catalog.get(resource_id)

    @app.post("/api/v1/catalog/model-profiles", status_code=201)
    async def create_model_profile(payload: ModelProfileCreateRequest):
        return studio.catalog.create_model_profile(
            name=payload.name,
            display_name=payload.display_name,
            version=payload.version,
            description=payload.description,
            spec=payload.spec,
        )

    @app.post("/api/v1/catalog/mcp-servers", status_code=201)
    async def create_mcp_resource(payload: MCPResourceCreateRequest):
        return studio.catalog.create_mcp_server(
            display_name=payload.display_name,
            description=payload.description,
            server=payload.server,
        )

    @app.post("/api/v1/catalog/mcp-servers/{resource_id}:probe")
    async def probe_mcp_resource(
        resource_id: str,
        timeout_seconds: int = Query(default=10, alias="timeoutSeconds", ge=1, le=60),
    ):
        descriptor = studio.catalog.get(resource_id)
        if descriptor.kind != "mcp":
            raise StudioError(
                "RESOURCE_KIND_INVALID",
                "该 Resource 不是 MCP Server",
                status_code=422,
                details={"resourceId": resource_id},
            )
        server_payload = {
            key: value for key, value in descriptor.contract.items() if key != "discoveredTools"
        }
        server = MCPServerRef.model_validate(server_payload)
        try:
            result = await studio.runtime.mcp_runtime.probe(
                server,
                timeout_seconds=timeout_seconds,
            )
        except StudioError as exc:
            studio.catalog.mark_probe_failed(resource_id, code=exc.code)
            raise
        return studio.catalog.save_probe(resource_id, result=result)

    @app.post("/api/v1/catalog/tools", status_code=201)
    async def create_tool_resource(payload: ToolResourceCreateRequest):
        return studio.catalog.create_tool(
            display_name=payload.display_name,
            category=payload.category,
            contract=payload.contract,
        )

    @app.post("/api/v1/catalog/skills:import", status_code=201)
    async def import_skill(file: UploadFile = File(...)):
        content = await file.read(50 * 1024 * 1024 + 1)
        return studio.catalog.import_skill_zip(
            content,
            filename=file.filename or "skill.zip",
        )

    @app.post("/api/v1/tool-schemas:validate")
    async def validate_tool_schema(payload: ToolSchemaValidationRequest):
        try:
            Draft202012Validator.check_schema(payload.schema_definition)
        except SchemaError as exc:
            return {
                "valid": False,
                "diagnostics": [
                    {
                        "code": "TOOL_SCHEMA_INVALID",
                        "message": exc.message,
                        "path": list(exc.path),
                    }
                ],
            }
        errors = sorted(
            Draft202012Validator(payload.schema_definition).iter_errors(payload.sample),
            key=lambda error: list(error.absolute_path),
        )
        return {
            "valid": not errors,
            "diagnostics": [
                {
                    "code": "TOOL_SAMPLE_INVALID",
                    "message": error.message,
                    "path": list(error.absolute_path),
                }
                for error in errors
            ],
        }

    @app.post("/api/v1/tool-policies:preview")
    async def preview_tool_policy(payload: ToolPolicyPreviewRequest):
        tools, permissions = studio.catalog.policy_preview(payload.bindings)
        return {
            "tools": tools,
            "allowedPermissions": permissions,
        }

    @app.post("/api/v1/mcp-servers:probe")
    async def probe_mcp_server(
        payload: MCPServerRef,
        timeout_seconds: int = Query(default=10, alias="timeoutSeconds", ge=1, le=60),
    ):
        return await studio.runtime.mcp_runtime.probe(
            payload,
            timeout_seconds=timeout_seconds,
        )

    @app.post("/api/v1/secret-references:check")
    async def check_secret_reference(payload: SecretReferenceCheckRequest):
        status = studio.credentials.status(payload.ref)
        return {
            "exists": status["configured"],
            "scheme": payload.ref.partition("://")[0],
            "source": status["source"],
        }

    @app.get("/api/v1/credentials/{credential_name}")
    async def get_credential_status(credential_name: str):
        return studio.credentials.status(f"env://{credential_name}")

    @app.put("/api/v1/credentials/{credential_name}")
    async def put_credential(
        credential_name: str,
        payload: CredentialPutRequest,
    ):
        return studio.credentials.put_session(
            credential_name,
            payload.value.get_secret_value(),
        )

    @app.delete("/api/v1/credentials/{credential_name}")
    async def delete_credential(credential_name: str):
        return studio.credentials.delete_session(credential_name)

    @app.post("/api/v1/model-profiles/{resource_id}:test")
    async def test_model_profile(resource_id: str):
        return await studio.test_model_profile(resource_id)

    return app


def _is_local_origin(origin: str) -> bool:
    parsed = urlparse(origin)
    return parsed.scheme in {"http", "https"} and (parsed.hostname or "").lower() in _LOCAL_HOSTS


def _error_response(exc: StudioError, request: Request) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.as_dict(request_id=request_id),
        headers={"X-Request-Id": request_id or ""},
    )


def _require_idempotency_key(value: str | None) -> str:
    if not value or not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", value):
        raise StudioError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "异步创建接口必须提供合法 Idempotency-Key",
            status_code=400,
        )
    return value


def _parse_revision(value: str | None) -> int:
    if not value:
        raise StudioError(
            "AGENT_REVISION_REQUIRED",
            "更新 Agent 必须提供 If-Match revision",
            status_code=428,
        )
    try:
        return int(value.strip().strip('"'))
    except ValueError as exc:
        raise StudioError(
            "AGENT_REVISION_INVALID",
            "If-Match 必须是整数 revision",
            status_code=400,
        ) from exc


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise StudioError(
            "REQUEST_VALIDATION_FAILED",
            "分页游标必须是整数",
            status_code=422,
        ) from exc


def _sse(events: list[Any]) -> StreamingResponse:
    def render():
        for event in events:
            data = json.dumps(
                event.data,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            yield f"id: {event.id}\nevent: {event.type}\ndata: {data}\n\n"

    return StreamingResponse(
        render(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )

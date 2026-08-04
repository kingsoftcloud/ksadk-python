"""FastAPI application for the loopback-only AgentKit Studio control plane."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Header, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ksadk.studio.api_catalog_routes import register_catalog_routes
from ksadk.studio.api_contracts import (
    AuthoringCommitRequest,
    BuildRequest,
    ConversationAuthoringRequest,
    CreateAgentRequest,
    EvaluationRequest,
    ProjectInspectRequest,
    QuickAuthoringRequest,
    RollbackRequest,
    RunRequest,
    SessionExchangeRequest,
    ValidationRequest,
    WorkspaceOpenRequest,
)
from ksadk.studio.api_helpers import (
    error_response as _error_response,
)
from ksadk.studio.api_helpers import (
    is_local_origin,
)
from ksadk.studio.api_helpers import (
    optional_int as _optional_int,
)
from ksadk.studio.api_helpers import (
    parse_revision as _parse_revision,
)
from ksadk.studio.api_helpers import (
    require_idempotency_key as _require_idempotency_key,
)
from ksadk.studio.api_helpers import (
    responses_input as _responses_input,
)
from ksadk.studio.api_helpers import (
    responses_session_id as _responses_session_id,
)
from ksadk.studio.api_helpers import (
    sse as _sse,
)
from ksadk.studio.codex_manifest import CodexAgentManifest
from ksadk.studio.contracts import (
    AgentBindings,
    AgentSpec,
    AgentTemplateComposeRequest,
    DeploymentRequest,
    OperationStatus,
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
    '<link id="agentkitStudioSharedChatTheme" rel="stylesheet" href="/static/shared-chat.css">'
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
        if origin and not is_local_origin(origin, local_hosts=_LOCAL_HOSTS):
            return _error_response(
                StudioError(
                    "LOCAL_ORIGIN_FORBIDDEN",
                    "请求 Origin 不是本地 Studio",
                    status_code=403,
                ),
                request,
            )
        content_length = request.headers.get("Content-Length")
        large_upload_paths = {
            "/api/v1/catalog/skills:import": 52 * 1024 * 1024,
            "/api/v1/authoring/imports:inspect": 102 * 1024 * 1024,
        }
        request_limit = large_upload_paths.get(request.url.path, 2 * 1024 * 1024)
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
            (studio_api and request.url.path not in _PUBLIC_API_PATHS) or shared_web_api
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
            if request.url.path.startswith(("/api/", "/v1/"))
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

    async def runtime_model_catalog():
        api_base = os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE")
        current_model = os.getenv("OPENAI_MODEL_NAME") or os.getenv("MODEL_NAME")
        try:
            api_key = studio.credentials.resolve("env://OPENAI_API_KEY")
        except StudioError:
            api_key = None
        return await studio.catalog.discover_provider_models(
            api_base=api_base,
            api_key=api_key,
            current_model=current_model,
        )

    @app.get("/v1/models")
    async def openai_models():
        models, source = await runtime_model_catalog()
        return {
            "object": "list",
            "data": [dict(item.contract.get("metadata") or {"id": item.name}) for item in models],
            "current": os.getenv("OPENAI_MODEL_NAME") or os.getenv("MODEL_NAME"),
            "source": source,
        }

    @app.post("/v1/responses")
    async def openai_responses(payload: dict[str, Any]):
        metadata = payload.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        agent_id = str(metadata.get("agent_id") or metadata.get("agentId") or "") or None
        session_id = _responses_session_id(payload, bridge=shared_web)
        response_id = str(
            metadata.get("invocation_id")
            or metadata.get("invocationId")
            or f"resp_{secrets.token_hex(12)}"
        )
        bridge_payload: dict[str, Any] = {
            "AgentId": shared_web.resolve_agent_id(agent_id),
            "SessionId": session_id or f"ses_{secrets.token_hex(12)}",
            "InvocationId": response_id,
            "Model": str(payload.get("model") or ""),
            "ResponsesInput": _responses_input(payload.get("input")),
        }
        bridge_payload["Model"] = shared_web.select_model(
            bridge_payload["AgentId"],
            bridge_payload["Model"],
        )
        if bool(payload.get("stream")):
            return StreamingResponse(
                shared_web.stream_run(bridge_payload),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )
        return await shared_web.invoke_response(bridge_payload)

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
                data = shared_web.bootstrap(shared_web.resolve_agent_id(requested_agent_id or None))
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
                data = shared_web.list_session_events(str(payload.get("SessionId") or ""))
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
                data = shared_web.cancel_run(str(payload.get("InvocationId") or ""))
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
                "runtimeRegistry": True,
                "runtimeTypes": ["codex", "adk", "langgraph"],
                "evaluation": True,
                "deployment": True,
                "cloudRebuild": False,
                "sharedChat": shared_static_root is not None,
            },
            "runtimes": studio.runtime_catalog(),
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

    @app.get("/api/v1/codex/manifest")
    async def get_codex_manifest():
        return studio.codex_manifest_state()

    @app.put("/api/v1/codex/manifest")
    async def put_codex_manifest(payload: CodexAgentManifest):
        return studio.save_codex_manifest(payload)

    @app.post("/api/v1/codex/manifest:validate")
    async def validate_codex_manifest(_payload: CodexAgentManifest):
        return {"valid": True, "diagnostics": []}

    @app.post("/api/v1/codex/builds", status_code=202)
    async def create_codex_build(
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        return studio.submit_codex_build(idempotency_key=_require_idempotency_key(idempotency_key))

    @app.get("/api/v1/codex/builds")
    async def list_codex_builds():
        return {"items": studio.codex_builds.list()}

    @app.get("/api/v1/codex/builds/{build_id}")
    async def get_codex_build(build_id: str):
        return studio.codex_builds.get(build_id)

    @app.post("/api/v1/codex/builds/{build_id}/runs", status_code=202)
    async def create_codex_run(
        build_id: str,
        payload: RunRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        if payload.environment != "local":
            raise StudioError(
                "RUN_ENVIRONMENT_UNSUPPORTED",
                "Codex Studio 只支持只读本地运行",
                status_code=422,
            )
        return studio.submit_codex_run(
            build_id,
            payload.input.content,
            session_id=payload.session_id,
            model=payload.model,
            idempotency_key=_require_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/builds/{build_id}/run:stream")
    async def stream_run(
        build_id: str,
        payload: RunRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        key = _require_idempotency_key(idempotency_key)
        if payload.environment != "local":
            raise StudioError(
                "RUN_ENVIRONMENT_UNSUPPORTED",
                "Codex Studio 只支持只读本地运行",
                status_code=422,
            )

        queue: asyncio.Queue = asyncio.Queue()

        def observe(event):
            queue.put_nowait(event)

        # The Operation owns the runtime task.  The SSE response is only one
        # observer, so refreshing or switching chats cannot cancel the Run.
        operation = studio.submit_studio_run(
            build_id,
            payload.input.content,
            session_id=payload.session_id,
            model=payload.model,
            idempotency_key=key,
            on_event=observe,
        )

        async def render():
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.1)
                except TimeoutError:
                    event = None
                if event is not None:
                    data = json.dumps(
                        event.data,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    yield f"id: {event.id}\nevent: {event.type}\ndata: {data}\n\n"
                current = studio.operations.get(operation.id)
                if (
                    current.status
                    in {
                        OperationStatus.SUCCEEDED,
                        OperationStatus.FAILED,
                        OperationStatus.CANCELLED,
                        OperationStatus.INTERRUPTED,
                    }
                    and queue.empty()
                ):
                    if current.status == OperationStatus.FAILED:
                        data = json.dumps(current.error or {}, ensure_ascii=False)
                        yield f"event: run.failed\ndata: {data}\n\n"
                    break

        return StreamingResponse(
            render(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/v1/agents", status_code=201)
    async def create_agent(payload: CreateAgentRequest):
        return studio.create_studio_agent(
            agent_id=payload.id,
            name=payload.name,
            description=payload.description,
            template=payload.template,
            spec=payload.spec,
            runtime=payload.runtime,
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

    @app.post("/api/v1/authoring/quick", status_code=201)
    async def quick_author_agent(payload: QuickAuthoringRequest):
        return studio.create_authored_agent(
            name=payload.name,
            slug=payload.slug,
            runtime_type=payload.runtime_type,
            template=payload.template,
            description=payload.description,
            spec=payload.spec,
        )

    @app.post("/api/v1/authoring/conversations:compose")
    async def compose_authoring_conversation(payload: ConversationAuthoringRequest):
        return await studio.compose_agent_conversation(
            messages=[item.model_dump(mode="json") for item in payload.messages],
            model_profile_id=payload.model_profile_id,
        )

    @app.post("/api/v1/authoring/imports:inspect")
    async def inspect_agent_import(file: UploadFile = File(...)):
        content = await file.read(100 * 1024 * 1024 + 1)
        return studio.inspect_agent_import(
            content,
            filename=file.filename or "agent.yaml",
        )

    @app.post(
        "/api/v1/authoring/imports/{inspection_token}:commit",
        status_code=201,
    )
    async def commit_agent_import(
        inspection_token: str,
        payload: AuthoringCommitRequest,
    ):
        return studio.commit_agent_import(
            inspection_token,
            name=payload.name,
            slug=payload.slug,
        )

    @app.post("/api/v1/authoring/projects:inspect")
    async def inspect_agent_project(payload: ProjectInspectRequest):
        return studio.inspect_agent_project(payload.path)

    @app.post(
        "/api/v1/authoring/projects/{inspection_token}:commit",
        status_code=201,
    )
    async def commit_agent_project(
        inspection_token: str,
        payload: AuthoringCommitRequest,
    ):
        return studio.commit_agent_project(
            inspection_token,
            name=payload.name,
            slug=payload.slug,
            model_profile_id=payload.model_profile_id,
        )

    @app.get("/api/v1/agents")
    async def list_agents(
        limit: int = Query(default=50, ge=1, le=200),
        query: str = "",
    ):
        return {
            "items": studio.list_agents(query=query, limit=limit),
            "nextCursor": None,
        }

    @app.get("/api/v1/agents/{agent_id}")
    async def get_agent(agent_id: str):
        return studio.agent_detail(agent_id)

    @app.get("/api/v1/agents/{agent_id}/models")
    async def get_agent_models(agent_id: str):
        return shared_web.list_models(shared_web.resolve_agent_id(agent_id))

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
        return studio.update_studio_agent(
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
        return studio.update_studio_agent_bindings(
            agent_id,
            bindings,
            expected_revision=revision,
        )

    @app.delete("/api/v1/agents/{agent_id}", status_code=204)
    async def delete_agent(agent_id: str, purge: bool = False):
        studio.delete_studio_agent(agent_id, purge=purge)
        return Response(status_code=204)

    @app.post("/api/v1/agents/{agent_id}/validations")
    async def validate_agent(agent_id: str, payload: ValidationRequest):
        return studio.validate_studio_agent(
            agent_id,
            revision=payload.revision,
            level=payload.level,
        )

    @app.post("/api/v1/agents/{agent_id}/builds", status_code=202)
    async def create_build(
        agent_id: str,
        payload: BuildRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        return studio.submit_studio_build(
            agent_id,
            revision=payload.revision,
            idempotency_key=_require_idempotency_key(idempotency_key),
        )

    @app.get("/api/v1/builds/{build_id}")
    async def get_build(build_id: str):
        return studio.build_view(build_id)

    @app.get("/api/v1/builds/{build_id}/manifest")
    async def get_build_manifest(build_id: str):
        try:
            record = studio.codex_builds.get(build_id)
        except StudioError as exc:
            if exc.status_code != 404:
                raise
        else:
            return {
                "agentengine": studio.codex_manifests.load(record.agent_name).manifest,
                "runtimeLock": record.runtime_lock,
                "manifestSha256": record.manifest_sha256,
            }
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
        return studio.submit_studio_run(
            build_id,
            payload.input.content,
            session_id=payload.session_id,
            model=payload.model,
            idempotency_key=_require_idempotency_key(idempotency_key),
        )

    @app.get("/api/v1/runs/{run_id}")
    async def get_run(run_id: str):
        return studio.event_store.get(run_id)

    @app.delete("/api/v1/sessions/{session_id}", status_code=204)
    async def delete_studio_session(session_id: str):
        studio.delete_session(session_id)
        return Response(status_code=204)

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

    @app.get("/api/v1/traces")
    async def list_traces(
        agent_id: str | None = Query(default=None, alias="agentId"),
        status: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=1000),
    ):
        return {
            "items": studio.event_store.list_traces(
                agent_id=agent_id,
                status=status,
                limit=limit,
            )
        }

    @app.get("/api/v1/traces/{trace_id}/otlp")
    async def get_trace_otlp(trace_id: str):
        return studio.event_store.trace_otlp(trace_id)

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

    register_catalog_routes(
        app,
        studio,
        runtime_model_catalog=runtime_model_catalog,
    )

    return app

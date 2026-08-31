"""FastAPI application for the loopback-only AgentKit Studio control plane."""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import os
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Header, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from ksadk.api.client import AgentEngineAPIError
from ksadk.conversations.contracts import (
    ConversationAttachmentPart,
    ConversationTextPart,
    validate_conversation_input,
)
from ksadk.conversations.projector import project_conversation_item
from ksadk.events.canonical import parse_runtime_event_lenient
from ksadk.scheduler.contracts import ScheduledTask
from ksadk.studio.api_catalog_routes import register_catalog_routes
from ksadk.studio.api_contracts import (
    AgentScheduleRequest,
    AuthoringCommitRequest,
    BuildRequest,
    CloudAgentVersionRollbackRequest,
    CloudChatInteractionSubmitRequest,
    CloudChatMessageRequest,
    ContextPreviewRequest,
    ConversationAuthoringRequest,
    ConversationTurnRequest,
    CreateAgentRequest,
    ImportRootRequest,
    InteractionSubmitRequest,
    ProjectInspectRequest,
    PromptCompileRequest,
    QuickAuthoringRequest,
    RollbackRequest,
    RunRequest,
    SessionExchangeRequest,
    StudioEvaluationCreate,
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
from ksadk.studio.api_memory_routes import register_memory_routes
from ksadk.studio.api_plugin_routes import register_plugin_routes
from ksadk.studio.codex_manifest import CodexAgentManifest
from ksadk.studio.contracts import (
    AgentAppearance,
    AgentBindings,
    AgentSpec,
    AgentTemplateComposeRequest,
    DeploymentRequest,
    OperationStatus,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.service import StudioService
from ksadk.studio.shared_web import StudioSharedWebBridge

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_PUBLIC_API_PATHS = {
    "/api/v1/system/health",
    "/api/v1/system/session",
}
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testserver"}

# RuntimeEvent/v2 itself is snake_case.  Some pre-existing cloud SessionEvent
# projections, however, serialized the *envelope* with the REST camelCase
# convention.  This is a narrow transport normalizer, not a provider payload
# rewrite: only fields owned by the frozen RuntimeEvent envelope/content
# contracts are translated before strict parsing.
_RUNTIME_EVENT_WIRE_ALIASES = {
    "schemaVersion": "schema_version",
    "eventId": "event_id",
    "runId": "run_id",
    "runSeq": "run_seq",
    "scopeId": "scope_id",
    "parentScopeId": "parent_scope_id",
    "eventType": "event_type",
    "itemId": "item_id",
    "itemKind": "item_kind",
    "interactionId": "interaction_id",
    "interactionKind": "interaction_kind",
    "continuationId": "continuation_id",
    "resumeAttemptId": "resume_attempt_id",
    "outputRefs": "output_refs",
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "totalTokens": "total_tokens",
    "cachedTokens": "cached_tokens",
    "reasoningTokens": "reasoning_tokens",
}
_SOURCE_WIRE_ALIASES = {
    "nativeEventId": "native_event_id",
    "nativeCursor": "native_cursor",
    "nativeRunId": "native_run_id",
    "nativeItemId": "native_item_id",
}
_CONTENT_WIRE_ALIASES = {
    "contentType": "content_type",
    "partId": "part_id",
    "callId": "call_id",
    "artifactId": "artifact_id",
    "mimeType": "mime_type",
    "isError": "is_error",
}


def _normalize_cloud_runtime_event_wire(value: dict[str, Any]) -> dict[str, Any]:
    """Normalize only historic REST casing at the RuntimeEvent boundary."""

    normalized = {
        _RUNTIME_EVENT_WIRE_ALIASES.get(key, key): item for key, item in value.items()
    }
    source = normalized.get("source")
    if isinstance(source, dict):
        normalized["source"] = {
            _SOURCE_WIRE_ALIASES.get(key, key): item for key, item in source.items()
        }

    def normalize_content(content: Any) -> Any:
        if not isinstance(content, dict):
            return content
        return {_CONTENT_WIRE_ALIASES.get(key, key): item for key, item in content.items()}

    normalized["update"] = normalize_content(normalized.get("update"))
    for key in ("initial", "snapshot"):
        snapshot = normalized.get(key)
        if not isinstance(snapshot, dict):
            continue
        parts = snapshot.get("parts")
        if isinstance(parts, list):
            normalized[key] = {
                **snapshot,
                "parts": [normalize_content(part) for part in parts],
            }
    return normalized


def _cloud_event_conversation_item(
    event: dict[str, Any], *, session_id: str
) -> dict[str, Any] | None:
    """Project a complete cloud RuntimeEvent without inventing a browser item.

    Cloud SessionEvent history has a compatibility envelope around the runtime
    payload.  Keep the source event untouched, but add the same typed
    ConversationItem that local Studio streams use whenever the nested event
    validates against RuntimeEvent/v2.  A malformed or older envelope is still
    observable to diagnostics, never guessed into an actionable chat card.
    """

    payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
    content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
    candidates = (
        content.get("runtime_event"),
        content.get("runtimeEvent"),
        payload.get("runtime_event"),
        payload.get("runtimeEvent"),
        event.get("runtime_event"),
        event.get("runtimeEvent"),
    )
    raw_event = next((candidate for candidate in candidates if isinstance(candidate, dict)), None)
    if raw_event is None:
        return None
    try:
        runtime_event = parse_runtime_event_lenient(
            _normalize_cloud_runtime_event_wire(raw_event)
        )
        item = project_conversation_item(
            runtime_event,  # type: ignore[arg-type]
            session_id=session_id,
            run_id=runtime_event.run_id,
        )
    except (TypeError, ValueError):
        return None
    return item.model_dump(by_alias=True, exclude_none=True, mode="json")


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
            await studio.start()
            await studio.run_service.recover_interrupted()
            await studio.scheduler.start_if_available()
            yield
        finally:
            await studio.scheduler.stop()
            await studio.aclose()
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

    def _stream_studio_run(
        build_id: str,
        *,
        user_input: str,
        session_id: str | None,
        model: str | None,
        sandbox: str | None,
        reasoning_effort: str | None = None,
        approval_mode: str | None = None,
        collaboration_mode: str | None = None,
        goal_objective: str | None = None,
        runtime_input: Any = None,
        idempotency_key: str,
    ) -> StreamingResponse:
        """Create an observer-only SSE response for one durable Studio run."""

        queue: asyncio.Queue = asyncio.Queue()

        def observe(event):
            queue.put_nowait(event)

        # The Operation owns the runtime task. The SSE response is only one
        # observer, so refreshing or switching chats cannot cancel the Run.
        operation = studio.submit_studio_run(
            build_id,
            user_input,
            session_id=session_id,
            model=model,
            sandbox=sandbox,
            reasoning_effort=reasoning_effort,
            approval_mode=approval_mode,
            collaboration_mode=collaboration_mode,
            goal_objective=goal_objective,
            runtime_input=runtime_input,
            idempotency_key=idempotency_key,
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

    static_root = Path(__file__).with_name("static")
    _studio_startup_epoch = str(int(time.time()))
    shared_web = StudioSharedWebBridge(studio)
    app.state.shared_web_bridge = shared_web
    app.mount("/static", StaticFiles(directory=static_root), name="studio-static")

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
            "/api/v1/evaluation-files": 2 * 1024 * 1024 + 64 * 1024,
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
        responses_api = request.url.path == "/v1/responses" or request.url.path.startswith(
            "/v1/responses/"
        )
        if security_enabled and (
            (studio_api and request.url.path not in _PUBLIC_API_PATHS)
            or shared_web_api
            or responses_api
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
            if (studio_api or responses_api) and request.method in _WRITE_METHODS:
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
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(StudioError)
    async def studio_error_handler(request: Request, exc: StudioError):
        return _error_response(exc, request)

    @app.exception_handler(AgentEngineAPIError)
    async def agentengine_api_error_handler(request: Request, exc: AgentEngineAPIError):
        http_status = exc.details.get("http_status", 502)
        status_code = (
            http_status if isinstance(http_status, int) and 400 <= http_status < 600 else 502
        )
        return _error_response(
            StudioError(
                "CLOUD_API_ERROR",
                exc.message or "云端服务返回错误",
                status_code=status_code,
                details={"api_code": exc.code, "raw_code": exc.raw_code},
            ),
            request,
        )

    @app.exception_handler(Exception)
    async def generic_error_handler(request: Request, exc: Exception):
        # Catch-all for bare Exception raises from the API client (e.g. HTTP
        # errors, JSON parse failures) so they surface as structured JSON
        # instead of opaque 500s.  StudioError and AgentEngineAPIError are
        # handled by their own dedicated handlers above.
        if isinstance(exc, StudioError):
            return _error_response(exc, request)
        return _error_response(
            StudioError(
                "INTERNAL_ERROR",
                "Studio 内部错误，请根据请求 ID 查看本地诊断日志。",
                status_code=500,
            ),
            request,
        )

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
        html = path.read_text(encoding="utf-8")
        # Inject a startup-scoped version so a restarted Studio with a rebuilt
        # bundle always wins over a stale browser tab.  The bundle filenames
        # are already content-hashed; this only defeats cached index.html.
        if "?v=" not in html:
            import re

            def _add_version(match: "re.Match[str]") -> str:
                attr, path_part = match.group(1), match.group(2)
                return f'{attr}="/static/assets/{path_part}?v={_studio_startup_epoch}"'

            html = re.sub(
                r'(src|href)="/static/assets/([^"]+)"',
                _add_version,
                html,
            )
        response = Response(content=html, media_type="text/html")
        response.headers["Cache-Control"] = "no-store"
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
        requested_approval_mode = (
            str(metadata.get("approval_mode") or metadata.get("approvalMode") or "").strip().lower()
        )
        if requested_approval_mode and requested_approval_mode not in {"ask", "risk", "full"}:
            raise StudioError(
                "APPROVAL_MODE_INVALID",
                "批准模式必须是 ask、risk 或 full",
                status_code=422,
                field="metadata.approval_mode",
            )
        collaboration_mode = (
            str(metadata.get("collaboration_mode") or metadata.get("collaborationMode") or "")
            .strip()
            .lower()
        )
        if collaboration_mode and collaboration_mode not in {"default", "plan"}:
            raise StudioError(
                "COLLABORATION_MODE_INVALID",
                "协作模式必须是 default 或 plan",
                status_code=422,
                field="metadata.collaboration_mode",
            )
        goal_objective = str(
            metadata.get("goal_objective") or metadata.get("goalObjective") or ""
        ).strip()
        reasoning = payload.get("reasoning")
        reasoning = reasoning if isinstance(reasoning, dict) else {}
        reasoning_effort = str(reasoning.get("effort") or "").strip().lower()
        if reasoning_effort and reasoning_effort not in {"low", "medium", "high"}:
            raise StudioError(
                "REASONING_EFFORT_INVALID",
                "推理强度必须是 low、medium 或 high",
                status_code=422,
                field="reasoning.effort",
            )
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
            "ApprovalMode": requested_approval_mode,
            "CollaborationMode": collaboration_mode,
            "GoalObjective": goal_objective,
            "ReasoningEffort": reasoning_effort,
            "ModelExplicit": bool(str(payload.get("model") or "").strip()),
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

    @app.post("/v1/responses/{response_id}/cancel")
    async def cancel_openai_response(response_id: str):
        result = shared_web.cancel_run(response_id)
        return {
            "id": response_id,
            "object": "response",
            "status": "cancelled" if result["Cancelled"] else "not_found",
        }

    @app.post("/v1/responses/{response_id}:pause", status_code=202)
    async def pause_openai_response(response_id: str):
        return await shared_web.pause_run(response_id)

    @app.post("/v1/responses/{response_id}:resume", status_code=202)
    async def resume_openai_response(response_id: str):
        return await shared_web.resume_run(response_id)

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
                data = await shared_web.list_messages(
                    str(payload.get("SessionId") or ""),
                    after_seq_id=_optional_int(payload.get("AfterSeqId")),
                    before_seq_id=_optional_int(payload.get("BeforeSeqId")),
                    limit=int(payload.get("Limit") or 50),
                )
            elif action == "ListSessionEvents":
                data = await shared_web.list_session_events(str(payload.get("SessionId") or ""))
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
            "operationScope": studio.deployment_operation_scope(),
            "features": {
                "build": True,
                "run": True,
                "runtimeRegistry": True,
                "runtimeTypes": ["codex", "adk", "langgraph"],
                "evaluation": True,
                "deployment": True,
                "cloudRebuild": False,
                "reactChat": True,
                "scheduler": studio.scheduler.availability(),
            },
            "runtimes": studio.runtime_catalog(),
            "importableProject": studio.detect_importable_project(),
        }

    @app.get("/api/v1/system/settings")
    async def get_settings():
        return studio.get_settings()

    @app.put("/api/v1/system/settings")
    async def update_settings(payload: dict[str, Any]):
        return studio.update_settings(payload)

    @app.get("/api/v1/schedules")
    async def list_schedules():
        return {
            "items": studio.scheduler.list_tasks(),
            "availability": studio.scheduler.availability(),
        }

    @app.post("/api/v1/schedules", status_code=201)
    async def create_schedule(payload: ScheduledTask):
        studio.validate_schedule_build(
            payload.target.agent_version_ref,
            agent_id=payload.target.agent_id,
        )
        return studio.scheduler.create_task(payload)

    @app.get("/api/v1/schedules/{task_id}")
    async def get_schedule(task_id: str):
        return studio.scheduler.get_task(task_id)

    @app.put("/api/v1/schedules/{task_id}")
    async def update_schedule(task_id: str, payload: ScheduledTask):
        studio.validate_schedule_build(
            payload.target.agent_version_ref,
            agent_id=payload.target.agent_id,
        )
        return studio.scheduler.update_task(task_id, payload)

    @app.delete("/api/v1/schedules/{task_id}", status_code=204)
    async def delete_schedule(task_id: str):
        studio.scheduler.delete_task(task_id)
        return Response(status_code=204)

    @app.post("/api/v1/schedules/{task_id}:run", status_code=202)
    async def run_schedule_now(task_id: str):
        return await studio.scheduler.run_now(task_id)

    @app.get("/api/v1/schedules/{task_id}/occurrences")
    async def list_schedule_occurrences(task_id: str, limit: int = Query(default=50, ge=1, le=200)):
        return {"items": studio.scheduler.list_occurrences(task_id, limit=limit)}

    @app.get("/api/v1/schedule-occurrences")
    async def list_all_schedule_occurrences(
        limit: int = Query(default=200, ge=1, le=500),
    ):
        return {"items": studio.scheduler.list_all_occurrences(limit=limit)}

    @app.get("/api/v1/agents/{agent_id}/schedules")
    async def list_agent_schedules(agent_id: str):
        return {
            "items": studio.list_agent_schedules(agent_id),
            "availability": studio.scheduler.availability(),
        }

    @app.post("/api/v1/agents/{agent_id}/schedules", status_code=201)
    async def create_agent_schedule(agent_id: str, payload: AgentScheduleRequest):
        return await studio.create_agent_schedule(
            agent_id,
            display_name=payload.display_name,
            prompt=payload.prompt,
            schedule=payload.schedule,
            enabled=payload.enabled,
            continuity=payload.continuity,
            session_id=payload.session_id,
        )

    @app.get("/api/v1/agents/{agent_id}/schedules/{task_id}")
    async def get_agent_schedule(agent_id: str, task_id: str):
        return studio.get_agent_schedule(agent_id, task_id)

    @app.put("/api/v1/agents/{agent_id}/schedules/{task_id}")
    async def update_agent_schedule(
        agent_id: str,
        task_id: str,
        payload: AgentScheduleRequest,
    ):
        return studio.update_agent_schedule(
            agent_id,
            task_id,
            display_name=payload.display_name,
            prompt=payload.prompt,
            schedule=payload.schedule,
            enabled=payload.enabled,
            continuity=payload.continuity,
            session_id=payload.session_id,
        )

    @app.delete("/api/v1/agents/{agent_id}/schedules/{task_id}", status_code=204)
    async def delete_agent_schedule(agent_id: str, task_id: str):
        studio.get_agent_schedule(agent_id, task_id)
        studio.scheduler.delete_task(task_id)
        return Response(status_code=204)

    @app.post("/api/v1/agents/{agent_id}/schedules/{task_id}:run", status_code=202)
    async def run_agent_schedule_now(agent_id: str, task_id: str):
        return await studio.run_agent_schedule_now(agent_id, task_id)

    @app.get("/api/v1/agents/{agent_id}/schedules/{task_id}/occurrences")
    async def list_agent_schedule_occurrences(
        agent_id: str,
        task_id: str,
        limit: int = Query(default=50, ge=1, le=200),
    ):
        studio.get_agent_schedule(agent_id, task_id)
        return {"items": studio.scheduler.list_occurrences(task_id, limit=limit)}

    @app.post("/api/v1/workspaces:open")
    async def open_workspace(payload: WorkspaceOpenRequest):
        # This endpoint only reconnects to the daemon's already-bound root.  Do
        # not resolve or otherwise touch a caller-provided filesystem path.
        requested = os.path.normcase(os.path.abspath(os.path.expanduser(payload.path)))
        bound_root = os.path.normcase(str(studio.workspace.root))
        if requested != bound_root:
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
        return _stream_studio_run(
            build_id,
            user_input=payload.input.content,
            session_id=payload.session_id,
            model=payload.model,
            sandbox=payload.sandbox,
            idempotency_key=key,
        )

    @app.get("/api/v1/builds/{build_id}/conversation-surface")
    async def get_conversation_surface(
        build_id: str,
        session_id: str = Query(alias="sessionId", min_length=1, max_length=256),
    ):
        return studio.conversation_surface(build_id, session_id=session_id)

    @app.get("/api/v1/agents/{agent_id}/conversation-surface")
    async def get_agent_conversation_surface(
        agent_id: str,
        session_id: str = Query(alias="sessionId", min_length=1, max_length=256),
    ):
        """Resolve the immutable Build and its composer contract atomically.

        The browser selects an Agent, not a mutable Draft or a Build id.  This
        route gives it the same current-Build decision used by chat and local
        Scheduler authoring, so controls cannot be rendered from a stale or
        unrelated Build.
        """

        build = await studio.ensure_current_build(agent_id)
        return {
            "buildId": build.id,
            "surface": studio.conversation_surface(
                build.id,
                session_id=session_id,
            ),
        }

    @app.post("/api/v1/builds/{build_id}/conversation:stream")
    async def stream_conversation_turn(
        build_id: str,
        payload: ConversationTurnRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        key = _require_idempotency_key(idempotency_key)
        conversation_input = payload.input
        if conversation_input.idempotency_key != key:
            raise StudioError(
                "CONVERSATION_IDEMPOTENCY_MISMATCH",
                "ConversationInput 的幂等键必须与请求头一致",
                status_code=422,
            )
        surface = studio.conversation_surface(
            build_id,
            session_id=conversation_input.session_id,
        )
        try:
            validate_conversation_input(surface, conversation_input)
        except ValueError as exc:
            raise StudioError(
                "CONVERSATION_INPUT_UNSUPPORTED",
                "当前 Agent 不支持此会话输入",
                status_code=422,
                details={"reason": str(exc), "surfaceId": surface.surface_id},
            ) from exc
        text_parts = [
            part.text for part in conversation_input.parts if isinstance(part, ConversationTextPart)
        ]
        runtime_parts: list[dict[str, str]] = [
            {"type": "text", "text": value} for value in text_parts
        ]
        attachment_names: list[str] = []
        for part in conversation_input.parts:
            if not isinstance(part, ConversationAttachmentPart):
                continue
            stored = studio.conversation_attachments.resolve(part.attachment_ref)
            if part.media_type.lower() != stored.media_type.lower():
                raise StudioError(
                    "CONVERSATION_ATTACHMENT_METADATA_MISMATCH",
                    "会话附件类型与已上传内容不一致",
                    status_code=422,
                    details={"attachmentRef": part.attachment_ref},
                )
            encoded = base64.b64encode(stored.path.read_bytes()).decode("ascii")
            data_url = f"data:{stored.media_type};base64,{encoded}"
            attachment_names.append(part.name or stored.name)
            if stored.media_type.startswith("image/"):
                runtime_parts.append({"type": "image", "url": data_url})
            else:
                runtime_parts.append(
                    {
                        "type": "input_file",
                        "filename": part.name or stored.name,
                        "file_data": data_url,
                    }
                )
        display_input = "\n".join(text_parts).strip()
        if not display_input:
            display_input = "请处理本轮附件：" + "、".join(attachment_names)
        return _stream_studio_run(
            build_id,
            user_input=display_input,
            session_id=conversation_input.session_id,
            model=conversation_input.model_ref,
            sandbox=payload.sandbox,
            reasoning_effort=conversation_input.reasoning,
            approval_mode=conversation_input.approval_mode,
            collaboration_mode=conversation_input.collaboration_mode,
            goal_objective=conversation_input.goal_objective,
            runtime_input=runtime_parts if attachment_names else None,
            idempotency_key=key,
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

    @app.post("/api/v1/conversation-attachments", status_code=201)
    async def upload_conversation_attachment(file: UploadFile = File(...)):
        content = await file.read(studio.conversation_attachments.MAX_BYTES + 1)
        return studio.conversation_attachments.store(
            content,
            filename=file.filename or "attachment",
            media_type=file.content_type or "application/octet-stream",
        )

    @app.post("/api/v1/assets/agent-avatars", status_code=201)
    async def upload_agent_avatar(request: Request):
        declared_size = request.headers.get("Content-Length")
        if (
            declared_size
            and declared_size.isdigit()
            and int(declared_size) > studio.avatar_assets.MAX_BYTES
        ):
            raise StudioError(
                "AGENT_AVATAR_TOO_LARGE",
                "头像文件不能超过 2 MiB",
                status_code=413,
                field="file",
                details={"maxBytes": studio.avatar_assets.MAX_BYTES},
            )
        return studio.avatar_assets.store(
            await request.body(),
            content_type=request.headers.get("Content-Type", ""),
        )

    @app.get("/api/v1/assets/agent-avatars/{asset_name}")
    async def get_agent_avatar(asset_name: str):
        path, media_type = studio.avatar_assets.get(asset_name)
        return FileResponse(
            path,
            media_type=media_type,
            headers={"Cache-Control": "private, max-age=31536000, immutable"},
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
            runtime_type=payload.runtime_type,
            agent_model_profile_ids=payload.agent_model_profile_ids,
            agent_default_model_profile_id=payload.agent_default_model_profile_id,
            tool_resource_ids=payload.tool_resource_ids,
            mcp_resource_ids=payload.mcp_resource_ids,
            skill_resource_ids=payload.skill_resource_ids,
            request_id=payload.request_id,
        )

    @app.get("/api/v1/authoring/conversations:status/{request_id}")
    async def get_authoring_conversation_status(request_id: str):
        status = studio.conversation_authoring_status(request_id)
        if status is None:
            raise StudioError(
                "AUTHORING_STATUS_NOT_FOUND",
                "未找到该构建请求的阶段记录",
                status_code=404,
            )
        return status

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
        name: str | None = Query(default=None, min_length=1, max_length=128),
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
            name=name,
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

    @app.put("/api/v1/agents/{agent_id}/appearance")
    async def update_agent_appearance(
        agent_id: str,
        appearance: AgentAppearance,
        if_match: str | None = Header(default=None, alias="If-Match"),
    ):
        return studio.update_studio_agent_appearance(
            agent_id,
            appearance,
            expected_revision=_parse_revision(if_match),
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

    @app.post("/api/v1/workspace:import-root", status_code=201)
    async def import_root_project(payload: ImportRootRequest):
        """PR-S6：一键导入根 Framework 项目（方案 §6.1）。"""
        return studio.import_root_project(name=payload.name, slug=payload.slug)

    @app.post("/api/v1/agents/{agent_id}/prompt:compile")
    async def compile_prompt(agent_id: str, payload: PromptCompileRequest):
        """PR-S2：Prompt 编译预览（方案 §6.2）。只读，不写 Session/Trace/Build。"""
        return studio.compile_prompt_preview(
            agent_id,
            request_instructions=payload.request_instructions,
            include_content=payload.include_content,
        )

    @app.post("/api/v1/agents/{agent_id}/context:preview")
    async def preview_context(agent_id: str, payload: ContextPreviewRequest):
        """PR-S2：Context 预览（方案 §6.2）。复用真实 Planner，不调模型。"""
        return await studio.preview_context(
            agent_id,
            user_input=payload.user_input,
            request_instructions=payload.request_instructions,
            simulated_history=[
                {"role": m.role, "content": m.content} for m in payload.simulated_history
            ],
            include_content=payload.include_content,
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

    @app.post("/api/v1/runs/{run_id}:cancel", status_code=202)
    async def cancel_run(run_id: str):
        return await studio.run_service.cancel_run(run_id)

    @app.post("/api/v1/runs/{run_id}:pause", status_code=202)
    async def pause_run(run_id: str):
        return await studio.run_service.pause_run(run_id)

    @app.post("/api/v1/runs/{run_id}:resume", status_code=202)
    async def resume_run(run_id: str):
        return await studio.run_service.resume_run(run_id)

    @app.post("/api/v1/runs/{run_id}/interactions/{interaction_id}:submit")
    async def submit_run_interaction(
        run_id: str,
        interaction_id: str,
        payload: InteractionSubmitRequest,
    ):
        return await studio.run_service.submit_interaction(
            run_id,
            interaction_id,
            name=payload.name,
            data=payload.data,
            expected_revision=payload.expected_revision,
            idempotency_key=payload.idempotency_key,
        )

    @app.get("/api/v1/runs/{run_id}/context")
    async def get_run_context(run_id: str):
        """Runtime Context Evidence：planned/projected/actual + 精度 + ownership。"""
        record = studio.event_store.get(run_id)
        plan = record.context_plan or {}
        evidence = record.prompt_evidence or {}
        return {
            "planId": plan.get("plan_id"),
            "accuracy": evidence.get("accountingAccuracy")
            or plan.get("accounting_accuracy", "opaque"),
            "policyVersion": plan.get("policy_version"),
            "tokensByKind": plan.get("tokens_by_kind", {}),
            "plannedInputTokens": plan.get("planned_input_tokens"),
            "projectedInputTokens": plan.get("projected_input_tokens"),
            "runtimeReportedInputTokens": plan.get("runtime_reported_input_tokens"),
            "selected": plan.get("selected", []),
            "decisions": plan.get("decisions", []),
            "ownership": {
                "promptOwner": evidence.get("promptOwner"),
                "historyOwner": (plan.get("history_owner") if isinstance(plan, dict) else None),
                "integrationMode": evidence.get("integrationMode"),
                "runtimeType": evidence.get("runtimeType"),
                "deploymentMode": evidence.get("deploymentMode"),
                "capabilityHash": evidence.get("capabilityHash"),
            },
            "warnings": [],
        }

    @app.get("/api/v1/runs/{run_id}/prompt")
    async def get_run_prompt(run_id: str, include_content: bool = Query(default=False)):
        """PR-S4：Prompt evidence（方案 §6.3 / §7.3）。section hash/版本，默认不返回正文。"""
        record = studio.event_store.get(run_id)
        evidence = record.prompt_evidence or {}
        result = {
            "contentHash": evidence.get("contentHash"),
            "stablePrefixHash": evidence.get("stablePrefixHash"),
            "sectionHashes": evidence.get("sectionHashes", {}),
            "tokensBySection": evidence.get("tokensBySection", {}),
            "estimatedTokens": evidence.get("estimatedTokens"),
            "sectionCount": evidence.get("sectionCount"),
            "plannedInputTokens": evidence.get("plannedInputTokens"),
            "accountingAccuracy": evidence.get("accountingAccuracy"),
            "runtimeType": evidence.get("runtimeType"),
            "integrationMode": evidence.get("integrationMode"),
        }
        if include_content:
            result["reveal"] = studio.reveal_run_prompt(run_id)
        return result

    @app.get("/api/v1/runs/{run_id}/working-state")
    async def get_run_working_state(run_id: str):
        """PR-S4：Working State evidence（方案 §6.5）。从 checkpoint/read record 读取。"""
        record = studio.event_store.get(run_id)
        return {"workingState": record.working_state}

    @app.delete("/api/v1/sessions/{session_id}", status_code=204)
    async def delete_studio_session(session_id: str):
        await studio.delete_session(session_id)
        return Response(status_code=204)

    @app.get("/api/v1/sessions/{session_id}/events")
    async def session_events(
        session_id: str,
        before_seq_id: int | None = Query(default=None, ge=1, alias="beforeSeqId"),
        invocation_id: str | None = Query(default=None, alias="invocationId"),
        limit: int = Query(default=100, ge=1, le=500),
    ):
        return await studio.trajectory_page(
            session_id,
            before_seq_id=before_seq_id,
            invocation_id=invocation_id,
            limit=limit,
        )

    @app.get("/api/v1/sessions/{session_id}/events/stream")
    async def session_event_stream(
        session_id: str,
        request: Request,
        after_seq_id: int = Query(default=0, ge=0, alias="afterSeqId"),
        invocation_id: str | None = Query(default=None, alias="invocationId"),
    ):
        await studio._require_runtime_session(session_id)
        last = request.headers.get("Last-Event-ID")
        cursor = int(last) if last and last.isdigit() else after_seq_id
        stream = studio.stream_trajectory(
            session_id,
            cursor,
            invocation_id=invocation_id,
        )

        async def frames():
            try:
                async for frame in stream:
                    if await request.is_disconnected():
                        return
                    yield frame
            finally:
                await stream.aclose()

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/v1/sessions/{session_id}:export")
    async def export_session(session_id: str, payload: dict[str, Any]):
        filename = payload.get("filename")
        invocation_id = payload.get("invocationId")
        download = payload.get("download", False)
        if not isinstance(filename, str):
            raise StudioError(
                "SESSION_EXPORT_FILENAME_INVALID",
                "filename 必须是字符串",
                status_code=422,
                field="filename",
            )
        if invocation_id is not None and not isinstance(invocation_id, str):
            raise StudioError(
                "SESSION_EXPORT_INVOCATION_INVALID",
                "invocationId 必须是字符串",
                status_code=422,
                field="invocationId",
            )
        if not isinstance(download, bool):
            raise StudioError(
                "SESSION_EXPORT_DOWNLOAD_INVALID",
                "download 必须是布尔值",
                status_code=422,
                field="download",
            )
        result = await studio.export_runtime_session(
            session_id,
            filename=filename,
            invocation_id=invocation_id,
        )
        if not download:
            return result

        path = studio.workspace.resolve(result["path"])
        return FileResponse(
            path,
            filename=filename,
            media_type="application/x-ndjson",
            headers={"X-Session-Event-Count": str(result["eventCount"])},
            background=BackgroundTask(path.unlink, missing_ok=True),
        )

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
        events = await studio.run_service.events(run_id, after=cursor)
        return _sse(events)

    @app.get("/api/v1/traces/overview")
    async def trace_overview(
        range_name: Literal["24h", "7d"] = Query(default="24h", alias="range"),
        agent_id: str | None = Query(default=None, alias="agentId"),
        status: str | None = Query(default=None),
    ):
        return studio.event_store.trace_overview(
            range_name=range_name,
            agent_id=agent_id,
            status=status,
        )

    @app.get("/api/v1/traces")
    async def list_traces(
        agent_id: str | None = Query(default=None, alias="agentId"),
        status: str | None = Query(default=None),
        query: str = Query(default=""),
        limit: int = Query(default=50, ge=1, le=1000),
        cursor: str | None = Query(default=None),
        sort: Literal["startedAt:desc", "startedAt:asc"] = "startedAt:desc",
    ):
        return studio.event_store.list_traces_page(
            agent_id=agent_id,
            status=status,
            query=query,
            limit=limit,
            cursor=cursor,
            sort=sort,
        )

    @app.get("/api/v1/traces/{trace_id}")
    async def get_trace(trace_id: str):
        return studio.event_store.trace(trace_id)

    @app.get("/api/v1/traces/{trace_id}/otlp")
    async def get_trace_otlp(trace_id: str):
        return studio.event_store.trace_otlp(trace_id)

    @app.post("/api/v1/evaluations", status_code=202)
    async def create_public_evaluation(
        payload: StudioEvaluationCreate,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        return studio.submit_public_evaluation(
            payload.evalset_file,
            payload.target,
            payload.config,
            idempotency_key=_require_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/evaluation-files", status_code=201)
    async def import_evaluation_file(file: UploadFile = File(...)):
        return studio.import_evaluation_file(
            await file.read(2 * 1024 * 1024 + 1),
            filename=file.filename or "evalset.yaml",
        )

    @app.get("/api/v1/evaluations")
    async def list_public_evaluations():
        return {"items": studio.list_public_evaluations()}

    @app.get("/api/v1/evaluation-runs")
    async def list_public_evaluation_runs():
        return {"items": studio.list_public_evaluation_runs()}

    @app.get("/api/v1/evaluation-runs/{evaluation_id}")
    async def get_public_evaluation_run(evaluation_id: str):
        return studio.get_public_evaluation_run(evaluation_id)

    @app.get("/api/v1/evaluation-targets")
    async def list_evaluation_targets():
        return studio.evaluation_catalog()

    @app.get("/api/v1/evaluation-cloud/catalog")
    async def list_evaluation_cloud_catalog(
        project_id: str | None = Query(default=None, alias="projectId"),
    ):
        items = await studio.evaluation_cloud_catalog(project_id=project_id)
        return {"items": items}

    @app.get("/api/v1/evaluations/{evaluation_id}")
    async def get_evaluation(evaluation_id: str):
        return studio.get_public_evaluation(evaluation_id)

    @app.get("/api/v1/evaluations/{evaluation_id}/cases/{case_id}")
    async def get_public_evaluation_case(evaluation_id: str, case_id: str):
        report = studio.get_public_evaluation(evaluation_id)
        for case_run in report.case_runs:
            if case_run.case_id == case_id:
                return case_run
        raise StudioError(
            "EVALUATION_CASE_NOT_FOUND",
            "Evaluation Case 不存在",
            status_code=404,
            details={"id": case_id, "evaluationId": evaluation_id},
        )

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

    @app.get("/api/v1/deployments")
    async def list_deployments():
        """Read local deployment receipts without implicit cloud refreshes."""

        return {"items": studio.cloud.list()}

    @app.get("/api/v1/cloud-agents")
    async def list_account_cloud_agents(
        page: int = Query(default=1, ge=1),
        size: int = Query(default=100, ge=1, le=100),
    ):
        """List Agents visible to Studio's configured signed cloud account."""

        return await studio.cloud.list_account_agents(page=page, size=size)

    @app.get("/api/v1/cloud-agents/{agent_id}")
    async def get_account_cloud_agent(agent_id: str):
        return await studio.cloud.get_account_agent(agent_id)

    @app.get("/api/v1/cloud-agents/{agent_id}/versions")
    async def list_account_cloud_agent_versions(
        agent_id: str,
        page: int = Query(default=1, ge=1),
        size: int = Query(default=100, ge=1, le=100),
    ):
        """List the Server-owned version history and rollback eligibility."""

        return await studio.cloud.list_account_agent_versions(
            agent_id,
            page=page,
            size=size,
        )

    @app.post(
        "/api/v1/cloud-agents/{agent_id}:rollback-version",
        status_code=202,
    )
    async def rollback_account_cloud_agent_version(
        agent_id: str,
        payload: CloudAgentVersionRollbackRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        """Submit Server RollbackVersion through Studio's process-only AK/SK."""

        return studio.submit_account_agent_version_rollback(
            agent_id,
            version_id=payload.version_id,
            idempotency_key=_require_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/cloud-agents/{agent_id}:dashboard")
    async def open_account_cloud_agent_dashboard(agent_id: str):
        return await studio.cloud.account_agent_dashboard_access(agent_id)

    @app.delete("/api/v1/cloud-agents/{agent_id}")
    async def delete_account_cloud_agent(agent_id: str):
        return await studio.cloud.delete_account_agent(agent_id)

    @app.get("/api/v1/deployments/{deployment_id}")
    async def get_deployment(deployment_id: str):
        return await studio.cloud.refresh(deployment_id)

    @app.post("/api/v1/deployments/{deployment_id}:dashboard")
    async def open_deployment_dashboard(deployment_id: str):
        return await studio.deployment_dashboard_access(deployment_id)

    @app.delete("/api/v1/deployments/{deployment_id}")
    async def delete_deployment(deployment_id: str):
        """Delete the receipt-bound cloud Agent and its superseded local receipts."""

        return await studio.cloud.delete(deployment_id)

    @app.get("/api/v1/deployments/{deployment_id}/cloud-chat/sessions")
    async def list_cloud_chat_sessions(
        deployment_id: str,
        page: int = Query(default=1, ge=1),
        size: int = Query(default=50, ge=1, le=100),
    ):
        """List Server-owned sessions for this local deployment receipt only."""

        return await studio.cloud.list_cloud_chat_sessions(deployment_id, page=page, size=size)

    @app.get("/api/v1/deployments/{deployment_id}/cloud-chat/models")
    async def list_cloud_chat_models(deployment_id: str):
        """List models through Studio's signed Server client."""

        return await studio.cloud.list_cloud_chat_models(deployment_id)

    @app.post(
        "/api/v1/deployments/{deployment_id}/cloud-chat/sessions",
        status_code=201,
    )
    async def create_cloud_chat_session(deployment_id: str):
        """Create a cloud session via loopback-held AK/SK; no secret reaches JS."""

        return await studio.cloud.create_cloud_chat_session(deployment_id)

    @app.get("/api/v1/deployments/{deployment_id}/cloud-chat/sessions/{session_id}/messages")
    async def list_cloud_chat_messages(
        deployment_id: str,
        session_id: str,
        after_seq_id: int | None = Query(default=None, alias="afterSeqId", ge=0),
        limit: int = Query(default=100, ge=1, le=200),
    ):
        return await studio.cloud.list_cloud_chat_messages(
            deployment_id,
            session_id=session_id,
            after_seq_id=after_seq_id,
            limit=limit,
        )

    @app.get("/api/v1/deployments/{deployment_id}/cloud-chat/sessions/{session_id}/events")
    async def list_cloud_chat_events(
        deployment_id: str,
        session_id: str,
        after_seq_id: int | None = Query(default=None, alias="afterSeqId", ge=0),
        limit: int = Query(default=200, ge=1, le=1000),
    ):
        return await studio.cloud.list_cloud_chat_events(
            deployment_id,
            session_id=session_id,
            after_seq_id=after_seq_id,
            limit=limit,
        )

    @app.get("/api/v1/deployments/{deployment_id}/cloud-chat/sessions/{session_id}/events/stream")
    async def stream_cloud_chat_events(
        request: Request,
        deployment_id: str,
        session_id: str,
        after_seq_id: int = Query(default=0, alias="afterSeqId", ge=0),
    ):
        """Stream canonical cloud events to the loopback browser.

        The public cloud control plane currently exposes cursor reads for this
        surface.  Keep that cursor in the Studio backend and present one SSE
        response to the browser, so assistant deltas arrive before the durable
        terminal message projection without exposing cloud credentials to JS.
        """

        async def event_stream() -> AsyncIterator[str]:
            cursor = after_seq_id
            idle_polls = 0
            while not await request.is_disconnected():
                payload = await studio.cloud.list_cloud_chat_events(
                    deployment_id,
                    session_id=session_id,
                    after_seq_id=cursor,
                    limit=200,
                )
                events = payload.get("events") or []
                if not isinstance(events, list):
                    events = []
                terminal = False
                emitted = False
                for event in events:
                    if not isinstance(event, dict):
                        continue
                    event_payload = (
                        event.get("payload") if isinstance(event.get("payload"), dict) else event
                    )
                    seq = int(
                        event_payload.get("seq")
                        or event_payload.get("seq_id")
                        or event_payload.get("source_session_seq")
                        or event.get("seq")
                        or event.get("seq_id")
                        or 0
                    )
                    if seq and seq <= cursor:
                        continue
                    if seq:
                        cursor = max(cursor, seq)
                    event_type = str(
                        event.get("event_type")
                        or event.get("eventType")
                        or event_payload.get("event_type")
                        or event_payload.get("eventType")
                        or ""
                    ).lower()
                    content = (
                        event_payload.get("content")
                        if isinstance(event_payload.get("content"), dict)
                        else {}
                    )
                    status = str(event_payload.get("status") or content.get("status") or "").lower()
                    event_is_terminal = event_type in {
                        "run.completed",
                        "run.complete",
                        "run.succeeded",
                        "run.failed",
                        "run.cancelled",
                        "run.expired",
                        "run.error",
                    } or (
                        event_type in {"run_status", "run.status"}
                        and status
                        in {
                            "completed",
                            "complete",
                            "succeeded",
                            "success",
                            "failed",
                            "cancelled",
                            "canceled",
                            "expired",
                            "error",
                            "aborted",
                        }
                    )
                    terminal = terminal or event_is_terminal
                    emitted = True
                    projected_event = dict(event)
                    conversation_item = _cloud_event_conversation_item(
                        event,
                        session_id=session_id,
                    )
                    if conversation_item is not None:
                        projected_event["conversationItem"] = conversation_item
                    yield (
                        (f"id: {seq}\n" if seq else "")
                        + "event: session.event\n"
                        + f"data: {json.dumps(projected_event, ensure_ascii=False)}\n\n"
                    )
                if terminal or payload.get("session_deleted"):
                    break
                idle_polls = 0 if emitted else idle_polls + 1
                if idle_polls and idle_polls % 20 == 0:
                    yield ": keepalive\n\n"
                await asyncio.sleep(0.25)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.delete(
        "/api/v1/deployments/{deployment_id}/cloud-chat/sessions/{session_id}",
        status_code=204,
    )
    async def delete_cloud_chat_session(deployment_id: str, session_id: str):
        await studio.cloud.delete_cloud_chat_session(deployment_id, session_id=session_id)
        return Response(status_code=204)

    @app.post(
        "/api/v1/deployments/{deployment_id}/cloud-chat/sessions/{session_id}/messages",
        status_code=202,
    )
    async def send_cloud_chat_message(
        deployment_id: str,
        session_id: str,
        payload: CloudChatMessageRequest,
    ):
        """Admit one cloud message through Server; response is a durable receipt."""

        return await studio.cloud.send_cloud_chat_message(
            deployment_id,
            session_id=session_id,
            content=payload.content,
            model=payload.model,
            model_options=payload.model_options,
            tool_approval_mode=payload.tool_approval_mode,
            collaboration_mode=payload.collaboration_mode,
            goal_objective=payload.goal_objective,
        )

    @app.post(
        "/api/v1/deployments/{deployment_id}/cloud-chat/sessions/{session_id}/messages/stream"
    )
    async def stream_cloud_chat_message(
        request: Request,
        deployment_id: str,
        session_id: str,
        payload: CloudChatMessageRequest,
    ):
        """Proxy one signed foreground RunAgent SSE response to loopback UI."""

        upstream = await studio.cloud.stream_cloud_chat_message(
            deployment_id,
            session_id=session_id,
            content=payload.content,
            model=payload.model,
            model_options=payload.model_options,
            tool_approval_mode=payload.tool_approval_mode,
            collaboration_mode=payload.collaboration_mode,
            goal_objective=payload.goal_objective,
        )

        async def proxy_stream() -> AsyncIterator[bytes]:
            try:
                while not await request.is_disconnected():
                    try:
                        chunk = await anext(upstream)
                    except StopAsyncIteration:
                        break
                    if await request.is_disconnected():
                        break
                    yield chunk
            finally:
                close = getattr(upstream, "aclose", None)
                if close is not None:
                    await close()

        return StreamingResponse(
            proxy_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post(
        "/api/v1/deployments/{deployment_id}/cloud-chat/sessions/{session_id}/interactions",
        status_code=202,
    )
    async def submit_cloud_chat_interaction(
        deployment_id: str,
        session_id: str,
        payload: CloudChatInteractionSubmitRequest,
    ):
        return await studio.cloud.submit_cloud_chat_interaction(
            deployment_id,
            session_id=session_id,
            run_id=payload.run_id,
            interaction_id=payload.interaction_id,
            expected_revision=payload.expected_revision,
            action=payload.action,
            response=payload.response,
            idempotency_key=payload.idempotency_key,
        )

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
        events = studio.operations.events(operation_id, after=cursor)
        if "application/json" in request.headers.get("Accept", ""):
            return {"items": events}
        return _sse(events)

    register_catalog_routes(
        app,
        studio,
        runtime_model_catalog=runtime_model_catalog,
    )
    register_memory_routes(app, studio)
    register_plugin_routes(app, studio)

    return app

# ksadk/server/app.py
"""
FastAPI 应用 - 提供 HTTP API 接口 (ADK Web 兼容)
"""

import base64
import httpx
import io
import json
import logging
import mimetypes
import os
import time
import uuid
import asyncio
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, File, Form, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import ksadk.conversations as conversation
from ksadk.conversations.attachment_storage import AttachmentStorageService
from ksadk.conversations.attachments import compact_attachment_result_for_session
from ksadk.conversations.session_title import (
    HEURISTIC_SESSION_TITLE_SOURCE,
    build_heuristic_title,
)
from ksadk.runners.base_runner import BaseRunner
from ksadk.server.api_models import AgentRunRequest
from ksadk.server.terminal_sessions import TerminalSessionManager, register_terminal_routes
from ksadk_runtime_common.workspace_files import (
    build_workspace_files_bootstrap,
    create_workspace_files_router,
    workspace_files_enabled,
)
from ksadk_runtime_common.workspace_files.preview import (
    build_workspace_file_base_href,
    build_workspace_preview_csp,
    inject_workspace_html_preview,
)
from ksadk.sessions import (
    ConversationSessionCore,
    Session,
    SessionEvent,
    describe_session_backend,
    resolve_session_service,
)
from ksadk.sessions.local_service import resolve_local_session_dir
from ksadk.tracing import get_memory_exporter
from ksadk.conversations.model_context import normalize_model_metadata
from ksadk.toolsets import describe_agentengine_tools

logger = logging.getLogger(__name__)


# Global Runner instance
runner: BaseRunner = None
_runner_loaded = False
_DETACHED_STREAMS: set[asyncio.Task[Any]] = set()
_DETACHED_STREAMS_BY_INVOCATION: dict[str, "_DetachedSSEStream"] = {}
_RUN_TERMINAL_STATUSES = {
    "completed",
    "failed",
    "error",
    "cancelled",
    "canceled",
    "aborted",
    "interrupted",
}


class _DetachedSSEStream:
    _MAX_BACKLOG_CHUNKS = 256

    def __init__(self, source: AsyncIterator[str], *, invocation_id: str | None = None):
        self._source = source
        self.invocation_id = invocation_id
        self._subscribers: set[asyncio.Queue[str | None]] = set()
        self._backlog: list[str] = []
        self._done = False
        self._task = asyncio.create_task(self._consume())
        _DETACHED_STREAMS.add(self._task)
        self._task.add_done_callback(_DETACHED_STREAMS.discard)
        if self.invocation_id:
            _DETACHED_STREAMS_BY_INVOCATION[self.invocation_id] = self
            self._task.add_done_callback(
                lambda _task: _DETACHED_STREAMS_BY_INVOCATION.pop(self.invocation_id or "", None)
            )

    async def _consume(self) -> None:
        try:
            async for chunk in self._source:
                self._backlog.append(chunk)
                if len(self._backlog) > self._MAX_BACKLOG_CHUNKS:
                    self._backlog = self._backlog[-self._MAX_BACKLOG_CHUNKS :]
                subscribers = list(self._subscribers)
                if not subscribers:
                    continue
                await asyncio.gather(
                    *(subscriber.put(chunk) for subscriber in subscribers),
                    return_exceptions=True,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Detached SSE stream failed")
            raise
        finally:
            self._done = True
            subscribers = list(self._subscribers)
            if subscribers:
                await asyncio.gather(
                    *(subscriber.put(None) for subscriber in subscribers),
                    return_exceptions=True,
                )

    def subscribe(self) -> asyncio.Queue[str | None]:
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        for chunk in self._backlog:
            queue.put_nowait(chunk)
        if self._done:
            queue.put_nowait(None)
        else:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str | None]) -> None:
        self._subscribers.discard(queue)

    def cancel(self) -> bool:
        if self._task.done():
            return False
        return self._task.cancel()

    async def iter_for_client(self) -> AsyncIterator[str]:
        queue = self.subscribe()
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                yield chunk
        finally:
            self.unsubscribe(queue)


def _detached_streaming_response(
    source: AsyncIterator[str], *, invocation_id: str | None = None
) -> StreamingResponse:
    detached = _DetachedSSEStream(source, invocation_id=invocation_id)
    return StreamingResponse(detached.iter_for_client(), media_type="text/event-stream")


async def _shutdown_runner_resources():
    terminal_manager.reset_for_tests()
    pending_streams = list(_DETACHED_STREAMS)
    for task in pending_streams:
        task.cancel()
    if pending_streams:
        await asyncio.gather(*pending_streams, return_exceptions=True)
    _DETACHED_STREAMS.clear()

    active_runner = runner
    if active_runner is None:
        return
    close = getattr(active_runner, "close", None)
    if callable(close):
        await close()


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await _shutdown_runner_resources()


# Create and configure the FastAPI application
app = FastAPI(
    title="ADK Core API",
    description="Agent Development Kit HTTP API",
    version="1.0.0",
    lifespan=_lifespan,
)

# Middleware for disabling cache on frontend entry points
@app.middleware("http")
async def no_cache_frontend(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith(".html"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# Configure CORS (permissive by default for ADK tools)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_TYPES = {
    "application/json",
    "application/pdf",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "application/x-ndjson",
}
_TEXT_FILE_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".json",
    ".yaml",
    ".yml",
    ".csv",
    ".tsv",
    ".log",
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".html",
    ".css",
    ".sql",
    ".xml",
    ".sh",
}
_MAX_INLINE_BASE64_CHARS = 4_000_000
_MAX_INLINE_TEXT_CHARS = 20_000
_MAX_REFERENCE_TEXT_BYTES = 3_000_000
_UPLOAD_URI_SCHEME = "ksadk-upload://"


def _workspace_root_dir() -> Path:
    return resolve_local_session_dir() / "workspace"


_NATIVE_TUI_FRAMEWORKS = {"hermes", "openclaw"}


def _current_framework() -> str:
    if not runner:
        return ""
    detection_type = getattr(getattr(runner, "detection_result", None), "type", None)
    return str(getattr(detection_type, "value", detection_type) or "").strip().lower()


def _build_native_terminal_capability(framework: str) -> dict[str, Any]:
    enabled = str(framework or "").strip().lower() in _NATIVE_TUI_FRAMEWORKS
    return {
        "Enabled": enabled,
        "Mode": "tui" if enabled else None,
        "Protocol": "ks-terminal.v1",
        "Path": "/_ksadk/terminal/ws" if enabled else None,
    }


terminal_manager = TerminalSessionManager(
    workspace_root_getter=_workspace_root_dir,
    framework_getter=_current_framework,
)

app.include_router(
    create_workspace_files_router(
        root_getter=_workspace_root_dir,
        enabled_getter=lambda: workspace_files_enabled(default=True),
    )
)
register_terminal_routes(app, terminal_manager)


def set_runner(r: BaseRunner):
    global runner, _runner_loaded
    runner = r
    _runner_loaded = False


def _ensure_runner_loaded() -> BaseRunner:
    global _runner_loaded
    if not runner:
        raise HTTPException(status_code=500, detail="Runner 未初始化")
    if _runner_loaded:
        return runner

    runner.load_agent()
    _runner_loaded = True
    return runner


def _resolve_active_runner() -> BaseRunner:
    try:
        return _ensure_runner_loaded()
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Runner 加载失败: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc) or "Runner 加载失败") from exc


def _prepare_runner_for_model(active_runner: BaseRunner, model: Optional[str]) -> None:
    try:
        active_runner.prepare_for_request(model)
    except Exception as exc:
        logger.warning("Runner 模型切换失败: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc) or "Runner 模型切换失败") from exc


def _resolve_current_model() -> tuple[Optional[str], Optional[str]]:
    candidates = (
        ("OPENAI_MODEL_NAME", os.getenv("OPENAI_MODEL_NAME")),
        ("MODEL_NAME", os.getenv("MODEL_NAME")),
        ("COZE_MODEL_NAME", os.getenv("COZE_MODEL_NAME")),
    )
    for source, value in candidates:
        model = str(value or "").strip()
        if model:
            return model, source
    return None, None


def _build_bootstrap_model_payload() -> Optional[dict[str, Any]]:
    current_model, source = _resolve_current_model()
    if not current_model:
        return None

    payload = normalize_model_metadata({"id": current_model})
    payload["source"] = source
    return payload


def _is_textual_mime(mime_type: str) -> bool:
    mime = (mime_type or "").lower()
    if not mime:
        return False
    return mime.startswith(_TEXT_MIME_PREFIXES) or mime in _TEXT_MIME_TYPES


def _looks_like_textual_attachment(mime_type: str, display_name: str) -> bool:
    suffix = Path(display_name or "").suffix.lower()
    return _is_textual_mime(mime_type) or suffix in _TEXT_FILE_EXTENSIONS


def _extract_pdf_text(raw: bytes) -> str:
    try:
        from pypdf import PdfReader
    except Exception:
        return ""

    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception:
        return ""

    segments: List[str] = []
    for page in reader.pages[:10]:
        try:
            page_text = page.extract_text() or ""
        except Exception:
            page_text = ""
        if page_text:
            segments.append(page_text)

    return "\n".join(segments).strip()


def _decode_inline_data(data_b64: str) -> bytes:
    return base64.b64decode((data_b64 or "").strip() + "===")


def _resolve_uploads_dir() -> Path:
    uploads_dir = resolve_local_session_dir() / "files"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    return uploads_dir


def _resolve_attachment_storage_path(file_uri: str) -> Optional[Path]:
    normalized_uri = (file_uri or "").strip()
    if not normalized_uri:
        return None

    if normalized_uri.startswith("local:"):
        path = Path(normalized_uri[6:]).expanduser()
        return path.resolve()

    if normalized_uri.startswith(_UPLOAD_URI_SCHEME):
        file_id = normalized_uri.removeprefix(_UPLOAD_URI_SCHEME).strip("/")
        if not file_id:
            return None

        for candidate in sorted(_resolve_uploads_dir().glob(f"{file_id}*")):
            if candidate.is_file():
                return candidate.resolve()

    return None


def _read_attachment_bytes(storage_path: Optional[Path], *, size_limit: Optional[int] = None) -> Optional[bytes]:
    if storage_path is None or not storage_path.is_file():
        return None

    try:
        if size_limit is not None and storage_path.stat().st_size > size_limit:
            return None
        return storage_path.read_bytes()
    except OSError:
        return None


def _extract_inline_attachment_text(*, display_name: str, mime_type: str, raw: bytes) -> str:
    if mime_type == "application/pdf" or display_name.lower().endswith(".pdf"):
        text = _extract_pdf_text(raw)
        if not text:
            return ""
        if len(text) > _MAX_INLINE_TEXT_CHARS:
            return text[:_MAX_INLINE_TEXT_CHARS] + "\n...[内容已截断]"
        return text

    if _looks_like_textual_attachment(mime_type, display_name):
        text = raw.decode("utf-8", errors="ignore")
        if len(text) > _MAX_INLINE_TEXT_CHARS:
            return text[:_MAX_INLINE_TEXT_CHARS] + "\n...[内容已截断]"
        return text

    return ""


def _attachment_prompt_text(attachment: Dict[str, Any]) -> str:
    display_name = str(attachment.get("display_name") or "uploaded_file")
    mime_type = str(attachment.get("mime_type") or "application/octet-stream")
    transport = str(attachment.get("transport") or "")

    if transport == "inline":
        data_b64 = str(attachment.get("data") or "").strip()
        if len(data_b64) > _MAX_INLINE_BASE64_CHARS:
            return (
                "[上传文件: "
                f"{display_name}, "
                f"mime={mime_type or 'unknown'}, "
                "内容过大，未直接展开]"
            )

        try:
            raw = _decode_inline_data(data_b64)
        except Exception:
            return f"[上传文件: {display_name}, 内容解码失败]"

        text = _extract_inline_attachment_text(
            display_name=display_name,
            mime_type=mime_type,
            raw=raw,
        )
        if text:
            return f"[上传文件: {display_name}]\n{text}"
        return (
            "[上传文件: "
            f"{display_name}, "
            f"mime={mime_type or 'application/octet-stream'}, "
            f"bytes={len(raw)}]"
        )

    storage_path_value = attachment.get("storage_path")
    storage_path = Path(str(storage_path_value)) if storage_path_value else None
    size_bytes = attachment.get("size_bytes")
    if size_bytes is None and storage_path is not None and storage_path.exists():
        try:
            size_bytes = storage_path.stat().st_size
        except OSError:
            size_bytes = None

    raw = _read_attachment_bytes(storage_path, size_limit=_MAX_REFERENCE_TEXT_BYTES)
    if raw is not None:
        text = _extract_inline_attachment_text(
            display_name=display_name,
            mime_type=mime_type,
            raw=raw,
        )
        if text:
            return f"[上传文件: {display_name}]\n{text}"
        return (
            "[上传文件: "
            f"{display_name}, "
            f"mime={mime_type or 'application/octet-stream'}, "
            f"bytes={len(raw)}]"
        )

    if size_bytes and size_bytes > _MAX_REFERENCE_TEXT_BYTES:
        return (
            "[上传文件: "
            f"{display_name}, "
            f"mime={mime_type or 'unknown'}, "
            f"bytes={size_bytes}, "
            "内容过大，未直接展开]"
        )

    file_uri = attachment.get("file_uri") or ""
    return (
        "[上传文件引用: "
        f"{display_name or file_uri}, "
        f"mime={mime_type or 'unknown'}]"
    )


def _extract_user_input_from_parts(parts: List[Any]) -> str:
    """兼容旧测试/旧调用点，统一复用 conversations 层的规范化逻辑。"""

    return conversation.extract_user_input_from_parts(parts)


def _attachment_from_part(part: Any) -> Optional[Dict[str, Any]]:
    """兼容旧入口，真实实现已经收口到 conversations.normalize。"""

    return conversation.attachment_from_part(part)


async def _hydrate_session(session: Optional[Session]) -> Optional[Session]:
    if not session:
        return None
    session.events = await resolve_session_service().get_events(session.id)
    return session


async def _ensure_session(agent_id: str, user_id: str, session_id: Optional[str]) -> Session:
    service = resolve_session_service()
    if session_id:
        existing = await service.get_session(session_id)
        if existing:
            if existing.agent_id != agent_id or existing.user_id != user_id:
                raise HTTPException(
                    status_code=409,
                    detail="Session id belongs to a different agent or user",
                )
            return await _hydrate_session(existing) or existing
        created = await service.create_session(agent_id, user_id, session_id=session_id)
        return await _hydrate_session(created) or created

    created = await service.create_session(agent_id, user_id)
    return await _hydrate_session(created) or created


def _sanitize_session_state_for_action(state: Mapping[str, Any] | None) -> dict[str, Any]:
    sanitized = dict(state or {})
    attachment_context = sanitized.get(conversation.runtime.ATTACHMENT_CONTEXT_STATE_KEY)
    if not isinstance(attachment_context, Mapping):
        return sanitized

    attachments = [
        conversation.compact_attachment_for_session(item)
        for item in attachment_context.get("attachments") or []
        if isinstance(item, dict)
    ]
    attachment_results = [
        compact_attachment_result_for_session(item)
        for item in attachment_context.get("attachment_results") or []
        if isinstance(item, dict)
    ]
    sanitized[conversation.runtime.ATTACHMENT_CONTEXT_STATE_KEY] = {
        "attachments": attachments,
        "attachment_results": attachment_results,
    }
    return sanitized


def _request_id() -> str:
    return f"req-{uuid.uuid4().hex[:12]}"


def _action_response(action: str, data: Any, *, request_id: Optional[str] = None, message: str = "Success") -> dict:
    payload = {
        "Code": 0,
        "Message": message,
        "RequestId": request_id or _request_id(),
        "Data": data,
    }
    if action:
        payload["Action"] = action
    return payload


async def _workspace_runtime_request(
    method: str,
    runtime_path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    files: Optional[Dict[str, Any]] = None,
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.request(
            method,
            runtime_path,
            params=params,
            files=files,
        )

    if response.status_code >= 400:
        detail = response.text
        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            detail = str(payload.get("detail") or detail)
        raise HTTPException(status_code=response.status_code, detail=detail or "Workspace request failed")
    return response


# ============================================================
# Core ADK API Endpoints
# ============================================================


@app.get("/health")
async def health_check():
    framework = "unknown"
    agent_name = "unknown"
    if runner and hasattr(runner, "detection_result"):
        framework = runner.detection_result.type.value  # langgraph, langchain, adk
        agent_name = runner.detection_result.name
    return {"status": "ok", "framework": framework, "agent": agent_name}


@app.get("/list-apps")
async def list_apps(relative_path: str = "./"):
    """Return available apps. For KsADK single-agent mode, returns the current agent."""
    name = runner.detection_result.name if runner else "default_agent"
    return [name]


class UiBootstrapRequest(BaseModel):
    AgentId: Optional[str] = None
    SessionId: Optional[str] = None


class CreateSessionActionRequest(BaseModel):
    AgentId: str
    UserId: Optional[str] = "user"
    SessionId: Optional[str] = None


class ListSessionsActionRequest(BaseModel):
    AgentId: str
    UserId: Optional[str] = "user"
    Page: int = Field(1, ge=1)
    PageSize: int = Field(20, ge=1, le=200)


class SessionIdRequest(BaseModel):
    SessionId: str


class ListSessionEventsActionRequest(BaseModel):
    SessionId: str
    Offset: Optional[int] = Field(None, ge=0)
    Limit: Optional[int] = Field(None, ge=1)


class ListSessionCheckpointsActionRequest(BaseModel):
    AgentId: str
    SessionId: str
    RunId: Optional[str] = None


class ListToolReceiptsActionRequest(BaseModel):
    AgentId: str
    SessionId: str
    RunId: Optional[str] = None
    CheckpointId: Optional[str] = None


class ResumeRunActionRequest(BaseModel):
    AgentId: str
    SessionId: str
    RunId: str
    CheckpointId: str
    ResumeAttemptId: Optional[str] = None
    InvocationId: Optional[str] = None
    Stream: bool = False
    Model: Optional[str] = None
    ModelMetadata: Optional[Dict[str, Any]] = None
    ModelOptions: Optional[Dict[str, Any]] = None


class PreviewCheckpointResumeActionRequest(BaseModel):
    AgentId: str
    SessionId: str
    RunId: str
    CheckpointId: str


class RunAgentActionRequest(BaseModel):
    AgentId: str
    Messages: List[Dict[str, Any]] = Field(default_factory=list)
    UserId: Optional[str] = "user"
    AccountId: Optional[str] = None
    SessionId: Optional[str] = None
    InvocationId: Optional[str] = None
    ApiFormat: str = "responses"
    Stream: bool = False
    Model: Optional[str] = None
    ModelMetadata: Optional[Dict[str, Any]] = None
    ModelOptions: Optional[Dict[str, Any]] = None
    ResponsesInput: Optional[Any] = None
    PreviousResponseId: Optional[str] = None


class ResponseFeedbackRefActionRequest(BaseModel):
    AgentId: str
    SessionId: str
    ResponseId: str


class UpsertResponseFeedbackActionRequest(ResponseFeedbackRefActionRequest):
    Rating: str
    Comment: Optional[str] = ""
    EventId: Optional[str] = None
    TraceId: Optional[str] = None
    RootSpanId: Optional[str] = None


class ResponsesRequest(BaseModel):
    input: Any
    model: Optional[str] = None
    model_metadata: Optional[Dict[str, Any]] = None
    model_options: Optional[Dict[str, Any]] = None
    instructions: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    conversation: Optional[Any] = None
    safety_identifier: Optional[str] = None
    prompt_cache_key: Optional[str] = None
    user: Optional[str] = None
    account_id: Optional[str] = None
    store: Optional[bool] = None
    previous_response_id: Optional[str] = None
    stream: bool = False
    session_id: Optional[str] = None


class WorkspaceListActionRequest(BaseModel):
    AgentId: Optional[str] = None
    Path: str = "."
    Recursive: bool = False


def _clean_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _resolve_responses_conversation_id(conversation_value: Any) -> str | None:
    if conversation_value is None:
        return None
    if isinstance(conversation_value, str):
        return _clean_optional_string(conversation_value)
    if isinstance(conversation_value, Mapping):
        return _clean_optional_string(conversation_value.get("id"))
    raise HTTPException(
        status_code=400,
        detail="Responses field 'conversation' must be a string or an object with an 'id'.",
    )


def _resolve_responses_session_and_user(request: ResponsesRequest) -> tuple[str | None, str]:
    conversation_id = _resolve_responses_conversation_id(request.conversation)
    legacy_session_id = _clean_optional_string(request.session_id)

    if conversation_id and legacy_session_id and conversation_id != legacy_session_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "Responses field 'conversation' conflicts with ksadk legacy field "
                "'session_id'. Use 'conversation' for OpenAI-compatible calls."
            ),
        )
    if conversation_id and request.previous_response_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "Responses fields 'conversation' and 'previous_response_id' cannot be "
                "used together."
            ),
        )

    resolved_session_id = conversation_id or legacy_session_id
    resolved_user_id = (
        _clean_optional_string(request.safety_identifier)
        or _clean_optional_string(request.user)
        or "user"
    )
    return resolved_session_id, resolved_user_id


def _runtime_agent_id(active_runner: BaseRunner) -> str:
    runtime_id = _clean_optional_string(os.getenv("AGENT_RUNTIME_ID"))
    if runtime_id:
        return runtime_id
    return str(getattr(active_runner.detection_result, "name", "") or "agent")


def _metadata_invocation_id(metadata: Mapping[str, Any] | None) -> str | None:
    if not isinstance(metadata, Mapping):
        return None
    agentengine_metadata = metadata.get("agentengine")
    if not isinstance(agentengine_metadata, Mapping):
        return None
    return _clean_optional_string(agentengine_metadata.get("invocation_id"))


class WorkspaceDeleteActionRequest(BaseModel):
    AgentId: Optional[str] = None
    Path: str


class CancelRunActionRequest(BaseModel):
    AgentId: Optional[str] = None
    InvocationId: str


async def _session_to_action_payload(session: Session) -> dict[str, Any]:
    title = session.title
    title_source = session.title_source
    if title_source == "fallback_first_prompt":
        heuristic = build_heuristic_title(
            first_prompt=session.first_prompt or title,
            assistant_text=session.summary or "",
        )
        if heuristic and heuristic != title:
            title = heuristic
            title_source = HEURISTIC_SESSION_TITLE_SOURCE
    payload = {
        "SessionId": session.id,
        "AgentId": session.agent_id,
        "UserId": session.user_id,
        "Title": title,
        "TitleSource": title_source,
        "Summary": session.summary,
        "FirstPrompt": session.first_prompt,
        "LastPrompt": session.last_prompt,
        "State": _sanitize_session_state_for_action(session.state),
        "CreatedAt": session.created_at,
        "UpdatedAt": session.updated_at,
        "Version": session.version,
    }
    if runner is not None:
        try:
            continuity = await runner.get_session_adapter().describe_continuity(
                runner=runner,
                session=session,
                core=ConversationSessionCore(resolve_session_service()),
            )
            payload["Continuity"] = continuity.to_payload()
        except Exception as exc:
            logger.debug("Failed to describe continuity for session %s: %s", session.id, exc)
    return payload


def _event_to_action_payload(event: SessionEvent) -> dict[str, Any]:
    payload = {
        "EventId": event.id,
        "SessionId": event.session_id,
        "Author": event.author,
        "EventType": event.event_type,
        "Content": event.content,
        "Timestamp": event.timestamp,
        "SeqId": event.seq_id,
        "Metadata": event.metadata,
    }
    if event.invocation_id:
        payload["InvocationId"] = event.invocation_id
    return payload


def _checkpoint_event_to_action_payload(event: SessionEvent) -> dict[str, Any] | None:
    if event.event_type != "run_checkpoint":
        return None
    metadata = event.metadata or {}
    run_id = str(metadata.get("run_id") or "").strip()
    checkpoint_id = str(metadata.get("checkpoint_id") or "").strip()
    framework = str(metadata.get("framework") or "").strip()
    framework_ref = metadata.get("framework_ref")
    if not run_id or not checkpoint_id or not framework or not isinstance(framework_ref, Mapping):
        return None
    payload = {
        "EventId": event.id,
        "SessionId": event.session_id,
        "InvocationId": event.invocation_id,
        "SeqId": event.seq_id,
        "Timestamp": event.timestamp,
        "RunId": run_id,
        "CheckpointId": checkpoint_id,
        "Framework": framework,
        "FrameworkRef": dict(framework_ref),
        "Phase": str(metadata.get("phase") or ""),
        "Metadata": metadata,
    }
    stage = str(metadata.get("stage") or metadata.get("title") or "").strip()
    summary = str(metadata.get("summary") or metadata.get("description") or "").strip()
    next_action = str(metadata.get("next_action") or metadata.get("nextAction") or "").strip()
    status = str(metadata.get("status") or "").strip()
    if stage:
        payload["Stage"] = stage
    if summary:
        payload["Summary"] = summary
    if next_action:
        payload["NextAction"] = next_action
    if status:
        payload["Status"] = status
    return payload


_SIDE_EFFECT_TOOL_NAMES = {
    "write_workspace_file",
    "write_workspace_files",
    "delete_workspace_file",
    "execute_skills",
    "run_command",
    "run_code",
}


def _tool_receipt_event_to_action_payload(event: SessionEvent) -> dict[str, Any] | None:
    if event.event_type != "tool_result":
        return None
    metadata = event.metadata or {}
    receipt = metadata.get("tool_receipt")
    if not isinstance(receipt, Mapping):
        return None
    tool_name = str(receipt.get("tool_name") or metadata.get("tool_name") or "").strip()
    if not tool_name:
        return None
    return {
        "EventId": event.id,
        "SessionId": event.session_id,
        "InvocationId": event.invocation_id,
        "SeqId": event.seq_id,
        "Timestamp": event.timestamp,
        "ReceiptId": str(receipt.get("receipt_id") or ""),
        "IdempotencyKey": str(receipt.get("idempotency_key") or ""),
        "ToolName": tool_name,
        "ToolCallId": str(receipt.get("tool_call_id") or ""),
        "RunId": str(receipt.get("run_id") or metadata.get("run_id") or ""),
        "CheckpointId": str(receipt.get("checkpoint_id") or ""),
        "Status": str(receipt.get("status") or ""),
        "Replayed": bool(receipt.get("replayed") or metadata.get("replayed")),
        "Metadata": dict(metadata),
    }


def _build_checkpoint_resume_preview(
    *,
    checkpoint: Mapping[str, Any],
    events: list[SessionEvent],
) -> dict[str, Any]:
    checkpoint_seq_id = int(checkpoint.get("SeqId") or 0)
    run_id = str(checkpoint.get("RunId") or "")
    receipts: list[dict[str, Any]] = []
    for event in events:
        if checkpoint_seq_id and int(event.seq_id or 0) > checkpoint_seq_id:
            continue
        receipt = _tool_receipt_event_to_action_payload(event)
        if receipt is None:
            continue
        if run_id and receipt["RunId"] and receipt["RunId"] != run_id:
            continue
        receipts.append(receipt)

    side_effect_receipts = [
        receipt for receipt in receipts if receipt["ToolName"] in _SIDE_EFFECT_TOOL_NAMES
    ]
    risk_level = "low"
    if side_effect_receipts:
        risk_level = "medium"
    if any(receipt["Status"] == "failed" for receipt in receipts):
        risk_level = "high"

    return {
        "Checkpoint": dict(checkpoint),
        "Capabilities": {
            "Checkpoints": True,
            "CheckpointResume": True,
            "ToolReceipts": True,
            "IdempotentToolReplay": True,
        },
        "ToolReceipts": receipts,
        "Risk": {
            "Level": risk_level,
            "DuplicateSideEffectRisk": bool(side_effect_receipts),
            "SideEffectReceiptCount": len(side_effect_receipts),
            "FailedReceiptCount": len([receipt for receipt in receipts if receipt["Status"] == "failed"]),
        },
        "Summary": {
            "RunId": run_id,
            "CheckpointId": str(checkpoint.get("CheckpointId") or ""),
            "Phase": str(checkpoint.get("Phase") or ""),
            "ToolReceiptCount": len(receipts),
        },
    }


async def _find_session_checkpoint(
    *,
    service: Any,
    session_id: str,
    run_id: str,
    checkpoint_id: str,
) -> dict[str, Any] | None:
    for event in reversed(await service.get_events(session_id)):
        checkpoint = _checkpoint_event_to_action_payload(event)
        if checkpoint is None:
            continue
        if checkpoint["RunId"] != run_id:
            continue
        if checkpoint["CheckpointId"] != checkpoint_id:
            continue
        return checkpoint
    return None


async def _resolve_checkpoint_resume_input_from_session(
    *,
    service: Any,
    agent_id: str,
    session_id: str | None,
    resume_input: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(resume_input, Mapping):
        return None
    if str(resume_input.get("type") or "").strip() != "agentengine.resume_checkpoint":
        return dict(resume_input)
    normalized_session_id = str(session_id or "").strip()
    if not normalized_session_id:
        raise HTTPException(status_code=400, detail="Checkpoint resume requires session_id")

    session = await service.get_session(normalized_session_id)
    if not session or session.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="Session not found")

    run_id = str(resume_input.get("run_id") or "").strip()
    checkpoint_id = str(resume_input.get("checkpoint_id") or "").strip()
    if not run_id or not checkpoint_id:
        raise HTTPException(status_code=400, detail="Checkpoint resume requires run_id and checkpoint_id")

    checkpoint = await _find_session_checkpoint(
        service=service,
        session_id=normalized_session_id,
        run_id=run_id,
        checkpoint_id=checkpoint_id,
    )
    if checkpoint is None:
        raise HTTPException(status_code=404, detail="Checkpoint not found")

    resume_attempt_id = str(resume_input.get("resume_attempt_id") or "").strip()
    return {
        "type": "agentengine.resume_checkpoint",
        "run_id": run_id,
        "checkpoint_id": checkpoint_id,
        "resume_attempt_id": resume_attempt_id or f"resume_{uuid.uuid4().hex}",
        "framework": checkpoint["Framework"],
        "framework_ref": checkpoint["FrameworkRef"],
    }


def _feedback_state_key(response_id: str) -> str:
    return str(response_id or "").strip()


def _feedback_payload_from_state(item: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    rating = str(item.get("Rating") or item.get("rating") or "").strip().lower()
    if rating not in {"up", "down"}:
        return None
    return {
        "AgentId": str(item.get("AgentId") or item.get("agent_id") or ""),
        "SessionId": str(item.get("SessionId") or item.get("session_id") or ""),
        "ResponseId": str(item.get("ResponseId") or item.get("response_id") or ""),
        "EventId": str(item.get("EventId") or item.get("event_id") or ""),
        "Rating": rating,
        "Comment": str(item.get("Comment") or item.get("comment") or ""),
        "TraceId": str(item.get("TraceId") or item.get("trace_id") or ""),
        "RootSpanId": str(item.get("RootSpanId") or item.get("root_span_id") or ""),
        "CreatedAt": str(item.get("CreatedAt") or item.get("created_at") or ""),
        "UpdatedAt": str(item.get("UpdatedAt") or item.get("updated_at") or ""),
    }


async def _find_feedback_assistant_event(
    *,
    session_id: str,
    response_id: str,
    event_id: str | None = None,
) -> SessionEvent | None:
    events = await resolve_session_service().get_events(session_id)
    normalized_event_id = str(event_id or "").strip()
    normalized_response_id = str(response_id or "").strip()
    for event in reversed(events):
        if normalized_event_id and event.id != normalized_event_id:
            continue
        metadata = event.metadata or {}
        if normalized_response_id and str(metadata.get("response_id") or "") != normalized_response_id:
            continue
        event_type = conversation.canonical_event_type(
            event.event_type,
            author=event.author,
            role=str((event.content or {}).get("role") or ""),
        )
        if event_type == "assistant_message":
            return event
    return None


@app.post("/agentengine/api/v1/GetResponseFeedback")
async def get_response_feedback_action(request: ResponseFeedbackRefActionRequest):
    session = await resolve_session_service().get_session(request.SessionId)
    if not session or session.agent_id != request.AgentId:
        return _action_response("GetResponseFeedback", {"Feedback": None})
    feedbacks = session.state.get("__ksadk_response_feedback__")
    feedback = None
    if isinstance(feedbacks, Mapping):
        feedback = _feedback_payload_from_state(feedbacks.get(_feedback_state_key(request.ResponseId)))
    return _action_response("GetResponseFeedback", {"Feedback": feedback})


@app.post("/agentengine/api/v1/UpsertResponseFeedback")
async def upsert_response_feedback_action(request: UpsertResponseFeedbackActionRequest):
    rating = str(request.Rating or "").strip().lower()
    if rating not in {"up", "down"}:
        raise HTTPException(status_code=400, detail="Feedback rating must be up or down")

    service = resolve_session_service()
    session = await service.get_session(request.SessionId)
    if not session or session.agent_id != request.AgentId:
        raise HTTPException(status_code=404, detail="Session not found")

    assistant_event = await _find_feedback_assistant_event(
        session_id=request.SessionId,
        response_id=request.ResponseId,
        event_id=request.EventId,
    )
    if assistant_event is None:
        raise HTTPException(status_code=404, detail="Assistant response not found")

    now = str(time.time())
    existing_feedbacks = session.state.get("__ksadk_response_feedback__")
    feedbacks = dict(existing_feedbacks) if isinstance(existing_feedbacks, Mapping) else {}
    existing = _feedback_payload_from_state(feedbacks.get(_feedback_state_key(request.ResponseId))) or {}
    metadata = assistant_event.metadata or {}
    feedback = {
        "AgentId": request.AgentId,
        "SessionId": request.SessionId,
        "ResponseId": request.ResponseId,
        "EventId": request.EventId or assistant_event.id,
        "Rating": rating,
        "Comment": request.Comment or "",
        "TraceId": request.TraceId or str(metadata.get("trace_id") or ""),
        "RootSpanId": request.RootSpanId or str(metadata.get("root_span_id") or ""),
        "CreatedAt": existing.get("CreatedAt") or now,
        "UpdatedAt": now,
    }
    feedbacks[_feedback_state_key(request.ResponseId)] = feedback
    await service.update_state(
        agent_id=session.agent_id,
        user_id=session.user_id,
        session_id=session.id,
        scope="session",
        state_delta={"__ksadk_response_feedback__": feedbacks},
    )
    return _action_response("UpsertResponseFeedback", {"Feedback": feedback})


@app.post("/agentengine/api/v1/DeleteResponseFeedback")
async def delete_response_feedback_action(request: ResponseFeedbackRefActionRequest):
    service = resolve_session_service()
    session = await service.get_session(request.SessionId)
    if not session or session.agent_id != request.AgentId:
        return _action_response("DeleteResponseFeedback", {"Deleted": False})
    existing_feedbacks = session.state.get("__ksadk_response_feedback__")
    feedbacks = dict(existing_feedbacks) if isinstance(existing_feedbacks, Mapping) else {}
    deleted = feedbacks.pop(_feedback_state_key(request.ResponseId), None) is not None
    if deleted:
        await service.update_state(
            agent_id=session.agent_id,
            user_id=session.user_id,
            session_id=session.id,
            scope="session",
            state_delta={"__ksadk_response_feedback__": feedbacks},
        )
    return _action_response("DeleteResponseFeedback", {"Deleted": deleted})


@app.post("/agentengine/api/v1/GetAgentUiBootstrap")
async def get_agent_ui_bootstrap(request: UiBootstrapRequest):
    agent_id = request.AgentId or (runner.detection_result.name if runner else "default-agent")
    description = getattr(runner.detection_result, "description", "") if runner else ""
    framework = ""
    if runner:
        detection_type = getattr(getattr(runner, "detection_result", None), "type", None)
        framework = str(getattr(detection_type, "value", detection_type) or "").strip().lower()
    workspace_enabled = workspace_files_enabled(default=True)
    return _action_response(
        "GetAgentUiBootstrap",
        {
            "Agent": {
                "AgentId": agent_id,
                "Name": runner.detection_result.name if runner else agent_id,
                "Description": description or "",
                "Framework": framework,
            },
            "Modules": ["Chat", "Build", "Deploy"],
            "Capabilities": {
                "Attachments": True,
                "WorkspaceFiles": workspace_enabled,
                "Approval": True,
                "Thinking": True,
                "StopRun": True,
                "ResumeRun": True,
                "RunLifecycle": {
                    "Enabled": True,
                    "Resume": True,
                    "Abort": True,
                    "Checkpoints": True,
                    "CheckpointResume": True,
                    "CheckpointResumePreview": True,
                },
                "MCP": False,
                "HostedRuntime": False,
                "NativeTerminal": _build_native_terminal_capability(framework),
                "BuiltinTools": describe_agentengine_tools(),
            },
            "WorkspaceFiles": build_workspace_files_bootstrap(enabled=workspace_enabled),
            "AccessMode": "Owner",
            "SharePermissions": {
                "Interactive": True,
                "DefaultPath": "/chat",
                "SharePath": "/chat",
            },
            "ApiFormats": ["responses", "chat_completions"],
            "Stream": True,
            "SessionId": request.SessionId,
            "SessionBackend": describe_session_backend(),
            "HostedRuntime": None,
            "Model": _build_bootstrap_model_payload(),
        },
    )


@app.post("/agentengine/api/v1/CreateSession")
async def create_session_action(request: CreateSessionActionRequest):
    session = await _ensure_session(request.AgentId, request.UserId or "user", request.SessionId)
    return _action_response("CreateSession", {"Session": await _session_to_action_payload(session)})


@app.post("/agentengine/api/v1/ListSessions")
async def list_sessions_action(request: ListSessionsActionRequest):
    service = resolve_session_service()
    offset = (request.Page - 1) * request.PageSize
    sessions = await service.list_sessions(
        request.AgentId,
        request.UserId or "user",
        offset=offset,
        limit=request.PageSize,
    )
    total = await service.count_sessions(request.AgentId, request.UserId or "user")
    session_payloads = [await _session_to_action_payload(session) for session in sessions]
    return _action_response(
        "ListSessions",
        {
            "Sessions": session_payloads,
            "Total": total,
            "Page": request.Page,
            "PageSize": request.PageSize,
        },
    )


@app.post("/agentengine/api/v1/GetSession")
async def get_session_action(request: SessionIdRequest):
    service = resolve_session_service()
    session = await _hydrate_session(await service.get_session(request.SessionId))
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return _action_response("GetSession", {"Session": await _session_to_action_payload(session)})


@app.post("/agentengine/api/v1/DeleteSession")
async def delete_session_action(request: SessionIdRequest):
    service = resolve_session_service()
    deleted = await service.delete_session(request.SessionId)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return _action_response("DeleteSession", {"Deleted": True})


@app.post("/agentengine/api/v1/ListSessionEvents")
async def list_session_events_action(request: ListSessionEventsActionRequest):
    service = resolve_session_service()
    events = await service.get_events(
        request.SessionId,
        offset=request.Offset,
        limit=request.Limit,
    )
    total = await service.count_events(request.SessionId)
    return _action_response(
        "ListSessionEvents",
        {
            "Events": [_event_to_action_payload(event) for event in events],
            "Total": total,
            "Offset": request.Offset or 0,
            "Limit": request.Limit if request.Limit is not None else len(events),
        },
    )


@app.post("/agentengine/api/v1/ListSessionCheckpoints")
async def list_session_checkpoints_action(request: ListSessionCheckpointsActionRequest):
    service = resolve_session_service()
    session = await service.get_session(request.SessionId)
    if not session or session.agent_id != request.AgentId:
        raise HTTPException(status_code=404, detail="Session not found")

    run_id_filter = str(request.RunId or "").strip()
    checkpoints: list[dict[str, Any]] = []
    for event in await service.get_events(request.SessionId):
        checkpoint = _checkpoint_event_to_action_payload(event)
        if checkpoint is None:
            continue
        if run_id_filter and checkpoint["RunId"] != run_id_filter:
            continue
        checkpoints.append(checkpoint)

    return _action_response(
        "ListSessionCheckpoints",
        {"Checkpoints": checkpoints},
    )


@app.post("/agentengine/api/v1/ListToolReceipts")
async def list_tool_receipts_action(request: ListToolReceiptsActionRequest):
    service = resolve_session_service()
    session = await service.get_session(request.SessionId)
    if not session or session.agent_id != request.AgentId:
        raise HTTPException(status_code=404, detail="Session not found")

    run_id_filter = str(request.RunId or "").strip()
    checkpoint_id_filter = str(request.CheckpointId or "").strip()
    receipts: list[dict[str, Any]] = []
    for event in await service.get_events(request.SessionId):
        receipt = _tool_receipt_event_to_action_payload(event)
        if receipt is None:
            continue
        if run_id_filter and receipt["RunId"] != run_id_filter:
            continue
        if checkpoint_id_filter and receipt["CheckpointId"] != checkpoint_id_filter:
            continue
        receipts.append(receipt)

    return _action_response(
        "ListToolReceipts",
        {"ToolReceipts": receipts},
    )


@app.post("/agentengine/api/v1/PreviewCheckpointResume")
async def preview_checkpoint_resume_action(request: PreviewCheckpointResumeActionRequest):
    service = resolve_session_service()
    session = await service.get_session(request.SessionId)
    if not session or session.agent_id != request.AgentId:
        raise HTTPException(status_code=404, detail="Session not found")

    events = await service.get_events(request.SessionId)
    checkpoint = None
    for event in reversed(events):
        candidate = _checkpoint_event_to_action_payload(event)
        if candidate is None:
            continue
        if candidate["RunId"] != str(request.RunId):
            continue
        if candidate["CheckpointId"] != str(request.CheckpointId):
            continue
        checkpoint = candidate
        break
    if checkpoint is None:
        raise HTTPException(status_code=404, detail="Checkpoint not found")

    return _action_response(
        "PreviewCheckpointResume",
        {"Preview": _build_checkpoint_resume_preview(checkpoint=checkpoint, events=events)},
    )


@app.post("/agentengine/api/v1/ResumeRun")
async def resume_run_action(request: ResumeRunActionRequest):
    service = resolve_session_service()
    session = await service.get_session(request.SessionId)
    if not session or session.agent_id != request.AgentId:
        raise HTTPException(status_code=404, detail="Session not found")

    checkpoint = await _find_session_checkpoint(
        service=service,
        session_id=request.SessionId,
        run_id=str(request.RunId),
        checkpoint_id=str(request.CheckpointId),
    )
    if checkpoint is None:
        raise HTTPException(status_code=404, detail="Checkpoint not found")

    resume_input = {
        "type": "agentengine.resume_checkpoint",
        "run_id": str(request.RunId),
        "checkpoint_id": str(request.CheckpointId),
        "resume_attempt_id": str(request.ResumeAttemptId or f"resume_{uuid.uuid4().hex}"),
        "framework": checkpoint["Framework"],
        "framework_ref": checkpoint["FrameworkRef"],
    }
    active_runner = _resolve_active_runner()
    user_id = session.user_id or "user"

    if request.Stream:
        resume_invocation_id = str(request.InvocationId or resume_input["resume_attempt_id"])
        return _detached_streaming_response(
            conversation.stream_responses_conversation_turn(
                runner=active_runner,
                agent_id=request.AgentId,
                user_id=user_id,
                messages=[],
                session_id=request.SessionId,
                model=request.Model,
                model_metadata=request.ModelMetadata,
                model_options=request.ModelOptions,
                request_metadata={"responses_conversation": True},
                resume_input=resume_input,
                invocation_id=resume_invocation_id,
                prepare_runner=_prepare_runner_for_model,
                session_service_provider=resolve_session_service,
            ),
            invocation_id=resume_invocation_id,
        )

    response_id = f"resp_{uuid.uuid4().hex}"
    resolved_session_id, result = await conversation.invoke_conversation_once(
        runner=active_runner,
        agent_id=request.AgentId,
        user_id=user_id,
        messages=[],
        session_id=request.SessionId,
        model=request.Model,
        model_metadata=request.ModelMetadata,
        model_options=request.ModelOptions,
        request_metadata={"responses_conversation": True},
        resume_input=resume_input,
        response_id=response_id,
        invocation_id=str(resume_input["resume_attempt_id"]),
        prepare_runner=_prepare_runner_for_model,
        session_service_provider=resolve_session_service,
    )
    payload = conversation.build_responses_payload(
        output_text=result["output_text"],
        model=request.Model,
        session_id=resolved_session_id,
        response_id=response_id,
        metadata=result.get("metadata") if isinstance(result.get("metadata"), dict) else None,
    )
    return _action_response("ResumeRun", payload)


@app.get("/agentengine/api/v1/SubscribeRunEvents", include_in_schema=False)
async def subscribe_run_events_action(
    SessionId: str = Query(...),
    InvocationId: str = Query(...),
    AfterSeqId: int = Query(0),
):
    session_id = str(SessionId or "").strip()
    invocation_id = str(InvocationId or "").strip()
    if not session_id or not invocation_id:
        raise HTTPException(status_code=400, detail="SessionId and InvocationId are required")

    async def event_generator() -> AsyncIterator[str]:
        service = resolve_session_service()
        last_seq_id = int(AfterSeqId or 0)
        deadline = time.monotonic() + 5 * 60
        while True:
            events = await service.get_events(session_id)
            matched_events = [
                event
                for event in events
                if event.seq_id > last_seq_id and event.invocation_id == invocation_id
            ]
            for event in matched_events:
                last_seq_id = max(last_seq_id, event.seq_id)
                payload = _event_to_action_payload(event)
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                if (
                    event.event_type == "run_status"
                    and str((event.content or {}).get("status") or "").strip().lower()
                    in _RUN_TERMINAL_STATUSES
                ):
                    yield "data: [DONE]\n\n"
                    return

            latest_status = None
            for event in events:
                if event.invocation_id != invocation_id or event.event_type != "run_status":
                    continue
                latest_status = str((event.content or {}).get("status") or "").strip().lower()
            if latest_status in _RUN_TERMINAL_STATUSES:
                yield "data: [DONE]\n\n"
                return
            if time.monotonic() > deadline:
                return
            await asyncio.sleep(0.25)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
@app.post("/agentengine/api/v1/UploadFile")
async def upload_file_action(file: UploadFile = File(...)):
    file_id = uuid.uuid4().hex
    data = await file.read()
    file_uri, _local_path = await AttachmentStorageService().store(
        data=data,
        file_id=file_id,
        display_name=file.filename,
        mime_type=file.content_type,
    )

    return _action_response(
        "UploadFile",
        {
            "FileData": {
                "fileUri": file_uri,
                "displayName": file.filename or "uploaded_file",
                "mimeType": file.content_type or "application/octet-stream",
                "sizeBytes": len(data),
            }
        }
    )


@app.get("/agentengine/api/v1/AttachmentContent", include_in_schema=False)
async def attachment_content_action(FileUri: str = Query(...)):
    loaded = AttachmentStorageService().read(FileUri)
    if loaded is None:
        raise HTTPException(status_code=404, detail="Attachment not found")

    return Response(
        content=loaded.data,
        media_type=loaded.mime_type or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{loaded.display_name}"'},
    )


@app.post("/agentengine/api/v1/ListWorkspaceFiles")
async def list_workspace_files_action(request: WorkspaceListActionRequest):
    response = await _workspace_runtime_request(
        "GET",
        "/_ksadk/workspace/v1/entries",
        params={
            "path": request.Path,
            "recursive": "true" if request.Recursive else "false",
        },
    )
    return _action_response("ListWorkspaceFiles", response.json())


@app.post("/agentengine/api/v1/AddWorkspaceFile")
async def upload_workspace_file_action(
    file: UploadFile = File(...),
    AgentId: Optional[str] = Form(None),
    Path: str = Form(...),
):
    del AgentId
    try:
        payload = await file.read()
    finally:
        await file.close()

    file_name = file.filename or Path.rsplit("/", 1)[-1]
    response = await _workspace_runtime_request(
        "POST",
        f"/_ksadk/workspace/v1/files/{quote(Path, safe='/')}",
        files={
            "file": (
                file_name,
                payload,
                file.content_type or "application/octet-stream",
            )
        },
    )
    return _action_response("AddWorkspaceFile", response.json())


@app.post("/agentengine/api/v1/DeleteWorkspaceFile")
async def delete_workspace_file_action(request: WorkspaceDeleteActionRequest):
    response = await _workspace_runtime_request(
        "DELETE",
        f"/_ksadk/workspace/v1/files/{quote(request.Path, safe='/')}",
    )
    return _action_response("DeleteWorkspaceFile", response.json())


@app.post("/agentengine/api/v1/CancelRun")
async def cancel_run_action(request: CancelRunActionRequest):
    detached = _DETACHED_STREAMS_BY_INVOCATION.get(request.InvocationId)
    found = detached is not None
    cancel_requested = False
    if detached is not None:
        cancel_requested = detached.cancel()
    runner_cancel_status = "not_found" if found else "unsupported"
    active_runner = _resolve_active_runner()
    if active_runner is not None:
        try:
            runner_result = active_runner.request_cancel(request.InvocationId)
            if isinstance(runner_result, str) and runner_result:
                runner_cancel_status = runner_result
            elif runner_result is True:
                runner_cancel_status = "accepted"
            elif runner_result is False and not found:
                runner_cancel_status = "not_found"
        except Exception as exc:
            runner_cancel_status = "error"
            logger.warning("CancelRun failed: %s", exc)
    runner_accepted = runner_cancel_status in {"accepted", "cancelling", "cancelled"}
    status = "cancelling" if found or runner_accepted else runner_cancel_status
    return _action_response(
        "CancelRun",
        {
            "Cancelled": bool(cancel_requested or runner_accepted),
            "Found": found,
            "Status": status,
            "RunnerCancelStatus": runner_cancel_status,
        },
    )


@app.get("/agentengine/api/v1/GetWorkspaceFileContent", include_in_schema=False)
async def get_workspace_file_content_action(
    FilePath: str = Query(...),
    AgentId: Optional[str] = Query(None),
):
    del AgentId
    response = await _workspace_runtime_request(
        "GET",
        f"/_ksadk/workspace/v1/files/{quote(FilePath, safe='/')}",
    )
    headers = {}
    for key in ("content-disposition", "last-modified"):
        value = response.headers.get(key)
        if value:
            headers[key] = value
    return Response(
        content=response.content,
        status_code=response.status_code,
        headers=headers,
        media_type=response.headers.get("content-type"),
    )


@app.get("/agentengine/api/v1/ws/{agent_id}/{file_path:path}", include_in_schema=False)
async def workspace_file_path_route(request: Request, agent_id: str, file_path: str):
    response = await _workspace_runtime_request(
        "GET",
        f"/_ksadk/workspace/v1/files/{quote(file_path, safe='/')}",
    )
    headers = {}
    for key in ("content-disposition", "last-modified"):
        value = response.headers.get(key)
        if value:
            headers[key] = value

    content_type = response.headers.get("content-type", "")
    is_html = "text/html" in content_type or file_path.lower().endswith((".html", ".htm"))

    if is_html and response.status_code == 200:
        del agent_id
        base_href = build_workspace_file_base_href(file_path)
        asset_source = f"{request.url.scheme}://{request.url.netloc}{base_href}"
        html_doc = response.content.decode("utf-8", errors="replace")
        html_doc = inject_workspace_html_preview(html_doc, file_path)
        headers.pop("content-disposition", None)
        headers["Content-Security-Policy"] = build_workspace_preview_csp(asset_source)
        return Response(
            content=html_doc.encode("utf-8"),
            status_code=response.status_code,
            headers=headers,
            media_type="text/html; charset=utf-8",
        )

    return Response(
        content=response.content,
        status_code=response.status_code,
        headers=headers,
        media_type=content_type,
    )


@app.get("/agentengine/api/v1/ExportWorkspaceZip", include_in_schema=False)
async def export_workspace_zip(
    AgentId: Optional[str] = Query(None),
    Path: str = Query("."),
):
    del AgentId
    dir_path = Path.strip() or "."
    response = await _workspace_runtime_request(
        "GET",
        "/_ksadk/workspace/v1/entries",
        params={"path": dir_path, "recursive": "true"},
    )
    data = response.json() if response.status_code == 200 else {}
    entries = data.get("Entries", []) if isinstance(data, dict) else []
    root = _workspace_root_dir()
    root_resolved = root.resolve()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry in entries:
            if entry.get("Type") != "file":
                continue
            rel = entry.get("Path", "")
            if not rel:
                continue
            rel_path = PurePosixPath(rel)
            if rel_path.is_absolute() or ".." in rel_path.parts:
                continue
            target = root.joinpath(*rel_path.parts)
            if target.is_symlink():
                continue
            try:
                resolved_target = target.resolve(strict=True)
            except OSError:
                continue
            if not resolved_target.is_relative_to(root_resolved):
                continue
            if resolved_target.is_file():
                zf.writestr(rel_path.as_posix(), resolved_target.read_bytes())
    buf.seek(0)
    zip_name = f"workspace-{dir_path.replace('/', '-')}.zip" if dir_path != "." else "workspace.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
    )


def _normalize_model_catalog_items(raw_models: list[Any]) -> list[dict[str, Any]]:
    """统一模型目录 shape，并按 id 去重。

    这里刻意保留上游原始 dict 字段，再补 canonical metadata。
    这样两周后模型服务扩展字段时，这一层不会再次把信息裁掉。
    """

    normalized_by_id: dict[str, dict[str, Any]] = {}
    for raw_model in raw_models:
        item = normalize_model_metadata(raw_model)
        normalized_by_id[item["id"]] = item
    return sorted(normalized_by_id.values(), key=lambda item: item["id"])


async def _build_models_payload() -> dict[str, Any]:
    import os
    import httpx

    api_base = os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE")
    api_key = os.getenv("OPENAI_API_KEY")
    current_model, source = _resolve_current_model()

    def _fallback_catalog() -> dict[str, Any]:
        models = _normalize_model_catalog_items([current_model]) if current_model else []
        return {
            "data": models,
            "current": current_model,
            "source": source,
        }

    if not api_base:
        return _fallback_catalog()

    try:
        base_url = api_base.rstrip("/")
        if base_url.endswith("/v1"):
            url = f"{base_url}/models"
        else:
            url = f"{base_url}/v1/models"

        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        async with httpx.AsyncClient(verify=False, timeout=10) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()

            if isinstance(data, list):
                models = _normalize_model_catalog_items(list(data))
            else:
                models = _normalize_model_catalog_items(list(data.get("data", [])))
            if current_model and all(str(item.get("id") or "").strip() != current_model for item in models):
                models = _normalize_model_catalog_items([*models, current_model])
            return {"data": models, "current": current_model, "source": source}
    except Exception as e:
        logger.error(f"Failed to fetch models: {e}")
        fallback = _fallback_catalog()
        fallback["error"] = str(e)
        return fallback

class ListAgentModelsRequest(BaseModel):
    AgentId: Optional[str] = None
    Name: Optional[str] = None


@app.post("/agentengine/api/v1/ListAgentModels")
async def list_agent_models_action(_request: ListAgentModelsRequest):
    payload = await _build_models_payload()
    return _action_response(
        "ListAgentModels",
        {
            "Models": payload.get("data", []),
            "Current": payload.get("current"),
            "Source": payload.get("source", ""),
        },
    )


@app.get("/v1/models")
async def list_openai_models():
    """Expose the current model catalog through the OpenAI-compatible path."""

    payload = await _build_models_payload()
    return {
        "object": "list",
        "data": payload.get("data", []),
        "current": payload.get("current"),
        "source": payload.get("source", ""),
    }


@app.post("/agentengine/api/v1/RunAgent")
async def run_agent_action(request: RunAgentActionRequest):
    api_format = (request.ApiFormat or "responses").strip().lower()
    run_user_id = _clean_optional_string(request.UserId) or "user"
    account_id = _clean_optional_string(request.AccountId)
    service = resolve_session_service()
    resume_input = (
        conversation.extract_responses_resume_input(request.ResponsesInput)
        if request.ResponsesInput is not None
        else None
    )
    resume_input = await _resolve_checkpoint_resume_input_from_session(
        service=service,
        agent_id=request.AgentId,
        session_id=request.SessionId,
        resume_input=resume_input,
    )
    if resume_input is not None:
        messages = []
    elif request.ResponsesInput is not None and api_format == "responses":
        messages = conversation.normalize_responses_input(request.ResponsesInput)
    else:
        messages = conversation.normalize_kop_messages(request.Messages)
    request_metadata = (
        {"previous_response_id": request.PreviousResponseId}
        if request.PreviousResponseId
        else {}
    )
    if api_format == "responses":
        request_metadata["responses_conversation"] = True

    if request.Stream:
        if api_format == "chat_completions":
            completion_request = ChatCompletionRequest(
                messages=messages,
                model=request.Model,
                model_metadata=request.ModelMetadata,
                model_options=request.ModelOptions,
                stream=True,
                session_id=request.SessionId,
                user=run_user_id,
                account_id=account_id,
            )
            return await chat_completions(completion_request)
        return _detached_streaming_response(
            conversation.stream_responses_conversation_turn(
                runner=_resolve_active_runner(),
                agent_id=request.AgentId,
                user_id=run_user_id,
                messages=messages,
                session_id=request.SessionId,
                model=request.Model,
                model_metadata=request.ModelMetadata,
                model_options=request.ModelOptions,
                request_metadata=request_metadata or None,
                resume_input=resume_input,
                account_id=account_id,
                invocation_id=request.InvocationId,
                prepare_runner=_prepare_runner_for_model,
                session_service_provider=resolve_session_service,
            ),
            invocation_id=request.InvocationId,
        )

    responses_response_id = (
        f"resp_{uuid.uuid4().hex}" if api_format != "chat_completions" else None
    )
    resolved_session_id, result = await conversation.invoke_conversation_once(
        runner=_resolve_active_runner(),
        agent_id=request.AgentId,
        user_id=run_user_id,
        messages=messages,
        session_id=request.SessionId,
        model=request.Model,
        model_metadata=request.ModelMetadata,
        model_options=request.ModelOptions,
        request_metadata=request_metadata or None,
        resume_input=resume_input,
        response_id=responses_response_id,
        account_id=account_id,
        invocation_id=request.InvocationId,
        prepare_runner=_prepare_runner_for_model,
        session_service_provider=resolve_session_service,
    )
    output_text = result["output_text"]
    if api_format == "chat_completions":
        payload = conversation.build_chat_completions_payload(
            output_text=output_text,
            model=request.Model,
            session_id=resolved_session_id,
            metadata=result.get("metadata"),
        )
    else:
        payload = conversation.build_responses_payload(
            output_text=output_text,
            model=request.Model,
            session_id=resolved_session_id,
            response_id=responses_response_id,
            metadata=result.get("metadata") if isinstance(result.get("metadata"), Mapping) else None,
        )
    return _action_response("RunAgent", payload)


# ============================================================
# Session Management API (ADK Web Compatible)
# ============================================================


@app.post("/apps/{app_name}/users/{user_id}/sessions")
async def create_session(app_name: str, user_id: str, request: Request):
    """Create a new session"""
    # Check if importing existing events
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    service = resolve_session_service()
    session = await _ensure_session(app_name, user_id, body.get("sessionId") or body.get("id"))

    for raw_event in body.get("events", []):
        session_event = SessionEvent.from_dict(raw_event, session_id=session.id)
        await service.append_event(session.id, session_event)

    hydrated = await _hydrate_session(await service.get_session(session.id))
    return hydrated.to_legacy_dict() if hydrated else session.to_legacy_dict()


@app.get("/apps/{app_name}/users/{user_id}/sessions")
async def list_sessions(app_name: str, user_id: str):
    """List all sessions for a user"""
    service = resolve_session_service()
    sessions = await service.list_sessions(app_name, user_id)
    hydrated: List[Dict[str, Any]] = []
    for session in sessions:
        session.events = await service.get_events(session.id)
        hydrated.append(session.to_legacy_dict())
    return hydrated


@app.get("/apps/{app_name}/users/{user_id}/sessions/{session_id}")
async def get_session(app_name: str, user_id: str, session_id: str):
    """Get a specific session with its events"""
    service = resolve_session_service()
    session = await _hydrate_session(await service.get_session(session_id))
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session.to_legacy_dict()


@app.delete("/apps/{app_name}/users/{user_id}/sessions/{session_id}")
async def delete_session(app_name: str, user_id: str, session_id: str):
    """Delete a session"""
    service = resolve_session_service()
    if await service.delete_session(session_id):
        return {"status": "deleted"}
    raise HTTPException(status_code=404, detail="Session not found")


# ============================================================
# Memory API - Save session to long-term memory
# ============================================================


@app.post("/apps/{app_name}/users/{user_id}/sessions/{session_id}/save_memory")
async def save_session_to_memory(app_name: str, user_id: str, session_id: str):
    """将指定 session 保存到长期记忆

    当配置了 KSADK_LTM_BACKEND 时，将 session 中的用户消息
    持久化到长期记忆后端，供后续 session 通过 load_memory 工具检索。
    """
    active_runner = _ensure_runner_loaded()

    # 检查 runner 是否支持长期记忆
    from ksadk.runners.adk_runner import ADKRunner as _ADKRunner

    if not isinstance(active_runner, _ADKRunner):
        raise HTTPException(
            status_code=400, detail="Long-term memory is only supported with ADK runner"
        )

    if not active_runner._long_term_memory:
        raise HTTPException(
            status_code=400,
            detail="Long-term memory not configured. Set KSADK_LTM_BACKEND environment variable.",
        )

    # 查找 ADK 内部 session ID
    internal_session_id = active_runner._session_map.get(session_id, session_id)

    success = await active_runner.save_session_to_long_term_memory(
        session_id=internal_session_id,
        user_id=user_id,
    )

    if success:
        return {"status": "saved", "session_id": session_id}
    else:
        raise HTTPException(status_code=500, detail="Failed to save session to long-term memory")


# ============================================================
# Run SSE - Core Agent Execution Endpoint
# ============================================================


@app.post("/run_sse")
async def run_sse(request: AgentRunRequest):
    """Unified Streaming Endpoint compatible with ADK Web

    Respects the `streaming` parameter:
    - streaming=False: Accumulate full response, send as single event
    - streaming=True: Stream tokens as they arrive (real-time)
    """
    active_runner = _ensure_runner_loaded()
    _prepare_runner_for_model(active_runner, request.model)
    use_streaming = request.streaming
    normalized_message = conversation.normalize_parts_content(request.newMessage.parts if request.newMessage else [])
    user_message = {
        "role": "user",
        "content": str(normalized_message.get("content") or ""),
        "display_content": str(normalized_message.get("display_content") or ""),
        "parts": list(normalized_message.get("parts") or []),
        "attachments": list(normalized_message.get("attachments") or []),
        "attachment_results": list(normalized_message.get("attachment_results") or []),
    }

    model_version = "models/gemini-pro" if "gemini" in request.appName.lower() else "models/unknown"
    prepared_non_stream: conversation.PreparedConversationTurn | None = None
    if request.sessionId:
        await conversation.ensure_conversation_session(
            agent_id=request.appName,
            user_id=request.userId,
            session_id=request.sessionId,
            session_service_provider=resolve_session_service,
        )
    if not use_streaming:
        prepared_non_stream = await conversation.build_run_input(
            agent_id=request.appName,
            user_id=request.userId,
            session_id=request.sessionId,
            messages=[user_message],
            state_delta=request.stateDelta or {},
            invocation_id=request.invocationId,
            session_service_provider=resolve_session_service,
        )
        await conversation.append_run_status_event(
            session_id=prepared_non_stream.session_id,
            author=active_runner.detection_result.name,
            status="in_progress",
            invocation_id=prepared_non_stream.invocation_id,
            session_service_provider=resolve_session_service,
        )

    async def event_generator():
        if not use_streaming:
            try:
                assert prepared_non_stream is not None
                session_id = prepared_non_stream.session_id
                user_input = prepared_non_stream.user_input
                attachments = prepared_non_stream.attachments
                attachment_results = prepared_non_stream.attachment_results
                current_attachments = prepared_non_stream.current_attachments
                current_attachment_results = prepared_non_stream.current_attachment_results
                input_content = prepared_non_stream.input_content
                input_messages = prepared_non_stream.input_messages
                user_parts = prepared_non_stream.user_parts
                history = prepared_non_stream.history
                invocation_id = prepared_non_stream.invocation_id
                common_metadata = {
                    "modelVersion": model_version,
                    "usageMetadata": {
                        "promptTokenCount": len(user_input),
                        "candidatesTokenCount": 0,
                        "totalTokenCount": len(user_input),
                    },
                }
                input_data = {
                    "session_id": session_id,
                    "input": user_input,
                    "history": history,
                    "input_content": list(input_content),
                    "input_messages": list(input_messages),
                    "input_parts": list(user_parts),
                    "attachments": attachments,
                    "attachment_results": attachment_results,
                    "current_attachments": current_attachments,
                    "current_attachment_results": current_attachment_results,
                    "has_current_files": prepared_non_stream.has_current_files,
                    "model": request.model,
                }
                result = await active_runner.invoke(input_data)
                final_text = result.get("output", "")
                response_event = {
                    "id": str(uuid.uuid4()),
                    "author": active_runner.detection_result.name,
                    "sessionId": session_id,
                    "invocationId": invocation_id,
                    "content": {"role": "model", "parts": [{"text": final_text}]},
                    "actions": {"finishReason": "STOP"},
                    "modelVersion": common_metadata["modelVersion"],
                    "usageMetadata": {
                        "promptTokenCount": len(user_input),
                        "candidatesTokenCount": len(final_text),
                        "totalTokenCount": len(user_input) + len(final_text),
                    },
                    "timestamp": int(time.time() * 1000),
                }
                yield f"data: {json.dumps(response_event, ensure_ascii=False)}\n\n"
                if final_text:
                    await conversation.append_conversation_event(
                        session_id=session_id,
                        author=active_runner.detection_result.name,
                        role="model",
                        text=final_text,
                        invocation_id=invocation_id,
                        event_type="assistant_message",
                        session_service_provider=resolve_session_service,
                    )
                await conversation.append_run_status_event(
                    session_id=session_id,
                    author=active_runner.detection_result.name,
                    status="completed",
                    invocation_id=invocation_id,
                    session_service_provider=resolve_session_service,
                )

            except Exception as e:
                logger.error(f"Error in invoke: {e}")
                await conversation.append_run_status_event(
                    session_id=session_id,
                    author=active_runner.detection_result.name,
                    status="failed",
                    invocation_id=invocation_id,
                    detail=str(e),
                    session_service_provider=resolve_session_service,
                )
                error_event = {
                    "id": str(uuid.uuid4()),
                    "sessionId": session_id,
                    "invocationId": invocation_id,
                    "error": str(e),
                    "errorMessage": str(e),
                    "timestamp": int(time.time() * 1000),
                }
                yield f"data: {json.dumps(error_event, ensure_ascii=False)}\n\n"
        else:
            try:
                compaction_preview = await conversation.preview_auto_compaction(
                    agent_id=request.appName,
                    user_id=request.userId,
                    session_id=request.sessionId,
                    messages=[user_message],
                    session_service_provider=resolve_session_service,
                )
                if compaction_preview.should_compact:
                    yield conversation.build_compaction_sse_event(
                        phase="start",
                        trigger="auto",
                        total_chars=compaction_preview.total_chars,
                        group_count=compaction_preview.group_count,
                    )

                prepared = await conversation.build_run_input(
                    agent_id=request.appName,
                    user_id=request.userId,
                    session_id=request.sessionId,
                    messages=[user_message],
                    state_delta=request.stateDelta or {},
                    invocation_id=request.invocationId,
                    session_service_provider=resolve_session_service,
                )
                if prepared.compaction_triggered:
                    yield conversation.build_compaction_sse_event(
                        phase="done",
                        trigger=str(prepared.compaction_trigger or "auto"),
                        compacted_until_seq_id=prepared.compacted_until_seq_id,
                        total_chars=compaction_preview.total_chars if compaction_preview.should_compact else None,
                        group_count=compaction_preview.group_count if compaction_preview.should_compact else None,
                    )

                session_id = prepared.session_id
                user_input = prepared.user_input
                attachments = prepared.attachments
                attachment_results = prepared.attachment_results
                current_attachments = prepared.current_attachments
                current_attachment_results = prepared.current_attachment_results
                input_content = prepared.input_content
                input_messages = prepared.input_messages
                user_parts = prepared.user_parts
                history = prepared.history
                invocation_id = prepared.invocation_id
                common_metadata = {
                    "modelVersion": model_version,
                    "usageMetadata": {
                        "promptTokenCount": len(user_input),
                        "candidatesTokenCount": 0,
                        "totalTokenCount": len(user_input),
                    },
                }
                await conversation.append_run_status_event(
                    session_id=session_id,
                    author=active_runner.detection_result.name,
                    status="in_progress",
                    invocation_id=invocation_id,
                    session_service_provider=resolve_session_service,
                )

                client_visible_text = ""
                authoritative_text = ""
                responses_output: list[Any] = []
                responses_response_id: str | None = None
                stream_iter = active_runner.stream(
                    {
                        "session_id": session_id,
                        "input": user_input,
                        "history": history,
                        "input_content": list(input_content),
                        "input_messages": list(input_messages),
                        "input_parts": list(user_parts),
                        "attachments": attachments,
                        "attachment_results": attachment_results,
                        "current_attachments": current_attachments,
                        "current_attachment_results": current_attachment_results,
                        "has_current_files": prepared.has_current_files,
                        "model": request.model,
                    }
                )
                while True:
                    try:
                        chunk = await asyncio.wait_for(stream_iter.__anext__(), timeout=15)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    event_id = str(uuid.uuid4())
                    if chunk.get("type") == "responses_output":
                        raw_output = chunk.get("output")
                        responses_output = raw_output if isinstance(raw_output, list) else []
                        raw_response_id = chunk.get("response_id")
                        responses_response_id = str(raw_response_id) if raw_response_id else responses_response_id
                        continue
                    if chunk.get("type") == "thinking":
                        delta = str(chunk.get("delta", ""))
                        if delta:
                            await conversation.append_reasoning_event(
                                session_id=session_id,
                                author=active_runner.detection_result.name,
                                text=delta,
                                invocation_id=invocation_id,
                                session_service_provider=resolve_session_service,
                            )
                            yield f"event: response.reasoning.delta\ndata: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"
                        continue
                    if chunk.get("type") == "text":
                        delta_text = chunk.get("delta", "")
                        client_visible_text += delta_text
                        authoritative_text = client_visible_text
                        response_event = {
                            "id": event_id,
                            "author": chunk.get("node", active_runner.detection_result.name),
                            "sessionId": session_id,
                            "invocationId": invocation_id,
                            "content": {"role": "model", "parts": [{"text": delta_text}]},
                            "partial": True,
                            "timestamp": int(time.time() * 1000),
                        }
                        yield f"data: {json.dumps(response_event, ensure_ascii=False)}\n\n"
                        continue
                    if chunk.get("type") == "tool_call":
                        yield (
                            "event: response.tool_call\n"
                            f"data: {json.dumps({'name': chunk.get('tool_name'), 'args': chunk.get('tool_args', {})}, ensure_ascii=False)}\n\n"
                        )
                        tool_event = {
                            "id": event_id,
                            "author": chunk.get("node", "tool"),
                            "sessionId": session_id,
                            "invocationId": invocation_id,
                            "content": {
                                "role": "model",
                                "parts": [
                                    {
                                        "functionCall": {
                                            "name": chunk.get("tool_name", "unknown"),
                                            "args": chunk.get("tool_args", {}),
                                        }
                                    }
                                ],
                            },
                            "actions": {
                                "finishReason": "STOP",
                                "stateDelta": {},
                            },
                            "modelVersion": common_metadata["modelVersion"],
                            "timestamp": int(time.time() * 1000),
                        }
                        yield f"data: {json.dumps(tool_event, ensure_ascii=False)}\n\n"
                        await conversation.append_conversation_event(
                            session_id=session_id,
                            author=chunk.get("node", "tool"),
                            role="model",
                            text="",
                            invocation_id=invocation_id,
                            event_type="tool_call",
                            session_service_provider=resolve_session_service,
                            metadata={
                                "tool_name": chunk.get("tool_name", "unknown"),
                                "tool_args": chunk.get("tool_args", {}),
                            },
                        )
                        continue
                    if chunk.get("type") == "tool_result":
                        await conversation.append_conversation_event(
                            session_id=session_id,
                            author=active_runner.detection_result.name,
                            role="user",
                            text=str(chunk.get("tool_output", "")),
                            invocation_id=invocation_id,
                            event_type="tool_result",
                            session_service_provider=resolve_session_service,
                            metadata={
                                "tool_name": chunk.get("tool_name"),
                                "tool_output": chunk.get("tool_output", {}),
                            },
                        )
                        yield (
                            "event: response.tool_result\n"
                            f"data: {json.dumps({'name': chunk.get('tool_name'), 'output': chunk.get('tool_output', {})}, ensure_ascii=False)}\n\n"
                        )
                        continue
                    if chunk.get("type") == "interrupt":
                        await conversation.append_conversation_event(
                            session_id=session_id,
                            author=active_runner.detection_result.name,
                            role="model",
                            text="approval requested",
                            invocation_id=invocation_id,
                            event_type="approval_request",
                            session_service_provider=resolve_session_service,
                            metadata={"interrupt_info": chunk.get("interrupt_info")},
                        )
                        yield (
                            "event: response.approval_request\n"
                            f"data: {json.dumps({'interrupt_info': chunk.get('interrupt_info')}, ensure_ascii=False)}\n\n"
                        )
                        continue
                    if chunk.get("type") == "final":
                        final_text = chunk.get("output", "")
                        if not final_text:
                            continue
                        authoritative_text = final_text
                        if final_text != client_visible_text:
                            final_event = {
                                "id": event_id,
                                "author": active_runner.detection_result.name,
                                "sessionId": session_id,
                                "invocationId": invocation_id,
                                "content": {"role": "model", "parts": [{"text": final_text}]},
                                "actions": {"finishReason": "STOP"},
                                "modelVersion": common_metadata["modelVersion"],
                                "usageMetadata": {
                                    "promptTokenCount": len(user_input),
                                    "candidatesTokenCount": len(final_text),
                                    "totalTokenCount": len(user_input) + len(final_text),
                                },
                                "timestamp": int(time.time() * 1000),
                            }
                            yield f"data: {json.dumps(final_event, ensure_ascii=False)}\n\n"
                            client_visible_text = final_text

                if authoritative_text:
                    await conversation.append_conversation_event(
                        session_id=session_id,
                        author=active_runner.detection_result.name,
                        role="model",
                        text=authoritative_text,
                        invocation_id=invocation_id,
                        event_type="assistant_message",
                        metadata={
                            **({"responses_output": responses_output} if responses_output else {}),
                            **({"response_id": responses_response_id} if responses_response_id else {}),
                        },
                        session_service_provider=resolve_session_service,
                    )
                await conversation.append_run_status_event(
                    session_id=session_id,
                    author=active_runner.detection_result.name,
                    status="completed",
                    invocation_id=invocation_id,
                    session_service_provider=resolve_session_service,
                )

            except Exception as e:
                logger.error(f"Error in stream: {e}")
                await conversation.append_run_status_event(
                    session_id=session_id,
                    author=active_runner.detection_result.name,
                    status="failed",
                    invocation_id=invocation_id,
                    detail=str(e),
                    session_service_provider=resolve_session_service,
                )
                error_event = {
                    "id": str(uuid.uuid4()),
                    "sessionId": session_id,
                    "invocationId": invocation_id,
                    "error": str(e),
                    "errorMessage": str(e),
                    "timestamp": int(time.time() * 1000),
                }
                yield f"data: {json.dumps(error_event, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ============================================================
# Trace / Debug API (ADK Web Compatible)
# ============================================================


@app.get("/debug/trace/session/{session_id}")
async def get_session_trace(session_id: str):
    """Get traces for a session - returns array of Span objects"""
    exporter = get_memory_exporter()
    if not exporter:
        return []  # Return empty array, not object

    # Get all spans and transform to ADK-Web expected format
    raw_spans = exporter.get_finished_spans()

    # Get session events for invocation mapping
    service = resolve_session_service()
    events = await service.get_events(session_id)

    # Build invocation ID mapping from session events
    invocation_ids = {}
    for event in events:
        if event.id and event.invocation_id:
            invocation_ids[event.id] = event.invocation_id

    # Transform spans to ADK-Web format
    spans = []
    for span in raw_spans:
        # Use session_id as trace_id for grouping
        trace_id = span.get("trace_id", session_id)

        # Get or create invocation_id
        invocation_id = span.get("attributes", {}).get("gcp.vertex.agent.invocation_id")
        if not invocation_id:
            # Try to derive from event association
            invocation_id = trace_id[:36] if len(trace_id) >= 36 else trace_id

        # Build attributes with required ADK fields
        attrs = span.get("attributes", {}).copy()
        attrs["gcp.vertex.agent.invocation_id"] = invocation_id

        # If this is a LLM span, add request/response
        if "llm" in span.get("name", "").lower() or "invoke" in span.get("name", "").lower():
            if "user.input" in attrs:
                attrs["gcp.vertex.agent.llm_request"] = json.dumps(
                    {
                        "contents": [
                            {"role": "user", "parts": [{"text": attrs.get("user.input", "")}]}
                        ]
                    }
                )
            if "agent.output" in attrs:
                attrs["gcp.vertex.agent.llm_response"] = json.dumps(
                    {
                        "candidates": [
                            {
                                "content": {
                                    "role": "model",
                                    "parts": [{"text": attrs.get("agent.output", "")}],
                                }
                            }
                        ]
                    }
                )

        formatted_span = {
            "trace_id": trace_id,
            "span_id": span.get("span_id", str(uuid.uuid4())[:16]),
            "parent_span_id": span.get("parent_span_id"),
            "name": span.get("name", "unknown"),
            "start_time": span.get("start_time", 0),
            "end_time": span.get("end_time", 0),
            "attributes": attrs,
            "status": span.get("status", {}),
        }
        spans.append(formatted_span)

    return spans  # Return array directly


@app.get("/debug/trace/{event_id}")
async def get_event_trace(event_id: str):
    """Get trace for a specific event - returns array of Span objects"""
    exporter = get_memory_exporter()
    if not exporter:
        return []

    spans = exporter.get_finished_spans()
    # Filter by event_id or return recent spans
    filtered = [s for s in spans if s.get("attributes", {}).get("event_id") == event_id]
    return filtered if filtered else spans[-10:]


@app.get("/apps/{app_name}/users/{user_id}/sessions/{session_id}/events/{event_id}/graph")
async def get_event_graph(app_name: str, user_id: str, session_id: str, event_id: str):
    """Get event graph (DOT format) - placeholder"""
    return {"dotSrc": None}


# ============================================================
# OpenAI Compatible API
# ============================================================


class ChatCompletionRequest(BaseModel):
    messages: List[Dict[str, Any]]
    model: Optional[str] = None
    model_metadata: Optional[Dict[str, Any]] = None
    model_options: Optional[Dict[str, Any]] = None
    stream: bool = False
    session_id: Optional[str] = None
    user: Optional[str] = None
    account_id: Optional[str] = None
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None


@app.post("/v1/responses")
async def responses(request: ResponsesRequest):
    """OpenAI Responses 兼容接口。"""
    active_runner = _resolve_active_runner()
    resolved_session_id, resolved_user_id = _resolve_responses_session_and_user(request)
    agent_id = _runtime_agent_id(active_runner)

    resume_input = conversation.extract_responses_resume_input(request.input)
    resume_input = await _resolve_checkpoint_resume_input_from_session(
        service=resolve_session_service(),
        agent_id=agent_id,
        session_id=resolved_session_id,
        resume_input=resume_input,
    )
    messages = [] if resume_input is not None else conversation.normalize_responses_input(request.input)
    request_metadata = dict(request.metadata or {})
    if request.previous_response_id:
        request_metadata.setdefault("previous_response_id", request.previous_response_id)
    if request.prompt_cache_key:
        request_metadata.setdefault("prompt_cache_key", request.prompt_cache_key)
    if request.safety_identifier:
        request_metadata.setdefault("safety_identifier", request.safety_identifier)
    if request.user:
        request_metadata.setdefault("user", request.user)
    if request.conversation is not None:
        request_metadata.setdefault("conversation", request.conversation)
    if request.store is not None:
        request_metadata.setdefault("store", request.store)
    account_id = _clean_optional_string(request.account_id)
    invocation_id = _metadata_invocation_id(request_metadata)

    if request.stream:
        return _detached_streaming_response(
            conversation.stream_responses_conversation_turn(
                runner=active_runner,
                agent_id=agent_id,
                user_id=resolved_user_id,
                messages=messages,
                session_id=resolved_session_id,
                model=request.model,
                model_metadata=request.model_metadata,
                model_options=request.model_options,
                instructions=request.instructions,
                request_metadata=request_metadata,
                resume_input=resume_input,
                account_id=account_id,
                invocation_id=invocation_id,
                prepare_runner=_prepare_runner_for_model,
                session_service_provider=resolve_session_service,
            ),
            invocation_id=invocation_id,
        )

    response_id = f"resp_{uuid.uuid4().hex}"
    resolved_session_id, result = await conversation.invoke_conversation_once(
        runner=active_runner,
        agent_id=agent_id,
        user_id=resolved_user_id,
        messages=messages,
        session_id=resolved_session_id,
        model=request.model,
        model_metadata=request.model_metadata,
        model_options=request.model_options,
        instructions=request.instructions,
        request_metadata=request_metadata,
        resume_input=resume_input,
        response_id=response_id,
        account_id=account_id,
        invocation_id=invocation_id,
        prepare_runner=_prepare_runner_for_model,
        session_service_provider=resolve_session_service,
    )
    return conversation.build_responses_payload(
        output_text=result["output_text"],
        model=request.model,
        session_id=resolved_session_id,
        response_id=response_id,
        metadata=result.get("metadata") if isinstance(result.get("metadata"), dict) else request_metadata,
    )


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    """OpenAI 兼容的聊天补全接口 (支持流式和非流式)"""
    active_runner = _resolve_active_runner()
    messages = conversation.normalize_kop_messages(request.messages)
    agent_id = _runtime_agent_id(active_runner)
    resolved_user_id = _clean_optional_string(request.user) or "user"
    account_id = _clean_optional_string(request.account_id)

    if request.stream:
        return StreamingResponse(
            conversation.stream_conversation_turn(
                runner=active_runner,
                agent_id=agent_id,
                user_id=resolved_user_id,
                messages=messages,
                session_id=request.session_id,
                model=request.model,
                model_metadata=request.model_metadata,
                model_options=request.model_options,
                account_id=account_id,
                prepare_runner=_prepare_runner_for_model,
                session_service_provider=resolve_session_service,
            ),
            media_type="text/event-stream",
        )

    resolved_session_id, result = await conversation.invoke_conversation_once(
        runner=active_runner,
        agent_id=agent_id,
        user_id=resolved_user_id,
        messages=messages,
        session_id=request.session_id,
        model=request.model,
        model_metadata=request.model_metadata,
        model_options=request.model_options,
        account_id=account_id,
        prepare_runner=_prepare_runner_for_model,
        session_service_provider=resolve_session_service,
    )
    return conversation.build_chat_completions_payload(
        output_text=result["output_text"],
        model=request.model,
        session_id=resolved_session_id,
        metadata=result.get("metadata"),
    )


# ============================================================
# Stub Endpoints for ADK-Web Compatibility
# ============================================================


@app.get("/apps/{app_name}/eval_sets")
async def list_eval_sets(app_name: str):
    """List evaluation sets - stub for ADK-Web"""
    return []


@app.get("/apps/{app_name}/eval_results")
async def list_eval_results(app_name: str):
    """List evaluation results - stub for ADK-Web"""
    return []


@app.get("/builder/app/{app_name}")
async def get_agent_builder(app_name: str, ts: int = 0, tmp: bool = False, file_path: str = None):
    """Get agent builder config - stub for ADK-Web"""
    # Return minimal YAML config for non-ADK projects
    return f"""name: {app_name}
model: glm-5.1
description: {app_name} agent
instruction: You are a helpful assistant.
"""


@app.post("/builder/save")
async def save_agent_builder(request: Request, tmp: bool = False):
    """Save agent builder config - stub for ADK-Web"""
    return True


@app.post("/builder/app/{app_name}/cancel")
async def cancel_agent_changes(app_name: str):
    """Cancel agent builder changes - stub for ADK-Web"""
    return True


# Legacy /traces endpoint
@app.get("/traces")
async def get_traces(limit: int = 50):
    """Get recent traces (OpenTelemetry)"""
    exporter = get_memory_exporter()
    if not exporter:
        return {"traces": []}

    spans = exporter.get_finished_spans()
    traces = []
    for span in spans[-limit:]:
        traces.append(
            {
                "name": span.get("name", "unknown"),
                "status": span.get("status", {}).get("code", "UNSET"),
                "start_time": span.get("start_time"),
                "end_time": span.get("end_time"),
                "attributes": span.get("attributes", {}),
            }
        )
    return {"traces": traces}


# ============================================================
# Static File Hosting
# ============================================================

# 静态文件目录
STATIC_DIR = Path(__file__).parent / "static"

# 使用 StaticFiles 挂载统一 Agent UI 静态文件
if STATIC_DIR.exists() and (STATIC_DIR / "index.html").exists():
    @app.get("/chat", include_in_schema=False)
    @app.get("/build", include_in_schema=False)
    @app.get("/deploy", include_in_schema=False)
    async def serve_agent_workbench_shell():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    logger.info(f"Unified Agent UI mounted from: {STATIC_DIR}")
else:
    logger.warning(f"Static files not found at: {STATIC_DIR}")
    logger.warning("Build and sync the unified Agent UI static bundle first")

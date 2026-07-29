"""Shared runtime server helpers, integrated resources, and health routes."""

from __future__ import annotations

import base64
import io
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response

from ksadk.conversations.attachments import compact_attachment_result_for_session
from ksadk.conversations.model_context import normalize_model_metadata
from ksadk.runners.base_runner import BaseRunner
from ksadk.runtime_state import load_state as load_runtime_state
from ksadk.server.factory import (
    RuntimeAppState,
    bind_runtime_state,
    get_runner,
    get_state,
)
from ksadk.server.terminal_sessions import (
    TerminalSessionManager,
    native_terminal_supported,
    register_terminal_routes,
)
from ksadk.sessions import Session
from ksadk.sessions.local_service import resolve_local_session_dir
from ksadk.ui_config import UI_PROFILE_CUSTOM, resolve_ui_config
from ksadk_runtime_common.workspace_files import (
    create_workspace_files_router,
    workspace_files_enabled,
)

from . import dependencies as deps
from .routers import health_meta_router

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent.parent / "static"

_RESERVED_UI_PATHS = {"/", "/chat", "/build", "/deploy"}
_CUSTOM_API_PROXY_ENV_KEYS = ("KSADK_USER_BACKEND_URL", "LUOLUO_USER_BACKEND_URL")
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

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
    return Path(resolve_local_session_dir()) / "workspace"


_NATIVE_TUI_FRAMEWORKS = {"hermes", "openclaw"}


def _current_framework() -> str:
    runner = get_state().runner
    if not runner:
        return ""
    detection_type = getattr(getattr(runner, "detection_result", None), "type", None)
    return str(getattr(detection_type, "value", detection_type) or "").strip().lower()


def _build_native_terminal_capability(framework: str) -> dict[str, Any]:
    enabled = (
        native_terminal_supported()
        and str(framework or "").strip().lower() in _NATIVE_TUI_FRAMEWORKS
    )
    return {
        "Enabled": enabled,
        "Mode": "tui" if enabled else None,
        "Protocol": "ks-terminal.v1",
        "Path": "/_ksadk/terminal/ws" if enabled else None,
    }


def _register_integrated_routers(app: FastAPI, state: RuntimeAppState) -> None:
    """装配随 app 走的内嵌 router(workspace files + terminal ws)。

    goal-01:由模块级 ``app.include_router(...)`` 改为在 factory 的 configure
    回调里调用,普通 app 与 HarnessApp 各自装配,互不共享。
    """
    app.include_router(
        create_workspace_files_router(
            root_getter=_workspace_root_dir,
            enabled_getter=lambda: workspace_files_enabled(default=True),
        )
    )
    state.terminal_manager = TerminalSessionManager(
        workspace_root_getter=_workspace_root_dir,
        framework_getter=_current_framework,
    )
    register_terminal_routes(
        app,
        state.terminal_manager,
        bind_context=lambda: bind_runtime_state(state),
    )


def set_runner(r: BaseRunner, *, loaded: bool = False):
    """Bind a runner and record whether its agent has already been loaded."""
    state = get_state()
    state.runner = r
    state.runner_loaded = loaded
    from ksadk.server.factory import wire_default_agui_for_runner

    wire_default_agui_for_runner(state, r)


def _ensure_runner_loaded() -> BaseRunner:
    return get_runner()


def _resolve_active_runner() -> BaseRunner:
    return get_runner()


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

    payload = dict(normalize_model_metadata({"id": current_model}))
    payload["source"] = source
    return payload


def _runner_project_dir() -> Path:
    runner = get_state().runner
    if runner and getattr(runner, "project_dir", None):
        try:
            return Path(str(runner.project_dir)).resolve()
        except Exception:
            pass
    return Path(".").resolve()


def _default_custom_ui_bundle_dir(project_dir: Path) -> Path:
    return project_dir / "research-ui" / "dist"


def _ui_state_with_env_fallback(state: dict[str, Any]) -> dict[str, Any]:
    merged = dict(state or {})
    env_fallbacks = {
        "ui_profile": os.environ.get("KSADK_UI_PROFILE"),
        "ui_path": os.environ.get("KSADK_UI_PATH"),
        "ui_url": os.environ.get("KSADK_UI_URL"),
        "ui_bundle_path": os.environ.get("KSADK_UI_BUNDLE_PATH"),
    }
    for key, value in env_fallbacks.items():
        if value and not merged.get(key):
            merged[key] = value
    return merged


def _resolve_agent_ui_spec() -> dict[str, Any]:
    project_dir = _runner_project_dir()
    state = _ui_state_with_env_fallback(load_runtime_state(project_dir))
    framework = _current_framework()
    auto_custom_bundle_dir = _default_custom_ui_bundle_dir(project_dir)
    if (
        not state.get("ui_profile")
        and not state.get("ui_path")
        and not state.get("ui_url")
        and (auto_custom_bundle_dir / "index.html").exists()
    ):
        state["ui_profile"] = UI_PROFILE_CUSTOM
        state["ui_path"] = "/"
        state["ui_bundle_path"] = str(auto_custom_bundle_dir)
    config = resolve_ui_config(
        framework=framework,
        state=state,
        cli_profile=None,
        cli_path=None,
        cli_url=None,
    )

    if config.profile == UI_PROFILE_CUSTOM:
        bundle_dir_value = state.get("ui_bundle_path") or state.get("ui_bundle_dir")
        bundle_dir = None
        if bundle_dir_value:
            candidate = Path(str(bundle_dir_value))
            if not candidate.is_absolute():
                candidate = project_dir / candidate
            if candidate.exists():
                bundle_dir = candidate.resolve()
        if bundle_dir is None:
            bundle_dir = _default_custom_ui_bundle_dir(project_dir)
        index_file = bundle_dir / "index.html"
        enabled = bundle_dir.exists() and index_file.exists()
        return {
            "enabled": enabled,
            "profile": config.profile,
            "ui_profile": config.profile,
            "path": config.path,
            "ui_path": config.path,
            "url": config.url,
            "ui_url": config.url,
            "bundle_path": str(bundle_dir),
            "ui_bundle_path": str(bundle_dir),
            "index_path": str(index_file),
            "source": "custom",
        }

    bundle_dir = STATIC_DIR
    index_file = bundle_dir / "index.html"
    enabled = bundle_dir.exists() and index_file.exists()
    return {
        "enabled": enabled,
        "profile": config.profile,
        "ui_profile": config.profile,
        "path": config.path,
        "ui_path": config.path,
        "url": config.url,
        "ui_url": config.url,
        "bundle_path": str(bundle_dir),
        "ui_bundle_path": str(bundle_dir),
        "index_path": str(index_file),
        "source": "builtin",
    }


def _normalize_request_ui_path(request_path: str) -> str:
    path = "/" + str(request_path or "").lstrip("/")
    return path if path != "//" else "/"


def _is_custom_ui_static_asset_path(relative_path: str) -> bool:
    path = str(relative_path or "").strip("/")
    if not path:
        return False
    first_segment = path.split("/", 1)[0]
    return first_segment == "assets" or bool(Path(path).suffix)


def _resolve_ui_static_response(request_path: str) -> Optional[FileResponse]:
    spec = _resolve_agent_ui_spec()
    if not spec.get("enabled"):
        return None

    bundle_dir = Path(str(spec["bundle_path"]))
    index_file = Path(str(spec["index_path"]))
    path = _normalize_request_ui_path(request_path)

    if spec.get("source") == "custom":
        ui_path = _normalize_request_ui_path(str(spec.get("path") or "/")).rstrip("/") or "/"
        if path == ui_path or path == f"{ui_path}/":
            return FileResponse(index_file)
        if ui_path != "/" and not path.startswith(f"{ui_path}/"):
            return None
        relative = path[len(ui_path) :].lstrip("/") if ui_path != "/" else path.lstrip("/")
        if not relative:
            return FileResponse(index_file)
        candidate = bundle_dir / relative
        if candidate.exists() and candidate.is_file():
            return FileResponse(candidate)
        if not _is_custom_ui_static_asset_path(relative):
            return FileResponse(index_file)
        return None

    if path in _RESERVED_UI_PATHS:
        return FileResponse(index_file)

    candidate = bundle_dir / path.lstrip("/")
    if candidate.exists() and candidate.is_file():
        return FileResponse(candidate)
    return None


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
    return bytes(base64.b64decode((data_b64 or "").strip() + "==="))


def _resolve_uploads_dir() -> Path:
    uploads_dir = Path(resolve_local_session_dir()) / "files"
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


def _custom_api_proxy_base_url() -> str:
    for key in _CUSTOM_API_PROXY_ENV_KEYS:
        value = os.environ.get(key)
        if value and value.strip():
            return value.strip().rstrip("/")
    return ""


def _proxy_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in headers.items() if key.lower() not in _HOP_BY_HOP_HEADERS}


def _response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in headers.items() if key.lower() not in _HOP_BY_HOP_HEADERS}


@health_meta_router.api_route(
    "/api/{proxy_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def custom_api_proxy(proxy_path: str, request: Request):
    base_url = _custom_api_proxy_base_url()
    if not base_url:
        raise HTTPException(status_code=404, detail="Custom API backend is not configured")

    path = f"/api/{proxy_path.lstrip('/')}"
    query = request.url.query
    target_url = f"{base_url}{path}"
    if query:
        target_url = f"{target_url}?{query}"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            upstream = await client.request(
                request.method,
                target_url,
                content=await request.body(),
                headers=_proxy_headers(request.headers),
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Custom API backend unavailable: {exc}"
        ) from exc

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=_response_headers(upstream.headers),
        media_type=upstream.headers.get("content-type"),
    )


def _read_attachment_bytes(
    storage_path: Optional[Path], *, size_limit: Optional[int] = None
) -> Optional[bytes]:
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
                f"[上传文件: {display_name}, mime={mime_type or 'unknown'}, 内容过大，未直接展开]"
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

    reference_bytes = _read_attachment_bytes(storage_path, size_limit=_MAX_REFERENCE_TEXT_BYTES)
    if reference_bytes is not None:
        text = _extract_inline_attachment_text(
            display_name=display_name,
            mime_type=mime_type,
            raw=reference_bytes,
        )
        if text:
            return f"[上传文件: {display_name}]\n{text}"
        return (
            "[上传文件: "
            f"{display_name}, "
            f"mime={mime_type or 'application/octet-stream'}, "
            f"bytes={len(reference_bytes)}]"
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
    return f"[上传文件引用: {display_name or file_uri}, mime={mime_type or 'unknown'}]"


def _extract_user_input_from_parts(parts: List[Any]) -> str:
    """兼容旧测试/旧调用点，统一复用 conversations 层的规范化逻辑。"""

    return str(deps.conversation().extract_user_input_from_parts(parts))


def _attachment_from_part(part: Any) -> Optional[Dict[str, Any]]:
    """兼容旧入口，真实实现已经收口到 conversations.normalize。"""

    attachment = deps.conversation().attachment_from_part(part)
    return dict(attachment) if isinstance(attachment, Mapping) else None


async def _hydrate_session(session: Optional[Session]) -> Optional[Session]:
    if not session:
        return None
    session.events = await deps.resolve_session_service().get_events(session.id)
    return session


async def _ensure_session(agent_id: str, user_id: str, session_id: Optional[str]) -> Session:
    service = deps.resolve_session_service()
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
    attachment_context = sanitized.get(deps.conversation().runtime.ATTACHMENT_CONTEXT_STATE_KEY)
    if not isinstance(attachment_context, Mapping):
        return sanitized

    attachments = [
        deps.conversation().compact_attachment_for_session(item)
        for item in attachment_context.get("attachments") or []
        if isinstance(item, dict)
    ]
    attachment_results = [
        compact_attachment_result_for_session(item)
        for item in attachment_context.get("attachment_results") or []
        if isinstance(item, dict)
    ]
    sanitized[deps.conversation().runtime.ATTACHMENT_CONTEXT_STATE_KEY] = {
        "attachments": attachments,
        "attachment_results": attachment_results,
    }
    return sanitized


def _request_id() -> str:
    return f"req-{uuid.uuid4().hex[:12]}"


def _action_response(
    action: str, data: Any, *, request_id: Optional[str] = None, message: str = "Success"
) -> dict:
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
    runtime_app = get_state().app
    if runtime_app is None:
        raise RuntimeError("runtime app is not bound to the current request")
    transport = httpx.ASGITransport(app=runtime_app)
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
        raise HTTPException(
            status_code=response.status_code, detail=detail or "Workspace request failed"
        )
    return response


# ============================================================
# Core ADK API Endpoints
# ============================================================


@health_meta_router.get("/health")
async def health_check():
    runner = get_state().runner
    framework = "unknown"
    agent_name = "unknown"
    if runner and hasattr(runner, "detection_result"):
        framework = runner.detection_result.type.value  # langgraph, langchain, adk
        agent_name = runner.detection_result.name
    return {"status": "ok", "framework": framework, "agent": agent_name}


@health_meta_router.get("/list-apps")
async def list_apps(relative_path: str = "./"):
    """Return available apps. For KsADK single-agent mode, returns the current agent."""
    runner = get_state().runner
    name = runner.detection_result.name if runner else "default_agent"
    return [name]

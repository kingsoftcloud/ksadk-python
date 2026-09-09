"""Workspace, attachment upload, model catalog, and cancel routes."""

from __future__ import annotations

import io
import logging
import time
import uuid
import zipfile
from pathlib import PurePosixPath
from typing import Any, Optional
from urllib.parse import quote

from fastapi import Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from ksadk.conversations.attachment_storage import AttachmentStorageService, is_hosted_upload_uri
from ksadk.conversations.model_context import normalize_model_metadata
from ksadk.conversations.runtime_persistence import require_conversation_session
from ksadk.runtime.adapter import CancelResult
from ksadk.runtime_context import PlatformIdentityContext
from ksadk.server.factory import get_runtime_execution, get_state
from ksadk.sessions.invocation_identity import identity_scope_ref
from ksadk_runtime_common.workspace_files.preview import (
    build_workspace_file_base_href,
    build_workspace_preview_csp,
    inject_workspace_html_preview,
)

from ..invocation_identity import resolve_trusted_invocation_identity
from . import dependencies as deps
from .common import (
    _action_response,
    _resolve_current_model,
    _workspace_root_dir,
    _workspace_runtime_request,
)
from .models import (
    CancelRunActionRequest,
    WorkspaceDeleteActionRequest,
    WorkspaceListActionRequest,
    _runtime_agent_id,
)
from .projection import (
    _agent_contains_invocation,
    _require_action_session,
    _session_contains_invocation,
)
from .routers import control_router, workspace_router

logger = logging.getLogger(__name__)


@workspace_router.post("/agentengine/api/v1/UploadFile")
async def upload_file_action(
    file: UploadFile = File(...),
    invocation_identity: PlatformIdentityContext = Depends(resolve_trusted_invocation_identity),
):
    file_id = uuid.uuid4().hex
    data = await file.read()
    owner_scope_ref = (
        identity_scope_ref(invocation_identity) if not invocation_identity.is_empty else None
    )
    file_uri, _local_path = await AttachmentStorageService(owner_scope_ref=owner_scope_ref).store(
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
        },
    )


@workspace_router.get("/agentengine/api/v1/AttachmentContent", include_in_schema=False)
async def attachment_content_action(
    FileUri: str = Query(...),
    SessionId: str | None = Query(None),
    AgentId: str | None = Query(None),
    UserId: str | None = Query(None),
    invocation_identity: PlatformIdentityContext = Depends(resolve_trusted_invocation_identity),
):
    service = deps.resolve_session_service()
    owner_scope_ref = (
        identity_scope_ref(invocation_identity) if not invocation_identity.is_empty else None
    )
    resolved_agent_id = str(AgentId or "").strip()

    async def resolve_attachment_agent_id() -> str:
        if resolved_agent_id:
            return resolved_agent_id
        if str(SessionId or "").strip():
            candidate = await service.get_session_metadata(str(SessionId))
            if candidate is not None:
                return str(candidate.agent_id or "").strip()
        try:
            _executor, launch_context = get_runtime_execution()
        except HTTPException:
            return ""
        return _runtime_agent_id(launch_context)

    if not invocation_identity.is_empty and is_hosted_upload_uri(FileUri):
        # Hosted attachment bytes belong to AgentEngine Server.  Require the
        # local session binding before asking that authenticated service for
        # the object; a runtime-scoped cache is not proof of ownership.
        if not str(SessionId or "").strip():
            raise HTTPException(status_code=404, detail="Attachment not found")
        resolved_agent_id = await resolve_attachment_agent_id()
        if not resolved_agent_id:
            raise HTTPException(status_code=404, detail="Attachment not found")
        await require_conversation_session(
            agent_id=resolved_agent_id,
            user_id=str(UserId or ""),
            session_id=str(SessionId),
            session_service_provider=lambda: service,
            invocation_identity=invocation_identity,
        )
        loaded = AttachmentStorageService().read(FileUri)
    else:
        loaded = AttachmentStorageService(owner_scope_ref=owner_scope_ref).read(FileUri)
    if loaded is None and not invocation_identity.is_empty:
        # Historical runtime uploads live in the legacy directory.  A verified
        # identity may use that compatibility path only through an authorized
        # session; new uploads never fall back across identity roots.
        resolved_agent_id = await resolve_attachment_agent_id()
        if not str(SessionId or "").strip() or not resolved_agent_id:
            raise HTTPException(status_code=404, detail="Attachment not found")
        await require_conversation_session(
            agent_id=resolved_agent_id,
            user_id=str(UserId or ""),
            session_id=str(SessionId),
            session_service_provider=lambda: service,
            invocation_identity=invocation_identity,
        )
        loaded = AttachmentStorageService().read(FileUri)
    if loaded is None:
        raise HTTPException(status_code=404, detail="Attachment not found")

    return Response(
        content=loaded.data,
        media_type=loaded.mime_type or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{loaded.display_name}"'},
    )


@workspace_router.post("/agentengine/api/v1/ListWorkspaceFiles")
async def list_workspace_files_action(
    request: WorkspaceListActionRequest,
    invocation_identity: PlatformIdentityContext = Depends(resolve_trusted_invocation_identity),
):
    response = await _workspace_runtime_request(
        "GET",
        "/_ksadk/workspace/v1/entries",
        params={
            "path": request.Path,
            "recursive": "true" if request.Recursive else "false",
        },
        invocation_identity=invocation_identity,
    )
    return _action_response("ListWorkspaceFiles", response.json())


@workspace_router.post("/agentengine/api/v1/AddWorkspaceFile")
async def upload_workspace_file_action(
    file: UploadFile = File(...),
    AgentId: Optional[str] = Form(None),
    Path: str = Form(...),
    invocation_identity: PlatformIdentityContext = Depends(resolve_trusted_invocation_identity),
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
        invocation_identity=invocation_identity,
    )
    return _action_response("AddWorkspaceFile", response.json())


@workspace_router.post("/agentengine/api/v1/DeleteWorkspaceFile")
async def delete_workspace_file_action(
    request: WorkspaceDeleteActionRequest,
    invocation_identity: PlatformIdentityContext = Depends(resolve_trusted_invocation_identity),
):
    response = await _workspace_runtime_request(
        "DELETE",
        f"/_ksadk/workspace/v1/files/{quote(request.Path, safe='/')}",
        invocation_identity=invocation_identity,
    )
    return _action_response("DeleteWorkspaceFile", response.json())


@control_router.post("/agentengine/api/v1/CancelRun")
async def cancel_run_action(
    request: CancelRunActionRequest,
    invocation_identity: PlatformIdentityContext = Depends(resolve_trusted_invocation_identity),
):
    executor, launch_context = get_runtime_execution()
    detached = get_state().stream_registry.streams_by_invocation.get(request.InvocationId)
    service = deps.resolve_session_service()
    scoped_session_id = str(request.SessionId or "").strip()
    if not invocation_identity.is_empty and not scoped_session_id:
        raise HTTPException(status_code=400, detail="SessionId is required")
    detached_session_id = str(detached.session_id or "").strip() if detached is not None else ""
    if scoped_session_id and detached_session_id and scoped_session_id != detached_session_id:
        raise HTTPException(
            status_code=409,
            detail="InvocationId does not belong to SessionId",
        )
    if not scoped_session_id and detached is not None:
        scoped_session_id = detached_session_id
    handle = (
        executor.find_handle(
            launch_context.runtime_type,
            request.InvocationId,
            scoped_session_id,
        )
        if scoped_session_id
        else None
    )
    if scoped_session_id:
        if invocation_identity.is_empty:
            await _require_action_session(
                service,
                session_id=scoped_session_id,
                agent_id=request.AgentId,
                user_id=request.UserId,
            )
        else:
            if not request.AgentId:
                raise HTTPException(status_code=400, detail="AgentId is required")
            await require_conversation_session(
                agent_id=request.AgentId,
                user_id=str(request.UserId or ""),
                session_id=scoped_session_id,
                session_service_provider=lambda: service,
                invocation_identity=invocation_identity,
            )
        if (
            detached is None
            and handle is None
            and not await _session_contains_invocation(
                service,
                scoped_session_id,
                request.InvocationId,
            )
        ):
            raise HTTPException(
                status_code=409,
                detail="InvocationId does not belong to SessionId",
            )
    elif request.UserId:
        if not request.AgentId:
            raise HTTPException(status_code=400, detail="AgentId is required with UserId")
        if not await _agent_contains_invocation(
            service,
            request.AgentId,
            request.InvocationId,
            user_id=request.UserId,
        ):
            raise HTTPException(status_code=404, detail="Invocation not found")
    found = detached is not None or handle is not None
    cancel_requested = False
    if detached is not None:
        cancel_requested = detached.cancel()
    runtime_cancel_status = "detached_task_cancelled" if cancel_requested else "not_running"
    if handle is not None and detached is None:
        try:
            runtime_result = await executor.cancel(handle)
            runtime_cancel_status = runtime_result.value
        except Exception as exc:  # noqa: BLE001
            runtime_cancel_status = CancelResult.FAILED.value
            logger.warning("CancelRun failed: %s", exc)
    runtime_accepted = runtime_cancel_status in {
        "detached_task_cancelled",
        CancelResult.INTERRUPTED_ACTIVE_TURN.value,
        CancelResult.PENDING_CANCEL_RECORDED.value,
    }
    status = "cancelling" if runtime_accepted else runtime_cancel_status
    return _action_response(
        "CancelRun",
        {
            "Cancelled": bool(cancel_requested or runtime_accepted),
            "Found": found,
            "Status": status,
            "RuntimeCancelStatus": runtime_cancel_status,
        },
    )


@workspace_router.get("/agentengine/api/v1/GetWorkspaceFileContent", include_in_schema=False)
async def get_workspace_file_content_action(
    FilePath: str = Query(...),
    AgentId: Optional[str] = Query(None),
    invocation_identity: PlatformIdentityContext = Depends(resolve_trusted_invocation_identity),
):
    del AgentId
    response = await _workspace_runtime_request(
        "GET",
        f"/_ksadk/workspace/v1/files/{quote(FilePath, safe='/')}",
        invocation_identity=invocation_identity,
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


@workspace_router.get("/agentengine/api/v1/ws/{agent_id}/{file_path:path}", include_in_schema=False)
async def workspace_file_path_route(
    request: Request,
    agent_id: str,
    file_path: str,
    invocation_identity: PlatformIdentityContext = Depends(resolve_trusted_invocation_identity),
):
    response = await _workspace_runtime_request(
        "GET",
        f"/_ksadk/workspace/v1/files/{quote(file_path, safe='/')}",
        invocation_identity=invocation_identity,
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


@workspace_router.get("/agentengine/api/v1/ExportWorkspaceZip", include_in_schema=False)
async def export_workspace_zip(
    AgentId: Optional[str] = Query(None),
    Path: str = Query("."),
    invocation_identity: PlatformIdentityContext = Depends(resolve_trusted_invocation_identity),
):
    del AgentId
    dir_path = Path.strip() or "."
    response = await _workspace_runtime_request(
        "GET",
        "/_ksadk/workspace/v1/entries",
        params={"path": dir_path, "recursive": "true"},
        invocation_identity=invocation_identity,
    )
    data = response.json() if response.status_code == 200 else {}
    entries = data.get("Entries", []) if isinstance(data, dict) else []
    root = _workspace_root_dir(invocation_identity)
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


_MODELS_CATALOG_TTL_SECONDS = 60.0
# api_base -> (monotonic_ts, payload)。进程内短 TTL 缓存，见 _build_models_payload。
_MODELS_CATALOG_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


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

    # 上游 /v1/models 是一次真实外网往返（数百毫秒），会话页初始化会
    # 调用本接口，不能每次都同步打上游。做短 TTL 进程内缓存：
    # 命中直接返回；上游失败时优先回退到过期的缓存值，避免模型列表
    # 因上游抖动整体不可用。
    cache_key = api_base.rstrip("/")
    cached = _MODELS_CATALOG_CACHE.get(cache_key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _MODELS_CATALOG_TTL_SECONDS:
        payload = dict(cached[1])
        payload["cached"] = True
        return payload

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
            if current_model and all(
                str(item.get("id") or "").strip() != current_model for item in models
            ):
                models = _normalize_model_catalog_items([*models, current_model])
            payload = {"data": models, "current": current_model, "source": source}
            _MODELS_CATALOG_CACHE[cache_key] = (now, payload)
            return dict(payload)
    except Exception as e:
        logger.error(f"Failed to fetch models: {e}")
        if cached is not None:
            payload = dict(cached[1])
            payload["stale"] = True
            payload["error"] = str(e)
            return payload
        fallback = _fallback_catalog()
        fallback["error"] = str(e)
        return fallback


class ListAgentModelsRequest(BaseModel):
    AgentId: Optional[str] = None
    Name: Optional[str] = None

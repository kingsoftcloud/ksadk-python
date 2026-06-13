"""Workspace files FastAPI router - preserves the current contract exactly."""

from __future__ import annotations

import mimetypes
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from ksadk_runtime_common.workspace_files.bootstrap import (
    workspace_files_enabled,
    workspace_files_max_upload_bytes,
    workspace_files_root_label,
)
from ksadk_runtime_common.workspace_files.path_utils import (
    _resolve_workspace_root,
    _resolve_workspace_target,
)
from ksadk_runtime_common.workspace_files.preview import (
    build_workspace_preview_csp,
    inject_workspace_html_preview,
)

EntryPayload = dict[str, str | int | None]
EntriesResponse = dict[str, str | list[EntryPayload]]
HealthzResponse = dict[str, bool | str]
UploadResponse = dict[str, EntryPayload]


def _isoformat_timestamp(path: Path) -> str:
    """Get an ISO 8601 timestamp for a file modification time."""
    return (
        datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _entry_payload(root: Path, path: Path) -> EntryPayload:
    """Build entry payload for file and directory listing."""
    entry_type = "directory" if path.is_dir() else "file"
    mime_type = None if entry_type == "directory" else mimetypes.guess_type(path.name)[0]
    size_bytes = None if entry_type == "directory" else path.stat().st_size
    return {
        "Name": path.name,
        "Path": path.relative_to(root).as_posix(),
        "Type": entry_type,
        "SizeBytes": size_bytes,
        "MimeType": mime_type,
        "ModifiedAt": _isoformat_timestamp(path),
    }


def create_workspace_files_router(
    *,
    root_getter: Callable[[], Path],
    enabled_getter: Callable[[], bool] | None = None,
) -> APIRouter:
    """Create a workspace files router using the v1 contract."""
    router = APIRouter(prefix="/_ksadk/workspace/v1", tags=["workspace-files"])
    is_enabled = enabled_getter or (lambda: workspace_files_enabled(default=True))

    def _ensure_enabled() -> None:
        if not is_enabled():
            raise HTTPException(status_code=404, detail="workspace files are disabled")

    @router.get("/healthz")
    async def workspace_healthz() -> HealthzResponse:
        _ensure_enabled()
        root = _resolve_workspace_root(root_getter)
        return {
            "ok": True,
            "root": workspace_files_root_label(),
            "workspace_path": str(root),
        }

    @router.get("/entries")
    async def list_workspace_entries(
        path: str = Query(".", alias="path"),
        recursive: bool = Query(False, alias="recursive"),
    ) -> EntriesResponse:
        _ensure_enabled()
        root = _resolve_workspace_root(root_getter)
        normalized, target = _resolve_workspace_target(root, path, allow_root=True)
        if not target.exists():
            raise HTTPException(status_code=404, detail="workspace path not found")
        if not target.is_dir():
            raise HTTPException(status_code=400, detail="workspace path is not a directory")

        iterator = target.rglob("*") if recursive else target.iterdir()
        entries = sorted(
            [entry for entry in iterator if entry.exists()],
            key=lambda entry: (entry.is_file(), entry.name.lower()),
        )
        return {
            "Root": workspace_files_root_label(),
            "Path": normalized,
            "Entries": [_entry_payload(root, entry) for entry in entries],
        }

    @router.head("/files/{file_path:path}")
    async def head_workspace_file(file_path: str) -> Response:
        _ensure_enabled()
        root = _resolve_workspace_root(root_getter)
        _, target = _resolve_workspace_target(root, file_path, allow_root=False)
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="workspace file not found")
        media_type, _ = mimetypes.guess_type(target.name)
        return Response(
            status_code=200,
            headers={
                "Content-Length": str(target.stat().st_size),
                "Content-Type": media_type or "application/octet-stream",
                "Last-Modified": _isoformat_timestamp(target),
            },
        )

    @router.get("/files/{file_path:path}")
    async def download_workspace_file(file_path: str) -> Response:
        _ensure_enabled()
        root = _resolve_workspace_root(root_getter)
        _, target = _resolve_workspace_target(root, file_path, allow_root=False)
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="workspace file not found")
        media_type, _ = mimetypes.guess_type(target.name)
        is_html = (media_type or "").split(";")[0].lower() == "text/html" or target.suffix.lower() in {
            ".html",
            ".htm",
        }
        if is_html:
            html_doc = target.read_bytes().decode("utf-8", errors="replace")
            return Response(
                content=inject_workspace_html_preview(html_doc, file_path).encode("utf-8"),
                media_type="text/html; charset=utf-8",
                headers={
                    "Content-Security-Policy": build_workspace_preview_csp(),
                    "Last-Modified": _isoformat_timestamp(target),
                },
            )
        return FileResponse(
            target,
            media_type=media_type or "application/octet-stream",
            filename=target.name,
        )

    @router.post("/files/{file_path:path}")
    async def upload_workspace_file(
        file_path: str,
        file: Annotated[UploadFile, File(...)],
    ) -> UploadResponse:
        _ensure_enabled()
        root = _resolve_workspace_root(root_getter)
        _, target = _resolve_workspace_target(root, file_path, allow_root=False)
        target.parent.mkdir(parents=True, exist_ok=True)

        size_bytes = 0
        max_upload_bytes = workspace_files_max_upload_bytes()
        try:
            with target.open("wb") as handle:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    size_bytes += len(chunk)
                    if size_bytes > max_upload_bytes:
                        raise HTTPException(
                            status_code=413, detail="workspace file exceeds upload limit"
                        )
                    handle.write(chunk)
        except HTTPException:
            target.unlink(missing_ok=True)
            raise
        finally:
            await file.close()

        entry = _entry_payload(root, target)
        if file.content_type:
            entry["MimeType"] = file.content_type
        return {"Entry": entry}

    @router.delete("/files/{file_path:path}")
    async def delete_workspace_file(file_path: str) -> JSONResponse:
        _ensure_enabled()
        root = _resolve_workspace_root(root_getter)
        _, target = _resolve_workspace_target(root, file_path, allow_root=False)
        if not target.exists():
            raise HTTPException(status_code=404, detail="workspace file not found")
        if target.is_dir():
            try:
                target.rmdir()
            except OSError as exc:
                raise HTTPException(
                    status_code=409,
                    detail="workspace directory is not empty",
                ) from exc
            return JSONResponse({"Deleted": True})
        if not target.is_file():
            raise HTTPException(status_code=400, detail="workspace path is not a file or directory")
        target.unlink()
        return JSONResponse({"Deleted": True})

    return router

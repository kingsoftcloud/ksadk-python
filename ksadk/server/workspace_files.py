"""Shared workspace file runtime routes for ksadk-backed runtimes."""

from __future__ import annotations

import mimetypes
import os
import posixpath
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, File, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse


DEFAULT_WORKSPACE_MAX_UPLOAD_BYTES = 100 * 1024 * 1024
WORKSPACE_PATH_ESCAPE_DETAIL = "workspace path escapes the workspace root"
WORKSPACE_ENTRY_ACTION = "ListWorkspaceFiles"
WORKSPACE_UPLOAD_ACTION = "AddWorkspaceFile"
WORKSPACE_CONTENT_PATH = "/agentengine/api/v1/GetWorkspaceFileContent"


def _env_flag(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def workspace_files_enabled(*, default: bool = True) -> bool:
    return _env_flag("KSADK_WORKSPACE_FILES_ENABLED", default)


def workspace_files_root_label() -> str:
    return str(os.getenv("KSADK_WORKSPACE_ROOT_LABEL") or "workspace").strip() or "workspace"


def workspace_files_max_upload_bytes() -> int:
    raw = str(os.getenv("KSADK_WORKSPACE_MAX_UPLOAD_BYTES") or "").strip()
    if not raw:
        return DEFAULT_WORKSPACE_MAX_UPLOAD_BYTES
    try:
        return max(int(raw), 1)
    except ValueError:
        return DEFAULT_WORKSPACE_MAX_UPLOAD_BYTES


def build_workspace_files_bootstrap(*, enabled: bool) -> dict | None:
    if not enabled:
        return None
    return {
        "Enabled": True,
        "MaxUploadBytes": workspace_files_max_upload_bytes(),
        "SupportsDelete": True,
        "RootLabel": workspace_files_root_label(),
        "EntryAction": WORKSPACE_ENTRY_ACTION,
        "UploadAction": WORKSPACE_UPLOAD_ACTION,
        "ContentPath": WORKSPACE_CONTENT_PATH,
    }


def _normalize_workspace_path(raw_path: str | None, *, allow_root: bool) -> str:
    raw = str(raw_path or ".").strip().replace("\\", "/")
    if raw in {"", "."}:
        if allow_root:
            return "."
        raise HTTPException(status_code=400, detail="workspace file path must not be empty")
    if raw.startswith("/"):
        raise HTTPException(status_code=400, detail=WORKSPACE_PATH_ESCAPE_DETAIL)

    normalized = posixpath.normpath(raw)
    if normalized in {"", "."}:
        if allow_root:
            return "."
        raise HTTPException(status_code=400, detail="workspace file path must not be empty")
    if normalized == ".." or normalized.startswith("../"):
        raise HTTPException(status_code=400, detail=WORKSPACE_PATH_ESCAPE_DETAIL)
    return normalized


def _resolve_workspace_root(root_getter: Callable[[], Path]) -> Path:
    root = Path(root_getter()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _resolve_workspace_target(root: Path, raw_path: str | None, *, allow_root: bool) -> tuple[str, Path]:
    normalized = _normalize_workspace_path(raw_path, allow_root=allow_root)
    target = root if normalized == "." else (root / Path(normalized))
    resolved_target = target.resolve(strict=False)
    if resolved_target != root and root not in resolved_target.parents:
        raise HTTPException(status_code=400, detail=WORKSPACE_PATH_ESCAPE_DETAIL)
    return normalized, resolved_target


def _isoformat_timestamp(path: Path) -> str:
    return (
        datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _entry_payload(root: Path, path: Path) -> dict:
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
    router = APIRouter(prefix="/_ksadk/workspace/v1", tags=["workspace-files"])
    is_enabled = enabled_getter or (lambda: workspace_files_enabled(default=True))

    def _ensure_enabled() -> None:
        if not is_enabled():
            raise HTTPException(status_code=404, detail="workspace files are disabled")

    @router.get("/healthz")
    async def workspace_healthz() -> dict:
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
    ) -> dict:
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
    async def download_workspace_file(file_path: str):
        _ensure_enabled()
        root = _resolve_workspace_root(root_getter)
        _, target = _resolve_workspace_target(root, file_path, allow_root=False)
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="workspace file not found")
        media_type, _ = mimetypes.guess_type(target.name)
        return FileResponse(
            target,
            media_type=media_type or "application/octet-stream",
            filename=target.name,
        )

    @router.post("/files/{file_path:path}")
    async def upload_workspace_file(file_path: str, file: UploadFile = File(...)) -> dict:
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
                        raise HTTPException(status_code=413, detail="workspace file exceeds upload limit")
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
    async def delete_workspace_file(file_path: str):
        _ensure_enabled()
        root = _resolve_workspace_root(root_getter)
        _, target = _resolve_workspace_target(root, file_path, allow_root=False)
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="workspace file not found")
        target.unlink()
        return JSONResponse({"Deleted": True})

    return router

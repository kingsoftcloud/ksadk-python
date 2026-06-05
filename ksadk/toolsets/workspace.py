from __future__ import annotations

from pathlib import Path
from typing import Any

from ksadk.sessions.local_service import resolve_local_session_dir
from ksadk.tools.gateway import ToolPolicy, default_tool_gateway
from ksadk.toolsets._langchain import as_tool


_WORKSPACE_TOOL_POLICIES = {
    "workspace_status": ToolPolicy(risk_level="low"),
    "list_workspace_files": ToolPolicy(risk_level="low"),
    "read_workspace_file": ToolPolicy(risk_level="low"),
    "write_workspace_file": ToolPolicy(risk_level="medium", side_effects=("workspace_write",)),
    "write_workspace_files": ToolPolicy(risk_level="medium", side_effects=("workspace_write",)),
    "search_workspace_files": ToolPolicy(risk_level="low"),
    "delete_workspace_file": ToolPolicy(risk_level="high", side_effects=("workspace_delete",)),
}


def _gateway():
    return default_tool_gateway(_WORKSPACE_TOOL_POLICIES)


def workspace_root() -> Path:
    return resolve_local_session_dir() / "workspace"


def resolve_workspace_path(relative_path: str) -> Path:
    root = workspace_root().resolve()
    raw = str(relative_path or "").strip().replace("\\", "/").lstrip("/") or "."
    target = (root / raw).resolve()
    if target != root and root not in target.parents:
        raise ValueError("workspace path must stay inside the workspace root")
    return target


def workspace_relative(path: Path) -> str:
    return path.resolve().relative_to(workspace_root().resolve()).as_posix()


def workspace_status() -> dict[str, Any]:
    """Return current AgentEngine workspace status."""

    return _gateway().invoke("workspace_status", _workspace_status_impl)


def _workspace_status_impl() -> dict[str, Any]:
    root = workspace_root()
    root.mkdir(parents=True, exist_ok=True)
    files = [
        {"path": workspace_relative(path), "size": path.stat().st_size}
        for path in sorted(item for item in root.rglob("*") if item.is_file())[:50]
    ]
    return {
        "ok": True,
        "workspace_root": str(root),
        "file_count_sampled": len(files),
        "files": files,
    }


def list_workspace_files(path: str = ".", recursive: bool = False) -> dict[str, Any]:
    """List files under the AgentEngine workspace."""

    return _gateway().invoke("list_workspace_files", _list_workspace_files_impl, path, recursive)


def _list_workspace_files_impl(path: str = ".", recursive: bool = False) -> dict[str, Any]:
    target = resolve_workspace_path(path)
    if not target.exists():
        return {"ok": False, "error_message": f"workspace path not found: {path}"}
    if target.is_file():
        items = [target]
    elif recursive:
        items = [item for item in target.rglob("*") if item != target]
    else:
        items = list(target.iterdir())
    entries = [
        {
            "name": item.name,
            "path": workspace_relative(item),
            "type": "directory" if item.is_dir() else "file",
            "size": item.stat().st_size if item.is_file() else 0,
        }
        for item in sorted(items, key=lambda candidate: (candidate.is_file(), candidate.name.lower()))[:200]
    ]
    return {"ok": True, "path": path, "recursive": recursive, "entries": entries, "truncated": len(items) > len(entries)}


def read_workspace_file(path: str, max_chars: int = 20000) -> dict[str, Any]:
    """Read a UTF-8 text file from the AgentEngine workspace."""

    return _gateway().invoke("read_workspace_file", _read_workspace_file_impl, path, max_chars)


def _read_workspace_file_impl(path: str, max_chars: int = 20000) -> dict[str, Any]:
    target = resolve_workspace_path(path)
    if not target.is_file():
        return {"ok": False, "error_message": f"workspace file not found: {path}"}
    if target.stat().st_size > 2 * 1024 * 1024:
        return {"ok": False, "error_message": "file is larger than 2MB"}
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {"ok": False, "error_message": "file is not utf-8 text"}
    limit = max(1000, min(int(max_chars or 20000), 100000))
    return {
        "ok": True,
        "path": workspace_relative(target),
        "size": target.stat().st_size,
        "content": text[:limit],
        "truncated": len(text) > limit,
    }


def write_workspace_file(
    path: str,
    content: str,
    overwrite: bool = True,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write a UTF-8 text file inside the AgentEngine workspace."""

    return _gateway().invoke(
        "write_workspace_file",
        _write_workspace_file_impl,
        path,
        content,
        overwrite,
        approval=approval,
    )


def _write_workspace_file_impl(path: str, content: str, overwrite: bool = True) -> dict[str, Any]:
    target = resolve_workspace_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        return {"ok": False, "error_message": f"workspace file already exists: {path}"}
    target.write_text(content or "", encoding="utf-8")
    return {
        "ok": True,
        "path": workspace_relative(target),
        "absolute_path": str(target),
        "size": target.stat().st_size,
    }


def write_workspace_files(
    files: list[dict[str, Any]],
    overwrite: bool = True,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write multiple UTF-8 text files inside the AgentEngine workspace."""

    return _gateway().invoke(
        "write_workspace_files",
        _write_workspace_files_impl,
        files,
        overwrite,
        approval=approval,
    )


def _write_workspace_files_impl(files: list[dict[str, Any]], overwrite: bool = True) -> dict[str, Any]:
    if not isinstance(files, list) or not files:
        return {"ok": False, "error_message": "files must be a non-empty list"}
    written = []
    for item in files[:100]:
        if not isinstance(item, dict):
            return {"ok": False, "error_message": "each file item must be an object"}
        path = str(item.get("path") or "").strip()
        if not path:
            return {"ok": False, "error_message": "each file item requires path"}
        result = _write_workspace_file_impl(path, str(item.get("content") or ""), overwrite=overwrite)
        if not result.get("ok"):
            return result
        written.append({"path": result["path"], "size": result["size"]})
    return {"ok": True, "written": written, "truncated": len(files) > len(written)}


def search_workspace_files(query: str, path: str = ".", max_results: int = 20) -> dict[str, Any]:
    """Search UTF-8 text files in the AgentEngine workspace."""

    return _gateway().invoke("search_workspace_files", _search_workspace_files_impl, query, path, max_results)


def _search_workspace_files_impl(query: str, path: str = ".", max_results: int = 20) -> dict[str, Any]:
    needle = str(query or "").strip().lower()
    if not needle:
        return {"ok": False, "error_message": "query is required"}
    base = resolve_workspace_path(path)
    candidates = [base] if base.is_file() else [item for item in base.rglob("*") if item.is_file()]
    results = []
    for item in candidates:
        if len(results) >= max_results:
            break
        if item.stat().st_size > 1024 * 1024:
            continue
        try:
            text = item.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        index = text.lower().find(needle)
        if index < 0:
            continue
        start = max(0, index - 80)
        end = min(len(text), index + len(needle) + 160)
        results.append({"path": workspace_relative(item), "snippet": text[start:end]})
    return {"ok": True, "query": query, "results": results}


def delete_workspace_file(path: str, approval: dict[str, Any] | None = None) -> dict[str, Any]:
    """Delete a file or empty directory inside the AgentEngine workspace."""

    return _gateway().invoke("delete_workspace_file", _delete_workspace_file_impl, path, approval=approval)


def _delete_workspace_file_impl(path: str) -> dict[str, Any]:
    target = resolve_workspace_path(path)
    root = workspace_root().resolve()
    if target == root:
        return {"ok": False, "error_message": "refuse to delete workspace root"}
    if not target.exists():
        return {"ok": False, "error_message": f"workspace path not found: {path}"}
    if target.is_dir():
        target.rmdir()
    else:
        target.unlink()
    return {"ok": True, "deleted": path}


def get_workspace_tools() -> list:
    return [
        as_tool(workspace_status),
        as_tool(list_workspace_files),
        as_tool(read_workspace_file),
        as_tool(write_workspace_file),
        as_tool(write_workspace_files),
        as_tool(search_workspace_files),
        as_tool(delete_workspace_file),
    ]

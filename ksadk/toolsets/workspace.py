from __future__ import annotations

import ast
import difflib
import fnmatch
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ksadk.sessions.local_service import resolve_local_session_dir
from ksadk.tools.gateway import ToolPolicy, default_tool_gateway
from ksadk.tools.result_budget import budget_tool_output, default_tool_result_budget
from ksadk.toolsets._langchain import as_tool
from ksadk.toolsets.workspace_state import WorkspaceReadState, get_read_state, record_read_state


_WORKSPACE_TOOL_POLICIES = {
    "workspace_status": ToolPolicy(risk_level="low"),
    "list_workspace_files": ToolPolicy(risk_level="low"),
    "read_workspace_file": ToolPolicy(risk_level="low"),
    "write_workspace_file": ToolPolicy(risk_level="medium", side_effects=("workspace_write",)),
    "write_workspace_files": ToolPolicy(risk_level="medium", side_effects=("workspace_write",)),
    "edit_workspace_file": ToolPolicy(risk_level="medium", side_effects=("workspace_edit",)),
    "multi_edit_workspace_file": ToolPolicy(risk_level="medium", side_effects=("workspace_edit",)),
    "lint_workspace_file": ToolPolicy(risk_level="low"),
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


def list_workspace_files(
    path: str = ".",
    glob: str | None = None,
    recursive: bool = False,
    include_dirs: bool = True,
    max_results: int = 500,
    sort_by: str = "name",
) -> dict[str, Any]:
    """List files under the AgentEngine workspace."""

    return _gateway().invoke(
        "list_workspace_files",
        _list_workspace_files_impl,
        path,
        glob,
        recursive,
        include_dirs,
        max_results,
        sort_by,
    )


def _list_workspace_files_impl(
    path: str = ".",
    glob: str | None = None,
    recursive: bool = False,
    include_dirs: bool = True,
    max_results: int = 500,
    sort_by: str = "name",
) -> dict[str, Any]:
    target = resolve_workspace_path(path)
    if not target.exists():
        return {"ok": False, "error_message": f"workspace path not found: {path}"}
    if target.is_file():
        items = [target]
    elif recursive:
        items = [item for item in target.rglob("*") if item != target]
    else:
        items = list(target.iterdir())
    pattern = str(glob or "").strip()
    if pattern:
        items = [
            item
            for item in items
            if fnmatch.fnmatch(workspace_relative(item), pattern) or fnmatch.fnmatch(item.name, pattern)
        ]
    if not include_dirs:
        items = [item for item in items if item.is_file()]
    sort_key = str(sort_by or "name").strip().lower()
    if sort_key == "mtime":
        key = lambda candidate: (candidate.stat().st_mtime_ns, workspace_relative(candidate))
    elif sort_key == "size":
        key = lambda candidate: (candidate.stat().st_size if candidate.is_file() else 0, workspace_relative(candidate))
    else:
        key = lambda candidate: (workspace_relative(candidate).lower(), candidate.is_file())
    limit = max(1, min(int(max_results or 500), 5000))
    sorted_items = sorted(items, key=key)
    entries = [
        {
            "name": item.name,
            "path": workspace_relative(item),
            "type": "directory" if item.is_dir() else "file",
            "size": item.stat().st_size if item.is_file() else 0,
            "mtime_ns": item.stat().st_mtime_ns,
        }
        for item in sorted_items[:limit]
    ]
    return {"ok": True, "path": path, "recursive": recursive, "entries": entries, "truncated": len(sorted_items) > len(entries)}


def read_workspace_file(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    max_chars: int | None = None,
    include_line_numbers: bool = True,
) -> dict[str, Any]:
    """Read a UTF-8 text file from the AgentEngine workspace."""

    return _gateway().invoke(
        "read_workspace_file",
        _read_workspace_file_impl,
        path,
        start_line,
        end_line,
        max_chars,
        include_line_numbers,
    )


def _read_workspace_file_impl(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    max_chars: int | None = None,
    include_line_numbers: bool = True,
) -> dict[str, Any]:
    target = resolve_workspace_path(path)
    if not target.is_file():
        return {"ok": False, "error_message": f"workspace file not found: {path}"}
    stat = target.stat()
    if stat.st_size > 2 * 1024 * 1024:
        return {"ok": False, "error_message": "file is larger than 2MB"}
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {"ok": False, "error_message": "file is not utf-8 text"}
    lines = text.splitlines()
    total_lines = len(lines)
    start = max(1, int(start_line or 1))
    end = min(total_lines, int(end_line or total_lines)) if total_lines else 0
    if end and start > end:
        return {"ok": False, "error_message": "start_line must be <= end_line"}
    selected = lines[start - 1 : end] if total_lines else []
    content = "\n".join(
        f"{line_no} | {line}" if include_line_numbers else line
        for line_no, line in enumerate(selected, start=start)
    )
    limit = max(1000, min(int(max_chars or 20000), 100000))
    relative = workspace_relative(target)
    budgeted = budget_tool_output(
        tool_name="read_workspace_file",
        field_name="content",
        value=content,
        metadata={"tool_use_id": f"read_{relative.replace('/', '_')}"},
        budget=default_tool_result_budget(max_chars=limit, preview_chars=limit, persist_threshold_chars=limit),
    )
    partial = start != 1 or end != total_lines
    record_read_state(
        WorkspaceReadState(
            path=relative,
            mtime_ns=stat.st_mtime_ns,
            size_bytes=stat.st_size,
            start_line=start,
            end_line=end,
            total_lines=total_lines,
            partial=partial or bool(budgeted.get("truncated")),
        )
    )
    return {
        "ok": True,
        "path": relative,
        "size": stat.st_size,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "start_line": start,
        "end_line": end,
        "line_count": len(selected),
        "read_range": {"start": start, "end": end},
        "total_lines": total_lines,
        "suggested_action": "",
        "partial": partial or bool(budgeted.get("truncated")),
        **budgeted,
    }


def _read_workspace_text(target: Path) -> tuple[str | None, dict[str, Any] | None]:
    if not target.is_file():
        return None, {"ok": False, "error_message": "workspace file not found"}
    if target.stat().st_size > 2 * 1024 * 1024:
        return None, {"ok": False, "error_message": "file is larger than 2MB"}
    try:
        return target.read_text(encoding="utf-8"), None
    except UnicodeDecodeError:
        return None, {"ok": False, "error_message": "file is not utf-8 text"}


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


def edit_workspace_file(
    path: str,
    old_text: str,
    new_text: str,
    expected_replacements: int = 1,
    replace_all: bool = False,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replace an exact text snippet inside a UTF-8 workspace file."""

    return _gateway().invoke(
        "edit_workspace_file",
        _edit_workspace_file_impl,
        path,
        old_text,
        new_text,
        expected_replacements,
        replace_all,
        approval=approval,
    )


def _edit_workspace_file_impl(
    path: str,
    old_text: str,
    new_text: str,
    expected_replacements: int = 1,
    replace_all: bool = False,
) -> dict[str, Any]:
    target = resolve_workspace_path(path)
    relative = workspace_relative(target)
    read_error = _validate_read_state(target, relative)
    if read_error is not None:
        return read_error
    text, error = _read_workspace_text(target)
    if error is not None:
        return {**error, "path": path}
    edit_result = _apply_single_edit(
        text,
        old_text,
        new_text,
        expected_replacements=expected_replacements,
        replace_all=replace_all,
        relative=relative,
    )
    if not edit_result["ok"]:
        return edit_result
    updated = str(edit_result["updated"])
    diff = _build_diff(relative, text, updated)
    target.write_text(updated, encoding="utf-8")
    new_stat = target.stat()
    _refresh_read_state(relative, new_stat, updated)
    diff_budget = _budget_diff("edit_workspace_file", diff, relative)
    return {
        "ok": True,
        "path": relative,
        "absolute_path": str(target),
        "replacements": edit_result["replacements"],
        "used_quote_normalization": edit_result["used_quote_normalization"],
        **diff_budget,
        "size": new_stat.st_size,
        "mtime_ns": new_stat.st_mtime_ns,
    }


def multi_edit_workspace_file(
    path: str,
    edits: list[dict[str, Any]],
    replace_all: bool = False,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply multiple exact snippet edits atomically inside one workspace file."""

    return _gateway().invoke(
        "multi_edit_workspace_file",
        _multi_edit_workspace_file_impl,
        path,
        edits,
        replace_all,
        approval=approval,
    )


def _multi_edit_workspace_file_impl(
    path: str,
    edits: list[dict[str, Any]],
    replace_all: bool = False,
) -> dict[str, Any]:
    target = resolve_workspace_path(path)
    relative = workspace_relative(target)
    read_error = _validate_read_state(target, relative)
    if read_error is not None:
        return read_error
    text, error = _read_workspace_text(target)
    if error is not None:
        return {**error, "path": path}
    if not isinstance(edits, list) or not edits:
        return {"ok": False, "path": relative, "error_type": "invalid_edits", "error_message": "edits must be a non-empty list"}
    original = text or ""
    updated = original
    diagnostics: list[dict[str, Any]] = []
    total_replacements = 0
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            return {"ok": False, "path": relative, "error_type": "invalid_edit", "error_message": f"edit at index {index} must be an object", "failed_edit_index": index}
        edit_result = _apply_single_edit(
            updated,
            edit.get("old_text"),
            edit.get("new_text"),
            expected_replacements=int(edit.get("expected_replacements") or 1),
            replace_all=bool(edit.get("replace_all", replace_all)),
            relative=relative,
        )
        if not edit_result["ok"]:
            return {**edit_result, "failed_edit_index": index, "edit_count": len(edits)}
        updated = str(edit_result["updated"])
        total_replacements += int(edit_result["replacements"])
        diagnostics.append(
            {
                "index": index,
                "replacements": edit_result["replacements"],
                "used_quote_normalization": edit_result["used_quote_normalization"],
                "matches": edit_result.get("matches", []),
            }
        )
    diff = _build_diff(relative, original, updated)
    target.write_text(updated, encoding="utf-8")
    new_stat = target.stat()
    _refresh_read_state(relative, new_stat, updated)
    diff_budget = _budget_diff("multi_edit_workspace_file", diff, relative)
    return {
        "ok": True,
        "path": relative,
        "absolute_path": str(target),
        "edit_count": len(edits),
        "replacements": total_replacements,
        "edits": diagnostics,
        **diff_budget,
        "size": new_stat.st_size,
        "mtime_ns": new_stat.st_mtime_ns,
    }


def lint_workspace_file(path: str, language: str = "auto") -> dict[str, Any]:
    """Run lightweight built-in lint checks for a UTF-8 workspace text file."""

    return _gateway().invoke("lint_workspace_file", _lint_workspace_file_impl, path, language)


def _lint_workspace_file_impl(path: str, language: str = "auto") -> dict[str, Any]:
    target = resolve_workspace_path(path)
    text, error = _read_workspace_text(target)
    if error is not None:
        return {**error, "path": path}
    language_name = _detect_language(target, language)
    issues: list[dict[str, Any]] = []
    if language_name == "python":
        try:
            ast.parse(text or "", filename=workspace_relative(target))
        except SyntaxError as exc:
            issues.append(
                {
                    "severity": "error",
                    "line": exc.lineno or 0,
                    "column": exc.offset or 0,
                    "message": exc.msg,
                }
            )
    elif language_name == "json":
        try:
            json.loads(text or "")
        except json.JSONDecodeError as exc:
            issues.append(
                {
                    "severity": "error",
                    "line": exc.lineno,
                    "column": exc.colno,
                    "message": exc.msg,
                }
            )
    else:
        for line_no, line in enumerate((text or "").splitlines(), start=1):
            if "\x00" in line:
                issues.append({"severity": "error", "line": line_no, "column": line.index("\x00") + 1, "message": "NUL byte found"})
            if line.rstrip("\n\r") != line.rstrip():
                issues.append({"severity": "warning", "line": line_no, "column": len(line), "message": "trailing whitespace"})
                if len(issues) >= 20:
                    break
    return {
        "ok": not any(issue["severity"] == "error" for issue in issues),
        "path": workspace_relative(target),
        "language": language_name,
        "issues": issues,
        "lint_model": "built_in_lightweight",
    }


def _detect_language(path: Path, language: str = "auto") -> str:
    value = str(language or "auto").strip().lower()
    if value and value != "auto":
        aliases = {"py": "python", "python3": "python", "js": "javascript"}
        return aliases.get(value, value)
    suffix = path.suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix == ".json":
        return "json"
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix in {".txt", ".log"}:
        return "text"
    return "text"


def search_workspace_files(
    query: str,
    path: str = ".",
    is_regex: bool = False,
    glob: str | None = None,
    case_sensitive: bool = False,
    context_lines: int = 0,
    max_results: int = 100,
) -> dict[str, Any]:
    """Search UTF-8 text files in the AgentEngine workspace."""

    return _gateway().invoke(
        "search_workspace_files",
        _search_workspace_files_impl,
        query,
        path,
        is_regex,
        glob,
        case_sensitive,
        context_lines,
        max_results,
    )


def _search_workspace_files_impl(
    query: str,
    path: str = ".",
    is_regex: bool = False,
    glob: str | None = None,
    case_sensitive: bool = False,
    context_lines: int = 0,
    max_results: int = 100,
) -> dict[str, Any]:
    needle = str(query or "").strip()
    if not needle:
        return {"ok": False, "error_message": "query is required"}
    base = resolve_workspace_path(path)
    if shutil.which("rg") and not context_lines:
        rg_result = _search_with_rg(needle, base, is_regex, glob, case_sensitive, context_lines, max_results)
        if rg_result is not None:
            return _search_payload(query, path, rg_result["results"], rg_result["truncated"], context_lines, "rg")
    candidates = [base] if base.is_file() else [item for item in base.rglob("*") if item.is_file()]
    if glob:
        candidates = [item for item in candidates if fnmatch.fnmatch(workspace_relative(item), glob) or fnmatch.fnmatch(item.name, glob)]
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(needle if is_regex else re.escape(needle), flags)
    results = []
    limit = max(1, min(int(max_results or 100), 5000))
    context_limit = max(0, min(int(context_lines or 0), 20))
    truncated = False
    for item in candidates:
        if item.stat().st_size > 1024 * 1024:
            continue
        try:
            lines = item.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for line_no, line in enumerate(lines, start=1):
            match = pattern.search(line)
            if not match:
                continue
            if len(results) >= limit:
                truncated = True
                break
            before = lines[max(0, line_no - 1 - context_limit) : line_no - 1]
            after = lines[line_no : line_no + context_limit]
            results.append(
                {
                    "path": workspace_relative(item),
                    "line": line_no,
                    "text": line,
                    "snippet": line,
                    "context_before": before,
                    "context_after": after,
                }
            )
        if truncated:
            break
    payload = _search_payload(query, path, results, truncated, context_lines, "python")
    budgeted = budget_tool_output(
        tool_name="search_workspace_files",
        field_name="results_json",
        value=results,
        metadata={"tool_use_id": "search_workspace_files"},
    )
    if budgeted.get("persisted"):
        payload["results_preview"] = budgeted["results_json"]
        payload["persisted_outputs"] = {"results": budgeted["persisted"]}
        payload["results_truncated"] = budgeted["truncated"]
        payload["results_original_chars"] = budgeted["original_chars"]
        payload["results_preview_chars"] = budgeted["preview_chars"]
    return payload


def _search_with_rg(
    needle: str,
    base: Path,
    is_regex: bool,
    glob: str | None,
    case_sensitive: bool,
    context_lines: int,
    max_results: int,
) -> dict[str, Any] | None:
    command = ["rg", "--line-number", "--color", "never", "--no-heading"]
    if not is_regex:
        command.append("--fixed-strings")
    if not case_sensitive:
        command.append("--ignore-case")
    if glob:
        command.extend(["--glob", glob])
    if context_lines:
        command.extend(["--context", str(max(0, min(int(context_lines), 20)))])
    command.extend([needle, str(base)])
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    except Exception:
        return None
    if completed.returncode not in {0, 1}:
        return None
    results = []
    limit = max(1, min(int(max_results or 100), 5000))
    truncated = False
    for raw_line in completed.stdout.splitlines():
        separator = ":" if ":" in raw_line else "-"
        parts = raw_line.split(separator, 2)
        if len(parts) < 3 or not parts[1].isdigit():
            continue
        path = Path(parts[0])
        try:
            relative = workspace_relative(path)
        except ValueError:
            continue
        if len(results) >= limit:
            truncated = True
            break
        results.append(
            {
                "path": relative,
                "line": int(parts[1]),
                "text": parts[2],
                "snippet": parts[2],
                "context_before": [],
                "context_after": [],
            }
        )
    return {"ok": True, "query": needle, "results": results, "truncated": truncated}


def _search_payload(
    query: str,
    path: str,
    results: list[dict[str, Any]],
    truncated: bool,
    context_lines: int,
    backend: str,
) -> dict[str, Any]:
    return {
        "ok": True,
        "query": query,
        "searched_path": path,
        "results": results,
        "match_count": len(results),
        "truncated": truncated,
        "context_lines": max(0, min(int(context_lines or 0), 20)),
        "search_backend": backend,
    }


def _validate_read_state(target: Path, relative: str) -> dict[str, Any] | None:
    read_state = get_read_state(relative)
    if read_state is None:
        return {
            "ok": False,
            "path": relative,
            "error_type": "file_not_read",
            "error_message": "file must be read before editing",
            "suggested_action": f"Call read_workspace_file(path={relative!r}) before editing.",
        }
    stat = target.stat() if target.exists() else None
    if stat is None or stat.st_mtime_ns != read_state.mtime_ns or stat.st_size != read_state.size_bytes:
        return {
            "ok": False,
            "path": relative,
            "error_type": "file_modified_since_read",
            "error_message": "workspace file changed since last read",
            "suggested_action": f"Re-read the file with read_workspace_file(path={relative!r}) and retry.",
        }
    return None


def _apply_single_edit(
    text: str,
    old_text: Any,
    new_text: Any,
    *,
    expected_replacements: int = 1,
    replace_all: bool = False,
    relative: str,
) -> dict[str, Any]:
    old = str(old_text or "")
    if not old:
        return {"ok": False, "path": relative, "error_type": "missing_old_text", "error_message": "old_text is required"}
    expected = max(1, int(expected_replacements or 1))
    actual_old, used_quote_normalization = _find_actual_string(text, old)
    matches = _match_previews(text, actual_old or old) if actual_old else []
    occurrences = text.count(actual_old) if actual_old else 0
    if occurrences == 0:
        return {
            "ok": False,
            "path": relative,
            "error_type": "snippet_not_found",
            "error_message": "old_text was not found in the workspace file",
            "occurrences": 0,
            "nearby_candidates": _nearby_candidates(text, old),
            "suggested_action": "Use one nearby candidate as old_text, or call read_workspace_file with line numbers and retry.",
        }
    if not replace_all and occurrences != expected:
        return {
            "ok": False,
            "path": relative,
            "error_type": "ambiguous_edit",
            "error_message": f"old_text matched {occurrences} occurrences, expected {expected}",
            "occurrences": occurrences,
            "expected_replacements": expected,
            "matches": matches,
            "suggested_action": "Provide a larger unique old_text snippet, set expected_replacements, or use replace_all=true.",
        }
    replacements = occurrences if replace_all else expected
    return {
        "ok": True,
        "updated": text.replace(actual_old, str(new_text or ""), replacements),
        "replacements": replacements,
        "used_quote_normalization": used_quote_normalization,
        "matches": matches,
    }


def _match_previews(text: str, needle: str, limit: int = 20) -> list[dict[str, Any]]:
    if not needle:
        return []
    matches: list[dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            matches.append({"line": line_no, "text": line.strip(), "snippet": line})
            if len(matches) >= limit:
                break
    return matches


def _nearby_candidates(text: str, old: str, limit: int = 5) -> list[dict[str, Any]]:
    old_norm = _normalize_quotes(old).strip()
    candidates = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        ratio = difflib.SequenceMatcher(None, old_norm, _normalize_quotes(line).strip()).ratio()
        if old_norm and (old_norm[: max(1, min(4, len(old_norm)))] in _normalize_quotes(line) or ratio > 0.45):
            candidates.append({"line": line_no, "text": line.strip(), "snippet": line, "score": round(ratio, 3)})
    candidates.sort(key=lambda item: (-float(item["score"]), int(item["line"])))
    return candidates[:limit]


def _build_diff(relative: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{relative} before",
            tofile=f"{relative} after",
        )
    )


def _budget_diff(tool_name: str, diff: str, relative: str) -> dict[str, Any]:
    return budget_tool_output(
        tool_name=tool_name,
        field_name="diff",
        value=diff,
        metadata={"tool_use_id": f"{tool_name}_{relative.replace('/', '_')}"},
    )


def _refresh_read_state(relative: str, stat: Any, text: str) -> None:
    lines = text.splitlines()
    record_read_state(
        WorkspaceReadState(
            path=relative,
            mtime_ns=stat.st_mtime_ns,
            size_bytes=stat.st_size,
            start_line=1,
            end_line=len(lines),
            total_lines=len(lines),
            partial=False,
        )
    )


def _find_actual_string(text: str, old: str) -> tuple[str | None, bool]:
    if old in text:
        return old, False
    normalized_old = _normalize_quotes(old)
    for candidate in {normalized_old, old.translate(str.maketrans({'"': "“", "'": "‘"}))}:
        if candidate and candidate in text:
            return candidate, True
    normalized_text = _normalize_quotes(text)
    index = normalized_text.find(normalized_old)
    if index < 0:
        return None, False
    return text[index : index + len(old)], True


def _normalize_quotes(value: str) -> str:
    return value.translate(
        str.maketrans(
            {
                "“": '"',
                "”": '"',
                "„": '"',
                "‘": "'",
                "’": "'",
                "‚": "'",
            }
        )
    )


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
        as_tool(edit_workspace_file),
        as_tool(multi_edit_workspace_file),
        as_tool(lint_workspace_file),
        as_tool(search_workspace_files),
        as_tool(delete_workspace_file),
    ]

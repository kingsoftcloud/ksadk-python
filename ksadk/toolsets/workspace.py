from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import re
import shutil
from dataclasses import replace
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from ksadk.sessions.local_service import resolve_local_session_dir
from ksadk.tools.gateway import ToolPolicy, default_tool_gateway
from ksadk.tools.result_budget import budget_tool_output, default_tool_result_budget
from ksadk.toolsets._langchain import as_tool
from ksadk.toolsets.workspace_editing import (
    _apply_single_edit,
    _budget_diff,
    _build_diff,
    _change_counts,
    _commit_workspace_edit,
    _dict_result,
    _read_workspace_text,
    _refresh_read_state,
    _validate_read_state,
    _validate_single_edit_request,
)
from ksadk.toolsets.workspace_editing import (
    _find_actual_string as _find_actual_string,
)
from ksadk.toolsets.workspace_editing import (
    _match_previews as _match_previews,
)
from ksadk.toolsets.workspace_editing import (
    _nearby_candidates as _nearby_candidates,
)
from ksadk.toolsets.workspace_editing import (
    _normalize_quotes as _normalize_quotes,
)
from ksadk.toolsets.workspace_identity import identity_workspace_root
from ksadk.toolsets.workspace_mutation import locked_workspace_paths, publish_workspace_bytes
from ksadk.toolsets.workspace_read_budget import current_source_read_budget
from ksadk.toolsets.workspace_search import search_payload as _search_payload
from ksadk.toolsets.workspace_state import WorkspaceReadState, record_read_state
from ksadk.toolsets.workspace_state import get_read_state as get_read_state

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
    return identity_workspace_root(Path(resolve_local_session_dir()) / "workspace")


def resolve_workspace_path(relative_path: str) -> Path:
    root = workspace_root().resolve()
    raw = str(relative_path or "").strip().replace("\\", "/").lstrip("/") or "."
    target = (root / raw).resolve()
    if target != root and root not in target.parents:
        raise ValueError("workspace path must stay inside the workspace root")
    return target


def workspace_relative(path: Path) -> str:
    return path.resolve().relative_to(workspace_root().resolve()).as_posix()


def _workspace_file_guard(function):
    """Serialize the admitted file operation; approval remains outside this lock."""
    @wraps(function)
    def guarded(path, *args, **kwargs):
        with locked_workspace_paths((resolve_workspace_path(path),)):
            return function(path, *args, **kwargs)
    return guarded


def workspace_status() -> dict[str, Any]:
    """Return current AgentEngine workspace status."""
    return _dict_result(
        _gateway().invoke("workspace_status", _workspace_status_impl),
        tool_name="workspace_status",
    )


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

    return _dict_result(
        _gateway().invoke(
            "list_workspace_files",
            _list_workspace_files_impl,
            path,
            glob,
            recursive,
            include_dirs,
            max_results,
            sort_by,
        ),
        tool_name="list_workspace_files",
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
            if fnmatch.fnmatch(workspace_relative(item), pattern)
            or fnmatch.fnmatch(item.name, pattern)
        ]
    if not include_dirs:
        items = [item for item in items if item.is_file()]
    sort_key = str(sort_by or "name").strip().lower()
    key: Callable[[Path], tuple[Any, ...]]
    if sort_key == "mtime":

        def key(candidate: Path) -> tuple[Any, ...]:
            return candidate.stat().st_mtime_ns, workspace_relative(candidate)

    elif sort_key == "size":

        def key(candidate: Path) -> tuple[Any, ...]:
            return (
                candidate.stat().st_size if candidate.is_file() else 0,
                workspace_relative(candidate),
            )

    else:

        def key(candidate: Path) -> tuple[Any, ...]:
            return workspace_relative(candidate).lower(), candidate.is_file()

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
    return {
        "ok": True,
        "path": path,
        "recursive": recursive,
        "entries": entries,
        "truncated": len(sorted_items) > len(entries),
    }


def read_workspace_file(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    max_chars: int | None = None,
    include_line_numbers: bool = True,
) -> dict[str, Any]:
    """Read a UTF-8 workspace file. Inspect the result before editing it.

    Do not issue a dependent read and edit in the same parallel tool-call batch.
    """

    return _dict_result(
        _gateway().invoke(
            "read_workspace_file",
            _read_workspace_file_impl,
            path,
            start_line,
            end_line,
            max_chars,
            include_line_numbers,
        ),
        tool_name="read_workspace_file",
    )


@_workspace_file_guard
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
        with target.open("rb") as source:
            payload = source.read(2 * 1024 * 1024 + 1)
        if len(payload) > 2 * 1024 * 1024:
            return {"ok": False, "error_message": "file is larger than 2MB"}
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return {"ok": False, "error_message": "file is not utf-8 text"}
    lines = text.splitlines()
    total_lines = len(lines)
    start = max(1, int(start_line or 1))
    end = min(total_lines, int(end_line or total_lines)) if total_lines else 0
    if end and start > end:
        relative = workspace_relative(target)
        return {
            "ok": False,
            "error_message": "start_line must be <= end_line",
            "path": relative,
            "requested_range": {"start": start, "end": end_line},
            "total_lines": total_lines,
            "suggested_action": (
                f"Re-read the current file with read_workspace_file(path={relative!r}, "
                f"start_line={max(1, min(start, total_lines))}) before editing."
            ),
        }
    selected = lines[start - 1 : end] if total_lines else []
    content = "\n".join(
        f"{line_no} | {line}" if include_line_numbers else line
        for line_no, line in enumerate(selected, start=start)
    )
    limit = max(1000, min(int(max_chars or 20000), 100000))
    output_budget = default_tool_result_budget(
        max_chars=limit, preview_chars=limit, persist_threshold_chars=limit
    )
    source_budget = current_source_read_budget()
    if source_budget is not None:
        try:
            limit = source_budget.character_limit(content, limit)
        except ValueError as error:
            code = str(error)
            if code not in {"source_read_budget_unavailable", "source_read_counter_invalid"}:
                code = "source_read_counter_invalid"
            if code == "source_read_budget_unavailable" and source_budget.artifact_only_fallback:
                limit = 1
            else:
                return {"ok": False, "error_message": code, "path": workspace_relative(target)}
        # An environment default must not enlarge the authenticated host's
        # source allowance. Page generation and edit read-state use this same
        # limit, before the result becomes a durable tool receipt.
        output_budget = replace(
            output_budget,
            max_chars=min(output_budget.max_chars, limit),
            preview_chars=min(output_budget.preview_chars, limit),
            persist_threshold_chars=min(output_budget.persist_threshold_chars, limit),
        )
    relative = workspace_relative(target)
    budgeted = budget_tool_output(
        tool_name="read_workspace_file",
        field_name="content",
        value=content,
        metadata={
            "tool_use_id": (
                f"read_{relative.replace('/', '_')}_{hashlib.sha256(content.encode()).hexdigest()}"
            )
        },
        budget=output_budget,
    )
    partial = start != 1 or end != total_lines
    returned_end, returned_count = end, len(selected)
    window = {}
    if budgeted["truncated"]:
        preview = budgeted["content"]
        # File reads are continuous source pages. A character-bound prefix
        # must not advertise the requested range as if all lines were visible.
        complete = bool(preview) and content[len(preview) : len(preview) + 1] == "\n"
        boundary = preview.rfind("\n")
        returned_count = (
            preview.count("\n") + 1
            if complete
            else preview[:boundary].count("\n") + 1 if boundary >= 0 else 0
        )
        visible = preview if complete or not returned_count else preview[:boundary]
        returned_end = start + returned_count - 1 if returned_count else None
        budgeted.update(content=visible, preview_chars=len(visible))
        window = {
            "source_read_range": {"start": start, "end": end},
            "next_start_line": returned_end + 1 if returned_count else None,
            "suggested_action": (
                f"Continue with read_workspace_file(path={relative!r}, "
                f"start_line={returned_end + 1}); the remaining lines were not returned."
                if returned_count
                else "A source line exceeds the output window. Use bound search or another "
                "permitted inspection tool; the unread remainder cannot be skipped."
            ),
            **({"partial_line": start} if not returned_count else {}),
        }
    result = {
        "ok": True,
        "path": relative,
        "size": stat.st_size,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "start_line": start,
        "end_line": returned_end,
        "line_count": returned_count,
        "read_range": {"start": start, "end": returned_end} if returned_end is not None else None,
        "total_lines": total_lines,
        "suggested_action": "",
        "partial": partial or bool(budgeted.get("truncated")),
        **({"source_output_budget": {
            "version": "host-source-page/v2",
            "counter_id": source_budget.counter_id,
            "max_units": source_budget.max_units,
            "max_result_units": source_budget.max_result_units,
        }} if source_budget is not None else {}),
        **budgeted,
        **window,
    }
    if source_budget is not None and source_budget.retain_requested_source:
        # Capture these exact already-read bytes, never a later path re-read.
        # This carrier is audit data, not proof the model inspected the range.
        result["requested_source"] = {
            "path": relative, "read_range": {"start": start, "end": end}, "content": content,
        }
    if source_budget is not None and not source_budget.result_fits(result):
        # Persist the original selected source once. Fit complete continuous
        # lines using the complete JSON reply, before recording edit read-state.
        if not content:
            return {"ok": False, "path": relative,
                    "error_message": "source_read_envelope_over_budget"}
        source_lines = content.split("\n")
        persisted = budget_tool_output(
            tool_name="read_workspace_file", field_name="content", value=content,
            metadata={"tool_use_id": (
                f"read_{relative.replace('/', '_')}_{hashlib.sha256(content.encode()).hexdigest()}"
            )}, budget=replace(output_budget, persist_threshold_chars=0),
        )["persisted"]

        def candidate(count):
            visible = "\n".join(source_lines[:count])
            last = start + count - 1 if count else None
            page = {
                **result, "content": visible, "preview_chars": len(visible),
                "truncated": True, "persisted": persisted, "partial": True,
                "end_line": last, "line_count": count,
                "read_range": {"start": start, "end": last} if last is not None else None,
                "source_read_range": {"start": start, "end": end},
                "next_start_line": last + 1 if last is not None else None,
                "suggested_action": (
                    f"Continue with read_workspace_file(path={relative!r}, start_line={last + 1}); "
                    "the remaining lines were not returned."
                    if last is not None else
                    "The source line or result metadata exceeds the output window. "
                    "Use bound search or another permitted inspection tool; "
                    "the unread remainder cannot be skipped."
                ),
                **({"partial_line": start} if not count else {}),
            }
            if not count and source_budget.artifact_only_fallback:
                page["artifact_only"] = True
            return page

        low, high = 0, returned_count
        if returned_count and source_budget.result_fits(candidate(1)):
            low = 1
        else:
            high = 0
            if not source_budget.result_fits(candidate(0)):
                return {"ok": False, "path": relative,
                        "error_message": "source_read_envelope_over_budget"}
        while low < high:
            middle = (low + high + 1) // 2
            if source_budget.result_fits(candidate(middle)):
                low = middle
            else:
                high = middle - 1
        result = candidate(low)
        returned_end = result["end_line"]
    if source_budget is not None and content and not result["content"] and not result["line_count"]:
        # A fitting envelope with no source is not a successful read. Do not
        # record it as a read/edit precondition or let an empty page look like
        # evidence that the requested range contained no relevant facts.
        if not result.get("artifact_only"):
            return {"ok": False, "path": relative,
                    "error_message": "source_read_envelope_over_budget"}
    if source_budget is not None and source_budget.artifact_only_fallback and content:
        if not result["line_count"]:
            result.update(artifact_only=True, content="", preview_chars=0)
            if not source_budget.result_fits(result):
                return {"ok": False, "path": relative,
                        "error_message": "source_read_envelope_over_budget"}
            # Saving an unread source does not satisfy the live edit precondition.
            return result
    record_read_state(
        WorkspaceReadState(
            path=relative,
            mtime_ns=stat.st_mtime_ns,
            size_bytes=stat.st_size,
            start_line=start,
            end_line=returned_end if returned_end is not None else start,
            total_lines=total_lines,
            partial=result["partial"],
            canonical_path=str(target.resolve()),
            content_sha256=hashlib.sha256(payload).hexdigest(),
        )
    )
    return result


def write_workspace_file(
    path: str,
    content: str,
    overwrite: bool = True,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write a UTF-8 text file inside the AgentEngine workspace.

    Creating a new file needs no prior read. Replacing an existing file requires
    a complete current read or a successful write/edit in this Session. A
    successful write records the new revision for subsequent changes.
    """

    return _dict_result(
        _gateway().invoke(
            "write_workspace_file",
            _write_workspace_file_impl,
            path,
            content,
            overwrite,
            approval=approval,
        ),
        tool_name="write_workspace_file",
    )


def _workspace_write_precondition(
    path: str, *, overwrite: bool
) -> tuple[Path, str, dict[str, Any] | None, str]:
    target = resolve_workspace_path(path)
    relative = workspace_relative(target)
    if not target.exists():
        return target, relative, None, ""
    if not overwrite:
        return target, relative, {
            "ok": False,
            "path": relative,
            "error_type": "file_exists",
            "error_message": f"workspace file already exists: {path}",
        }, ""
    current, error = _read_workspace_text(target)
    if error is not None:
        return target, relative, {**error, "path": relative}, ""
    state = get_read_state(relative)
    if state is not None and state.partial:
        return target, relative, {
            "ok": False,
            "path": relative,
            "error_type": "file_not_fully_read",
            "error_message": "existing file must be read completely before overwriting",
            "suggested_action": (
                f"Call read_workspace_file(path={relative!r}) without a line range, then retry."
            ),
        }, ""
    validation = _validate_read_state(target, relative, state=state, content=current)
    if validation is not None:
        if validation.get("error_type") == "file_not_read":
            validation = {
                **validation,
                "error_message": "existing file must be read before overwriting",
                "suggested_action": (
                    f"Call read_workspace_file(path={relative!r}) before overwriting it."
                ),
            }
        return target, relative, validation, ""
    return target, relative, None, current


@_workspace_file_guard
def _write_workspace_file_impl(path: str, content: str, overwrite: bool = True) -> dict[str, Any]:
    create_only = not resolve_workspace_path(path).exists()
    target, relative, error, previous = _workspace_write_precondition(path, overwrite=overwrite)
    if error is not None:
        return error
    written = content or ""
    try:
        publish_workspace_bytes(target, written.encode("utf-8"), create_only=create_only)
    except FileExistsError:
        return {"ok": False, "path": relative, "error_type": "file_exists",
                "error_message": "workspace file was created before publication",
                "suggested_action": "Read the new file before proposing an overwrite."}
    stat = target.stat()
    # The successfully written bytes are an observed revision, just as an
    # edit's result is. A subsequent edit still validates the actual file.
    _refresh_read_state(relative, stat, written, target=target)
    return {
        "ok": True,
        "path": relative,
        "absolute_path": str(target),
        "size": stat.st_size,
        **_change_counts(previous, written),
    }


def write_workspace_files(
    files: list[dict[str, Any]],
    overwrite: bool = True,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write multiple UTF-8 text files inside the AgentEngine workspace.

    Existing files must already have complete current revisions. Preconditions
    for the admitted batch are checked before the first file is changed.
    """

    return _dict_result(
        _gateway().invoke(
            "write_workspace_files",
            _write_workspace_files_impl,
            files,
            overwrite,
            approval=approval,
        ),
        tool_name="write_workspace_files",
    )


def _write_workspace_files_impl(
    files: list[dict[str, Any]], overwrite: bool = True
) -> dict[str, Any]:
    if not isinstance(files, list) or not files:
        return {"ok": False, "error_message": "files must be a non-empty list"}
    prepared: list[tuple[str, str]] = []
    for item in files[:100]:
        if not isinstance(item, dict):
            return {"ok": False, "error_message": "each file item must be an object"}
        path = str(item.get("path") or "").strip()
        if not path:
            return {"ok": False, "error_message": "each file item requires path"}
        prepared.append((path, str(item.get("content") or "")))
    with locked_workspace_paths(resolve_workspace_path(path) for path, _ in prepared):
        for path, _ in prepared:
            _target, _relative, error, _previous = _workspace_write_precondition(
                path, overwrite=overwrite
            )
            if error is not None:
                return error
        written = []
        for path, content in prepared:
            result = _write_workspace_file_impl(path, content, overwrite=overwrite)
            if not result.get("ok"):
                return result
            written.append({key: result[key] for key in
                            ("path", "size", "diff_additions", "diff_deletions") if key in result})
        return {"ok": True, "written": written, "truncated": len(files) > len(written)}


def edit_workspace_file(
    path: str,
    old_text: str,
    new_text: str,
    expected_replacements: int = 1,
    replace_all: bool = False,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replace exact text after reading or successfully writing the file.

    The prior operation must complete; do not batch dependent reads/writes and edits.
    """

    return _dict_result(
        _gateway().invoke(
            "edit_workspace_file",
            _edit_workspace_file_impl,
            path,
            old_text,
            new_text,
            expected_replacements,
            replace_all,
            approval=approval,
        ),
        tool_name="edit_workspace_file",
    )


@_workspace_file_guard
def _edit_workspace_file_impl(
    path: str,
    old_text: str,
    new_text: str,
    expected_replacements: int = 1,
    replace_all: bool = False,
) -> dict[str, Any]:
    target = resolve_workspace_path(path)
    relative = workspace_relative(target)
    request_error = _validate_single_edit_request(old_text, relative=relative)
    if request_error is not None:
        return request_error
    observed = get_read_state(relative)
    # Pin this edit to its admitted read even if another call refreshes the
    # session's cache while matching or producing the diff.
    observed = replace(observed) if observed is not None else None
    read_error = _validate_read_state(target, relative, state=observed)
    if read_error is not None:
        return read_error
    text, error = _read_workspace_text(target)
    if error is not None:
        return {**error, "path": path}
    if text is None:
        return {"ok": False, "path": path, "error_message": "workspace file read failed"}
    read_error = _validate_read_state(target, relative, state=observed, content=text)
    if read_error is not None:
        return read_error
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
    diff_budget = _budget_diff("edit_workspace_file", diff, relative)
    new_stat, error = _commit_workspace_edit(target, relative, updated, state=observed)
    if error is not None:
        return error
    return {
        "ok": True,
        "path": relative,
        "absolute_path": str(target),
        "replacements": edit_result["replacements"],
        "used_quote_normalization": edit_result["used_quote_normalization"],
        **diff_budget,
        **_change_counts(text, updated),
        "size": new_stat.st_size,
        "mtime_ns": new_stat.st_mtime_ns,
    }


def multi_edit_workspace_file(
    path: str,
    edits: list[dict[str, Any]],
    replace_all: bool = False,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply atomic exact edits after reading or successfully writing the file.

    The prior operation must complete; do not batch dependent reads/writes and edits.
    """

    return _dict_result(
        _gateway().invoke(
            "multi_edit_workspace_file",
            _multi_edit_workspace_file_impl,
            path,
            edits,
            replace_all,
            approval=approval,
        ),
        tool_name="multi_edit_workspace_file",
    )


@_workspace_file_guard
def _multi_edit_workspace_file_impl(
    path: str,
    edits: list[dict[str, Any]],
    replace_all: bool = False,
) -> dict[str, Any]:
    target = resolve_workspace_path(path)
    relative = workspace_relative(target)
    if not isinstance(edits, list) or not edits:
        return {
            "ok": False,
            "path": relative,
            "error_type": "invalid_edits",
            "error_message": "edits must be a non-empty list",
        }
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            return {
                "ok": False,
                "path": relative,
                "error_type": "invalid_edit",
                "error_message": f"edit at index {index} must be an object",
                "failed_edit_index": index,
            }
        request_error = _validate_single_edit_request(edit.get("old_text"), relative=relative)
        if request_error is not None:
            return {**request_error, "failed_edit_index": index, "edit_count": len(edits)}
    observed = get_read_state(relative)
    observed = replace(observed) if observed is not None else None
    read_error = _validate_read_state(target, relative, state=observed)
    if read_error is not None:
        return read_error
    text, error = _read_workspace_text(target)
    if error is not None:
        return {**error, "path": path}
    original = text or ""
    read_error = _validate_read_state(target, relative, state=observed, content=original)
    if read_error is not None:
        return read_error
    updated = original
    diagnostics: list[dict[str, Any]] = []
    total_replacements = 0
    for index, edit in enumerate(edits):
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
    diff_budget = _budget_diff("multi_edit_workspace_file", diff, relative)
    new_stat, error = _commit_workspace_edit(target, relative, updated, state=observed)
    if error is not None:
        return error
    return {
        "ok": True,
        "path": relative,
        "absolute_path": str(target),
        "edit_count": len(edits),
        "replacements": total_replacements,
        "edits": diagnostics,
        **diff_budget,
        **_change_counts(original, updated),
        "size": new_stat.st_size,
        "mtime_ns": new_stat.st_mtime_ns,
    }


def lint_workspace_file(path: str, language: str = "auto") -> dict[str, Any]:
    """Run lightweight built-in lint checks for a UTF-8 workspace text file."""

    return _dict_result(
        _gateway().invoke("lint_workspace_file", _lint_workspace_file_impl, path, language),
        tool_name="lint_workspace_file",
    )


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
                issues.append(
                    {
                        "severity": "error",
                        "line": line_no,
                        "column": line.index("\x00") + 1,
                        "message": "NUL byte found",
                    }
                )
            if line.rstrip("\n\r") != line.rstrip():
                issues.append(
                    {
                        "severity": "warning",
                        "line": line_no,
                        "column": len(line),
                        "message": "trailing whitespace",
                    }
                )
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

    return _dict_result(
        _gateway().invoke(
            "search_workspace_files",
            _search_workspace_files_impl,
            query,
            path,
            is_regex,
            glob,
            case_sensitive,
            context_lines,
            max_results,
        ),
        tool_name="search_workspace_files",
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
    if not base.exists():
        return {"ok": False, "error_code": "workspace_path_not_found",
                "error_message": f"workspace path not found: {path}"}
    if shutil.which("rg"):
        rg_result = _search_with_rg(
            needle, base, is_regex, glob, case_sensitive, context_lines, max_results
        )
        if rg_result is not None:
            if not rg_result["ok"]:
                return rg_result
            return _search_payload(
                query, path, rg_result["results"], rg_result["truncated"], context_lines, "rg"
            )
    candidates = [base] if base.is_file() else [item for item in base.rglob("*") if item.is_file()]
    if glob:
        candidates = [
            item
            for item in candidates
            if fnmatch.fnmatch(workspace_relative(item), glob) or fnmatch.fnmatch(item.name, glob)
        ]
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        pattern = re.compile(needle if is_regex else re.escape(needle), flags)
    except re.error as error:
        return {"ok": False, "error_code": "invalid_search_pattern",
                "error_message": str(error)}
    results: list[dict[str, Any]] = []
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
    from ksadk.toolsets.workspace_search import search_with_rg

    return search_with_rg(
        needle, base, is_regex, glob, case_sensitive, context_lines, max_results,
        relative_path=workspace_relative,
    )


def delete_workspace_file(path: str, approval: dict[str, Any] | None = None) -> dict[str, Any]:
    """Delete a file or empty directory inside the AgentEngine workspace."""

    return _dict_result(
        _gateway().invoke(
            "delete_workspace_file", _delete_workspace_file_impl, path, approval=approval
        ),
        tool_name="delete_workspace_file",
    )


@_workspace_file_guard
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

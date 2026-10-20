"""Workspace edit validation, read-state reconciliation and bounded diff evidence.

Filesystem authorization and tool entry points remain in ``workspace``.
"""

from __future__ import annotations

import difflib
import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

from ksadk.tools.result_budget import budget_tool_output, default_tool_result_budget
from ksadk.toolsets.workspace_mutation import publish_workspace_bytes
from ksadk.toolsets.workspace_state import WorkspaceReadState, record_read_state


def _dict_result(value: Any, *, tool_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{tool_name} returned {type(value).__name__}, expected dict")
    return dict(value)


def _read_workspace_text(target: Path) -> tuple[str | None, dict[str, Any] | None]:
    if not target.is_file():
        return None, {"ok": False, "error_message": "workspace file not found"}
    if target.stat().st_size > 2 * 1024 * 1024:
        return None, {"ok": False, "error_message": "file is larger than 2MB"}
    try:
        # Editing must retain the original separators and UTF-8 BOM. The
        # model's line-oriented read view is not the file's canonical bytes.
        with target.open("rb") as source:
            payload = source.read(2 * 1024 * 1024 + 1)
        if len(payload) > 2 * 1024 * 1024:
            return None, {"ok": False, "error_message": "file is larger than 2MB"}
        return payload.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, {"ok": False, "error_message": "file is not utf-8 text"}


def _validate_read_state(
    target: Path,
    relative: str,
    *,
    state: WorkspaceReadState | None,
    content: str | None = None,
) -> dict[str, Any] | None:
    if (
        state is None
        or state.canonical_path != str(target.resolve())
        or not state.content_sha256
    ):
        return {
            "ok": False,
            "path": relative,
            "error_type": "file_not_read",
            "error_message": "file must be read before editing",
            "suggested_action": f"Call read_workspace_file(path={relative!r}) before editing.",
        }
    stat = target.stat() if target.exists() else None
    if (
        stat is None
        or stat.st_mtime_ns != state.mtime_ns
        or stat.st_size != state.size_bytes
        or (content is not None
            and hashlib.sha256(content.encode("utf-8")).hexdigest() != state.content_sha256)
    ):
        return {
            "ok": False,
            "path": relative,
            "error_type": "file_modified_since_read",
            "error_message": "workspace file changed since last read",
            "suggested_action": (
                f"Re-read the file with read_workspace_file(path={relative!r}) and retry."
            ),
        }
    return None


def _commit_workspace_edit(
    target: Path, relative: str, updated: str, *, state: WorkspaceReadState
) -> tuple[Any | None, dict[str, Any] | None]:
    """Recheck the pinned source immediately before writing, never a new read state.

    This detects intervening changes during matching/diff generation. It is
    not a filesystem CAS against unrelated writers after the final check.
    """
    current, error = _read_workspace_text(target)
    if error is not None:
        return None, {**error, "path": relative}
    if current is None:
        return None, {"ok": False, "path": relative, "error_message": "workspace file read failed"}
    error = _validate_read_state(target, relative, state=state, content=current)
    if error is not None:
        return None, error
    publish_workspace_bytes(target, updated.encode("utf-8"))
    stat = target.stat()
    _refresh_read_state(relative, stat, updated, target=target)
    return stat, None


def _apply_single_edit(
    text: str,
    old_text: Any,
    new_text: Any,
    *,
    expected_replacements: int = 1,
    replace_all: bool = False,
    relative: str,
) -> dict[str, Any]:
    request_error = _validate_single_edit_request(old_text, relative=relative)
    if request_error is not None:
        return request_error
    # Match LF/CRLF snippets against the file's convention without converting
    # any unrelated bytes. In particular, never normalize the whole file.
    ending = "\r\n" if "\r\n" in text else "\n"
    old = str(old_text).replace("\r\n", "\n").replace("\n", ending)
    replacement = str(new_text or "").replace("\r\n", "\n").replace("\n", ending)
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
            "suggested_action": (
                "Use one nearby candidate as old_text, or call read_workspace_file "
                "with line numbers and retry."
            ),
        }
    if actual_old is None:
        return {
            "ok": False,
            "path": relative,
            "error_type": "snippet_not_found",
            "error_message": "old_text was not found in the workspace file",
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
            "suggested_action": (
                "Provide a larger unique old_text snippet, set expected_replacements, "
                "or use replace_all=true."
            ),
        }
    replacements = occurrences if replace_all else expected
    return {
        "ok": True,
        "updated": text.replace(actual_old, replacement, replacements),
        "replacements": replacements,
        "used_quote_normalization": used_quote_normalization,
        "matches": matches,
    }


def _validate_single_edit_request(old_text: Any, *, relative: str) -> dict[str, Any] | None:
    if not str(old_text or ""):
        return {
            "ok": False,
            "path": relative,
            "error_type": "missing_old_text",
            "error_message": "old_text is required",
        }
    return None


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
    candidates: list[dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        ratio = difflib.SequenceMatcher(None, old_norm, _normalize_quotes(line).strip()).ratio()
        if old_norm and (
            old_norm[: max(1, min(4, len(old_norm)))] in _normalize_quotes(line) or ratio > 0.45
        ):
            candidates.append(
                {"line": line_no, "text": line.strip(), "snippet": line, "score": round(ratio, 3)}
            )
    candidates.sort(key=lambda item: (-float(item["score"]), int(item["line"])))
    return candidates[:limit]


def _changed_middle(before: str, after: str):
    old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
    first = 0
    while first < min(len(old), len(new)) and old[first] == new[first]:
        first += 1
    end_old, end_new = len(old), len(new)
    while end_old > first and end_new > first and old[end_old - 1] == new[end_new - 1]:
        end_old -= 1
        end_new -= 1
    return old, new, first, end_old, end_new


def _build_diff(relative: str, before: str, after: str) -> str:
    old, new, first, end_old, end_new = _changed_middle(before, after)
    if (end_old - first) * (end_new - first) > 1_000_000:
        # A valid whole-middle replacement avoids quadratic matching while
        # holding the workspace lock. Counts and preview use the same hunk.
        return (f"--- {relative} before\n+++ {relative} after\n"
                f"@@ -{first + 1},{end_old - first} +{first + 1},{end_new - first} @@\n"
                + "".join("-" + line for line in old[first:end_old])
                + "".join("+" + line for line in new[first:end_new]))
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{relative} before",
            tofile=f"{relative} after",
        )
    )


def _change_counts(before: str, after: str) -> dict[str, int]:
    """Count the applied revisions, before output-budget clipping.

    Reference: ZCode executor/result-display.ts counts structured patch hunks
    before limiting their display. Neither model claims nor truncated preview
    lines can establish these statistics.
    """
    old, new, first, end_old, end_new = _changed_middle(before, after)
    if (end_old - first) * (end_new - first) > 1_000_000:
        return {"diff_additions": end_new - first, "diff_deletions": end_old - first}
    additions = deletions = 0
    for kind, first, last, new_first, new_last in difflib.SequenceMatcher(
        None, old[first:end_old], new[first:end_new]
    ).get_opcodes():
        if kind != "equal":
            additions += new_last - new_first
            deletions += last - first
    return {"diff_additions": additions, "diff_deletions": deletions}


def _budget_diff(tool_name: str, diff: str, relative: str) -> dict[str, Any]:
    # The model supplied the edit, so echoing a whole-file diff back into its
    # next context wastes the same budget it needs to verify and repair. Keep
    # the full diff in a distinct audit file and return a bounded preview.
    default = default_tool_result_budget()
    budget = replace(
        default,
        max_chars=min(default.max_chars, 4096),
        preview_chars=min(default.preview_chars, 2048),
        persist_threshold_chars=min(default.persist_threshold_chars, 4096),
    )
    diff_digest = hashlib.sha256(diff.encode("utf-8")).hexdigest()
    path_digest = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:12]
    result = _dict_result(
        budget_tool_output(
            tool_name=tool_name,
            field_name="diff",
            value=diff,
            metadata={"tool_use_id": f"{tool_name}_{path_digest}_{diff_digest[:16]}"},
            budget=budget,
        ),
        tool_name=tool_name,
    )
    result["diff_sha256"] = diff_digest
    result["diff_line_count"] = diff.count("\n")
    if result["truncated"]:
        result["suggested_action"] = (
            "Edit succeeded. Read the affected file and run relevant checks; "
            "the full diff is retained at the persisted path for audit."
        )
    return result


def _refresh_read_state(relative: str, stat: Any, text: str, *, target: Path) -> None:
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
            canonical_path=str(target.resolve()),
            content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
    )


def _find_actual_string(text: str, old: str) -> tuple[str | None, bool]:
    if old in text:
        return old, False
    normalized_old = _normalize_quotes(old)
    smart_quote_map: dict[str, str | int | None] = {'"': "“", "'": "‘"}
    for candidate in {normalized_old, old.translate(str.maketrans(smart_quote_map))}:
        if candidate and candidate in text:
            return candidate, True
    normalized_text = _normalize_quotes(text)
    index = normalized_text.find(normalized_old)
    if index < 0:
        return None, False
    return text[index : index + len(old)], True


def _normalize_quotes(value: str) -> str:
    quote_map: dict[str, str | int | None] = {
        "“": '"',
        "”": '"',
        "„": '"',
        "‘": "'",
        "’": "'",
        "‚": "'",
    }
    return value.translate(str.maketrans(quote_map))

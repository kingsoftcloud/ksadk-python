"""Host tool output disclosure and pure display contract compatibility exports."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from .public_activity import public_commentary_text
from .tool_display_contract import (
    ToolDisplay as ToolDisplay,
)
from .tool_display_contract import (
    display_text,
    safe_tool_display,
    tool_display_start,
    workspace_document_path,
)


def tool_display_output(
    start: dict[str, Any], *, output: Any = None, diff: Any = None, exit_code: Any = None
) -> dict[str, Any]:
    """For trusted host adapters only, after their disclosure/policy decision."""
    value = dict(start)
    total = max(
        len(output) if isinstance(output, str) else 0, len(diff) if isinstance(diff, str) else 0
    )
    for key, raw in (("output", output), ("diff", diff)):
        if isinstance(raw, str):
            value[key] = display_text(raw)
    value.update(truncated=total > 16384, total_chars=total)
    if type(exit_code) is int:
        value["exit_code"] = exit_code
    return safe_tool_display(value) or {}


def builtin_tool_display(name: str, args: Any, result: Any) -> dict[str, Any]:
    """Explicit SDK host disclosure allowlist. Never call for custom/MCP tools."""
    start = tool_display_start(name, args)
    if not isinstance(result, dict):
        return start
    category = start["category"]
    output = diff = None
    error_message = None
    if name == "browser_inspect_workspace":
        output, error_message = _browser_public_diagnostics(result)
    elif category == "terminal":
        output = "\n".join(
            result[key] for key in ("stdout", "stderr")
            if isinstance(result.get(key), str) and result[key]
        )
    elif category == "read" and start.get("path"):
        output = result.get("content")
    elif category == "edit" and start.get("path") and result.get("ok") is True:
        diff = result.get("diff")
    elif category == "search" and isinstance(result.get("results"), list):
        # Only file location/line text, never arbitrary search result objects.
        lines = []
        for item in result["results"][:100]:
            if isinstance(item, dict):
                path = PurePosixPath(str(item.get("path") or "")).name
                if not re.search(r"(?i)(?:\.env|credential|secret|token|password)", path):
                    lines.append(f"{path}:{item.get('line', '')} {item.get('text', '')}")
        output = "\n".join(lines)
    display = tool_display_output(
        start, output=output, diff=diff, exit_code=result.get("exit_code")
    )
    if (result.get("ok") is True and (
        name in {"read_command_output", "stop_command"}
        or name == "run_command" and isinstance(args, dict) and args.get("background") is True
    ) and isinstance(result.get("status"), str) and result["status"] in {
        "starting", "running", "completed", "failed", "cancelled",
        "timed_out", "output_limit", "unknown",
    }):
        display["background_status"] = result["status"]
        if (isinstance(result.get("task_id"), str)
                and re.fullmatch(r"[a-f0-9]{64}", result["task_id"])):
            display["background_task_id"] = result["task_id"]
        if result.get("cancel_requested") is True:
            display["background_cancel_requested"] = True
        # Controlled evidence for a reconnect gap: the outcome is unknown with
        # incomplete evidence even if an exit code was observed. Only the
        # boolean and the observed exit code are disclosed — never raw provider
        # errors or sensitive metadata.
        if result.get("evidence_incomplete") is True:
            display["background_evidence_incomplete"] = True
            observed = result.get("observed_exit_code")
            if isinstance(observed, int):
                display["background_observed_exit_code"] = observed
    if result.get("ok") is False and error_message is None:
        error_message = _fixed_failure_message(_BUILTIN_FAILURE_MESSAGES, result.get("code"))
        if error_message is None:
            # Exact legacy host messages only; custom/unrecognized errors do
            # not acquire public disclosure simply by naming an error field.
            error_message = _fixed_failure_message(_LEGACY_FAILURE_MESSAGES, result.get("error_message"))
    if error_message:
        display["error_message"] = error_message
    if name in {"read_workspace_file", "write_workspace_file", "edit_workspace_file", "multi_edit_workspace_file"} and result.get("ok") is True:
        path = workspace_document_path(args.get("path") if isinstance(args, dict) else None)
        if path and start.get("path"):
            display["workspace_path"] = path
    if category == "edit" and start.get("path") and result.get("ok") is True:
        for key in ("additions", "deletions"):
            count = result.get(f"diff_{key}")
            if type(count) is int and count >= 0:
                display[key] = count
    if result.get("truncated") is True or result.get("diff_truncated") is True:
        display["truncated"] = True
    retained = diff if isinstance(diff, str) else output
    if isinstance(retained, str) and len(retained) > 16384:
        from .public_output import retain_public_output

        identity = retain_public_output(
            retained, incomplete=(result.get("truncated") is True
                                  or result.get("diff_truncated") is True),
        )
        if identity:
            display["artifact_id"] = identity
    return display


_BUILTIN_FAILURE_MESSAGES = {
    "workspace_image_requires_visual_tool": "这是图片文件，文本读取工具无法查看；需要图像工具或视觉模型。",
    "workspace_file_not_text": "文件不是 UTF-8 文本，无法用文本读取工具查看。",
}
_LEGACY_FAILURE_MESSAGES = {
    "file is not utf-8 text": _BUILTIN_FAILURE_MESSAGES["workspace_file_not_text"],
    "file is larger than 2MB": "文件超过文本读取工具的 2MB 上限。",
    "start_line must be <= end_line": "读取的行号范围无效，请重新确认文件长度。",
}
_BROWSER_FAILURE_MESSAGES = {
    "selector_not_visible": "目标控件不可见，请先重新观察页面。",
    "selector_not_found": "目标控件不存在，请先重新观察页面。",
    "selector_not_actionable": "目标控件无法操作，请先重新观察页面。",
    "element_ref_stale": "页面已变化，请使用最新观察中的控件。",
    "action_timed_out": "浏览器操作超时，结果未确认；请核对页面状态后再继续。",
    "observation_failed": "浏览器页面采集失败；可重新检查当前页面。",
    "observation_capture_failed_after_action": "浏览器操作已执行，但页面采集失败；请重新检查页面，不要重复操作。",
    "browser_entry_not_found": "待检查的页面文件不存在。",
    "browser_session_lost": "浏览器会话已丢失，需要重新加载页面。",
    "browser_actions_invalid": "浏览器操作参数无效。",
    "browser_action_invalid": "浏览器操作参数无效。",
    "browser_action_time_budget_exceeded": "浏览器操作的等待时间超过上限。",
}


def _fixed_failure_message(messages: dict[str, str], value: Any) -> str | None:
    return messages.get(value) if isinstance(value, str) else None


def _browser_public_diagnostics(result: dict[str, Any]) -> tuple[str, str | None]:
    """Publish selected repair facts, omitting DOM, controls, URLs and raw logs."""
    lines = []
    errors = result.get("page_errors")
    if isinstance(errors, list):
        for value in errors[:5]:
            text = public_commentary_text(value, limit=300)
            if text:
                lines.append(f"页面错误：{display_text(text, 300)}")
    locations = result.get("page_error_locations")
    if isinstance(locations, list):
        for value in locations[:5]:
            if not isinstance(value, str):
                continue
            match = re.fullmatch(r"/?(?:[\w.-]+/)*([\w.-]+:\d+:\d+)", value)
            if match and not re.search(r"(?i)(?:secret|token|password|credential)", value):
                lines.append(f"位置：{match.group(1)}")
    failures = result.get("request_failures")
    if isinstance(failures, list):
        for value in failures[:20]:
            if not isinstance(value, str):
                continue
            match = re.fullmatch(r"asset_http_error:(\d{3}): (/[^\s?#]+)", value)
            if match and workspace_document_path(match.group(2).lstrip("/")):
                lines.append(f"资源加载失败：HTTP {match.group(1)} · {PurePosixPath(match.group(2)).name}")
    error_message = None
    if result.get("ok") is False or result.get("code") or result.get("error_code"):
        error_message = _fixed_failure_message(
            _BROWSER_FAILURE_MESSAGES, result.get("error_code") or result.get("code")
        )
        if error_message is None:
            error_message = "页面运行出现错误，请查看检查结果。" if errors else (
                "页面资源加载失败，请查看检查结果。" if failures else "浏览器检查未通过，请核对页面状态。"
            )
    return "\n".join(dict.fromkeys(lines)), error_message

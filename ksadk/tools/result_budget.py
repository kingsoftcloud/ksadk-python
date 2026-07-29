from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from ksadk.sessions.local_service import resolve_local_session_dir

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class ToolResultBudget:
    max_chars: int = 50_000
    preview_chars: int = 8_000
    persist_threshold_chars: int = 50_000
    persist_dir: Path | str | None = None

    @property
    def resolved_persist_dir(self) -> Path:
        if self.persist_dir is not None:
            return Path(self.persist_dir).expanduser().resolve()
        configured = os.environ.get("KSADK_TOOL_RESULT_DIR", "").strip()
        if configured:
            return Path(configured).expanduser().resolve()
        return (resolve_local_session_dir() / "tool-results").resolve()


def default_tool_result_budget(
    *,
    max_chars: int | None = None,
    preview_chars: int | None = None,
    persist_threshold_chars: int | None = None,
) -> ToolResultBudget:
    return ToolResultBudget(
        max_chars=_int_env("KSADK_TOOL_RESULT_MAX_CHARS", max_chars or 50_000),
        preview_chars=_int_env("KSADK_TOOL_RESULT_PREVIEW_CHARS", preview_chars or 8_000),
        persist_threshold_chars=_int_env(
            "KSADK_TOOL_RESULT_PERSIST_THRESHOLD_CHARS",
            persist_threshold_chars or max_chars or 50_000,
        ),
    )


def budget_tool_output(
    *,
    tool_name: str,
    field_name: str,
    value: Any,
    metadata: dict[str, Any] | None = None,
    budget: ToolResultBudget | None = None,
) -> dict[str, Any]:
    active_budget = budget or default_tool_result_budget()
    text, mime_type, extension = _stringify_value(value)
    original_chars = len(text)
    should_persist = (
        original_chars > active_budget.persist_threshold_chars
        or original_chars > active_budget.max_chars
    )
    preview_limit = max(0, min(active_budget.preview_chars, active_budget.max_chars))
    preview = text[:preview_limit] if should_persist else text[: active_budget.max_chars]
    result: dict[str, Any] = {
        field_name: preview,
        "truncated": original_chars > len(preview),
        "original_chars": original_chars,
        "preview_chars": len(preview),
    }
    if should_persist:
        path = _persist_tool_output(
            tool_name=tool_name,
            field_name=field_name,
            text=text,
            extension=extension,
            metadata=metadata or {},
            budget=active_budget,
        )
        result["persisted"] = {"path": str(path), "mime_type": mime_type}
    return result


def budget_text_fields(
    payload: dict[str, Any],
    *,
    tool_name: str,
    fields: tuple[str, ...],
    metadata: dict[str, Any] | None = None,
    budget: ToolResultBudget | None = None,
) -> dict[str, Any]:
    updated = dict(payload)
    for field in fields:
        if field not in updated:
            continue
        budgeted = budget_tool_output(
            tool_name=tool_name,
            field_name=field,
            value=updated[field],
            metadata={**(metadata or {}), "field_name": field},
            budget=budget,
        )
        updated[field] = budgeted[field]
        for key in ("truncated", "original_chars", "preview_chars", "persisted"):
            if key in budgeted:
                if key == "persisted":
                    updated.setdefault("persisted_outputs", {})[field] = budgeted[key]
                else:
                    updated[f"{field}_{key}"] = budgeted[key]
    return updated


def _persist_tool_output(
    *,
    tool_name: str,
    field_name: str,
    text: str,
    extension: str,
    metadata: dict[str, Any],
    budget: ToolResultBudget,
) -> Path:
    persist_dir = budget.resolved_persist_dir
    persist_dir.mkdir(parents=True, exist_ok=True)
    tool_use_id = str(
        metadata.get("tool_use_id")
        or metadata.get("tool_call_id")
        or metadata.get("id")
        or uuid4().hex
    )
    stem = _safe_filename(f"{tool_use_id}.{field_name}") or _safe_filename(
        f"{tool_name}.{field_name}.{uuid4().hex}"
    )
    path = (persist_dir / f"{stem}.{extension}").resolve()
    if persist_dir not in path.parents and path != persist_dir:
        raise ValueError("tool result path must stay inside persist_dir")
    path.write_text(text, encoding="utf-8")
    return path


def _stringify_value(value: Any) -> tuple[str, str, str]:
    if isinstance(value, str):
        return value, "text/plain", "txt"
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        "application/json",
        "json",
    )


def _safe_filename(value: str) -> str:
    return _SAFE_NAME_RE.sub("_", value.strip()).strip("._")


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return int(default)
    try:
        return max(0, int(raw))
    except ValueError:
        return int(default)

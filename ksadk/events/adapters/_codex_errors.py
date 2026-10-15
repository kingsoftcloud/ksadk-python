"""Public Codex retry diagnostics without persisting provider response bodies."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ._codex_validators import _safe_codex_error_info_kind


def codex_http_status(error: Mapping[str, Any]) -> int | None:
    info = error.get("codexErrorInfo")
    kind = _safe_codex_error_info_kind(info)
    detail = info.get(kind) if isinstance(info, Mapping) and kind else None
    value = detail.get("httpStatusCode") if isinstance(detail, Mapping) else None
    if isinstance(value, int) and not isinstance(value, bool) and 400 <= value <= 599:
        return value
    # Older App Servers put the HTTP status only in the diagnostic string.
    # Extract the bounded numeric field; never copy that free text into progress.
    for key in ("message", "additionalDetails"):
        text = error.get(key)
        match = (
            re.search(r"\b(?:HTTP(?: status)?|status code)[:\s]+([45]\d{2})\b", text, re.I)
            if isinstance(text, str)
            else None
        )
        if match:
            return int(match.group(1))
    return None


def codex_retry_numbers(message: str, observed: int) -> tuple[int, int | None]:
    # This exact counter is emitted by Codex itself. It counts retries, not
    # initial requests, and may reset for the next sampling step in a turn.
    match = re.fullmatch(r"Reconnecting\.\.\. (\d+)/(\d+)", message)
    if match:
        count, maximum = (int(value) for value in match.groups())
        if 0 < count <= maximum <= 100:
            return count, maximum
    return observed, None


def codex_error_label(status: int | None) -> str:
    if status == 429:
        return "模型限流（HTTP 429）"
    if status is not None:
        return f"模型请求失败（HTTP {status}）"
    return "模型连接中断"

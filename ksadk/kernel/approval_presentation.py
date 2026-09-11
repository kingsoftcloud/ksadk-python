"""Bounded, redacted approval previews; native continuation data stays private."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from ksadk.events.canonical import ApprovalRequest
from ksadk.interaction.contracts import InteractionPresentation

_SECRET_FIELD = re.compile(
    r"secret|password|passphrase|token|credential|api[_-]?key|access[_-]?key|private[_-]?key|authorization|cookie",
    re.IGNORECASE,
)
_TRUNCATED = "…（预览已截断）"


class _Preview:
    def __init__(self):
        self.remaining = 12_000
        self.nodes = 128
        self.truncated = False

    def value(self, value, depth=0):
        self.nodes -= 1
        if depth > 6 or self.nodes < 0 or self.remaining <= 0:
            self.truncated = True
            return _TRUNCATED
        if isinstance(value, str):
            size = min(4096, self.remaining)
            text = value[:size]
            self.remaining -= len(text)
            if len(text) < len(value):
                self.truncated = True
                return text + _TRUNCATED
            return text
        if isinstance(value, Mapping):
            result = {}
            for key, item in value.items():
                if len(result) >= 32 or self.nodes <= 0:
                    self.truncated = True
                    break
                key = str(key)
                display_key = key[:128]
                if len(key) > 128:
                    self.truncated = True
                result[display_key] = (
                    "[REDACTED]" if _SECRET_FIELD.search(key) else self.value(item, depth + 1)
                )
            return result
        if isinstance(value, (list, tuple)):
            if len(value) > 32:
                self.truncated = True
            return [self.value(item, depth + 1) for item in value[:32]]
        return value


def approval_presentation(request: ApprovalRequest) -> InteractionPresentation:
    detail = request.detail if isinstance(request.detail, Mapping) else {}
    # Unwrap only the typed child request. Handles, policy references and other
    # surrounding execution metadata are never copied into the public preview.
    for _ in range(32):
        child = detail.get("child_approval")
        if not isinstance(child, Mapping):
            break
        detail = child
    title = {
        "command_execution": "run_command",
        "file_change": "apply_patch",
        "permissions": "request_permission",
        "dynamic_tool_call": "tool_call",
    }.get(request.kind, request.kind)
    arguments = {
        key: detail[key]
        for key in ("command", "cwd", "reason", "grantRoot", "proposedExecpolicyAmendment")
        if key in detail and detail[key] is not None
    }
    if isinstance(detail.get("name"), str) and isinstance(detail.get("args"), Mapping):
        title, arguments = detail["name"], detail["args"]
    preview = _Preview()
    payload = {"arguments": preview.value(arguments)}
    risk = detail.get("risk")
    if isinstance(risk, str) and risk in {"low", "medium", "high", "critical"}:
        payload["risk"] = risk
    if preview.truncated:
        payload["previewTruncated"] = True
    return InteractionPresentation(
        title=title[:128],
        description=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )

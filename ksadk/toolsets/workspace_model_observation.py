"""Source-reader model view; the full SDK result remains the audit record.

Only host-selected builtin readers use this projection. Source text, visible
range, omissions, continuation and overflow locators stay intact. Internal
accounting and duplicate display counters need not compete with the source
for the model's window. Unknown fields are preserved, never silently dropped.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

WORKSPACE_SOURCE_MODEL_VERSION = "workspace-source-model/v1"
_AUDIT_FIELDS = frozenset(
    {"size", "size_bytes", "mtime_ns", "source_output_budget", "original_chars", "preview_chars"}
)


def source_model_reply(result: object) -> str | None:
    """Return a detached, structured view of a recognized successful page.

    No file access, source extraction, interpretation or acceptance occurs.
    Malformed/failed results keep the existing raw projection instead.
    """
    if (
        not isinstance(result, Mapping)
        or result.get("ok") is not True
        or not isinstance(result.get("path"), str)
        or not result["path"]
        or not isinstance(result.get("content"), str)
    ):
        return None
    start, end, total, count = (
        result.get(key) for key in ("start_line", "end_line", "total_lines", "line_count")
    )
    carrier = result.get("requested_source")
    if result.get("artifact_only") is True:
        if (
            not isinstance(carrier, Mapping) or carrier.get("path") != result["path"]
            or not isinstance(carrier.get("content"), str) or not carrier["content"]
            or not isinstance(carrier.get("read_range"), dict)
            or type(start) is not int or type(total) is not int or not 1 <= start <= total
            or type(count) is not int or count != 0
            or end is not None or result.get("read_range") is not None
            or result["content"] != "" or result.get("truncated") is not True
            or result.get("partial") is not True
        ):
            return None
        requested = carrier["read_range"]
        if (
            set(requested) != {"start", "end"}
            or any(type(v) is not int for v in requested.values())
            or requested["start"] != start or not start <= requested["end"] <= total
        ):
            return None
        return json.dumps({"status": "ok", "content": {
            "ok": True, "path": result["path"], "source_state": "unread",
            "source_read_range": requested,
        }}, ensure_ascii=False, separators=(",", ":"))
    if any(type(value) is not int for value in (start, total, count)):
        return None
    if start < 1 or total < 0 or count < 0:
        return None
    read_range = result.get("read_range")
    if count:
        if (
            type(end) is not int
            or end != start + count - 1
            or end > total
            or not isinstance(read_range, dict)
            or any(type(read_range.get(key)) is not int for key in ("start", "end"))
            or read_range != {"start": start, "end": end}
        ):
            return None
    elif total:
        if (
            start > total
            or end is not None
            or read_range is not None
            or result.get("partial_line") != start
            or result.get("truncated") is not True
            or not result["content"]
        ):
            return None
    elif end != 0 or result["content"] or start != 1 or read_range != {"start": 1, "end": 0}:
        return None
    if any(type(result.get(key)) is not bool for key in ("partial", "truncated")):
        return None
    hidden = _AUDIT_FIELDS
    if "requested_source" in result:
        # The opt-in carrier crosses the host boundary, but never the model
        # window. SDK filesystem locators cannot grant Native artifact access.
        hidden = hidden | {"requested_source", "persisted"}
    view = {k: v for k, v in result.items() if k not in hidden}
    if "requested_source" in result:
        view["suggested_action"] = (
            "Inspect the complete requested source through the host artifact reference."
        )
    return json.dumps(
        {"status": "ok", "content": view},
        ensure_ascii=False,
        separators=(",", ":"),
    )

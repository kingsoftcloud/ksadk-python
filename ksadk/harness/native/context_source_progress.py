"""Derive source navigation from canonical, host-labeled tool messages.

Only a host-granted workspace reader may supply the range protocol. Its data
is an observation, not semantic coverage or a fresh authorization to edit.
No disk reads, model calls or parsing of compaction prose occurs here.
"""

import hashlib
import json

from .context import SourceReadProgress
from .context_pruning import project_workspace_read_result
from .ports import ToolDefinition
from .state import RunSnapshot


def _range(source):
    start, end, total, count = (
        source.get(k) for k in ("start_line", "end_line", "total_lines", "line_count")
    )
    declared = source.get("read_range")
    if (
        any(type(v) is not int for v in (start, end, total, count))
        or not 1 <= start <= end <= total
        or count != end - start + 1
        or not isinstance(declared, dict)
        or any(type(declared.get(k)) is not int for k in ("start", "end"))
        or declared != {"start": start, "end": end}
        or not isinstance(source.get("content"), str)
        or len(source["content"].split("\n")) != count
    ):
        return None
    return start, end


def source_read_progress(
    snapshot: RunSnapshot, tools: tuple[ToolDefinition, ...], *, max_chars: int | None
) -> tuple[SourceReadProgress, ...]:
    names = {t.name for t in tools if t.read_only and t.context_role == "workspace_read"}
    calls, progress = {}, {}
    for message in snapshot.messages:
        if message.get("role") == "assistant":
            # A result must belong to the preceding declared tool group.
            calls = {}
            for call in message.get("tool_calls") or ():
                try:
                    if call["function"]["name"] in names:
                        args = json.loads(call["function"]["arguments"])
                        path = args.get("path")
                        if isinstance(path, str) and path.strip() and len(path) <= 2048:
                            calls[call["id"]] = path
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue
            continue
        if message.get("role") != "tool":
            continue
        call_id = message.get("tool_call_id")
        path = calls.get(call_id)
        ref, cursor = message.get("receipt_ref"), message.get("receipt_tool_call")
        if (
            not path
            or not isinstance(ref, str)
            or not ref.startswith(f"{snapshot.run_id}:effect:")
            or type(cursor) is not int
            or not 0 < cursor <= snapshot.tool_calls
                or type(message.get("receipt_goal_revision")) is not int
                or message.get("receipt_goal_revision") != snapshot.goal_revision
            or not isinstance(message.get("content"), str)
        ):
            continue
        try:
            receipt = json.loads(message["content"])
            source = receipt.get("content")
            if (
                receipt.get("status") != "ok"
                or not isinstance(source, dict)
                or source.get("ok") is not True
                or source.get("path") != path
                or not isinstance(source.get("content"), str)
            ):
                continue
            returned = _range(source)
            if returned is None:
                continue
            view = (
                project_workspace_read_result(message, path=path, max_chars=max_chars)
                if max_chars is not None
                else message
            )
            if view is None:
                continue
            visible = json.loads(view["content"])["content"]
            main = _range(visible) if isinstance(visible.get("content"), str) else None
            item = SourceReadProgress(
                path=path,
                receipt_ref=ref,
                tool_call_id=call_id,
                returned_lines=returned,
                configured_prefix_lines=main,
                configured_next_start_line=(
                    main[1] + 1 if main and main[1] < source["total_lines"] else None
                ),
                total_lines=source["total_lines"],
                source_mtime_ns=(
                    source.get("mtime_ns") if type(source.get("mtime_ns")) is int else None
                ),
                source_size_bytes=(
                    source.get("size_bytes") if type(source.get("size_bytes")) is int else None
                ),
                source_payload_digest="sha256:"
                + hashlib.sha256(source["content"].encode("utf-8")).hexdigest(),
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        # Keep the latest successful page per exact path, never merge ranges
        # across file versions or claim all intermediate lines were observed.
        progress.pop(path, None)
        progress[path] = item
    return tuple(progress.values())

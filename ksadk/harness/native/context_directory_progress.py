"""Derive navigation from host-granted listing receipts, without filesystem I/O."""

import json
from pathlib import PurePosixPath

from .context import DirectoryListingProgress
from .ports import ToolDefinition
from .state import RunSnapshot


def _path(value: object) -> PurePosixPath | None:
    if (not isinstance(value, str) or not value or len(value) > 2048
            or "\\" in value or "\x00" in value):
        return None
    path = PurePosixPath(value)
    return path if not path.is_absolute() and ".." not in path.parts else None


def directory_listing_observation(message, *, arguments):
    """Recognize an original listing as a read-only source-batch companion.

    A partial listing is still an observation of its returned entries. This
    validates their identity, never complete directory coverage or freshness.
    The caller owns tool authority, batch pairing and the full input budget.
    """
    if not isinstance(arguments, dict):
        return None
    path = _path(arguments.get("path", "."))
    if path is None or message.get("role") != "tool":
        return None
    try:
        receipt = json.loads(message["content"])
        source = receipt["content"]
        if (receipt.get("status") != "ok" or source.get("ok") is not True
                or _path(source.get("path")) != path
                or type(source.get("truncated")) is not bool
                or not isinstance(source.get("entries"), list)):
            return None
        for entry in source["entries"]:
            child = _path(entry.get("path"))
            if (child is None or entry.get("type") not in {"file", "directory"}
                    or child == path or not child.is_relative_to(path)):
                return None
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    return str(path)


def directory_listing_progress(
    snapshot: RunSnapshot, tools: tuple[ToolDefinition, ...],
) -> tuple[DirectoryListingProgress, ...]:
    """Only canonical current-goal results may supply directory names.

    The host explicitly grants listing semantics in the locked definition.
    Neither model prose, a summary nor ungranted JSON can create this state.
    Full results remain canonical; this records only historically returned
    immediate children. A later partial listing cannot erase earlier navigation.
    These names need not still exist; freshness is explicitly not attested.
    """
    names = {t.name for t in tools if t.read_only and t.context_role == "workspace_list"}
    if not names:
        return ()
    calls, progress = {}, {}
    for message in snapshot.messages:
        if message.get("role") == "assistant":
            calls = {}
            for call in message.get("tool_calls") or ():
                try:
                    if call["function"]["name"] in names:
                        path = _path(json.loads(call["function"]["arguments"]).get("path", "."))
                        if path is not None:
                            calls[call["id"]] = path
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue
            continue
        if message.get("role") != "tool":
            continue
        call_id = message.get("tool_call_id")
        path = calls.get(call_id)
        ref, cursor = message.get("receipt_ref"), message.get("receipt_tool_call")
        if (path is None or not isinstance(ref, str)
                or not ref.startswith(f"{snapshot.run_id}:effect:")
                or type(cursor) is not int or not 0 < cursor <= snapshot.tool_calls
                or type(message.get("receipt_goal_revision")) is not int
                or message["receipt_goal_revision"] != snapshot.goal_revision):
            continue
        try:
            receipt = json.loads(message["content"])
            source = receipt["content"]
            if (receipt.get("status") != "ok" or source.get("ok") is not True
                    or _path(source.get("path")) != path
                    or type(source.get("truncated")) is not bool
                    or not isinstance(source.get("entries"), list)):
                continue
            children = []
            for entry in source["entries"]:
                child = _path(entry.get("path"))
                if (child is None or entry.get("type") not in {"file", "directory"}
                        or child == path or not child.is_relative_to(path)):
                    raise ValueError("invalid directory entry")
                if entry["type"] == "directory" and child.parent == path:
                    children.append(child.name)
            previous = progress.get(str(path))
            item = DirectoryListingProgress(
                path=str(path),
                receipt_refs=tuple(dict.fromkeys((*previous.receipt_refs, ref))) if previous else (ref,),
                tool_call_ids=tuple(dict.fromkeys((*previous.tool_call_ids, call_id))) if previous else (call_id,),
                child_directories=tuple(dict.fromkeys((
                    *(previous.child_directories if previous else ()), *children,
                ))),
                latest_result_truncated=source["truncated"],
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        progress.pop(str(path), None)
        progress[str(path)] = item
    return tuple(progress.values())

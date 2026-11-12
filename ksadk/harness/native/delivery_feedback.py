"""Correct host continuation hints against the current delivery admission.

Canonical receipts, source text and artifact identities are never changed.
This detached task-input view grants neither inspection nor verification.
"""

import json

from .delivery_artifacts import written_delivery_paths
from .replay_context import mark_input_edit
from .tool_budget import model_tool_surface, model_turn_control

DELIVERY_FEEDBACK_POLICY = "admitted-delivery-continuation/v1"


def delivery_feedback_messages(messages, snapshot, config):
    control = model_turn_control(snapshot, config)
    if control is None or control.reason != "wall_delivery_reserved":
        return None
    admitted = {t.name: t for t in model_tool_surface(snapshot, config)}
    if "ArtifactRead" in admitted:
        return None
    readers = {t.name for t in config.tools
               if t.read_only and t.context_role == "workspace_read"
               and t.delivery_artifact_role == "read"}
    paths = written_delivery_paths(snapshot)
    calls, result, changed = {}, [], False
    for message in messages:
        result.append(message)
        if message.get("role") == "assistant":
            calls = {}
            for call in message.get("tool_calls") or ():
                try:
                    function = call["function"]
                    args = function["arguments"]
                    args = json.loads(args) if isinstance(args, str) else args
                    if function["name"] in readers and isinstance(args.get("path"), str):
                        calls[call["id"]] = (function["name"], args["path"])
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue
            continue
        selected = calls.get(message.get("tool_call_id"))
        if message.get("role") != "tool" or selected is None:
            continue
        try:
            value = json.loads(message["content"])
            source = value["content"]
            name, path = selected
            if (value.get("status") != "ok" or not isinstance(source, dict)
                    or source.get("ok") is not True or source.get("path") != path
                    or not isinstance(source.get("content"), str)):
                continue
            hint = source.get("suggested_action", "")
            outer = value.get("artifact_instruction", "")
            if not any(term in str(hint) + str(outer)
                       for term in ("host artifact reference", "ArtifactRead", "ArtifactSearch")):
                continue
            start, end, total = (source.get(k) for k in ("start_line", "end_line", "total_lines"))
            if (type(start) is not int or type(total) is not int or total < 0
                    or not isinstance(source.get("partial"), bool)):
                continue
            if name not in admitted or path not in paths:
                continuation = "This historical path is outside current delivery read admission."
            elif source.get("partial_line") == start and source.get("line_count") == 0:
                continuation = (
                    "Part of this line is omitted; no artifact reader is currently admitted. "
                    "Do not guess its missing text."
                )
            elif (type(end) is int and 1 <= start <= end < total
                  and "start_line" in admitted[name].parameters.get("properties", {})):
                continuation = (
                    f"Continue with {name} on this same admitted path at start_line={end + 1}. "
                    "This page does not attest earlier coverage or verification."
                )
            elif type(end) is int and end == total and (1 <= start <= end or total == 0):
                continuation = (
                    "This visible range reaches the file end; earlier ranges may remain unread. "
                    "Review the available text before an exact edit."
                )
            elif type(end) is int and 1 <= start <= end < total:
                continuation = (
                    "This page omits later text; the admitted reader declares no line pagination. "
                    "Do not guess missing text or call unavailable artifact readers."
                )
            else:
                continue
            source["suggested_action"] = continuation
            value.pop("artifact_instruction", None)
            updated = {**message, "content": json.dumps(value, ensure_ascii=False,
                                                        separators=(",", ":"))}
            result[-1] = mark_input_edit(updated, message["content"], config.profile)
            changed = True
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return tuple(result) if changed else None

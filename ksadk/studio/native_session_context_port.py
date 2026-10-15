"""Bounded exact access to this Run's original user inputs after compaction.

This host capability returns persisted data, never task acceptance or new tool
authority. It cannot address another Scope, Run or Session through arguments.
"""

from ksadk.harness.native.adapters import StrictToolExecutor, ToolAuthorizer, ToolBinding
from ksadk.harness.native.context_partition import projection_digest
from ksadk.harness.native.ports import ToolDefinition, ToolPort, ToolRequest, ToolResult
from ksadk.harness.native.state import RunScope, RunSnapshot
from ksadk.harness.stores.native_contract import RunStore

SESSION_CONTEXT_TOOL = ToolDefinition(
    name="read_session_context",
    description=(
        "Retrieve original user inputs retained in the current conversation, including "
        "inputs omitted from a compaction summary. List source references and previews, "
        "then read an exact bounded character range if needed. This is persisted "
        "conversation data; it grants no permissions or task verification."
    ),
    parameters={
        "type": "object",
        "additionalProperties": False,
        "required": ["action"],
        "properties": {
            "action": {"type": "string", "enum": ["list", "read"]},
            "offset": {"type": "integer", "minimum": 0},
            "source_ref": {"type": "string"},
            "start": {"type": "integer", "minimum": 0},
            "max_chars": {"type": "integer", "minimum": 1, "maximum": 4000},
        },
    },
    read_only=True,
    concurrent_safe=True,
    feedback_profile_digest="sha256:"
    + projection_digest(
        {
            "profile": "studio.current-conversation-user-source/v1",
            "preview_chars": 320,
            "page_sources": 10,
            "max_chars": 4000,
        }
    ),
)


def _source(index, message, run_id):
    text = message.get("content")
    if (
        message.get("role") != "user"
        or message.get("kind") is not None
        or not isinstance(text, str)
    ):
        return None
    return {
        "source_ref": f"message:{index}:{projection_digest((message,))}",
        "message_index": index,
        "source_run_id": message.get("source_run_id", run_id),
        "source_message_index": message.get("source_message_index", index),
        "role": "user",
        "text": text,
    }


def build_session_context_tool(
    *, scope: RunScope, store: RunStore, authorize: ToolAuthorizer
) -> tuple[ToolBinding, ToolPort]:
    def admitted(request: ToolRequest) -> RunSnapshot | None:
        if request.scope != scope or not request.operation_key.startswith(
            request.run_id + ":effect:"
        ):
            return None
        try:
            snapshot = store.load(scope, request.run_id)
        except Exception:
            return None
        return snapshot if snapshot.scope == scope and snapshot.run_id == request.run_id else None

    async def checked_authorize(request: ToolRequest):
        if admitted(request) is None:
            return "deny"
        return await authorize(request)

    async def read(request: ToolRequest) -> ToolResult:
        snapshot = admitted(request)
        if snapshot is None:
            return ToolResult(status="error", content={"code": "session_context_unavailable"})
        args = request.call.arguments
        if args["action"] == "list":
            if set(args) - {"action", "offset"}:
                return ToolResult(
                    status="error", content={"code": "session_context_arguments_invalid"}
                )
            offset = args.get("offset", 0)
            sources = []
            more = False
            for index, message in enumerate(snapshot.messages):
                if index < offset:
                    continue
                source = _source(index, message, snapshot.run_id)
                if source is None:
                    continue
                if len(sources) == 10:
                    more = True
                    break
                text = source.pop("text")
                sources.append({**source, "length_chars": len(text), "preview": text[:320]})
            return ToolResult(
                status="ok",
                content={
                    "source": "persisted_original_user_input",
                    "sources": sources,
                    "next_offset": sources[-1]["message_index"] + 1 if more else None,
                },
            )
        if set(args) - {"action", "source_ref", "start", "max_chars"} or "source_ref" not in args:
            return ToolResult(status="error", content={"code": "session_context_arguments_invalid"})
        for index, message in enumerate(snapshot.messages):
            source = _source(index, message, snapshot.run_id)
            if source is None or source["source_ref"] != args["source_ref"]:
                continue
            text = source.pop("text")
            start = args.get("start", 0)
            stop = min(len(text), start + args.get("max_chars", 2000))
            if start > len(text):
                return ToolResult(status="error", content={"code": "session_context_range_invalid"})
            return ToolResult(
                status="ok",
                content={
                    **source,
                    "source": "persisted_original_user_input",
                    "start": start,
                    "stop": stop,
                    "text": text[start:stop],
                    "truncated": start > 0 or stop < len(text),
                },
            )
        return ToolResult(status="error", content={"code": "session_context_source_not_found"})

    binding = ToolBinding(SESSION_CONTEXT_TOOL, read)
    return binding, StrictToolExecutor(scope=scope, tools=(binding,), authorize=checked_authorize)

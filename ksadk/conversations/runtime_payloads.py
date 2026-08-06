from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from ksadk.conversations.run_kinds import (
    RUN_MODE_FOREGROUND,
    RUN_TRIGGER_NEW_RUN,
)
from ksadk.conversations.runtime_observability import (
    _chat_usage_payload,
    _responses_usage_payload,
    _usage_from_metadata,
)
from ksadk.sessions import SessionEvent


@dataclass
class PreparedConversationTurn:
    """一次 turn 编排后的标准输入。

    这个对象把“会话归属”“用户最新输入”“投影后的上下文 history”
    和“附件/parts”等运行时所需信息收拢到一起，避免不同 endpoint
    各自重新拼装。
    """

    session_id: str
    invocation_id: str
    user_input: str
    user_display_input: str
    history: list[dict[str, str]]
    input_content: list[dict[str, Any]]
    input_messages: list[dict[str, Any]]
    user_parts: list[dict[str, Any]]
    attachments: list[dict[str, Any]]
    attachment_results: list[dict[str, Any]]
    current_attachments: list[dict[str, Any]]
    current_attachment_results: list[dict[str, Any]]
    has_current_files: bool
    model_metadata: dict[str, Any] = field(default_factory=dict)
    model_options: dict[str, Any] = field(default_factory=dict)
    instructions: str = ""
    request_metadata: dict[str, Any] = field(default_factory=dict)
    compaction_triggered: bool = False
    compaction_trigger: str | None = None
    compacted_until_seq_id: int | None = None
    resume_input: dict[str, Any] | None = None
    # 双维度 run 标识：run_mode=怎么跑（background/foreground），
    # run_trigger=怎么开始（new_run/checkpoint_resume/approval_resume）
    run_mode: str = RUN_MODE_FOREGROUND
    run_trigger: str = RUN_TRIGGER_NEW_RUN
    request_history: list[dict[str, str]] = field(default_factory=list)
    request_responses_history: list[dict[str, Any]] = field(default_factory=list)
    responses_history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CompactionPlan:
    """一次 compaction 规划结果。

    预览阶段和真正落 checkpoint 阶段都复用这份规划，避免 `/run_sse`
    与 conversation runtime 各自写一套“是否需要压缩”的条件判断。
    """

    should_compact: bool
    groups_to_compact: list[list[SessionEvent]]
    total_chars: int
    total_estimated_tokens: int
    group_count: int
    tail_groups: int
    auto_compact_threshold_tokens: int | None = None
    auto_compact_threshold_percentage: int | None = None
    compacted_until_seq_id: int | None = None
    pinned_group_indexes: list[int] = field(default_factory=list)
    pinned_state: dict[str, Any] = field(default_factory=dict)


def build_responses_payload(
    *,
    output_text: str,
    model: Optional[str],
    session_id: str,
    response_id: str | None = None,
    created_at: int | None = None,
    status: str = "completed",
    metadata: Mapping[str, Any] | None = None,
    usage: Mapping[str, Any] | None = None,
    incomplete_details: Mapping[str, Any] | None = None,
    error: Mapping[str, Any] | None = None,
    output_items: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    response_id = response_id or f"resp_{uuid.uuid4().hex}"
    created_at = created_at or int(time.time())
    message_id = f"msg_{uuid.uuid4().hex[:12]}"
    output_item_status = "completed" if status == "completed" else status
    message_item = {
        "id": message_id,
        "type": "message",
        "status": output_item_status,
        "role": "assistant",
        "content": [{"type": "output_text", "text": output_text}],
    }
    normalized_output_items = [
        dict(item) for item in list(output_items or []) if isinstance(item, Mapping)
    ]
    if normalized_output_items:
        output = normalized_output_items
        if not any(str(item.get("type") or "") == "message" for item in output):
            output = [message_item, *output]
    else:
        output = [message_item]
    usage_payload = _responses_usage_payload(usage or _usage_from_metadata(metadata))
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "error": dict(error) if isinstance(error, Mapping) else None,
        "incomplete_details": (
            dict(incomplete_details) if isinstance(incomplete_details, Mapping) else None
        ),
        "instructions": None,
        "metadata": dict(metadata or {}),
        "model": model or "agent",
        "parallel_tool_calls": True,
        "temperature": None,
        "top_p": None,
        "tools": [],
        "output": output,
        "output_text": output_text,
        "usage": usage_payload,
        "session_id": session_id,
    }


def extract_responses_resume_input(input_payload: Any) -> dict[str, Any] | None:
    """Extract OpenAI Responses approval resume input without exposing runner details."""
    if isinstance(input_payload, Mapping):
        candidates = [input_payload]
    elif isinstance(input_payload, Sequence) and not isinstance(
        input_payload, (str, bytes, bytearray)
    ):
        candidates = [item for item in input_payload if isinstance(item, Mapping)]
    else:
        return None

    for item in candidates:
        item_type = str(item.get("type") or "").strip()
        if item_type == "agentengine.resume_checkpoint":
            checkpoint_resume: dict[str, Any] = {"type": "agentengine.resume_checkpoint"}
            for key in (
                "run_id",
                "checkpoint_id",
                "resume_attempt_id",
                "framework",
                "framework_ref",
            ):
                if key in item:
                    checkpoint_resume[key] = item.get(key)
            return checkpoint_resume

        if item_type == "mcp_approval_response":
            approval_resume: dict[str, Any] = {"type": "mcp_approval_response"}
            if item.get("id"):
                approval_resume["id"] = str(item.get("id"))
            approval_request_id = item.get("approval_request_id")
            if approval_request_id:
                approval_resume["approval_request_id"] = str(approval_request_id)
            if "approve" in item:
                approval_resume["approve"] = item.get("approve")
            elif "approved" in item:
                approval_resume["approve"] = item.get("approved")
            if item.get("reason") is not None:
                approval_resume["reason"] = str(item.get("reason") or "")
            return approval_resume

        if item_type == "function_call_output":
            call_id = item.get("call_id")
            if not call_id:
                continue
            function_resume: dict[str, Any] = {
                "type": "function_call_output",
                "call_id": str(call_id),
                "output": item.get("output", ""),
            }
            if item.get("id"):
                function_resume["id"] = str(item.get("id"))
            return function_resume

        if item_type in {"ksadk_resume", "ksadk.approval_response"}:
            ksadk_resume: dict[str, Any] = {"type": "ksadk_resume"}
            interrupt_id = (
                item.get("interrupt_id") or item.get("approval_request_id") or item.get("id")
            )
            if interrupt_id:
                ksadk_resume["interrupt_id"] = str(interrupt_id)
            if "value" in item:
                ksadk_resume["value"] = item.get("value")
            elif "resume" in item:
                ksadk_resume["value"] = item.get("resume")
            else:
                ksadk_resume["value"] = {
                    key: value
                    for key, value in item.items()
                    if key not in {"type", "interrupt_id", "approval_request_id", "id"}
                }
            return ksadk_resume

    return None


def build_chat_completions_payload(
    *,
    output_text: str,
    model: Optional[str],
    session_id: str,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    usage = _chat_usage_payload(_usage_from_metadata(metadata))
    payload = {
        "id": f"chatcmpl-{uuid.uuid4()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or "agent",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": output_text},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
        "session_id": session_id,
    }
    if isinstance(metadata, Mapping) and metadata:
        payload["metadata"] = dict(metadata)
    return payload


def build_compaction_sse_event(
    *,
    phase: str,
    trigger: str,
    compacted_until_seq_id: int | None = None,
    total_chars: int | None = None,
    total_estimated_tokens: int | None = None,
    group_count: int | None = None,
    threshold_percentage: int | None = None,
) -> str:
    """统一生成 compaction 相关 SSE，方便不同入口保持同一语义。"""

    payload: dict[str, Any] = {
        "phase": phase,
        "trigger": trigger,
        "timestamp": int(time.time() * 1000),
    }
    if compacted_until_seq_id is not None:
        payload["compacted_until_seq_id"] = compacted_until_seq_id
    if total_chars is not None:
        payload["total_chars"] = total_chars
    if total_estimated_tokens is not None:
        payload["total_estimated_tokens"] = total_estimated_tokens
    if group_count is not None:
        payload["group_count"] = group_count
    if threshold_percentage is not None:
        payload["threshold_percentage"] = threshold_percentage
    return (
        f"event: response.compaction.{phase}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )

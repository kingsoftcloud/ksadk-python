"""Project LangGraph tool updates into session events."""

from __future__ import annotations

import re
from typing import Any, Mapping


class _LangGraphToolProjectionMixin:
    @staticmethod
    def _tool_events_from_graph_update(
        node_output: Any,
        *,
        tool_names: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Preserve LangGraph update-mode tool messages as structured runner events."""

        if isinstance(node_output, Mapping):
            messages = node_output.get("messages")
        else:
            messages = getattr(node_output, "messages", None)
        if not isinstance(messages, (list, tuple)):
            return []

        events: list[dict[str, Any]] = []
        for message in messages:
            if isinstance(message, Mapping):
                tool_calls = message.get("tool_calls")
                tool_call_id = message.get("tool_call_id")
                message_name = message.get("name")
                message_content = message.get("content")
                message_status = message.get("status")
            else:
                tool_calls = getattr(message, "tool_calls", None)
                tool_call_id = getattr(message, "tool_call_id", None)
                message_name = getattr(message, "name", None)
                message_content = getattr(message, "content", None)
                message_status = getattr(message, "status", None)

            if isinstance(tool_calls, (list, tuple)):
                for tool_call in tool_calls:
                    if isinstance(tool_call, Mapping):
                        call_id = str(tool_call.get("id") or tool_call.get("tool_call_id") or "")
                        name = str(tool_call.get("name") or "tool")
                        args = tool_call.get("args")
                    else:
                        call_id = str(
                            getattr(tool_call, "id", None)
                            or getattr(tool_call, "tool_call_id", None)
                            or ""
                        )
                        name = str(getattr(tool_call, "name", None) or "tool")
                        args = getattr(tool_call, "args", None)
                    if call_id:
                        tool_names[call_id] = name
                    events.append(
                        {
                            "type": "tool_call",
                            "tool_call_id": call_id or name,
                            "tool_name": name,
                            "tool_args": args,
                        }
                    )

            if tool_call_id:
                call_id = str(tool_call_id)
                name = str(message_name or tool_names.get(call_id) or "tool")
                status = str(message_status or "").lower()
                events.append(
                    {
                        "type": "tool_result",
                        "tool_call_id": call_id,
                        "tool_name": name,
                        "tool_output": message_content,
                        "error": message_content if status in {"error", "failed"} else None,
                    }
                )
        return events

    def _filter_tool_tags(self, content: str) -> str:
        """过滤 <tool_call> 标签"""
        if not isinstance(content, str):
            return content
        content = re.sub(r"<tool_call>.*?</tool_call>", "", content, flags=re.DOTALL)
        content = re.sub(r"</?(?:tool_call|arg_key|arg_value)>", "", content)
        return content

    def _extract_output(self, result: Any) -> str:
        """从结果中提取输出文本"""
        if isinstance(result, dict):
            # 自定义 output 字段是业务显式出参，优先于内部 messages state。
            if "output" in result:
                return str(result["output"])
            # LangGraph 示例常用 answer 作为最终业务回答字段。
            if "answer" in result:
                return str(result["answer"])
            # 标准 messages 格式
            if "messages" in result:
                messages = result["messages"]
                if messages:
                    last = messages[-1]
                    content = (
                        last.get("content", str(last))
                        if isinstance(last, dict)
                        else getattr(last, "content", str(last))
                    )
                    return str(content)
        return str(result) if result else ""

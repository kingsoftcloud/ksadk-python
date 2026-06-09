"""
RemoteRunner - 远程 Agent 运行时

与 AgentTUI 配合使用，提供和本地 Runner 一致的接口
"""

import json
import os
from typing import Any, AsyncIterator, Dict, Mapping, Optional, Sequence

from ksadk.runners.base_runner import BaseRunner


class RemoteRunner(BaseRunner):
    """远程 Agent 运行时

    通过 HTTP 调用远程部署的 Agent，兼容 OpenAI API 格式
    """

    def __init__(
        self,
        endpoint: str,
        api_key: Optional[str] = None,
        session_id: Optional[str] = None,
        insecure: bool = False,
        model: Optional[str] = None,
        api_format: str = "chat_completions",
        responses_session_header: Optional[str] = None,
    ):
        # 不调用父类 __init__，因为不需要 detection_result
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.session_id = session_id
        self.insecure = insecure
        self.model = model
        self.api_format = self._normalize_api_format(api_format)
        self.responses_session_header = (
            str(
                responses_session_header or os.environ.get("KSADK_RESPONSES_SESSION_HEADER") or ""
            ).strip()
            or None
        )
        self._agent = None  # 兼容 BaseRunner
        self._responses_tool_names: dict[str, str] = {}
        self._responses_tool_args: dict[str, str] = {}

    @staticmethod
    def _normalize_api_format(api_format: Optional[str]) -> str:
        normalized = str(api_format or "chat_completions").strip().lower()
        if normalized in {"responses", "response", "openresponses", "open_responses"}:
            return "responses"
        return "chat_completions"

    def load_agent(self) -> None:
        """远程 Runner 不需要加载 Agent"""
        pass

    def prepare_for_request(self, model: Optional[str]) -> None:
        normalized = self.normalize_requested_model(model)
        if normalized is None:
            return
        self.model = normalized

    def _get_client_kwargs(self) -> dict:
        """获取 httpx 客户端配置"""
        is_local = any(x in self.endpoint for x in ["localhost", "127.0.0.1", "0.0.0.0"])
        kwargs = {"timeout": 120, "trust_env": not is_local}
        if self.insecure:
            kwargs["verify"] = False
        return kwargs

    @staticmethod
    def _build_responses_input(user_input: Any) -> Any:
        """Use the OpenAI Responses-compatible simple string shape when possible.

        OpenClaw accepts `input` as a string or item array, but rejects a Chat-style
        message object whose `content` is a bare string. For remote chat/TUI calls
        we only need the current user turn, so the string form is the safest common
        denominator and matches OpenClaw's documented examples.
        """
        if isinstance(user_input, Mapping):
            if user_input.get("type"):
                return [dict(user_input)]
            if RemoteRunner._is_chat_style_message(user_input):
                return RemoteRunner._responses_message_text(user_input)
            return dict(user_input)
        if isinstance(user_input, Sequence) and not isinstance(user_input, (str, bytes, bytearray)):
            items = list(user_input)
            if any(isinstance(item, Mapping) and item.get("type") for item in items):
                return [dict(item) if isinstance(item, Mapping) else item for item in items]
            chat_messages = [
                item
                for item in items
                if isinstance(item, Mapping) and RemoteRunner._is_chat_style_message(item)
            ]
            if chat_messages:
                latest_user = next(
                    (
                        item
                        for item in reversed(chat_messages)
                        if str(item.get("role") or "").strip().lower() == "user"
                    ),
                    chat_messages[-1],
                )
                return RemoteRunner._responses_message_text(latest_user)
            return items
        return str(user_input or "")

    @staticmethod
    def _build_responses_conversation_history(history: Any, current_input: Any) -> list[dict[str, Any]]:
        if not isinstance(history, Sequence) or isinstance(
            history, (str, bytes, bytearray)
        ):
            return []

        messages: list[dict[str, Any]] = []
        current_text = RemoteRunner._responses_message_text(
            {"role": "user", "content": current_input}
        ).strip()
        for item in history:
            if not isinstance(item, Mapping):
                continue
            role = str(item.get("role") or "").strip().lower()
            if role == "model":
                role = "assistant"
            if role not in {"user", "assistant"}:
                continue
            text = RemoteRunner._responses_message_text(item).strip()
            if not text:
                continue
            if role == "user" and current_text and text == current_text:
                continue
            messages.append(
                {
                    "role": role,
                    "content": [{"type": "input_text", "text": text}],
                }
            )
        return messages

    @staticmethod
    def _responses_conversation_name(input_data: Mapping[str, Any], session_id: Optional[str]) -> str:
        if not session_id:
            return ""
        platform_context = input_data.get("platform_context")
        agent_id = ""
        if isinstance(platform_context, Mapping):
            agent_id = str(platform_context.get("agent_id") or "").strip()
        if agent_id:
            return f"agentengine:{agent_id}:{session_id}"
        return f"agentengine:{session_id}"

    @staticmethod
    def _responses_conversation_value(value: Any) -> str:
        if isinstance(value, Mapping):
            return str(value.get("id") or "").strip()
        return str(value or "").strip()

    def _build_responses_payload(
        self,
        input_data: Mapping[str, Any],
        *,
        stream: bool,
    ) -> dict[str, Any]:
        user_input = input_data.get("input", "")
        session_id = input_data.get("session_id") or self.session_id
        previous_response_id = input_data.get("previous_response_id")

        if self.responses_session_header:
            payload = {
                "input": self._build_responses_input(user_input),
                "stream": stream,
            }
        else:
            payload = {
                "input": self._build_responses_input(user_input),
                "stream": stream,
            }
            history_enabled = bool(input_data.get("responses_conversation")) and not previous_response_id
            if history_enabled:
                history = self._build_responses_conversation_history(
                    input_data.get("history"),
                    user_input,
                )
                if history:
                    payload["conversation_history"] = history
            conversation = self._responses_conversation_value(input_data.get("conversation"))
            if conversation and not previous_response_id:
                payload["conversation"] = conversation
            elif (
                input_data.get("responses_conversation")
                and session_id
                and not previous_response_id
            ):
                conversation = self._responses_conversation_name(input_data, str(session_id))
                if conversation:
                    payload["conversation"] = conversation

        if previous_response_id:
            payload["previous_response_id"] = str(previous_response_id)
        return payload

    @staticmethod
    def _is_chat_style_message(value: Mapping[str, Any]) -> bool:
        return not value.get("type") and ("role" in value or "content" in value)

    @staticmethod
    def _responses_message_text(message: Mapping[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
            parts: list[str] = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, Mapping):
                    text = part.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts)
        return str(content or "")

    def _get_headers(self, session_id: Optional[str] = None) -> dict:
        """获取请求头"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.api_format == "responses" and session_id and self.responses_session_header:
            headers[self.responses_session_header] = session_id
        return headers

    async def invoke(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """非流式调用远程 Agent"""
        import httpx

        user_input = input_data.get("input", "")
        session_id = input_data.get("session_id") or self.session_id

        if self.api_format == "responses":
            url = f"{self.endpoint}/v1/responses"
            payload = self._build_responses_payload(input_data, stream=False)
        else:
            url = f"{self.endpoint}/v1/chat/completions"
            payload = {
                "messages": [{"role": "user", "content": user_input}],
                "stream": False,
            }
        if session_id and self.api_format != "responses":
            payload["session_id"] = session_id
        if self.model:
            payload["model"] = self.model

        async with httpx.AsyncClient(**self._get_client_kwargs()) as client:
            response = await client.post(url, json=payload, headers=self._get_headers(session_id))
            response.raise_for_status()
            data = response.json()

        if self.api_format == "responses":
            return {"output": self._extract_responses_output_text(data) or str(data)}

        # 提取 OpenAI Chat Completions 格式响应
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            content = str(data)

        return {"output": content}

    async def stream(self, input_data: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        """流式调用远程 Agent"""
        import httpx

        user_input = input_data.get("input", "")
        session_id = input_data.get("session_id") or self.session_id

        if self.api_format == "responses":
            url = f"{self.endpoint}/v1/responses"
            payload = self._build_responses_payload(input_data, stream=True)
        else:
            url = f"{self.endpoint}/v1/chat/completions"
            payload = {
                "messages": [{"role": "user", "content": user_input}],
                "stream": True,
            }
        if session_id and self.api_format != "responses":
            payload["session_id"] = session_id
        if self.model:
            payload["model"] = self.model

        async with httpx.AsyncClient(**self._get_client_kwargs()) as client:
            async with client.stream(
                "POST", url, json=payload, headers=self._get_headers(session_id)
            ) as response:
                response.raise_for_status()

                event_name = ""
                async for line in response.aiter_lines():
                    if not line:
                        event_name = ""
                        continue

                    if line.startswith("event:"):
                        event_name = line.split(":", 1)[1].strip()
                        continue

                    if line.startswith("data: "):
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            break

                        try:
                            data = json.loads(data_str)

                            if self.api_format == "responses":
                                async for item in self._iter_responses_stream_events(
                                    data, event_name=event_name
                                ):
                                    yield item
                                continue

                            # 解析 OpenAI Chat Completions 流式格式
                            choices = data.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                content = delta.get("content", "")
                                reasoning = delta.get("reasoning_content", "")

                                if reasoning:
                                    yield {"delta": reasoning, "type": "thinking"}
                                if content:
                                    yield {"delta": content, "type": "text"}

                        except json.JSONDecodeError:
                            pass

    @staticmethod
    def _extract_responses_output_text(data: Dict[str, Any]) -> str:
        output_text = data.get("output_text")
        if output_text:
            return str(output_text)
        output = data.get("output") or []
        for item in output:
            if not isinstance(item, dict):
                continue
            for content in item.get("content") or []:
                if isinstance(content, dict) and content.get("text"):
                    return str(content["text"])
        return ""

    @staticmethod
    def _stringify_responses_payload(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, indent=2)
        return str(value)

    @staticmethod
    def _responses_error_message(data: Dict[str, Any]) -> str:
        response = data.get("response") if isinstance(data.get("response"), dict) else data
        error = response.get("error") if isinstance(response, dict) else data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or "Agent 运行失败")
        if error:
            return str(error)
        return "Agent 运行失败"

    @staticmethod
    def _responses_item_key(item: Dict[str, Any], data: Dict[str, Any]) -> str:
        return str(
            item.get("id")
            or item.get("item_id")
            or item.get("call_id")
            or data.get("item_id")
            or data.get("call_id")
            or data.get("output_index")
            or ""
        )

    def _remember_responses_tool(
        self, key: str, item: Dict[str, Any], name: str, args: str
    ) -> None:
        if key:
            self._responses_tool_names[key] = name
            self._responses_tool_args[key] = args
        call_id = str(item.get("call_id") or "")
        if call_id:
            self._responses_tool_names[call_id] = name
            self._responses_tool_args[call_id] = args

    def _responses_tool_name(self, key: str, item: Dict[str, Any], fallback: str = "tool") -> str:
        call_id = str(item.get("call_id") or "")
        return str(
            item.get("name")
            or item.get("tool_name")
            or (self._responses_tool_names.get(key) if key else "")
            or (self._responses_tool_names.get(call_id) if call_id else "")
            or fallback
        )

    @staticmethod
    def _responses_item_text(item: Dict[str, Any]) -> str:
        for field in ("output_text", "text", "summary_text", "summary", "delta"):
            value = item.get(field)
            if isinstance(value, str) and value:
                return value
        content = item.get("content")
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
            return "".join(parts)
        if isinstance(content, str):
            return content
        return ""

    async def _iter_responses_output_item(
        self,
        data: Dict[str, Any],
        *,
        status: str,
    ) -> AsyncIterator[Dict[str, Any]]:
        item = data.get("item") or data.get("output_item") or data
        if not isinstance(item, dict):
            return

        item_type = str(item.get("type") or "").strip()
        key = self._responses_item_key(item, data)
        if item_type == "function_call":
            name = self._responses_tool_name(key, item)
            args = self._stringify_responses_payload(
                item.get("arguments")
                if "arguments" in item
                else item.get("args", item.get("input"))
            )
            self._remember_responses_tool(key, item, name, args)
            yield {"type": "tool_call", "tool_name": name, "tool_args": args, "status": status}
            return

        if item_type == "function_call_output":
            name = self._responses_tool_name(key, item)
            output = self._stringify_responses_payload(
                item.get("output") if "output" in item else item.get("result", item.get("content"))
            )
            yield {"type": "tool_result", "tool_name": name, "tool_output": output}
            return

        if item_type == "mcp_approval_request":
            name = str(item.get("name") or "approval")
            args = self._stringify_responses_payload(item.get("arguments") or item.get("args"))
            approval_request_id = str(item.get("id") or item.get("approval_request_id") or "")
            yield {
                "type": "tool_call",
                "tool_name": name,
                "tool_args": args,
                "status": "paused",
                "approval_request_id": approval_request_id,
            }
            yield {
                "type": "interrupt",
                "interrupt_info": {
                    "id": approval_request_id,
                    "approval_request_id": approval_request_id,
                    "name": name,
                    "server_label": str(item.get("server_label") or ""),
                },
            }
            return

        if item_type in {"reasoning", "reasoning_summary", "reasoning_summary_text"}:
            text = self._responses_item_text(item)
            if text:
                yield {"delta": text, "type": "thinking"}
            return

        if item_type == "message":
            text = self._responses_item_text(item)
            if text and status != "completed":
                yield {"delta": text, "type": "text"}
            return

    async def _iter_responses_stream_events(
        self,
        data: Dict[str, Any],
        *,
        event_name: str = "",
    ) -> AsyncIterator[Dict[str, Any]]:
        event_type = str(data.get("type") or event_name or data.get("_event") or "")
        if event_name == "response.reasoning.delta":
            delta = data.get("delta")
            if delta:
                yield {"delta": str(delta), "type": "thinking"}
            return
        if event_type in {
            "response.reasoning.delta",
            "response.reasoning_text.delta",
            "response.reasoning_summary.delta",
            "response.reasoning_summary_text.delta",
        }:
            delta = data.get("delta") or data.get("text")
            if delta:
                yield {"delta": str(delta), "type": "thinking"}
            return
        if event_type == "response.output_text.delta":
            delta = data.get("delta")
            if delta:
                yield {"delta": str(delta), "type": "text"}
            return
        if event_type == "response.output_item.added":
            async for item in self._iter_responses_output_item(data, status="running"):
                yield item
            return
        if event_type == "response.output_item.done":
            async for item in self._iter_responses_output_item(data, status="completed"):
                yield item
            return
        if event_type == "response.function_call_arguments.delta":
            key = str(data.get("item_id") or data.get("call_id") or "")
            name = self._responses_tool_name(key, data)
            args = f"{self._responses_tool_args.get(key, '')}{str(data.get('delta') or '')}"
            if key:
                self._responses_tool_args[key] = args
            yield {"type": "tool_call", "tool_name": name, "tool_args": args, "status": "running"}
            return
        if event_type == "response.function_call_arguments.done":
            key = str(data.get("item_id") or data.get("call_id") or "")
            name = self._responses_tool_name(key, data)
            args = self._stringify_responses_payload(
                data.get("arguments") or self._responses_tool_args.get(key, "")
            )
            if key:
                self._responses_tool_args[key] = args
            yield {"type": "tool_call", "tool_name": name, "tool_args": args, "status": "running"}
            return
        if event_type == "response.completed":
            response = data.get("response") if isinstance(data.get("response"), dict) else data
            output = response.get("output") if isinstance(response, dict) else None
            if isinstance(output, list):
                yield {
                    "type": "responses_output",
                    "output": output,
                    "response_id": response.get("id"),
                }
            return
        if event_type == "response.failed":
            yield {"type": "error", "message": self._responses_error_message(data)}
            return
        if event_type == "response.incomplete":
            yield {"type": "error", "message": "Agent 响应未完成"}
            return
        if isinstance(data.get("delta"), str):
            yield {"delta": str(data["delta"]), "type": "text"}
            return
        output_text = RemoteRunner._extract_responses_output_text(data)
        if output_text and event_type != "response.completed":
            yield {"delta": output_text, "type": "text"}

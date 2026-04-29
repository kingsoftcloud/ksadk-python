"""
RemoteRunner - 远程 Agent 运行时

与 AgentTUI 配合使用，提供和本地 Runner 一致的接口
"""

import json
from typing import Any, AsyncIterator, Dict, Optional

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
    ):
        # 不调用父类 __init__，因为不需要 detection_result
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.session_id = session_id
        self.insecure = insecure
        self.model = model
        self.api_format = self._normalize_api_format(api_format)
        self._agent = None  # 兼容 BaseRunner

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

    def _get_headers(self) -> dict:
        """获取请求头"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def invoke(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """非流式调用远程 Agent"""
        import httpx

        user_input = input_data.get("input", "")
        session_id = input_data.get("session_id") or self.session_id

        if self.api_format == "responses":
            url = f"{self.endpoint}/v1/responses"
            payload = {
                "input": [{"role": "user", "content": user_input}],
                "stream": False,
            }
        else:
            url = f"{self.endpoint}/v1/chat/completions"
            payload = {
                "messages": [{"role": "user", "content": user_input}],
                "stream": False,
            }
        if session_id:
            payload["session_id"] = session_id
        if self.model:
            payload["model"] = self.model

        async with httpx.AsyncClient(**self._get_client_kwargs()) as client:
            response = await client.post(url, json=payload, headers=self._get_headers())
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
            payload = {
                "input": [{"role": "user", "content": user_input}],
                "stream": True,
            }
        else:
            url = f"{self.endpoint}/v1/chat/completions"
            payload = {
                "messages": [{"role": "user", "content": user_input}],
                "stream": True,
            }
        if session_id:
            payload["session_id"] = session_id
        if self.model:
            payload["model"] = self.model

        async with httpx.AsyncClient(**self._get_client_kwargs()) as client:
            async with client.stream("POST", url, json=payload, headers=self._get_headers()) as response:
                response.raise_for_status()

                async for line in response.aiter_lines():
                    if not line:
                        continue

                    if line.startswith("data: "):
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            break

                        try:
                            data = json.loads(data_str)
                            
                            if self.api_format == "responses":
                                async for item in self._iter_responses_stream_events(data):
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
    async def _iter_responses_stream_events(data: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        event_name = str(data.get("type") or data.get("_event") or "")
        if event_name == "response.reasoning.delta":
            delta = data.get("delta")
            if delta:
                yield {"delta": str(delta), "type": "thinking"}
            return
        if event_name == "response.output_text.delta":
            delta = data.get("delta")
            if delta:
                yield {"delta": str(delta), "type": "text"}
            return
        if isinstance(data.get("delta"), str):
            yield {"delta": str(data["delta"]), "type": "text"}
            return
        output_text = RemoteRunner._extract_responses_output_text(data)
        if output_text and event_name != "response.completed":
            yield {"delta": output_text, "type": "text"}

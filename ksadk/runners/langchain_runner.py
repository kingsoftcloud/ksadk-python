"""
LangChainRunner - LangChain 框架运行时（精简版）

透传 LangChain 原生能力，Tracing 通过 Callback 集成
"""

import os
import uuid
from typing import Any, AsyncIterator, Dict

from ksadk.runners.base_runner import BaseRunner
from ksadk.runners.utils import (
    get_langfuse_callback,
    get_langfuse_metadata,
    load_agent_module,
    prepare_trace_metadata,
)


class LangChainRunner(BaseRunner):
    """LangChain 框架运行时"""

    def load_agent(self) -> None:
        self._load_agent(force_reload=False)

    def _load_agent(self, *, force_reload: bool) -> None:
        """加载 LangChain Agent/Chain"""
        self._agent, self._module = load_agent_module(
            self.project_dir,
            self.detection_result.entry_point,
            self.detection_result.agent_variable,
            force_reload=force_reload,
        )
        self._loaded_model_name = self.normalize_requested_model(
            os.getenv("OPENAI_MODEL_NAME") or os.getenv("MODEL_NAME")
        )

    def prepare_for_request(self, model: str | None) -> None:
        normalized = self.sync_process_model_env(model)
        if normalized is None or self._agent is None:
            return
        if normalized == getattr(self, "_loaded_model_name", None):
            return
        self._load_agent(force_reload=True)

    def _get_config(self, session_id: str = None) -> dict:
        """获取运行配置（包含 Langfuse Callback）"""
        config = {}
        
        # Langfuse Callback
        langfuse_cb = get_langfuse_callback()
        if langfuse_cb:
            config["callbacks"] = [langfuse_cb]
            
            # 获取 metadata
            metadata = get_langfuse_metadata(session_id)
            user_id, tags, _, _ = prepare_trace_metadata(session_id)
            if user_id:
                metadata["langfuse_user_id"] = user_id
            if tags:
                metadata["langfuse_tags"] = tags
            config["metadata"] = metadata
        
        return config if config else None

    async def invoke(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """调用 LangChain Agent/Chain"""
        user_input = input_data.get("input", "")
        session_id = input_data.get("session_id") or str(uuid.uuid4())[:8]
        config = self._get_config(session_id)

        # 支持多种调用方式
        if hasattr(self._agent, "ainvoke"):
            result = await self._agent.ainvoke({"input": user_input}, config=config)
        elif hasattr(self._agent, "invoke"):
            result = self._agent.invoke({"input": user_input}, config=config)
        elif callable(self._agent):
            result = self._agent(user_input)
        else:
            raise TypeError("Agent 不支持 invoke 调用")

        # 提取输出
        if isinstance(result, dict):
            output = result.get("output", result.get("text", str(result)))
        else:
            output = str(result)

        return {"output": output}

    async def stream(self, input_data: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        """流式调用 LangChain Agent/Chain"""
        user_input = input_data.get("input", "")
        session_id = input_data.get("session_id") or str(uuid.uuid4())[:8]
        config = self._get_config(session_id)

        accumulated_text = ""

        try:
            if hasattr(self._agent, "astream"):
                async for chunk in self._agent.astream({"input": user_input}, config=config):
                    delta, chunk_type = self._extract_chunk(chunk)
                    if delta:
                        accumulated_text += delta
                        yield {"delta": delta, "type": chunk_type}
                        
            elif hasattr(self._agent, "stream"):
                for chunk in self._agent.stream({"input": user_input}, config=config):
                    delta, chunk_type = self._extract_chunk(chunk)
                    if delta:
                        accumulated_text += delta
                        yield {"delta": delta, "type": chunk_type}
                        
        except Exception as e:
            # 流式失败时回退到同步调用
            print(f"\n⚠️ 流式调用失败: {e}，回退到同步模式")

        if not accumulated_text:
            result = await self.invoke(input_data)
            yield {"output": result.get("output", ""), "type": "final"}

    def _extract_chunk(self, chunk) -> tuple:
        """从 chunk 中提取内容和类型"""
        if isinstance(chunk, dict):
            if "output" in chunk:
                return chunk["output"], "text"
            elif "text" in chunk:
                return chunk["text"], "text"
            return None, None

        # 处理 LangChain Message 对象
        reasoning = None
        if hasattr(chunk, "reasoning_content") and chunk.reasoning_content:
            reasoning = chunk.reasoning_content
        elif hasattr(chunk, "additional_kwargs"):
            reasoning = chunk.additional_kwargs.get("reasoning_content")

        if reasoning:
            return reasoning, "thinking"

        content = chunk.content if hasattr(chunk, "content") else str(chunk)
        return content if content else None, "text"

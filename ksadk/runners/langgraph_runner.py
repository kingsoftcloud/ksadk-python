"""
LangGraphRunner - LangGraph 框架运行时

直接透传 LangGraph 原生能力，最小化封装
"""

import os
import uuid
import re
from typing import Any, AsyncIterator, Dict
import base64
from pathlib import Path

from ksadk.runners.base_runner import BaseRunner
from ksadk.sessions.continuity import LangGraphSessionAdapter
from ksadk.runners.utils import get_langfuse_callback, get_langfuse_metadata, load_agent_module
from langgraph.types import Command
from ksadk.conversations.attachments import classify_attachment_kind, read_attachment_bytes


class LangGraphRunner(BaseRunner):
    """LangGraph 框架运行时
    
    透传原生 LangGraph 功能，支持任意 State 格式
    """

    def load_agent(self) -> None:
        self._load_agent(force_reload=False)

    def _load_agent(self, *, force_reload: bool) -> None:
        """加载 LangGraph 编译后的图"""
        self._agent, self._module = load_agent_module(
            self.project_dir,
            self.detection_result.entry_point,
            self.detection_result.agent_variable,
            force_reload=force_reload,
        )
        self._loaded_model_name = self.normalize_requested_model(
            os.getenv("OPENAI_MODEL_NAME") or os.getenv("MODEL_NAME")
        )
        
        if not hasattr(self._agent, "invoke"):
            raise TypeError("加载的对象不是有效的 LangGraph CompiledGraph")

    def prepare_for_request(self, model: str | None) -> None:
        normalized = self.sync_process_model_env(model)
        if normalized is None or self._agent is None:
            return
        if normalized == getattr(self, "_loaded_model_name", None):
            return
        self._load_agent(force_reload=True)

    def get_session_adapter(self):
        return LangGraphSessionAdapter()

    def _get_config(self, session_id: str) -> dict:
        """获取运行配置"""
        config = {"configurable": {"thread_id": session_id}}
        
        langfuse_cb = get_langfuse_callback()
        if langfuse_cb:
            config["callbacks"] = [langfuse_cb]
            config["metadata"] = get_langfuse_metadata(session_id)
        
        return config

    @staticmethod
    def _ambient_context_text(payload: Dict[str, Any]) -> str:
        sections: list[str] = []
        kb_context = payload.get("kb_context") or {}
        kb_text = str(kb_context.get("formatted_text") or "").strip() if isinstance(kb_context, dict) else ""
        if kb_text:
            sections.append(f"Knowledge base context:\n{kb_text}")

        memory_context = payload.get("memory_context") or {}
        memory_text = (
            str(memory_context.get("formatted_text") or "").strip()
            if isinstance(memory_context, dict)
            else ""
        )
        if memory_text:
            sections.append(f"Long-term memory context:\n{memory_text}")

        return "\n\n".join(section for section in sections if section)

    @staticmethod
    def _strip_platform_context_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: value
            for key, value in payload.items()
            if key not in {"platform_context", "kb_context", "memory_context"}
        }

    def _has_prepare_state_hook(self) -> bool:
        module = getattr(self, "_module", None)
        return callable(getattr(module, "ksadk_prepare_state", None))

    def _prepare_state_with_hook(
        self,
        payload: Dict[str, Any],
        session_id: str,
        history: list,
        *,
        is_resume: bool = False,
    ) -> Dict[str, Any]:
        module = getattr(self, "_module", None)
        prepare_state = getattr(module, "ksadk_prepare_state", None)
        if not callable(prepare_state):
            return self._to_state(payload, history)

        normalized_payload = self._strip_platform_context_fields(payload)
        session_context = {
            "session_id": session_id,
            "history": list(history),
            "is_resume": bool(is_resume),
            "platform_context": payload.get("platform_context"),
            "kb_context": payload.get("kb_context"),
            "memory_context": payload.get("memory_context"),
        }
        prepared = prepare_state(dict(normalized_payload), session_context)
        if not isinstance(prepared, dict):
            raise TypeError("ksadk_prepare_state(payload, session_context) must return a dict")
        return prepared

    def _to_state(self, payload: Dict[str, Any], history: list) -> Dict[str, Any]:
        """将简化输入转换为 state，并保留除 input 外的附加字段。"""
        normalized_payload = self._strip_platform_context_fields(payload)
        ambient_text = self._ambient_context_text(payload)
        instructions = str(normalized_payload.pop("instructions", "") or "").strip()
        system_sections = [section for section in (instructions, ambient_text) if section]
        system_text = "\n\n".join(system_sections)

        if "input" in normalized_payload and "messages" not in normalized_payload:
            from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

            messages = []
            if system_text:
                messages.append(SystemMessage(content=system_text))
            for msg in history:
                role = msg.get("role")
                content = msg.get("content", "")
                if role == "user":
                    messages.append(HumanMessage(content=content))
                elif role in ("assistant", "model"):
                    messages.append(AIMessage(content=content))

            user_input = normalized_payload["input"] or "[empty message]"
            attachments = list(normalized_payload.get("attachments") or [])
            model_metadata = normalized_payload.get("model_metadata")
            user_content = self._build_langgraph_human_content(
                user_input,
                attachments,
                model_metadata=model_metadata if isinstance(model_metadata, dict) else None,
            )
            if not self._history_tail_matches_user_content(history, user_content):
                messages.append(HumanMessage(content=user_content))
            state = {k: v for k, v in normalized_payload.items() if k != "input"}
            state["messages"] = messages
            return state

        if "messages" in normalized_payload:
            state = dict(normalized_payload)
            if system_text and isinstance(state.get("messages"), list):
                from langchain_core.messages import SystemMessage

                state["messages"] = [SystemMessage(content=system_text), *state["messages"]]
            return state

        return normalized_payload

    @classmethod
    def _history_tail_matches_user_content(cls, history: list, user_content: Any) -> bool:
        if not history:
            return False
        tail = history[-1]
        if not isinstance(tail, dict) or tail.get("role") != "user":
            return False
        tail_text = cls._normalizable_text_content(tail.get("content"))
        user_text = cls._normalizable_text_content(user_content)
        return tail_text is not None and tail_text == user_text

    @staticmethod
    def _normalizable_text_content(content: Any) -> str | None:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            text_parts: list[str] = []
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "text":
                    return None
                text_parts.append(str(item.get("text") or ""))
            return "\n".join(text_parts).strip()
        return None

    @staticmethod
    def _build_langgraph_human_content(
        user_input: str,
        attachments: list[dict[str, Any]],
        *,
        model_metadata: dict[str, Any] | None,
    ) -> Any:
        del model_metadata
        image_blocks: list[dict[str, Any]] = []

        for attachment in attachments or []:
            if not isinstance(attachment, dict):
                continue

            mime_type = str(attachment.get("mime_type") or "application/octet-stream")
            display_name = str(attachment.get("display_name") or "")
            if classify_attachment_kind(mime_type, display_name) != "image":
                continue

            data_b64 = str(attachment.get("data") or "").strip()
            transport = str(attachment.get("transport") or "")
            file_uri = str(attachment.get("file_uri") or "").strip()

            if transport == "inline" and data_b64:
                image_blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{data_b64}",
                        },
                    }
                )
                continue

            if file_uri.startswith(("http://", "https://")):
                image_blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": file_uri,
                        },
                    }
                )
                continue

            storage_path = attachment.get("storage_path")
            if not storage_path:
                continue

            raw = read_attachment_bytes(Path(str(storage_path)))
            if not raw:
                continue

            image_blocks.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{base64.b64encode(raw).decode('ascii')}",
                    },
                }
            )

        if not image_blocks:
            return user_input

        content: list[dict[str, Any]] = []
        if user_input:
            content.append({"type": "text", "text": user_input})
        content.extend(image_blocks)
        return content

    async def _invoke_graph(
        self,
        payload: Any,
        *,
        config: dict[str, Any],
        context: dict[str, Any] | None,
    ) -> Any:
        if hasattr(self._agent, "ainvoke"):
            kwargs = self._build_optional_call_kwargs(
                self._agent.ainvoke,
                config=config,
                context=context,
            )
            return await self._agent.ainvoke(payload, **kwargs)
        kwargs = self._build_optional_call_kwargs(
            self._agent.invoke,
            config=config,
            context=context,
        )
        return self._agent.invoke(payload, **kwargs)

    async def invoke(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """调用 LangGraph 图
        
        支持两种输入格式：
        1. 简化格式: {"input": "hello"} - 自动转换为 messages
        2. 原生格式: {"messages": [...]} 或自定义 State - 直接透传
        """
        payload = dict(input_data)
        session_id = payload.pop("session_id", None) or str(uuid.uuid4())[:8]
        is_resume = payload.pop("resume", False)
        history = payload.pop("history", [])
        native_context = self.build_native_context(payload.get("platform_context"))
        normalized_payload = self._strip_platform_context_fields(payload)
        
        config = self._get_config(session_id)
        
        # 判断输入格式 / resume
        if self._has_prepare_state_hook():
            state = self._prepare_state_with_hook(payload, session_id, history, is_resume=is_resume)
        elif is_resume:
            if "input" in normalized_payload and len(normalized_payload) == 1:
                state = normalized_payload["input"]
            else:
                state = normalized_payload
        else:
            state = self._to_state(payload, history)

        try:
            if is_resume:
                result = await self._invoke_graph(
                    Command(resume=state),
                    config=config,
                    context=native_context,
                )
            else:
                result = await self._invoke_graph(
                    state,
                    config=config,
                    context=native_context,
                )

            return {"output": self._extract_output(result), "raw": result}
            
        except Exception as e:
            if "Interrupt" in type(e).__name__:
                interrupt_info = self._get_interrupt_info(self._agent.get_state(config))
                return {
                    "type": "interrupt",
                    "interrupt_info": interrupt_info,
                    "session_id": session_id,
                    "output": interrupt_info.get("message", "需要用户确认") if isinstance(interrupt_info, dict) else "需要用户确认",
                }
            raise

    def _extract_output(self, result: Any) -> str:
        """从结果中提取输出文本"""
        if isinstance(result, dict):
            # 自定义 output 字段是业务显式出参，优先于内部 messages state。
            if "output" in result:
                return result["output"]
            # 标准 messages 格式
            if "messages" in result:
                messages = result["messages"]
                if messages:
                    last = messages[-1]
                    return last.get("content", str(last)) if isinstance(last, dict) else getattr(last, "content", str(last))
        return str(result) if result else ""

    def _get_interrupt_info(self, state) -> dict:
        """从 state 中获取 interrupt 信息"""
        if hasattr(state, "tasks") and state.tasks:
            for task in state.tasks:
                if hasattr(task, "interrupts") and task.interrupts:
                    for intr in task.interrupts:
                        if hasattr(intr, "value"):
                            return intr.value
        return {}

    async def stream(self, input_data: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        """流式调用 LangGraph 图"""
        payload = dict(input_data)
        session_id = payload.pop("session_id", None) or str(uuid.uuid4())[:8]
        history = payload.pop("history", [])
        is_resume = payload.pop("resume", False)
        native_context = self.build_native_context(payload.get("platform_context"))
        normalized_payload = self._strip_platform_context_fields(payload)

        invoke_payload = dict(payload)
        invoke_payload["session_id"] = session_id
        if history:
            invoke_payload["history"] = history
        if is_resume:
            invoke_payload["resume"] = True
        
        config = self._get_config(session_id)

        if self._has_prepare_state_hook():
            state = self._prepare_state_with_hook(payload, session_id, history, is_resume=is_resume)
        elif is_resume:
            if "input" in normalized_payload and len(normalized_payload) == 1:
                state = normalized_payload["input"]
            else:
                state = normalized_payload
        else:
            state = self._to_state(payload, history)

        accumulated_text = ""
        accumulated_reasoning = ""
        emitted_non_text_event = False

        if not hasattr(self._agent, "astream_events"):
            result = await self.invoke(invoke_payload)
            yield {"output": result.get("output", ""), "type": "final"}
            return

        try:
            stream_input = Command(resume=state) if is_resume else state
            stream_kwargs = {"version": "v2", "config": config}
            if native_context and self._callable_accepts_keyword(self._agent.astream_events, "context"):
                stream_kwargs["context"] = native_context
            async for event in self._agent.astream_events(stream_input, **stream_kwargs):
                event_kind = event.get("event", "")

                if event_kind == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
                    if not chunk:
                        continue

                    # 推理内容
                    reasoning = getattr(chunk, "reasoning_content", None)
                    if not reasoning and hasattr(chunk, "additional_kwargs"):
                        reasoning = chunk.additional_kwargs.get("reasoning_content")
                    
                    if reasoning:
                        accumulated_reasoning += reasoning
                        yield {"delta": reasoning, "type": "thinking"}

                    # 常规内容
                    if hasattr(chunk, "content") and chunk.content:
                        content = self._filter_tool_tags(chunk.content)
                        if isinstance(content, str):
                            if accumulated_reasoning and content.startswith(accumulated_reasoning):
                                content = content[len(accumulated_reasoning):]
                            elif reasoning and content.startswith(reasoning):
                                content = content[len(reasoning):]
                        if content and content.strip():
                            accumulated_text += content
                            yield {"delta": content, "type": "text"}

                elif event_kind == "on_tool_start":
                    emitted_non_text_event = True
                    yield {
                        "type": "tool_call",
                        "tool_name": event.get("name", "unknown"),
                        "tool_args": event.get("data", {}).get("input", {}),
                        "run_id": event.get("run_id"),
                    }
                
                elif event_kind == "on_tool_end":
                    emitted_non_text_event = True
                    tool_output = event.get("data", {}).get("output", "")
                    yield {
                        "type": "tool_result",
                        "tool_name": event.get("name", "unknown"),
                        "tool_args": event.get("data", {}).get("input", {}),
                        "tool_output": tool_output if isinstance(tool_output, dict) else (str(tool_output) if tool_output else ""),
                        "run_id": event.get("run_id"),
                    }
                    
                elif event_kind == "on_chain_end":
                    output = event.get("data", {}).get("output", {})
                    if isinstance(output, dict) and "__interrupt__" in output:
                        emitted_non_text_event = True
                        yield {"type": "interrupt", "interrupt_info": output["__interrupt__"], "session_id": session_id}
                        return

        except Exception as e:
            if "Interrupt" in type(e).__name__:
                yield {"type": "interrupt", "interrupt_info": self._get_interrupt_info(self._agent.get_state(config)), "session_id": session_id}
                return
            raise

        if not accumulated_text and not emitted_non_text_event:
            result = await self.invoke(invoke_payload)
            yield {"output": result.get("output", ""), "type": "final"}

    def _filter_tool_tags(self, content: str) -> str:
        """过滤 <tool_call> 标签"""
        if not isinstance(content, str):
            return content
        content = re.sub(r'<tool_call>.*?</tool_call>', '', content, flags=re.DOTALL)
        content = re.sub(r'</?(?:tool_call|arg_key|arg_value)>', '', content)
        return content

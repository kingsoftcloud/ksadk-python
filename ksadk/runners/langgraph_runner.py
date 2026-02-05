"""
LangGraphRunner - LangGraph 框架运行时

直接透传 LangGraph 原生能力，最小化封装
"""

import uuid
import re
from typing import Any, AsyncIterator, Dict

from ksadk.runners.base_runner import BaseRunner
from ksadk.runners.utils import get_langfuse_callback, get_langfuse_metadata, load_agent_module
from langgraph.types import Command


class LangGraphRunner(BaseRunner):
    """LangGraph 框架运行时
    
    透传原生 LangGraph 功能，支持任意 State 格式
    """

    def load_agent(self) -> None:
        """加载 LangGraph 编译后的图"""
        self._agent, self._module = load_agent_module(
            self.project_dir,
            self.detection_result.entry_point,
            self.detection_result.agent_variable,
        )
        
        if not hasattr(self._agent, "invoke"):
            raise TypeError("加载的对象不是有效的 LangGraph CompiledGraph")

    def _get_config(self, session_id: str) -> dict:
        """获取运行配置"""
        config = {"configurable": {"thread_id": session_id}}
        
        langfuse_cb = get_langfuse_callback()
        if langfuse_cb:
            config["callbacks"] = [langfuse_cb]
            config["metadata"] = get_langfuse_metadata(session_id)
        
        return config

    async def invoke(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """调用 LangGraph 图
        
        支持两种输入格式：
        1. 简化格式: {"input": "hello"} - 自动转换为 messages
        2. 原生格式: {"messages": [...]} 或自定义 State - 直接透传
        """
        session_id = input_data.pop("session_id", None) or str(uuid.uuid4())[:8]
        is_resume = input_data.pop("resume", False)
        history = input_data.pop("history", [])
        
        config = self._get_config(session_id)
        
        # 判断输入格式
        if "input" in input_data and "messages" not in input_data:
            # 简化格式 -> 转换为 messages（包含历史）
            from langchain_core.messages import HumanMessage, AIMessage
            messages = []
            for msg in history:
                role = msg.get("role")
                content = msg.get("content", "")
                if role == "user":
                    messages.append(HumanMessage(content=content))
                elif role in ("assistant", "model"):
                    messages.append(AIMessage(content=content))
            messages.append(HumanMessage(content=input_data["input"]))
            state = {"messages": messages}
        else:
            # 原生格式 -> 直接透传
            state = input_data

        try:
            if is_resume:
                result = await self._agent.ainvoke(Command(resume=state), config=config) \
                    if hasattr(self._agent, "ainvoke") \
                    else self._agent.invoke(Command(resume=state), config=config)
            else:
                result = await self._agent.ainvoke(state, config=config) \
                    if hasattr(self._agent, "ainvoke") \
                    else self._agent.invoke(state, config=config)

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
            # 标准 messages 格式
            if "messages" in result:
                messages = result["messages"]
                if messages:
                    last = messages[-1]
                    return last.get("content", str(last)) if isinstance(last, dict) else getattr(last, "content", str(last))
            # 自定义 output 字段
            elif "output" in result:
                return result["output"]
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
        session_id = input_data.pop("session_id", None) or str(uuid.uuid4())[:8]
        history = input_data.pop("history", [])
        
        config = self._get_config(session_id)
        
        # 判断输入格式
        if "input" in input_data and "messages" not in input_data:
            # 简化格式 -> 转换为 messages（包含历史）
            from langchain_core.messages import HumanMessage, AIMessage
            messages = []
            for msg in history:
                role = msg.get("role")
                content = msg.get("content", "")
                if role == "user":
                    messages.append(HumanMessage(content=content))
                elif role in ("assistant", "model"):
                    messages.append(AIMessage(content=content))
            messages.append(HumanMessage(content=input_data["input"]))
            state = {"messages": messages}
        else:
            state = input_data

        accumulated_text = ""

        if not hasattr(self._agent, "astream_events"):
            result = await self.invoke({**input_data, "session_id": session_id})
            yield {"output": result.get("output", ""), "type": "final"}
            return

        try:
            async for event in self._agent.astream_events(state, version="v2", config=config):
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
                        accumulated_text += reasoning
                        yield {"delta": reasoning, "type": "thinking"}

                    # 常规内容
                    if hasattr(chunk, "content") and chunk.content:
                        content = self._filter_tool_tags(chunk.content)
                        if content and content.strip():
                            accumulated_text += content
                            yield {"delta": content, "type": "text"}

                elif event_kind == "on_tool_start":
                    yield {
                        "type": "tool_call",
                        "tool_name": event.get("name", "unknown"),
                        "tool_args": event.get("data", {}).get("input", {}),
                        "run_id": event.get("run_id"),
                    }
                
                elif event_kind == "on_tool_end":
                    tool_output = event.get("data", {}).get("output", "")
                    yield {
                        "type": "tool_result",
                        "tool_name": event.get("name", "unknown"),
                        "tool_output": str(tool_output) if tool_output else "",
                        "run_id": event.get("run_id"),
                    }
                    
                elif event_kind == "on_chain_end":
                    output = event.get("data", {}).get("output", {})
                    if isinstance(output, dict) and "__interrupt__" in output:
                        yield {"type": "interrupt", "interrupt_info": output["__interrupt__"], "session_id": session_id}
                        return

        except Exception as e:
            if "Interrupt" in type(e).__name__:
                yield {"type": "interrupt", "interrupt_info": self._get_interrupt_info(self._agent.get_state(config)), "session_id": session_id}
                return
            raise

        if not accumulated_text:
            result = await self.invoke({**input_data, "session_id": session_id})
            yield {"output": result.get("output", ""), "type": "final"}

    def _filter_tool_tags(self, content: str) -> str:
        """过滤 <tool_call> 标签"""
        if not isinstance(content, str):
            return content
        content = re.sub(r'<tool_call>.*?</tool_call>', '', content, flags=re.DOTALL)
        content = re.sub(r'</?(?:tool_call|arg_key|arg_value)>', '', content)
        return content

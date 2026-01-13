"""
LangGraphRunner - LangGraph 框架运行时

支持:
- Token 级别流式输出
- ADK-compatible OpenTelemetry Tracing
- Langfuse Graph Visualization (via CallbackHandler)
"""

import os
import sys
import json
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional
from ksadk.runners.base_runner import BaseRunner
from langchain_core.messages import HumanMessage

# OpenTelemetry for tracing
from opentelemetry import trace

tracer = trace.get_tracer(__name__)

# Langfuse CallbackHandler for graph visualization
_langfuse_callback = None


def _get_langfuse_callback():
    """获取 Langfuse CallbackHandler (懒加载)"""
    global _langfuse_callback
    
    if _langfuse_callback is not None:
        return _langfuse_callback
    
    # 检查是否配置了 Langfuse
    if not os.getenv("LANGFUSE_PUBLIC_KEY"):
        return None
    
    try:
        # 使用官方推荐的 langfuse.langchain 模块
        from langfuse.langchain import CallbackHandler
        _langfuse_callback = CallbackHandler()
        return _langfuse_callback
    except ImportError:
        return None
    except Exception:
        return None


def _make_llm_request_json(user_input: str) -> str:
    """Create ADK-compatible llm_request JSON"""
    return json.dumps({
        "contents": [{"role": "user", "parts": [{"text": user_input}]}]
    })


def _make_llm_response_json(output: str) -> str:
    """Create ADK-compatible llm_response JSON"""
    return json.dumps({
        "candidates": [{"content": {"role": "model", "parts": [{"text": output}]}}]
    })


class LangGraphRunner(BaseRunner):
    """LangGraph 框架运行时"""
    
    def load_agent(self) -> None:
        """加载 LangGraph 编译后的图"""
        package_path = Path(self.detection_result.package_path)
        project_path = Path(self.project_dir).resolve()
        
        # 添加项目目录到 Python 路径
        if str(project_path) not in sys.path:
            sys.path.insert(0, str(project_path))
        
        # 确定模块名：从 entry_point 获取 (e.g., "agent.py" -> "agent")
        entry_point = self.detection_result.entry_point
        if entry_point.endswith('.py'):
            module_name = entry_point[:-3]  # 移除 .py 后缀
        else:
            module_name = entry_point
        
        # 如果 entry_point 包含路径 (e.g., "subdir/agent.py")，转换为模块路径
        module_name = module_name.replace('/', '.').replace('\\', '.')
        
        try:
            module = __import__(module_name, fromlist=[self.detection_result.agent_variable])
            self._agent = getattr(module, self.detection_result.agent_variable)
        except ImportError as e:
            raise ImportError(f"无法导入模块 {module_name}: {e}")
        except AttributeError:
            raise AttributeError(f"模块 {module_name} 中未找到 {self.detection_result.agent_variable}")
        
        if not hasattr(self._agent, 'invoke'):
            raise TypeError("加载的对象不是有效的 LangGraph CompiledGraph")
    
    async def invoke(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """调用 LangGraph 图 (非流式)"""
        user_input = input_data.get("input", "")
        history = input_data.get("history", [])
        invocation_id = str(uuid.uuid4()).replace("-", "")
        
        with tracer.start_as_current_span("langgraph.invoke") as root_span:
            # Set ADK-compatible attributes on root span
            root_span.set_attribute("gcp.vertex.agent.invocation_id", invocation_id)
            root_span.set_attribute("user.input", user_input[:200])
            root_span.set_attribute("gcp.vertex.agent.llm_request", _make_llm_request_json(user_input))
            
            # Build messages from history + current input
            from langchain_core.messages import AIMessage
            messages = []
            for msg in history:
                if msg.get("role") == "user":
                    messages.append(HumanMessage(content=msg.get("content", "")))
                elif msg.get("role") == "model":
                    messages.append(AIMessage(content=msg.get("content", "")))
            messages.append(HumanMessage(content=user_input))
            
            initial_state = {
                "messages": messages
            }
            
            # Create child span for LLM call
            with tracer.start_as_current_span("call_llm") as llm_span:
                llm_span.set_attribute("gcp.vertex.agent.invocation_id", invocation_id)
                llm_span.set_attribute("gcp.vertex.agent.llm_request", _make_llm_request_json(user_input))
                
                # 获取 Langfuse 回调（用于图可视化）
                langfuse_cb = _get_langfuse_callback()
                config = {"callbacks": [langfuse_cb]} if langfuse_cb else None
                
                if hasattr(self._agent, 'ainvoke'):
                    result = await self._agent.ainvoke(initial_state, config=config)
                else:
                    result = self._agent.invoke(initial_state, config=config)
                
                output = self._extract_output(result)
                
                llm_span.set_attribute("gcp.vertex.agent.llm_response", _make_llm_response_json(output))
            
            # Set final output on root span
            root_span.set_attribute("agent.output", output[:500] if output else "")
            root_span.set_attribute("gcp.vertex.agent.llm_response", _make_llm_response_json(output))
            
            return {"output": output}
    
    def _extract_output(self, result: Any) -> str:
        """从结果中提取输出文本"""
        if isinstance(result, dict) and "messages" in result:
            messages = result["messages"]
            if messages:
                last_message = messages[-1]
                if isinstance(last_message, dict):
                    return last_message.get("content", str(last_message))
                elif hasattr(last_message, 'content'):
                    return last_message.content
                else:
                    return str(last_message)
        return str(result) if result else ""
    
    async def stream(self, input_data: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        """流式调用 LangGraph 图 (Token 级别)"""
        user_input = input_data.get("input", "")
        history = input_data.get("history", [])
        invocation_id = str(uuid.uuid4()).replace("-", "")
        
        # Build messages from history + current input
        from langchain_core.messages import AIMessage
        messages = []
        for msg in history:
            if msg.get("role") == "user":
                messages.append(HumanMessage(content=msg.get("content", "")))
            elif msg.get("role") == "model":
                messages.append(AIMessage(content=msg.get("content", "")))
        messages.append(HumanMessage(content=user_input))
        
        initial_state = {
            "messages": messages
        }
        
        # Start root span for entire streaming operation
        with tracer.start_as_current_span("langgraph.stream") as root_span:
            root_span.set_attribute("gcp.vertex.agent.invocation_id", invocation_id)
            root_span.set_attribute("user.input", user_input[:200])
            root_span.set_attribute("gcp.vertex.agent.llm_request", _make_llm_request_json(user_input))
            
            accumulated_text = ""
            tool_calls = []
            
            # Create child span for LLM streaming
            with tracer.start_as_current_span("call_llm") as llm_span:
                llm_span.set_attribute("gcp.vertex.agent.invocation_id", invocation_id)
                llm_span.set_attribute("gcp.vertex.agent.llm_request", _make_llm_request_json(user_input))
                
                # Try astream_events for token-level streaming
                # 获取 Langfuse 回调（用于图可视化）
                langfuse_cb = _get_langfuse_callback()
                config = {"callbacks": [langfuse_cb]} if langfuse_cb else None
                
                if hasattr(self._agent, 'astream_events'):
                    try:
                        async for event in self._agent.astream_events(initial_state, version="v2", config=config):
                            event_kind = event.get("event", "")
                            
                            if event_kind == "on_chat_model_stream":
                                chunk = event.get("data", {}).get("chunk")
                                if chunk and hasattr(chunk, "content") and chunk.content:
                                    content = chunk.content
                                    if isinstance(content, str):
                                        accumulated_text += content
                                        yield {"delta": content, "type": "text", "node": "llm"}
                            
                            elif event_kind == "on_tool_start":
                                tool_name = event.get("name", "unknown")
                                tool_input = event.get("data", {}).get("input", {})
                                tool_calls.append({"name": tool_name, "input": tool_input})
                                
                                # Create tool span (completed immediately for now)
                                with tracer.start_as_current_span(f"tool.{tool_name}") as tool_span:
                                    tool_span.set_attribute("gcp.vertex.agent.invocation_id", invocation_id)
                                    tool_span.set_attribute("tool.name", tool_name)
                                    tool_span.set_attribute("tool.input", str(tool_input)[:500])
                                
                                yield {
                                    "type": "tool_call",
                                    "tool_name": tool_name,
                                    "tool_args": tool_input,
                                    "node": "tool"
                                }
                            
                            elif event_kind == "on_tool_end":
                                tool_output = event.get("data", {}).get("output", "")
                                yield {"delta": f"\n[Tool Result: {tool_output}]\n", "type": "text", "node": "tool"}
                        
                        # Set LLM response after streaming completes
                        llm_span.set_attribute("gcp.vertex.agent.llm_response", _make_llm_response_json(accumulated_text))
                        
                    except Exception as e:
                        root_span.set_attribute("stream.error", str(e))
                        # Fallback to astream
                        pass
                
                # Fallback: node-level streaming with astream
                if not accumulated_text and hasattr(self._agent, 'astream'):
                    async for event in self._agent.astream(initial_state):
                        if isinstance(event, dict):
                            for node_name, node_output in event.items():
                                if isinstance(node_output, dict) and "messages" in node_output:
                                    for msg in node_output["messages"]:
                                        content = ""
                                        if hasattr(msg, 'content'):
                                            content = msg.content
                                        elif isinstance(msg, dict) and "content" in msg:
                                            content = msg["content"]
                                        
                                        if content:
                                            accumulated_text += content
                                            yield {"delta": content, "type": "text", "node": node_name}
                    
                    llm_span.set_attribute("gcp.vertex.agent.llm_response", _make_llm_response_json(accumulated_text))
                
                # Final fallback: synchronous invoke
                if not accumulated_text:
                    result = await self.invoke(input_data)
                    accumulated_text = result.get("output", "")
                    yield {"output": accumulated_text, "type": "final"}
            
            # Set final output on root span  
            root_span.set_attribute("agent.output", accumulated_text[:500])
            root_span.set_attribute("gcp.vertex.agent.llm_response", _make_llm_response_json(accumulated_text))

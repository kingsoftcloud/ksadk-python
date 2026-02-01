"""
ADKRunner - Google ADK 框架运行时

参考 adk-python 原生实现，缓存 Runner 和 SessionService
"""

import sys
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional
from ksadk.runners.base_runner import BaseRunner
from opentelemetry import trace

tracer = trace.get_tracer(__name__)


class ADKRunner(BaseRunner):
    """ADK 框架运行时"""

    def __init__(self, detection_result: Any, project_dir: str):
        super().__init__(detection_result, project_dir)
        self._runner = None
        self._session_service = None
        # Map external session_ids (e.g. from run_interactive or web) to ADK internal session IDs
        self._session_map: Dict[str, str] = {}
        # Fallback default session
        self._default_session_id: Optional[str] = None

    def _apply_json_patch(self):
        """Monkey patch google.adk.models.lite_llm to handle invalid JSON safely"""
        try:
            import json
            import google.adk.models.lite_llm as adk_lite_llm

            # Create a proxy for the json module
            class RobustJson:
                def __getattr__(self, name):
                    return getattr(json, name)

                def loads(self, s, **kwargs):
                    result = {}
                    try:
                        result = json.loads(s, **kwargs)
                    except json.JSONDecodeError:
                        # Try json_repair if available
                        try:
                            import json_repair

                            result = json_repair.loads(s)
                        except ImportError:
                            # Fallback: return empty dict to prevent crash
                            print(
                                f"\n⚠️ [KSADK] Warning: Captured invalid JSON from LLM: {s[:50]}..."
                            )
                            result = {}

                    # Ensure result is a dict (Google GenAI FunctionCall requires dict args)
                    if not isinstance(result, dict):
                        return {}
                    return result

            # Replace the 'json' module reference INSIDE lite_llm module
            # This is safer than patching json.loads globally
            adk_lite_llm.json = RobustJson()

        except ImportError:
            pass  # ADK not installed
        except Exception:
            pass

    def load_agent(self) -> None:
        """加载 ADK Agent"""
        import warnings

        warnings.filterwarnings("ignore", category=UserWarning, module="pydantic.main")

        self._apply_json_patch()

        # 添加项目目录到 Python 路径
        project_path = Path(self.project_dir).resolve()
        if str(project_path) not in sys.path:
            sys.path.insert(0, str(project_path))

        # 确定模块名：从 entry_point 获取 (e.g., "smart_assistant_adk/agent.py" -> "smart_assistant_adk.agent")
        entry_point = self.detection_result.entry_point
        if entry_point.endswith(".py"):
            module_name = entry_point[:-3]  # 移除 .py 后缀
        else:
            module_name = entry_point

        # 转换路径为模块路径 (e.g., "subdir/agent" -> "subdir.agent")
        module_name = module_name.replace("/", ".").replace("\\", ".")

        try:
            module = __import__(module_name, fromlist=[self.detection_result.agent_variable])
            self._agent = getattr(module, self.detection_result.agent_variable)

            # Inject safety instruction for DeepSeek/LLMs to prevent empty tool names
            if hasattr(self._agent, "instruction"):
                safety_prompt = "\nIMPORTANT: Do NOT output tool calls with empty names."
                if self._agent.instruction:
                    self._agent.instruction += safety_prompt
                else:
                    self._agent.instruction = safety_prompt

        except ImportError as e:
            raise ImportError(f"无法导入模块 {module_name}: {e}")
        except AttributeError:
            raise AttributeError(
                f"模块 {module_name} 中未找到 {self.detection_result.agent_variable}"
            )

        # 验证是否为 ADK Agent
        if not hasattr(self._agent, "name"):
            raise TypeError(f"加载的对象不是有效的 ADK Agent")

        # 初始化 Runner 和 SessionService (只做一次)
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService

        self._session_service = InMemorySessionService()
        self._runner = Runner(
            agent=self._agent, session_service=self._session_service, app_name=self._agent.name
        )

    def _prepare_trace_metadata(self, session_id: str):
        """准备 Trace 元数据 (Tags, UserID, etc.)"""
        from ksadk.tracing.span_utils import prepare_trace_metadata
        return prepare_trace_metadata(
            detection_result=getattr(self, "detection_result", None)
        )

    async def _ensure_session(self, external_session_id: str = None) -> str:
        """Get or create ADK session ID based on external ID"""
        # Case 1: External ID provided
        if external_session_id:
            if external_session_id in self._session_map:
                return self._session_map[external_session_id]

            # Create new ADK session and map it
            session = await self._session_service.create_session(
                app_name=self._agent.name, user_id="ksadk_user"
            )
            self._session_map[external_session_id] = session.id
            return session.id

        # Case 2: No external ID (use default singleton)
        if self._default_session_id is None:
            session = await self._session_service.create_session(
                app_name=self._agent.name, user_id="ksadk_user"
            )
            self._default_session_id = session.id
        return self._default_session_id

    async def invoke(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """调用 ADK Agent"""
        from google.genai import types

        user_input = input_data.get("input", "")
        invocation_id = str(uuid.uuid4()).replace("-", "")

        # 1. 准备 Metadata (提前以此获取 Agent Name)
        _, _, _, agent_name = self._prepare_trace_metadata(None)
        trace_name = agent_name or "adk.invoke"

        with tracer.start_as_current_span(trace_name) as span:
            # Set input.value for Langfuse top-level input display
            span.set_attribute("input.value", user_input)
            span.set_attribute("user.input", user_input[:200])

            # Use external session ID if provided
            req_session_id = input_data.get("session_id")
            session_id = await self._ensure_session(req_session_id)

            # 准备 Metadata 并设置 Span Attributes
            # Langfuse Exporter 会读取这些 span attributes
            agent_user_id, tags, _, _ = self._prepare_trace_metadata(session_id)

            span.set_attribute("langfuse.session_id", session_id)
            if agent_user_id:
                span.set_attribute("langfuse.user_id", agent_user_id)
            if tags:
                span.set_attribute("langfuse.tags", ",".join(tags))

            # 创建 Content 对象
            new_message = types.Content(role="user", parts=[types.Part(text=user_input)])

            final_response = ""

            events_list = []
            async for event in self._runner.run_async(
                session_id=session_id, user_id="ksadk_user", new_message=new_message
            ):
                events_list.append(event)
                if hasattr(event, "content") and event.content:
                    if hasattr(event.content, "parts"):
                        for part in event.content.parts:
                            # 过滤掉思考内容 (thought=True)，只保留最终答案
                            is_thought = getattr(part, "thought", False)
                            if hasattr(part, "text") and part.text and not is_thought:
                                final_response = part.text

            # Set output.value for Langfuse top-level output display
            span.set_attribute("output.value", final_response[:5000] if final_response else "")
            span.set_attribute("agent.output", final_response[:500] if final_response else "")
            return {"output": final_response, "events": events_list}

    async def stream(self, input_data: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        """流式调用 ADK Agent

        使用 StreamingMode.SSE 启用真正的流式 token 输出
        """
        from google.genai import types
        from google.adk.agents.run_config import RunConfig, StreamingMode

        user_input = input_data.get("input", "")
        invocation_id = str(uuid.uuid4()).replace("-", "")

        # 1. 准备 Metadata (提前以此获取 Agent Name)
        _, _, _, agent_name = self._prepare_trace_metadata(None)
        trace_name = agent_name or "adk.stream"

        with tracer.start_as_current_span(trace_name) as span:
            # Set input.value for Langfuse top-level input display
            span.set_attribute("input.value", user_input)
            span.set_attribute("user.input", user_input[:200])

            # Use external session ID if provided
            req_session_id = input_data.get("session_id")
            session_id = await self._ensure_session(req_session_id)

            # 准备 Metadata 并设置 Span Attributes
            agent_user_id, tags, _, _ = self._prepare_trace_metadata(session_id)

            span.set_attribute("langfuse.session_id", session_id)
            if agent_user_id:
                span.set_attribute("langfuse.user_id", agent_user_id)
            if tags:
                span.set_attribute("langfuse.tags", ",".join(tags))

            new_message = types.Content(role="user", parts=[types.Part(text=user_input)])

            accumulated_text = ""

            # 使用 StreamingMode.SSE 启用真正的流式输出
            run_config = RunConfig(streaming_mode=StreamingMode.SSE)

            async for event in self._runner.run_async(
                session_id=session_id,
                user_id="ksadk_user",
                new_message=new_message,
                run_config=run_config,
            ):
                # Only yield text delta if event is partial to avoid duplication of final summary
                if hasattr(event, "content") and event.content and getattr(event, "partial", False):
                    if hasattr(event.content, "parts"):
                        for part in event.content.parts:
                            if hasattr(part, "text") and part.text:
                                is_thought = getattr(part, "thought", False)
                                accumulated_text += part.text
                                # 标记思考内容，前端可以选择是否展示
                                yield {
                                    "delta": part.text,
                                    "type": "thinking" if is_thought else "text",
                                }

                # 处理工具调用事件
                if hasattr(event, "actions") and event.actions:
                    tool_calls = getattr(event.actions, "tool_calls", None)
                    if tool_calls:
                        for tool_call in tool_calls:
                            yield {
                                "type": "tool_call",
                                "tool_name": getattr(tool_call, "name", "unknown"),
                                "tool_args": getattr(tool_call, "input", {}),
                            }

            # Set output.value for Langfuse top-level output display
            span.set_attribute("output.value", accumulated_text[:5000] if accumulated_text else "")
            span.set_attribute("agent.output", accumulated_text[:500])

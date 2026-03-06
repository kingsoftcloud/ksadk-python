"""
ADKRunner - Google ADK 框架运行时

参考 adk-python 原生实现，缓存 Runner 和 SessionService。
支持通过环境变量配置记忆体 (ShortTermMemory / LongTermMemory)。
"""

import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional
from ksadk.runners.base_runner import BaseRunner
from opentelemetry import trace

logger = logging.getLogger(__name__)

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
        # Memory integration
        self._short_term_memory = None
        self._long_term_memory = None
        # Knowledge base integration
        self._knowledge_base = None

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

    def _init_short_term_memory(self):
        """从环境变量初始化短期记忆

        环境变量:
            KSADK_STM_BACKEND: local / sqlite / database
            KSADK_STM_DB_URL: 数据库连接 URL
        """
        backend = os.environ.get("KSADK_STM_BACKEND", "")
        if not backend:
            return None

        try:
            from ksadk.memory.adk import ShortTermMemory
            stm = ShortTermMemory(
                backend=backend,
                db_url=os.environ.get("KSADK_STM_DB_URL", ""),
                local_database_path=os.environ.get(
                    "KSADK_STM_DB_PATH", "/tmp/ksadk_local_database.db"
                ),
            )
            logger.info(f"ShortTermMemory initialized: backend={backend}")
            return stm
        except Exception as e:
            logger.warning(f"Failed to init ShortTermMemory: {e}. Using default.")
            return None

    def _init_long_term_memory(self):
        """从环境变量初始化长期记忆

        环境变量:
            KSADK_LTM_BACKEND: local / http / sdk
            KSADK_LTM_HTTP_URL: HTTP 记忆服务地址
            KSADK_LTM_HTTP_TOKEN: 认证 Token
            KSADK_LTM_ACCESS_KEY: SDK AK (fallback to KSYUN_ACCESS_KEY)
            KSADK_LTM_SECRET_KEY: SDK SK (fallback to KSYUN_SECRET_KEY)
            KSADK_LTM_TOP_K: 检索数量
        """
        backend = os.environ.get("KSADK_LTM_BACKEND", "")
        if not backend:
            return None

        try:
            from ksadk.memory.adk import LongTermMemory

            backend_config = {}
            if backend == "http":
                base_url = os.environ.get("KSADK_LTM_HTTP_URL", "")
                token = os.environ.get("KSADK_LTM_HTTP_TOKEN", "")
                if not base_url:
                    logger.warning(
                        "KSADK_LTM_BACKEND=http but KSADK_LTM_HTTP_URL is empty."
                    )
                backend_config = {"base_url": base_url, "token": token}
            elif backend == "sdk":
                backend_config = {
                    "access_key": (
                        os.environ.get("KSADK_LTM_ACCESS_KEY")
                        or os.environ.get("KSYUN_ACCESS_KEY", "")
                    ),
                    "secret_key": (
                        os.environ.get("KSADK_LTM_SECRET_KEY")
                        or os.environ.get("KSYUN_SECRET_KEY", "")
                    ),
                    "region": os.environ.get("KSADK_LTM_REGION", "cn-north-vip1"),
                    "endpoint": os.environ.get("KSADK_LTM_ENDPOINT", "aicp.api.ksyun.com"),
                    "scheme": os.environ.get("KSADK_LTM_SCHEME", "https"),
                    "namespace": os.environ.get("KSADK_LTM_NAMESPACE", ""),
                    "agent_id": os.environ.get("KSADK_LTM_AGENT_ID", ""),
                    "scene_id": os.environ.get("KSADK_LTM_SCENE_ID", ""),
                }

            agent_name = self._agent.name if self._agent else "default"
            ltm = LongTermMemory(
                backend=backend,
                backend_config=backend_config,
                top_k=int(os.environ.get("KSADK_LTM_TOP_K", "5")),
                index=os.environ.get("KSADK_LTM_INDEX", ""),
                app_name=agent_name,
            )
            logger.info(
                f"LongTermMemory initialized: backend={backend}, "
                f"app_name={agent_name}"
            )
            return ltm
        except Exception as e:
            logger.warning(f"Failed to init LongTermMemory: {e}.")
            return None

    def _init_knowledge_base(self):
        """从环境变量初始化知识库

        环境变量:
            KSADK_KB_DATASET_ID: 知识库 ID (必填，存在即启用)
            KSADK_KB_ACCESS_KEY: AK (可选)
            KSADK_KB_SECRET_KEY: SK (可选)
            KSADK_KB_REGION: 区域 (默认 cn-north-vip1)
            KSADK_KB_TOP_K: 返回结果数 (默认 5)
        """
        try:
            from ksadk.knowledge_base.client import KnowledgeBaseClient

            if not KnowledgeBaseClient.is_configured():
                return None

            kb = KnowledgeBaseClient.from_env()
            logger.info(
                f"KnowledgeBase initialized: dataset_id={kb.dataset_id}, "
                f"region={kb.region}"
            )
            return kb
        except ImportError:
            logger.warning(
                "kingsoftcloud-sdk-python not installed, "
                "knowledge base disabled. "
                "Install with: pip install kingsoftcloud-sdk-python"
            )
            return None
        except Exception as e:
            logger.warning(f"Failed to init KnowledgeBase: {e}.")
            return None

    def _inject_search_knowledge_tool(self):
        """自动注入 search_knowledge_base 工具到 Agent"""
        try:
            from ksadk.knowledge_base.adk_tool import search_knowledge_base

            if hasattr(self._agent, "tools"):
                tool_names = []
                for t in self._agent.tools:
                    name = getattr(t, "name", None) or getattr(t, "__name__", "")
                    tool_names.append(name)

                if "search_knowledge_base" not in tool_names:
                    self._agent.tools.append(search_knowledge_base)
                    logger.info(
                        "Injected 'search_knowledge_base' tool into agent "
                        f"(total tools: {len(self._agent.tools)})"
                    )
                else:
                    logger.debug("Agent already has 'search_knowledge_base' tool")
            else:
                logger.warning(
                    "Agent has no 'tools' attribute, "
                    "cannot inject search_knowledge_base"
                )
        except ImportError as e:
            logger.warning(f"Failed to import knowledge base tool: {e}")
        except Exception as e:
            logger.warning(f"Failed to inject search_knowledge_base tool: {e}")

    def _inject_load_memory_tool(self):
        """自动注入 load_memory 工具到 Agent"""
        try:
            from google.adk.tools import load_memory

            if hasattr(self._agent, "tools"):
                # 检查是否已有 load_memory
                tool_names = []
                for t in self._agent.tools:
                    name = getattr(t, "name", None) or getattr(t, "__name__", "")
                    tool_names.append(name)

                if "load_memory" not in tool_names:
                    self._agent.tools.append(load_memory)
                    logger.info(
                        "Injected 'load_memory' tool into agent "
                        f"(total tools: {len(self._agent.tools)})"
                    )
                else:
                    logger.debug("Agent already has 'load_memory' tool")
            else:
                logger.warning(
                    "Agent has no 'tools' attribute, cannot inject load_memory"
                )
        except ImportError:
            logger.warning(
                "google.adk.tools.load_memory not available. "
                "Ensure google-adk >= 1.0.0 is installed."
            )
        except Exception as e:
            logger.warning(f"Failed to inject load_memory tool: {e}")

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

        # 初始化记忆体 (从环境变量读取配置)
        self._short_term_memory = self._init_short_term_memory()
        self._long_term_memory = self._init_long_term_memory()

        # 初始化知识库 (从环境变量读取配置)
        self._knowledge_base = self._init_knowledge_base()
        if self._knowledge_base:
            self._inject_search_knowledge_tool()

        # 初始化 SessionService
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService

        if self._short_term_memory:
            self._session_service = self._short_term_memory.session_service
            logger.info("ADKRunner: using ShortTermMemory session service")
        else:
            self._session_service = InMemorySessionService()

        # 如果配置了长期记忆，自动注入 load_memory 工具到 agent
        if self._long_term_memory:
            self._inject_load_memory_tool()

        # 初始化 Runner (传入 memory_service)
        runner_kwargs = dict(
            agent=self._agent,
            session_service=self._session_service,
            app_name=self._agent.name,
        )
        if self._long_term_memory:
            runner_kwargs["memory_service"] = self._long_term_memory
            logger.info("ADKRunner: LongTermMemory injected as memory_service")

        self._runner = Runner(**runner_kwargs)

    def _prepare_trace_metadata(self, session_id: str):
        """准备 Trace 元数据 (Tags, UserID, etc.)"""
        from ksadk.tracing.span_utils import prepare_trace_metadata
        return prepare_trace_metadata(
            detection_result=getattr(self, "detection_result", None)
        )

    async def _ensure_session(self, external_session_id: str = None) -> str:
        """Get or create ADK session ID based on external ID

        When ShortTermMemory is configured, uses its create_session method
        which supports session retrieval (if session_id already exists).
        """
        # Case 1: External ID provided
        if external_session_id:
            if external_session_id in self._session_map:
                return self._session_map[external_session_id]

            # Create new ADK session and map it
            if self._short_term_memory:
                session = await self._short_term_memory.create_session(
                    app_name=self._agent.name,
                    user_id="ksadk_user",
                    session_id=external_session_id,
                )
            else:
                session = await self._session_service.create_session(
                    app_name=self._agent.name, user_id="ksadk_user"
                )
            self._session_map[external_session_id] = session.id
            return session.id

        # Case 2: No external ID (use default singleton)
        if self._default_session_id is None:
            if self._short_term_memory:
                session = await self._short_term_memory.create_session(
                    app_name=self._agent.name,
                    user_id="ksadk_user",
                )
            else:
                session = await self._session_service.create_session(
                    app_name=self._agent.name, user_id="ksadk_user"
                )
            self._default_session_id = session.id
        return self._default_session_id

    async def save_session_to_long_term_memory(
        self, session_id: str, user_id: str = "ksadk_user"
    ) -> bool:
        """将指定 session 保存到长期记忆

        Args:
            session_id: ADK 内部 session ID
            user_id: 用户 ID

        Returns:
            是否保存成功
        """
        if not self._long_term_memory:
            logger.warning("LongTermMemory not configured, cannot save session.")
            return False

        try:
            session = await self._session_service.get_session(
                app_name=self._agent.name,
                user_id=user_id,
                session_id=session_id,
            )
            if not session:
                logger.error(f"Session {session_id} not found, cannot save.")
                return False

            await self._long_term_memory.add_session_to_memory(session)
            logger.info(f"Session {session_id} saved to long-term memory.")
            return True
        except Exception as e:
            logger.error(f"Error saving session to long-term memory: {e}")
            return False

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

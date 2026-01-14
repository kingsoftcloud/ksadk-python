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
        self._session = None
    
    def load_agent(self) -> None:
        """加载 ADK Agent"""
        # 添加项目目录到 Python 路径
        project_path = Path(self.project_dir).resolve()
        if str(project_path) not in sys.path:
            sys.path.insert(0, str(project_path))
        
        # 确定模块名：从 entry_point 获取 (e.g., "smart_assistant_adk/agent.py" -> "smart_assistant_adk.agent")
        entry_point = self.detection_result.entry_point
        if entry_point.endswith('.py'):
            module_name = entry_point[:-3]  # 移除 .py 后缀
        else:
            module_name = entry_point
        
        # 转换路径为模块路径 (e.g., "subdir/agent" -> "subdir.agent")
        module_name = module_name.replace('/', '.').replace('\\', '.')
        
        try:
            module = __import__(module_name, fromlist=[self.detection_result.agent_variable])
            self._agent = getattr(module, self.detection_result.agent_variable)
        except ImportError as e:
            raise ImportError(f"无法导入模块 {module_name}: {e}")
        except AttributeError:
            raise AttributeError(f"模块 {module_name} 中未找到 {self.detection_result.agent_variable}")
        
        # 验证是否为 ADK Agent
        if not hasattr(self._agent, 'name'):
            raise TypeError(f"加载的对象不是有效的 ADK Agent")
        
        # 初始化 Runner 和 SessionService (只做一次)
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        
        self._session_service = InMemorySessionService()
        self._runner = Runner(
            agent=self._agent,
            session_service=self._session_service,
            app_name=self._agent.name
        )
    
    def _prepare_trace_metadata(self, session_id: str):
        """准备 Trace 元数据 (Tags, UserID, etc.)"""
        user_id = None
        tags = []
        version = None
        agent_name = None
        
        try:
            from ksadk.configs import settings
            agent_config = settings.agent
            
            user_id = agent_config.user_id
            version = agent_config.version
            tags = list(agent_config.tags or [])
            
            # Add Environment
            if agent_config.environment and agent_config.environment not in tags:
                tags.append(agent_config.environment)
            
            # Add Region (Kingsoft Cloud)
            if settings.cloud.region and settings.cloud.region not in tags:
                tags.append(settings.cloud.region)
                
            # Add Model Name
            if settings.model.model_name and settings.model.model_name not in tags:
                tags.append(settings.model.model_name)
            
            # Add Agent Name (Configured -> Fallback)
            agent_name = agent_config.agent_name
            if not agent_name and hasattr(self, "detection_result"):
                 try:
                     # Fallback to package name
                     agent_name = Path(self.detection_result.package_path).name
                 except Exception:
                     pass
            
            if agent_name and agent_name not in tags:
                tags.append(agent_name)
                
            # Add Agent ID
            if agent_config.agent_id and agent_config.agent_id not in tags:
                tags.append(agent_config.agent_id)
                
            # Add Tenant ID (Account ID)
            if agent_config.tenant_id and agent_config.tenant_id not in tags:
                tags.append(agent_config.tenant_id)
                
        except ImportError:
            pass
        except Exception:
            pass
            
        return user_id, tags, version, agent_name
    
    async def _ensure_session(self) -> str:
        """确保有活跃的 session"""
        if self._session is None:
            self._session = await self._session_service.create_session(
                app_name=self._agent.name,
                user_id="ksadk_user"
            )
        return self._session.id
    
    async def invoke(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """调用 ADK Agent"""
        from google.genai import types
        
        user_input = input_data.get("input", "")
        invocation_id = str(uuid.uuid4()).replace("-", "")
        
        # 1. 准备 Metadata (提前以此获取 Agent Name)
        _, _, _, agent_name = self._prepare_trace_metadata(None)
        trace_name = agent_name or "adk.invoke"
        
        with tracer.start_as_current_span(trace_name) as span:
            span.set_attribute("user.input", user_input[:200])
            
            session_id = await self._ensure_session()
            
            # 准备 Metadata 并设置 Span Attributes
            # Langfuse Exporter 会读取这些 span attributes
            agent_user_id, tags, _, _ = self._prepare_trace_metadata(session_id)
            
            span.set_attribute("langfuse.session_id", session_id)
            if agent_user_id:
                span.set_attribute("langfuse.user_id", agent_user_id)
            if tags:
                span.set_attribute("langfuse.tags", ",".join(tags))
            
            # 创建 Content 对象
            new_message = types.Content(
                role="user",
                parts=[types.Part(text=user_input)]
            )
            
            final_response = ""
            
            events_list = []
            async for event in self._runner.run_async(
                session_id=session_id,
                user_id="ksadk_user",
                new_message=new_message
            ):
                events_list.append(event)
                if hasattr(event, 'content') and event.content:
                    if hasattr(event.content, 'parts'):
                        for part in event.content.parts:
                            if hasattr(part, 'text') and part.text:
                                final_response = part.text
            
            span.set_attribute("agent.output", final_response[:500] if final_response else "")
            return {"output": final_response, "events": events_list}
    
    async def stream(self, input_data: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        """流式调用 ADK Agent"""
        from google.genai import types
        
        user_input = input_data.get("input", "")
        invocation_id = str(uuid.uuid4()).replace("-", "")
        
        # 1. 准备 Metadata (提前以此获取 Agent Name)
        _, _, _, agent_name = self._prepare_trace_metadata(None)
        trace_name = agent_name or "adk.stream"
        
        with tracer.start_as_current_span(trace_name) as span:
            span.set_attribute("user.input", user_input[:200])
            
            session_id = await self._ensure_session()
            
            # 准备 Metadata 并设置 Span Attributes
            agent_user_id, tags, _, _ = self._prepare_trace_metadata(session_id)
            
            span.set_attribute("langfuse.session_id", session_id)
            if agent_user_id:
                span.set_attribute("langfuse.user_id", agent_user_id)
            if tags:
                span.set_attribute("langfuse.tags", ",".join(tags))
            
            new_message = types.Content(
                role="user",
                parts=[types.Part(text=user_input)]
            )
            
            accumulated_text = ""
            
            async for event in self._runner.run_async(
                session_id=session_id,
                user_id="ksadk_user",
                new_message=new_message
            ):
                if hasattr(event, 'content') and event.content:
                    if hasattr(event.content, 'parts'):
                        for part in event.content.parts:
                            if hasattr(part, 'text') and part.text:
                                accumulated_text += part.text
                                yield {"delta": part.text, "type": "text"}
                
                # 处理工具调用事件
                if hasattr(event, 'actions') and event.actions:
                    tool_calls = getattr(event.actions, 'tool_calls', None)
                    if tool_calls:
                        for tool_call in tool_calls:
                            yield {
                                "type": "tool_call",
                                "tool_name": getattr(tool_call, 'name', 'unknown'),
                                "tool_args": getattr(tool_call, 'input', {})
                            }
            
            span.set_attribute("agent.output", accumulated_text[:500])

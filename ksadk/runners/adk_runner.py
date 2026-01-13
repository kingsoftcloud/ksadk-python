"""
ADKRunner - Google ADK 框架运行时

参考 adk-python 原生实现，缓存 Runner 和 SessionService
"""

import sys
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional
from ksadk.runners.base_runner import BaseRunner


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
        
        session_id = await self._ensure_session()
        user_input = input_data.get("input", "")
        
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
        
        return {"output": final_response, "events": events_list}
    
    async def stream(self, input_data: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        """流式调用 ADK Agent"""
        from google.genai import types
        
        session_id = await self._ensure_session()
        user_input = input_data.get("input", "")
        
        new_message = types.Content(
            role="user",
            parts=[types.Part(text=user_input)]
        )
        
        async for event in self._runner.run_async(
            session_id=session_id,
            user_id="ksadk_user",
            new_message=new_message
        ):
            if hasattr(event, 'content') and event.content:
                if hasattr(event.content, 'parts'):
                    for part in event.content.parts:
                        if hasattr(part, 'text') and part.text:
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

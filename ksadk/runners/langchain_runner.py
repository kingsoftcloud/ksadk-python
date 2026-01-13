"""
LangChainRunner - LangChain 框架运行时
"""

import sys
from pathlib import Path
from typing import Any, AsyncIterator, Dict
from ksadk.runners.base_runner import BaseRunner


class LangChainRunner(BaseRunner):
    """LangChain 框架运行时"""
    
    def load_agent(self) -> None:
        """加载 LangChain Agent/Chain"""
        if self.project_dir not in sys.path:
            sys.path.insert(0, self.project_dir)
        
        package_path = Path(self.detection_result.package_path)
        package_name = package_path.name
        
        try:
            module = __import__(package_name, fromlist=[self.detection_result.agent_variable])
            self._agent = getattr(module, self.detection_result.agent_variable)
        except ImportError as e:
            raise ImportError(f"无法导入模块 {package_name}: {e}")
        except AttributeError:
            raise AttributeError(f"模块 {package_name} 中未找到 {self.detection_result.agent_variable}")
    
    async def invoke(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """调用 LangChain Agent/Chain"""
        user_input = input_data.get("input", "")
        
        # 支持多种调用方式
        if hasattr(self._agent, 'ainvoke'):
            # LCEL Chain 或 AgentExecutor
            result = await self._agent.ainvoke({"input": user_input})
            if isinstance(result, dict):
                output = result.get("output", result.get("text", str(result)))
            else:
                output = str(result)
        elif hasattr(self._agent, 'invoke'):
            result = self._agent.invoke({"input": user_input})
            if isinstance(result, dict):
                output = result.get("output", result.get("text", str(result)))
            else:
                output = str(result)
        elif callable(self._agent):
            result = self._agent(user_input)
            output = str(result)
        else:
            raise TypeError("Agent 不支持 invoke 调用")
        
        return {"output": output}
    
    async def stream(self, input_data: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        """流式调用 LangChain Agent/Chain"""
        user_input = input_data.get("input", "")
        
        # 尝试流式调用
        if hasattr(self._agent, 'astream'):
            async for chunk in self._agent.astream({"input": user_input}):
                if isinstance(chunk, dict):
                    if "output" in chunk:
                        yield {"delta": chunk["output"], "type": "text"}
                    elif "text" in chunk:
                        yield {"delta": chunk["text"], "type": "text"}
                else:
                    yield {"delta": str(chunk), "type": "text"}
        elif hasattr(self._agent, 'stream'):
            for chunk in self._agent.stream({"input": user_input}):
                if isinstance(chunk, dict):
                    if "output" in chunk:
                        yield {"delta": chunk["output"], "type": "text"}
                else:
                    yield {"delta": str(chunk), "type": "text"}
        else:
            # 回退到同步调用
            result = await self.invoke(input_data)
            yield {"output": result.get("output", ""), "type": "final"}

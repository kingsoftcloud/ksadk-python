"""
BaseRunner - 运行时基类

所有框架 Runner 的抽象基类，定义统一接口
"""

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Dict, Optional
import asyncio


class BaseRunner(ABC):
    """运行时基类"""
    
    def __init__(self, detection_result: Any, project_dir: str):
        self.detection_result = detection_result
        self.project_dir = project_dir
        self._agent = None
    
    @abstractmethod
    def load_agent(self) -> None:
        """加载 Agent"""
        pass
    
    @abstractmethod
    async def invoke(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """同步调用 Agent
        
        Args:
            input_data: 输入数据，通常包含 {"input": "用户消息"}
        
        Returns:
            输出数据，通常包含 {"output": "Agent 回复"}
        """
        pass
    
    @abstractmethod
    async def stream(self, input_data: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        """流式调用 Agent
        
        Args:
            input_data: 输入数据
        
        Yields:
            流式输出的数据块
        """
        pass
    
    async def run_interactive(self) -> None:
        """交互式运行
        
        在整个交互会话期间保持同一个 session_id 和对话历史，
        以便 Agent 可以记住上下文。
        """
        import uuid
        
        print("🤖 交互模式已启动，输入 'exit' 退出\n")
        
        # 创建一个持久的 session_id 和对话历史
        session_id = str(uuid.uuid4())[:8]
        history = []
        
        while True:
            try:
                user_input = input("👤 你: ").strip()
                
                if not user_input:
                    continue
                
                if user_input.lower() in ('exit', 'quit', '退出'):
                    print("\n👋 再见!")
                    break
                
                print("🤖 助手: ", end="", flush=True)
                
                # 构建输入，包含 session_id 和 history
                input_data = {
                    "input": user_input,
                    "session_id": session_id,
                    "history": history
                }
                
                response_text = ""
                async for chunk in self.stream(input_data):
                    if "output" in chunk:
                        text = chunk["output"]
                        print(text, end="", flush=True)
                        response_text += text
                    elif "delta" in chunk:
                        text = chunk["delta"]
                        print(text, end="", flush=True)
                        response_text += text
                
                if not response_text:
                    # 如果没有流式输出，使用同步调用
                    result = await self.invoke(input_data)
                    response_text = result.get("output", "(无响应)")
                    print(response_text)
                else:
                    print()  # 换行
                
                # 更新对话历史
                history.append({"role": "user", "content": user_input})
                history.append({"role": "model", "content": response_text})
                
                print()
                
            except KeyboardInterrupt:
                print("\n\n👋 再见!")
                break
            except Exception as e:
                print(f"\n❌ 错误: {e}\n")
    
    def run_server(self, port: int = 8000) -> None:
        """启动 HTTP Server"""
        from ksadk.server import app, set_runner
        import uvicorn
        
        set_runner(self)
        uvicorn.run(app, host="0.0.0.0", port=port)

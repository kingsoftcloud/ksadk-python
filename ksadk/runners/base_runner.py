"""
BaseRunner - 运行时基类

所有框架 Runner 的抽象基类，定义统一接口
"""

import os
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Dict, Optional


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

    @staticmethod
    def normalize_requested_model(model: Optional[str]) -> Optional[str]:
        if not isinstance(model, str):
            return None
        normalized = model.strip()
        return normalized or None

    @classmethod
    def sync_process_model_env(cls, model: Optional[str]) -> Optional[str]:
        normalized = cls.normalize_requested_model(model)
        if normalized is None:
            return None
        os.environ["OPENAI_MODEL_NAME"] = normalized
        os.environ["MODEL_NAME"] = normalized
        return normalized

    def prepare_for_request(self, model: Optional[str]) -> None:
        """在请求进入实际 runner 前同步模型或做必要刷新。"""
        self.sync_process_model_env(model)


    def run_server(self, port: int = 8000) -> None:
        """启动 HTTP Server"""
        from ksadk.server import app, set_runner
        import uvicorn

        set_runner(self)
        uvicorn.run(app, host="0.0.0.0", port=port)

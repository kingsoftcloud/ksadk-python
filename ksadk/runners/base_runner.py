"""
BaseRunner - 运行时基类

所有框架 Runner 的抽象基类，定义统一接口
"""

import inspect
import os
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Dict, Optional

from ksadk.sessions.continuity import RunnerSessionAdapter, TranscriptReplayAdapter


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

    def request_cancel(self, invocation_id: str) -> str:
        """请求取消指定调用。

        返回值用于 API 层区分真实取消、未命中和不支持取消的边界。
        子类可返回 ``accepted``、``not_found`` 或 ``unsupported``。
        """
        return "unsupported"

    async def close(self) -> None:
        """释放 runner 持有的运行期资源。"""
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()
        return False

    def get_session_adapter(self) -> RunnerSessionAdapter:
        return TranscriptReplayAdapter()

    @staticmethod
    def _callable_accepts_keyword(callable_obj: Any, keyword: str) -> bool:
        try:
            signature = inspect.signature(callable_obj)
        except (TypeError, ValueError):
            return False

        for parameter in signature.parameters.values():
            if parameter.kind == inspect.Parameter.VAR_KEYWORD:
                return True
        return keyword in signature.parameters

    @classmethod
    def _build_optional_call_kwargs(
        cls,
        callable_obj: Any,
        *,
        config: Optional[dict[str, Any]] = None,
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if config is not None and cls._callable_accepts_keyword(callable_obj, "config"):
            kwargs["config"] = config
        if context is not None and cls._callable_accepts_keyword(callable_obj, "context"):
            kwargs["context"] = context
        return kwargs

    @staticmethod
    def build_native_context(platform_context: Any) -> dict[str, Any] | None:
        if not isinstance(platform_context, dict):
            return None
        native_context = {
            key: platform_context[key]
            for key in ("agent_id", "user_id", "session_id")
            if platform_context.get(key) is not None
        }
        return native_context or None


    def run_server(self, port: int = 8000) -> None:
        """启动 HTTP Server"""
        from ksadk.server import app, set_runner
        import uvicorn

        set_runner(self)
        uvicorn.run(app, host="0.0.0.0", port=port)

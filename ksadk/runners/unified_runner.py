"""
UnifiedRunner - 统一运行器入口
"""

from typing import Any, Dict, AsyncIterator
from ksadk.detection import DetectionResult, FrameworkType
from ksadk.runners.base_runner import BaseRunner


class UnifiedRunner:
    """统一运行器工厂"""

    @staticmethod
    def create(detection_result: DetectionResult, project_dir: str) -> BaseRunner:
        """根据检测结果创建对应的 Runner 实例

        Args:
            detection_result: 框架检测结果
            project_dir: 项目根目录

        Returns:
            Specific Runner instance (ADKRunner, LangChainRunner, etc.)
        """
        # Apply langchain patch for reasoning_content support
        try:
            from ksadk.runners.patch_langchain import apply_patch

            apply_patch()
        except ImportError:
            pass

        if detection_result.type == FrameworkType.ADK:
            from ksadk.runners.adk_runner import ADKRunner

            return ADKRunner(detection_result, project_dir)

        elif detection_result.type == FrameworkType.LANGGRAPH:
            from ksadk.runners.langgraph_runner import LangGraphRunner

            return LangGraphRunner(detection_result, project_dir)

        elif detection_result.type == FrameworkType.LANGCHAIN:
            from ksadk.runners.langchain_runner import LangChainRunner

            return LangChainRunner(detection_result, project_dir)

        else:
            raise ValueError(f"不支持的框架类型: {detection_result.type}")

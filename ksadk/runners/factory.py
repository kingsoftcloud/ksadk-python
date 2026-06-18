"""
Runner Factory - 根据检测结果创建对应的 Runner
"""

import importlib
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from ksadk.detection import DetectionResult, FrameworkType
from ksadk.runners.base_runner import BaseRunner

if TYPE_CHECKING:
    pass


def create_runner(detection_result: DetectionResult, project_dir: str) -> BaseRunner:
    """根据检测结果创建对应的 Runner
    
    Args:
        detection_result: 框架检测结果
        project_dir: 项目目录
    
    Returns:
        对应框架的 Runner 实例
    """
    # Apply langchain patch for reasoning_content support
    try:
        from ksadk.runners.patch_langchain import apply_patch

        apply_patch()
    except ImportError:
        pass

    custom_runner_class = str(getattr(detection_result, "runner_class", "") or "").strip()
    if custom_runner_class:
        project_path = Path(project_dir).resolve()
        for candidate in (project_path, project_path / "src"):
            if candidate.is_dir() and str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
        module_name, separator, class_name = custom_runner_class.rpartition(".")
        if not separator or not module_name or not class_name:
            raise ValueError(
                "runner_class must use a fully-qualified class path, "
                "for example 'agent.CustomRunner'"
            )
        try:
            module = importlib.import_module(module_name)
            runner_class = getattr(module, class_name)
        except (ImportError, AttributeError) as exc:
            raise ValueError(f"无法加载自定义 Runner: {custom_runner_class}") from exc
        if not isinstance(runner_class, type) or not issubclass(runner_class, BaseRunner):
            raise TypeError(f"自定义 Runner 必须继承 BaseRunner: {custom_runner_class}")
        runner = runner_class(detection_result, project_dir)
        return runner

    if detection_result.type == FrameworkType.ADK:
        from ksadk.runners.adk_runner import ADKRunner
        return ADKRunner(detection_result, project_dir)
    
    elif detection_result.type == FrameworkType.LANGGRAPH:
        from ksadk.runners.langgraph_runner import LangGraphRunner
        return LangGraphRunner(detection_result, project_dir)
    
    elif detection_result.type == FrameworkType.LANGCHAIN:
        from ksadk.runners.langchain_runner import LangChainRunner
        return LangChainRunner(detection_result, project_dir)

    elif detection_result.type == FrameworkType.DEEPAGENTS:
        from ksadk.runners.deepagents_runner import DeepAgentsRunner
        return DeepAgentsRunner(detection_result, project_dir)
    
    else:
        raise ValueError(f"不支持的框架类型: {detection_result.type}")

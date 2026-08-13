"""
模块加载工具

提供通用的 Agent 模块加载逻辑
"""

import importlib
import sys
from pathlib import Path
from typing import Any


def load_agent_module(
    project_dir: str,
    entry_point: str,
    agent_variable: str,
    *,
    force_reload: bool = False,
) -> Any:
    """加载 Agent 模块

    Args:
        project_dir: 项目目录
        entry_point: 入口文件 (e.g., "agent.py")
        agent_variable: Agent 变量名 (e.g., "root_agent", "graph")

    Returns:
        加载的 Agent 对象

    Raises:
        ImportError: 模块导入失败
        AttributeError: 未找到 Agent 变量
    """
    project_path = Path(project_dir).resolve()

    # 添加项目目录到 Python 路径
    if str(project_path) not in sys.path:
        sys.path.insert(0, str(project_path))
    src_path = project_path / "src"
    if src_path.is_dir() and str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))

    # 确定模块名
    if entry_point.endswith(".py"):
        module_name = entry_point[:-3]
        entry_file = (project_path / entry_point).resolve()
    else:
        module_name = entry_point
        entry_file = None

    # 路径转换为模块路径
    module_name = module_name.replace("/", ".").replace("\\", ".")

    try:
        # Studio 会在同一 Python 进程内运行多个不可变 Bundle。它们通常都以
        # ``agent.py`` 为入口；仅靠 ``import_module("agent")`` 会复用上一个
        # Bundle 的 sys.modules 缓存，进而加载错误的 agentVariable。只复用与
        # 当前 entry 文件一致的缓存，其余情况让 import 从当前 project_path 重载。
        cached = sys.modules.get(module_name)
        cached_file = getattr(cached, "__file__", None)
        cached_matches_entry = bool(
            cached_file is not None
            and entry_file is not None
            and Path(str(cached_file)).resolve() == entry_file
        )
        if cached is not None and not cached_matches_entry:
            sys.modules.pop(module_name, None)
            importlib.invalidate_caches()
        if force_reload and module_name in sys.modules:
            module = importlib.reload(sys.modules[module_name])
        else:
            module = importlib.import_module(module_name)
        agent = getattr(module, agent_variable)
        return agent, module
    except ImportError as e:
        raise ImportError(f"无法导入模块 {module_name}: {e}")
    except AttributeError:
        raise AttributeError(f"模块 {module_name} 中未找到 {agent_variable}")

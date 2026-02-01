"""
模块加载工具

提供通用的 Agent 模块加载逻辑
"""

import sys
from pathlib import Path
from typing import Any


def load_agent_module(project_dir: str, entry_point: str, agent_variable: str) -> Any:
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
    
    # 确定模块名
    if entry_point.endswith(".py"):
        module_name = entry_point[:-3]
    else:
        module_name = entry_point
    
    # 路径转换为模块路径
    module_name = module_name.replace("/", ".").replace("\\", ".")
    
    try:
        module = __import__(module_name, fromlist=[agent_variable])
        agent = getattr(module, agent_variable)
        return agent, module
    except ImportError as e:
        raise ImportError(f"无法导入模块 {module_name}: {e}")
    except AttributeError:
        raise AttributeError(f"模块 {module_name} 中未找到 {agent_variable}")

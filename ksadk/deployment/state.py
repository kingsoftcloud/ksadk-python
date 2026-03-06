"""
部署状态管理

用于管理本地 .agentengine.state 文件，记录已部署的 Agent/MCP 信息。
"""

import yaml
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime


STATE_FILE_NAME = ".agentengine.state"


def get_state_file_path(project_dir: Path) -> Path:
    """获取状态文件路径"""
    return project_dir / STATE_FILE_NAME


def load_state(project_dir: Path) -> Dict[str, Any]:
    """加载状态文件"""
    state_file = get_state_file_path(project_dir)
    if not state_file.exists():
        return {}
        
    try:
        with open(state_file, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def save_state(project_dir: Path, data: Dict[str, Any]) -> None:
    """保存状态文件"""
    state_file = get_state_file_path(project_dir)
    
    # 自动添加 updated_at
    if "updated_at" not in data:
        data["updated_at"] = datetime.now().isoformat()
        
    with open(state_file, 'w', encoding='utf-8') as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)


def update_state(project_dir: Path, updates: Dict[str, Any]) -> Dict[str, Any]:
    """更新状态文件 (增量更新)"""
    current_state = load_state(project_dir)
    current_state.update(updates)
    save_state(project_dir, current_state)
    return current_state


def clear_state(project_dir: Path, key: str = None) -> bool:
    """清理状态文件
    
    Args:
        project_dir: 项目目录
        key: 如果指定，仅当状态中的 ID 与 key 匹配时才删除

    Returns:
        bool: 是否实际删除了状态文件
    """
    state_file = get_state_file_path(project_dir)
    if not state_file.exists():
        return False
        
    if key:
        # 检查是否匹配
        state = load_state(project_dir)
        # 检查 agent_id 或 mcp_id
        if state.get("agent_id") == key or state.get("mcp_id") == key:
            state_file.unlink()
            return True
        return False
    else:
        state_file.unlink()
        return True

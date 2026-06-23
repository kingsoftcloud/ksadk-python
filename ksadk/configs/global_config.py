"""
全局配置管理模块

管理 ~/.agentengine/settings.json 全局配置文件

配置格式 v1.0 - 嵌套结构:
{
    "version": "1.0",
    "model": {
        "OPENAI_API_KEY": "sk-xxx",
        "OPENAI_BASE_URL": "http://kspmas.ksyun.com/v1",
        "OPENAI_MODEL_NAME": "glm-5.2"
    },
    "cloud": {
        "KSYUN_ACCESS_KEY": "AKxxx",
        "KSYUN_SECRET_KEY": "SKxxx",
        "KSYUN_ACCOUNT_ID": "2000003485",
        "KSYUN_REGION": "cn-beijing-6"
    }
}
"""

import json
from pathlib import Path
from typing import Any, Dict

# 当前配置版本
CURRENT_CONFIG_VERSION = "1.0"

# 配置分组定义
CONFIG_GROUPS = {
    "model": [
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL_NAME",
    ],
    "cloud": [
        "KSYUN_ACCESS_KEY",
        "KSYUN_SECRET_KEY",
        "KSYUN_ACCOUNT_ID",
        "KSYUN_REGION",
    ],
    # 未来可扩展更多分组
    # "observability": ["LANGFUSE_PUBLIC_KEY", ...],
    # "plugins": {...},
}


def get_global_config_dir() -> Path:
    """获取全局配置目录路径

    Returns:
        Path: ~/.agentengine/
    """
    return Path.home() / ".agentengine"


def get_global_config_path() -> Path:
    """获取全局配置文件路径

    Returns:
        Path: ~/.agentengine/settings.json
    """
    return get_global_config_dir() / "settings.json"


def load_global_config() -> Dict[str, Any]:
    """加载全局配置

    Returns:
        dict: 全局配置字典，如果文件不存在则返回空字典
    """
    config_path = get_global_config_path()
    if not config_path.exists():
        return {}

    try:
        # 使用 utf-8-sig 自动处理 BOM，确保 Windows 兼容性
        with open(config_path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_global_config(config: Dict[str, Any]) -> bool:
    """保存全局配置

    Args:
        config: 配置字典

    Returns:
        bool: 是否保存成功
    """
    config_dir = get_global_config_dir()
    config_path = get_global_config_path()

    try:
        # 确保目录存在
        config_dir.mkdir(parents=True, exist_ok=True)

        # 添加版本号
        config["version"] = CURRENT_CONFIG_VERSION

        # 使用 utf-8-sig 编码写入，确保 Windows 兼容性
        with open(config_path, "w", encoding="utf-8-sig") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

        return True
    except IOError:
        return False


def global_config_exists() -> bool:
    """检查全局配置是否存在

    Returns:
        bool: 是否存在全局配置文件
    """
    return get_global_config_path().exists()


def get_env_from_global_config() -> Dict[str, str]:
    """从全局配置获取环境变量格式的配置

    返回可以直接写入 .env 文件的键值对
    注意：不包含 Langfuse 配置，因为这些通常是项目级别的配置

    Returns:
        dict: 环境变量字典
    """
    config = load_global_config()
    if not config:
        return {}

    env_vars = {}
    
    # 从各分组中提取环境变量
    for group_name, keys in CONFIG_GROUPS.items():
        group_config = config.get(group_name, {})
        for key in keys:
            if key in group_config and group_config[key]:
                env_vars[key] = group_config[key]

    return env_vars


def build_global_config_from_env(env_vars: Dict[str, str]) -> Dict[str, Any]:
    """从环境变量字典构建全局配置结构

    注意：不包含 Langfuse 配置，因为这些通常是项目级别的配置

    Args:
        env_vars: 环境变量字典

    Returns:
        dict: 全局配置结构 (嵌套格式)
    """
    config = {}
    
    # 按分组构建嵌套结构
    for group_name, keys in CONFIG_GROUPS.items():
        group_config = {}
        for key in keys:
            if key in env_vars and env_vars[key]:
                group_config[key] = env_vars[key]
        if group_config:
            config[group_name] = group_config

    return config

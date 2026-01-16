"""
构建结果和基类定义
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class BuildResult:
    """构建结果"""
    success: bool
    artifact_path: Optional[Path] = None
    artifact_size: int = 0  # bytes
    metadata: Dict[str, Any] = field(default_factory=dict)
    error_message: Optional[str] = None
    
    @property
    def artifact_size_mb(self) -> float:
        """获取 artifact 大小 (MB)"""
        return self.artifact_size / (1024 * 1024)


class BaseBuilder(ABC):
    """构建器基类"""
    
    def __init__(self, project_dir: Path, config: Dict[str, Any] = None):
        self.project_dir = Path(project_dir).resolve()
        self.config = config or {}
    
    @abstractmethod
    def build(self) -> BuildResult:
        """执行构建
        
        Returns:
            BuildResult: 构建结果
        """
        pass
    
    def _load_config(self) -> Dict[str, Any]:
        """加载项目配置文件"""
        import yaml
        
        config_path = self.project_dir / 'agentengine.yaml'
        if not config_path.exists():
            config_path = self.project_dir / 'ksadk.yaml'
        
        if config_path.exists():
            # 使用 utf-8-sig 自动处理 BOM，确保 Windows 兼容性
            with open(config_path, encoding='utf-8-sig') as f:
                return yaml.safe_load(f) or {}
        
        return {}
    
    def _load_dotenv(self) -> None:
        """加载项目目录的 .env 文件"""
        env_file = self.project_dir / ".env"
        if env_file.exists():
            try:
                from dotenv import load_dotenv
                # 使用 utf-8-sig 自动处理 BOM，确保 Windows 兼容性
                load_dotenv(env_file, override=True, encoding='utf-8-sig')
            except ImportError:
                pass

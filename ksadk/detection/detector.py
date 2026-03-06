"""
框架检测器 - 自动检测 LangChain / LangGraph / DeepAgents / ADK 项目
"""

import ast
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional
import yaml


class FrameworkType(Enum):
    """支持的框架类型"""
    ADK = "adk"
    LANGCHAIN = "langchain"
    LANGGRAPH = "langgraph"
    DEEPAGENTS = "deepagents"
    UNKNOWN = "unknown"


@dataclass
class DetectionResult:
    """检测结果"""
    type: FrameworkType
    name: str
    entry_point: str
    package_path: str
    agent_variable: str = "root_agent"
    confidence: float = 0.0
    
    @property
    def is_valid(self) -> bool:
        return self.type != FrameworkType.UNKNOWN


class FrameworkDetector:
    """框架检测器"""
    _ENTRY_FILES = ("agent.py", "main.py", "app.py")
    
    def __init__(self, project_dir: str):
        self.project_dir = Path(project_dir).resolve()
    
    def detect(self) -> DetectionResult:
        """检测项目使用的框架类型"""
        
        # 1. 检查 ksadk.yaml 配置文件 (显式声明)
        config_result = self._check_config()
        if config_result:
            return config_result
        
        # 2. 查找 Python 包目录
        package_path = self._find_package_dir()
        if not package_path:
            return DetectionResult(
                type=FrameworkType.UNKNOWN,
                name="Unknown",
                entry_point="",
                package_path=""
            )
        
        # 3. 查找 agent.py 或 __init__.py
        agent_file = self._find_agent_file(package_path)
        if not agent_file:
            return DetectionResult(
                type=FrameworkType.UNKNOWN,
                name="Unknown",
                entry_point="",
                package_path=str(package_path)
            )
        
        # 4. 分析代码确定框架类型
        return self._analyze_code(agent_file, package_path)
    
    def _check_config(self) -> Optional[DetectionResult]:
        """检查配置文件 (agentengine.yaml 或 ksadk.yaml)"""
        # 优先检查 agentengine.yaml
        config_path = self.project_dir / "agentengine.yaml"
        if not config_path.exists():
            config_path = self.project_dir / "ksadk.yaml"
        if not config_path.exists():
            config_path = self.project_dir / "ksadk.yml"
        
        if not config_path.exists():
            return None
        
        try:
            # 使用 utf-8-sig 自动处理 BOM，确保 Windows 兼容性
            with open(config_path, 'r', encoding='utf-8-sig') as f:
                config = yaml.safe_load(f)
            
            framework = config.get("framework", "adk").lower()
            framework_type = {
                "adk": FrameworkType.ADK,
                "langchain": FrameworkType.LANGCHAIN,
                "langgraph": FrameworkType.LANGGRAPH,
                "deepagents": FrameworkType.DEEPAGENTS,
            }.get(framework, FrameworkType.UNKNOWN)
            
            return DetectionResult(
                type=framework_type,
                name=config.get("name", self.project_dir.name),
                entry_point=config.get("entry_point", "agent.py"),
                package_path=str(self.project_dir / config.get("package", self.project_dir.name.replace('-', '_'))),
                agent_variable=config.get("agent_variable", "root_agent"),
                confidence=1.0
            )
        except Exception:
            return None
    
    def _find_package_dir(self) -> Optional[Path]:
        """查找 Python 包目录"""
        # 优先查找与项目同名的包 (下划线版本)
        expected_name = self.project_dir.name.replace('-', '_')
        expected_path = self.project_dir / expected_name
        if expected_path.exists() and expected_path.is_dir():
            if (expected_path / "__init__.py").exists() or any(
                (expected_path / entry_file).exists() for entry_file in self._ENTRY_FILES
            ):
                return expected_path
        
        # 查找任何包含 __init__.py 的子目录
        for item in self.project_dir.iterdir():
            if (
                item.is_dir()
                and not item.name.startswith('.')
                and item.name not in ('tests', 'test', '__pycache__')
                and (
                    (item / "__init__.py").exists()
                    or any((item / entry_file).exists() for entry_file in self._ENTRY_FILES)
                )
            ):
                return item
        
        # 当前目录本身也可能是一个包
        if (self.project_dir / "__init__.py").exists():
            return self.project_dir

        # 兼容脚本式项目: 当前目录直接包含 agent.py/main.py/app.py
        if any((self.project_dir / entry_file).exists() for entry_file in self._ENTRY_FILES):
            return self.project_dir
        
        return None
    
    def _find_agent_file(self, package_path: Path) -> Optional[Path]:
        """查找 agent.py 文件"""
        # 优先查找常见入口文件
        for entry_file in self._ENTRY_FILES:
            entry_path = package_path / entry_file
            if entry_path.exists():
                return entry_path
        
        # 检查 __init__.py 是否导出 root_agent
        init_py = package_path / "__init__.py"
        if init_py.exists():
            try:
                content = init_py.read_text(encoding="utf-8-sig")
                if "root_agent" in content or "graph" in content or "app" in content:
                    return init_py
            except Exception:
                pass
        
        return None
    
    def _analyze_code(self, agent_file: Path, package_path: Path) -> DetectionResult:
        """分析代码确定框架类型"""
        try:
            # 使用 utf-8-sig 自动处理 BOM，兼容 Windows/编辑器带 BOM 的脚本
            content = agent_file.read_text(encoding="utf-8-sig")
            tree = ast.parse(content)
        except Exception:
            return DetectionResult(
                type=FrameworkType.UNKNOWN,
                name=self.project_dir.name,
                entry_point=str(agent_file.relative_to(self.project_dir)),
                package_path=str(package_path)
            )
        
        imports = self._extract_imports(tree)
        
        # 检测 ADK
        if self._is_adk(imports, content):
            return DetectionResult(
                type=FrameworkType.ADK,
                name=package_path.name,
                entry_point=str(agent_file.relative_to(self.project_dir)),
                package_path=str(package_path),
                agent_variable="root_agent",
                confidence=0.9
            )
        
        # 检测 DeepAgents (LangChain 生态, 底层基于 LangGraph)
        if self._is_deepagents(imports, content):
            return DetectionResult(
                type=FrameworkType.DEEPAGENTS,
                name=package_path.name,
                entry_point=str(agent_file.relative_to(self.project_dir)),
                package_path=str(package_path),
                agent_variable="root_agent",
                confidence=0.9
            )

        # 检测 LangGraph
        if self._is_langgraph(imports, content):
            return DetectionResult(
                type=FrameworkType.LANGGRAPH,
                name=package_path.name,
                entry_point=str(agent_file.relative_to(self.project_dir)),
                package_path=str(package_path),
                agent_variable="root_agent",
                confidence=0.9
            )
        
        # 检测 LangChain
        if self._is_langchain(imports, content):
            return DetectionResult(
                type=FrameworkType.LANGCHAIN,
                name=package_path.name,
                entry_point=str(agent_file.relative_to(self.project_dir)),
                package_path=str(package_path),
                agent_variable="root_agent",
                confidence=0.8
            )
        
        return DetectionResult(
            type=FrameworkType.UNKNOWN,
            name=self.project_dir.name,
            entry_point=str(agent_file.relative_to(self.project_dir)),
            package_path=str(package_path)
        )
    
    def _extract_imports(self, tree: ast.AST) -> set:
        """提取导入的模块"""
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module.split('.')[0])
        return imports
    
    def _is_adk(self, imports: set, content: str) -> bool:
        """检测是否为 ADK 项目"""
        # 检查 google.adk 导入
        if "google" in imports and ("google.adk" in content or "from google.adk" in content):
            return True
        return False
    
    def _is_langgraph(self, imports: set, content: str) -> bool:
        """检测是否为 LangGraph 项目"""
        if "langgraph" in imports:
            return True
        if "StateGraph" in content or "langgraph.graph" in content:
            return True
        return False
    
    def _is_langchain(self, imports: set, content: str) -> bool:
        """检测是否为 LangChain 项目"""
        langchain_modules = {"langchain", "langchain_openai", "langchain_core", "langchain_community"}
        if langchain_modules & imports:
            return True
        return False

    def _is_deepagents(self, imports: set, content: str) -> bool:
        """检测是否为 DeepAgents 项目"""
        if "deepagents" in imports:
            return True
        if "from deepagents import" in content or "create_deep_agent(" in content:
            return True
        return False

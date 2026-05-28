"""
MCP 检测器 - 自动检测 FastMCP 项目
"""

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List
import yaml


@dataclass
class MCPDetectionResult:
    """MCP 检测结果"""
    is_mcp: bool
    name: str
    entry_point: str
    package_path: str
    mcp_variable: str = "mcp"
    tools: List[str] = None  # 检测到的工具名称
    confidence: float = 0.0
    
    def __post_init__(self):
        if self.tools is None:
            self.tools = []
    
    @property
    def is_valid(self) -> bool:
        return self.is_mcp


class MCPDetector:
    """MCP 检测器 - 检测 FastMCP 项目"""
    
    # FastMCP 特征模式
    FASTMCP_PATTERNS = [
        "from fastmcp import FastMCP",
        "from fastmcp import",
        "import fastmcp",
    ]
    
    MCP_DECORATORS = [
        "@mcp.tool",
        "@mcp.resource",
        "@mcp.prompt",
    ]
    
    def __init__(self, project_dir: str):
        self.project_dir = Path(project_dir).resolve()
    
    def detect(self) -> MCPDetectionResult:
        """检测项目是否为 MCP 项目"""
        
        # 1. 检查配置文件中的显式声明
        config_result = self._check_config()
        if config_result:
            return config_result
        
        # 2. 查找 Python 文件
        mcp_file = self._find_mcp_file()
        if not mcp_file:
            return MCPDetectionResult(
                is_mcp=False,
                name=self.project_dir.name,
                entry_point="",
                package_path=""
            )
        
        # 3. 分析代码
        return self._analyze_code(mcp_file)
    
    def _check_config(self) -> Optional[MCPDetectionResult]:
        """检查配置文件中的 MCP 声明"""
        config_paths = [
            self.project_dir / "agentengine.yaml",
            self.project_dir / "ksadk.yaml",
            self.project_dir / "mcp.yaml",
        ]
        
        for config_path in config_paths:
            if not config_path.exists():
                continue
            
            try:
                with open(config_path, 'r', encoding='utf-8-sig') as f:
                    config = yaml.safe_load(f)
                
                # 检查 type: mcp
                if config.get("type") == "mcp" or config.get("framework") == "mcp":
                    return MCPDetectionResult(
                        is_mcp=True,
                        name=config.get("name", self.project_dir.name),
                        entry_point=config.get("entry_point", "server.py"),
                        package_path=str(self.project_dir / config.get("package", self.project_dir.name.replace('-', '_'))),
                        mcp_variable=config.get("mcp_variable", "mcp"),
                        confidence=1.0
                    )
            except Exception:
                continue
        
        return None
    
    def _find_mcp_file(self) -> Optional[Path]:
        """查找 MCP 入口文件"""
        # 常见的 MCP 入口文件名
        entry_files = ["server.py", "mcp_server.py", "main.py", "__init__.py"]
        
        # 首先在根目录查找
        for entry in entry_files:
            file_path = self.project_dir / entry
            if file_path.exists() and self._is_mcp_file(file_path):
                return file_path
        
        # 在子包中查找
        for item in self.project_dir.iterdir():
            if item.is_dir() and (item / "__init__.py").exists():
                if item.name.startswith('.') or item.name in ('tests', 'test', '__pycache__'):
                    continue
                
                for entry in entry_files:
                    file_path = item / entry
                    if file_path.exists() and self._is_mcp_file(file_path):
                        return file_path
                
                # 检查 __init__.py
                init_file = item / "__init__.py"
                if self._is_mcp_file(init_file):
                    return init_file
        
        # 扫描所有 .py 文件
        for py_file in self.project_dir.rglob("*.py"):
            path_str = str(py_file)
            if "__pycache__" in path_str or "/.agentengine/" in path_str or "/.venv/" in path_str or "/venv/" in path_str:
                continue
            if self._is_mcp_file(py_file):
                return py_file
        
        return None
    
    def _is_mcp_file(self, file_path: Path) -> bool:
        """检查文件是否包含 FastMCP 代码"""
        try:
            content = file_path.read_text(encoding='utf-8')
            
            # 检查 FastMCP 导入
            for pattern in self.FASTMCP_PATTERNS:
                if pattern in content:
                    return True
            
            return False
        except Exception:
            return False
    
    def _analyze_code(self, mcp_file: Path) -> MCPDetectionResult:
        """分析 MCP 代码"""
        try:
            content = mcp_file.read_text(encoding='utf-8')
            tree = ast.parse(content)
        except Exception:
            return MCPDetectionResult(
                is_mcp=False,
                name=self.project_dir.name,
                entry_point=str(mcp_file.relative_to(self.project_dir)),
                package_path=""
            )
        
        # 检测 FastMCP 导入
        has_fastmcp = False
        for pattern in self.FASTMCP_PATTERNS:
            if pattern in content:
                has_fastmcp = True
                break
        
        if not has_fastmcp:
            return MCPDetectionResult(
                is_mcp=False,
                name=self.project_dir.name,
                entry_point=str(mcp_file.relative_to(self.project_dir)),
                package_path=""
            )
        
        # 提取 MCP 实例变量名
        mcp_variable = self._find_mcp_variable(tree, content)
        
        # 提取工具名称
        tools = self._extract_tools(content)
        
        # 确定包路径
        package_path = mcp_file.parent
        if package_path == self.project_dir:
            # 文件在根目录
            package_path = self.project_dir
        
        return MCPDetectionResult(
            is_mcp=True,
            name=self.project_dir.name,
            entry_point=str(mcp_file.relative_to(self.project_dir)),
            package_path=str(package_path),
            mcp_variable=mcp_variable,
            tools=tools,
            confidence=0.9
        )
    
    def _find_mcp_variable(self, tree: ast.AST, content: str) -> str:
        """查找 FastMCP 实例变量名"""
        # 查找 xxx = FastMCP(...) 模式
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                # 检查右侧是否是 FastMCP 调用
                if isinstance(node.value, ast.Call):
                    if isinstance(node.value.func, ast.Name):
                        if node.value.func.id == "FastMCP":
                            # 返回左侧变量名
                            if node.targets and isinstance(node.targets[0], ast.Name):
                                return node.targets[0].id
        
        # 默认返回 mcp
        return "mcp"
    
    def _extract_tools(self, content: str) -> List[str]:
        """提取 @mcp.tool 装饰的函数名"""
        tools = []
        
        try:
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    for decorator in node.decorator_list:
                        # @mcp.tool
                        if isinstance(decorator, ast.Attribute):
                            if decorator.attr == "tool":
                                tools.append(node.name)
                                break
                        # @mcp.tool()
                        elif isinstance(decorator, ast.Call):
                            if isinstance(decorator.func, ast.Attribute):
                                if decorator.func.attr == "tool":
                                    tools.append(node.name)
                                    break
        except Exception:
            pass
        
        return tools

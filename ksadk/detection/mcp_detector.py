"""
MCP 检测器 - 自动检测 FastMCP 项目
"""

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

from ksadk.detection.detector import FrameworkType


@dataclass
class MCPDetectionResult:
    """MCP 检测结果"""

    is_mcp: bool
    name: str
    entry_point: str
    package_path: str
    mcp_variable: str = "mcp"
    tools: List[str] = field(default_factory=list)  # 检测到的工具名称
    confidence: float = 0.0

    @property
    def is_valid(self) -> bool:
        return self.is_mcp

    @property
    def type(self) -> FrameworkType:
        """Expose the framework contract expected by shared builders."""
        return FrameworkType.FASTMCP


class MCPDetector:
    """MCP 检测器 - 检测 FastMCP / MCPServer 项目"""

    # Server 类导入特征。
    #
    # ## 为什么要认三套
    #
    # `mcp` 2.3.0 把 `FastMCP` 改名成 `MCPServer` 并挪了路径，
    # **同时删除** `mcp.server.fastmcp`。反过来，mcp 1.x 没有新路径。
    #
    # 只认一套的后果（2026-11-18 实测）：迁移到 mcp 2.x 后
    # `_find_mcp_file()` 返回 `None` → `is_mcp=False` → `ksadk mcp build`
    # 直接报「未检测到 FastMCP 项目」，**构建彻底失败**。
    #
    # 顺序：新的在前（用户装的通常是新版），旧的兜底。
    FASTMCP_PATTERNS = [
        # mcp >= 2.3
        "from mcp.server.mcpserver import",
        "import mcp.server.mcpserver",
        # mcp 2.0 ~ 2.2 的内部路径
        "from mcp.server.mcpserver.server import",
        # mcp 1.x（兼独立 fastmcp 包）
        "from mcp.server.fastmcp import",
        "from fastmcp import FastMCP",
        "from fastmcp import",
        "import fastmcp",
    ]

    # 实例化时的类名（`_find_mcp_variable` 用它定位变量名）
    SERVER_CLASS_NAMES = ("MCPServer", "FastMCP")

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
                is_mcp=False, name=self.project_dir.name, entry_point="", package_path=""
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
                with open(config_path, "r", encoding="utf-8-sig") as f:
                    config = yaml.safe_load(f)

                # 检查 type: mcp
                if config.get("type") == "mcp" or config.get("framework") == "mcp":
                    return MCPDetectionResult(
                        is_mcp=True,
                        name=config.get("name", self.project_dir.name),
                        entry_point=config.get("entry_point", "server.py"),
                        package_path=str(
                            self.project_dir
                            / config.get("package", self.project_dir.name.replace("-", "_"))
                        ),
                        mcp_variable=config.get("mcp_variable", "mcp"),
                        confidence=1.0,
                    )
            except Exception:
                continue

        return None

    def _find_mcp_file(self) -> Optional[Path]:
        """查找 MCP 入口文件"""
        # 常见的 MCP 入口文件名
        entry_files = ["server.py", "mcp_server.py", "main.py", "__init__.py"]

        # 首先在根目录查找（根目录文件不会误判，不需要跳过）
        for entry in entry_files:
            file_path = self.project_dir / entry
            if file_path.exists() and self._is_mcp_file(file_path):
                return file_path

        # 在子包中查找
        for item in self.project_dir.iterdir():
            if item.is_dir() and (item / "__init__.py").exists():
                if self._should_skip(item):
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
            if self._should_skip(py_file):
                continue
            if self._is_mcp_file(py_file):
                return py_file

        return None

    # 已知非源码目录：入口探测必须跳过它们。
    NON_SOURCE_DIRS = frozenset(
        {
            "tests",
            "test",
            "testing",
            "__pycache__",
            "node_modules",
            "venv",
            "env",
        }
    )

    def _should_skip(self, path: Path) -> bool:
        """判断路径是否应被入口探测跳过。

        ## 为什么必须跳过 `tests/`

        单元测试里经常 `from fastmcp import FastMCP`（为了构造 Server 断言工具面）。
        而 `_is_mcp_file()` 只看这一行导入，于是测试文件会被当成生产入口。

        实测（ezone-mcp）：一个 4310 文件的项目，`rglob("*.py")` 先命中
        `tests/test_agent_surface.py`，生成的 entrypoint 变成
        `from tests.test_agent_surface import mcp` —— **打包能过，运行必崩**
        （模块级没有 `mcp`，且 tests 不在运行路径里）。

        ## 为什么还要跳过点目录

        `.venv/` / `.agentengine/` / `.worktrees/` / `.git/` / `.tox/` 里可能是
        **完整仓库副本或已安装依赖**。扫描它们既慢（几千个文件），
        又必然把三方包里的 FastMCP 示例当成入口。

        判断只看**父目录**（`parts[:-1]`）——文件名本身不参与，
        所以 `test_server.py` 这种根目录文件仍会被根目录扫描命中。
        """
        try:
            parts = path.relative_to(self.project_dir).parts
        except ValueError:
            # 不在项目目录内（理论上不会发生），保守地不跳过
            return False
        return any(part.startswith(".") or part in self.NON_SOURCE_DIRS for part in parts[:-1])

    def _is_mcp_file(self, file_path: Path) -> bool:
        """检查文件是否包含 FastMCP 代码"""
        try:
            content = file_path.read_text(encoding="utf-8")

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
            content = mcp_file.read_text(encoding="utf-8")
            tree = ast.parse(content, filename=str(mcp_file))
        except Exception:
            return MCPDetectionResult(
                is_mcp=False,
                name=self.project_dir.name,
                entry_point=str(mcp_file.relative_to(self.project_dir)),
                package_path="",
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
                package_path="",
            )

        # 提取 MCP 实例变量名
        mcp_variable = self._find_mcp_variable(tree, content)

        # 提取工具名称
        tools = self._extract_tools(content, filename=str(mcp_file))

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
            confidence=0.9,
        )

    def _find_mcp_variable(self, tree: ast.AST, content: str) -> str:
        """查找 server 实例的变量名（`xxx = MCPServer(...)` / `xxx = MCPServer(...)`）。"""
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(node.value, ast.Call):
                continue
            func = node.value.func
            # 兼容 `MCPServer(...)` 与 `fastmcp.MCPServer(...)` 两种写法
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in self.SERVER_CLASS_NAMES:
                if node.targets and isinstance(node.targets[0], ast.Name):
                    return node.targets[0].id

        # 默认返回 mcp
        return "mcp"

    def _extract_tools(self, content: str, *, filename: str = "<extract_tools>") -> List[str]:
        """提取 @mcp.tool 装饰的函数名"""
        tools = []

        try:
            tree = ast.parse(content, filename=filename)
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

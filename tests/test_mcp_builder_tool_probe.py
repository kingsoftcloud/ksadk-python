"""``MCPCodeBuilder`` 的**运行时工具探测**回归测试。

## 为什么需要

`MCPDetector._extract_tools()` 用 AST 扫入口文件的 `@mcp.tool` 装饰器。
只要项目用**程序化注册**（`mcp.add_tool(...)`）或工厂函数拼装，AST 就扫不到：

```python
# mcp_server.py
from ezone_mcp.server import create_server
mcp = create_server()      # ← 工具在别的模块里 add_tool 注册
```

后果（2026-11-18 云端实测）：`build-metadata.json` 的 `tools` 恒为 `[]`，
平台「工具」tab 空白，`ksadk mcp status` 显示 `tools: []`。

**修法**：构建时起子进程**真调** `list_tools()`。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ksadk.builders.mcp_builder import MCPCodeBuilder
from ksadk.detection.mcp_detector import MCPDetectionResult, MCPDetector

# 程序化注册：AST 扫不到 `dynamic_tool`（没有装饰器），只有 list_tools() 能看到
DYNAMIC_SERVER = '''\
from fastmcp import FastMCP
from fastmcp.tools import Tool

mcp = FastMCP("demo")


def _dynamic_impl() -> str:
    """动态注册的工具。"""
    return "ok"


mcp.add_tool(Tool.from_function(_dynamic_impl, name="dynamic_tool"))
'''

# 装饰器注册：AST 和运行时都能看到
DECORATED_SERVER = '''\
from fastmcp import FastMCP

mcp = FastMCP("demo")


@mcp.tool
def decorated_tool() -> str:
    """装饰器注册。"""
    return "ok"
'''

# 导入即抛异常：探测必须回退而不是崩
BROKEN_SERVER = '''\
raise RuntimeError("boom at import time")

from fastmcp import FastMCP

mcp = FastMCP("demo")
'''


def _project(tmp_path: Path, source: str, *, name: str = "mcp_server.py") -> Path:
    (tmp_path / name).write_text(source, encoding="utf-8")
    return tmp_path


def _result(tmp_path: Path, entry: str = "mcp_server.py") -> MCPDetectionResult:
    return MCPDetectionResult(
        is_mcp=True,
        name="demo",
        entry_point=entry,
        package_path=str(tmp_path),
        mcp_variable="mcp",
        tools=[],
        confidence=1.0,
    )


# --------------------------------------------------------------------------- #
# 探测解释器
# --------------------------------------------------------------------------- #

def test_find_probe_python_prefers_project_venv(tmp_path: Path) -> None:
    """优先用项目自己的 `.venv`——构建机的 ksadk 环境未必装了项目依赖。"""
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")

    builder = MCPCodeBuilder(tmp_path)
    assert builder._find_probe_python() == venv_python


def test_find_probe_python_falls_back_to_current_interpreter(tmp_path: Path) -> None:
    """没有 venv 时回退到 `sys.executable`（不返回 None 才能尽量探）。"""
    builder = MCPCodeBuilder(tmp_path)
    assert builder._find_probe_python() == Path(sys.executable)


@pytest.mark.parametrize("dirname", ["venv", "env"])
def test_find_probe_python_accepts_alternate_venv_names(tmp_path: Path, dirname: str) -> None:
    candidate = tmp_path / dirname / "bin" / "python"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("", encoding="utf-8")

    assert MCPCodeBuilder(tmp_path)._find_probe_python() == candidate


# --------------------------------------------------------------------------- #
# 运行时探测（核心）
# --------------------------------------------------------------------------- #

def test_probe_finds_programmatically_registered_tools(tmp_path: Path) -> None:
    """**核心用例**：AST 扫不到、`list_tools()` 能扫到。

    这是 2026-11-18 云端「工具 tab 空白」的直接回归。
    """
    _project(tmp_path, DYNAMIC_SERVER)
    builder = MCPCodeBuilder(tmp_path)
    detection = _result(tmp_path)

    # 前提：AST 确实扫不到（否则这个用例证明不了什么）
    assert MCPDetector(str(tmp_path)).detect().tools == [], (
        "AST 能扫到就不需要运行时探测了——用例前提失效"
    )

    probed = builder._probe_tools_runtime(detection)

    assert probed == ["dynamic_tool"], f"运行时探测失败：{probed!r}"


def test_probe_matches_ast_for_decorated_tools(tmp_path: Path) -> None:
    """装饰器注册的工具两边应该一致（探测不能引入偏差）。"""
    _project(tmp_path, DECORATED_SERVER)
    builder = MCPCodeBuilder(tmp_path)
    detection = _result(tmp_path)

    assert MCPDetector(str(tmp_path)).detect().tools == ["decorated_tool"]
    assert builder._probe_tools_runtime(detection) == ["decorated_tool"]


def test_probe_returns_none_when_import_fails(tmp_path: Path) -> None:
    """导入即抛异常时必须返回 `None`（让调用方回退 AST），不能崩构建。"""
    _project(tmp_path, BROKEN_SERVER)
    builder = MCPCodeBuilder(tmp_path)

    assert builder._probe_tools_runtime(_result(tmp_path)) is None


def test_probe_returns_none_when_entry_missing(tmp_path: Path) -> None:
    """入口文件不存在时返回 `None` 而不是异常。"""
    builder = MCPCodeBuilder(tmp_path)
    detection = _result(tmp_path, entry="missing.py")

    assert builder._probe_tools_runtime(detection) is None


def test_probe_handles_subdirectory_entrypoint(tmp_path: Path) -> None:
    """入口在子目录（`pkg/server.py`）时模块路径要正确拼接。"""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "server.py").write_text(DYNAMIC_SERVER, encoding="utf-8")

    probed = MCPCodeBuilder(tmp_path)._probe_tools_runtime(_result(tmp_path, "pkg/server.py"))

    assert probed == ["dynamic_tool"]


# --------------------------------------------------------------------------- #
# _resolve_tools：覆盖 + 回退
# --------------------------------------------------------------------------- #

def test_resolve_tools_overrides_ast_result(tmp_path: Path) -> None:
    _project(tmp_path, DYNAMIC_SERVER)
    detection = _result(tmp_path)
    assert detection.tools == []

    resolved = MCPCodeBuilder(tmp_path)._resolve_tools(detection)

    assert resolved.tools == ["dynamic_tool"]
    # 其它字段不能丢
    assert resolved.name == detection.name
    assert resolved.mcp_variable == detection.mcp_variable
    assert resolved.entry_point == detection.entry_point


def test_resolve_tools_keeps_ast_when_probe_fails(tmp_path: Path) -> None:
    _project(tmp_path, BROKEN_SERVER)
    detection = MCPDetectionResult(
        is_mcp=True, name="demo", entry_point="mcp_server.py",
        package_path=str(tmp_path), mcp_variable="mcp",
        tools=["from_ast"], confidence=1.0,
    )

    resolved = MCPCodeBuilder(tmp_path)._resolve_tools(detection)

    assert resolved is detection, "探测失败时应原样返回（不复制、不丢 AST 结果）"
    assert resolved.tools == ["from_ast"]


def test_resolve_tools_does_not_copy_when_identical(tmp_path: Path) -> None:
    """探测结果和 AST 一致时不要无谓地 `replace`（保持对象身份）。"""
    _project(tmp_path, DECORATED_SERVER)
    detection = MCPDetectionResult(
        is_mcp=True, name="demo", entry_point="mcp_server.py",
        package_path=str(tmp_path), mcp_variable="mcp",
        tools=["decorated_tool"], confidence=1.0,
    )

    assert MCPCodeBuilder(tmp_path)._resolve_tools(detection) is detection


# --------------------------------------------------------------------------- #
# 子进程隔离：不能污染构建进程
# --------------------------------------------------------------------------- #

def test_probe_does_not_import_into_builder_process(tmp_path: Path) -> None:
    """探测必须跑在子进程——否则会把项目代码/日志塞进构建进程。"""
    marker = "ksadk_probe_marker_module"
    (tmp_path / f"{marker}.py").write_text(
        "from fastmcp import FastMCP\nmcp = FastMCP('x')\n",
        encoding="utf-8",
    )
    _project(tmp_path, DYNAMIC_SERVER)

    MCPCodeBuilder(tmp_path)._probe_tools_runtime(_result(tmp_path))

    assert marker not in sys.modules, "探测不能把项目模块 import 进构建进程"

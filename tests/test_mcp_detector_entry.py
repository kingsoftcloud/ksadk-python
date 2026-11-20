"""``MCPDetector`` 入口探测的跳过规则回归测试。

## 为什么需要这个文件

`_find_mcp_file()` 的兜底分支 `rglob("*.py")` 曾经只跳过
`__pycache__` / `.agentengine` / `.venv` / `venv`，**漏了 `tests/` 和点目录**。

后果（2026-11 实测）：一个 4310 文件的 FastMCP 项目里，`rglob` 先命中
`tests/test_agent_surface.py`（该文件为了构造 Server 而 `from fastmcp import FastMCP`），
于是：

```python
# 生成的 entrypoint.py
from tests.test_agent_surface import mcp     # ← 打包能过，运行必崩
```

**打包阶段完全不报错**，只有真正 `mcp.run()` 时才 ImportError。
这类「构建成功、运行失败」的缺陷最难排查，所以必须有用例锁住。

## 覆盖的目录类型

| 目录 | 为什么不能当入口 |
|---|---|
| `tests/` `test/` `testing/` | 测试里常 import fastmcp |
| `.venv/` `.tox/` `venv/` `env/` | 已安装依赖，含三方 FastMCP 示例 |
| `.worktrees/` | **完整仓库副本**，会扫出几万个同名文件 |
| `.agentengine/` | 上一次构建产物 |
| `node_modules/` | 前端依赖 |
| `__pycache__/` | 字节码 |

## 反向保证

跳过规则**不能误伤**真正的入口：

- 根目录的 `server.py` / `mcp_server.py` / `main.py`
- 子包里的 `__init__.py`
- 根目录名为 `test_server.py` 的文件（父目录是项目根，不该被跳过）
"""

from __future__ import annotations

from pathlib import Path

from ksadk.detection.mcp_detector import MCPDetector

FASTMCP_SERVER = """\
from fastmcp import FastMCP

mcp = FastMCP("demo")


@mcp.tool
def hello(name: str) -> str:
    return f"hello {name}"
"""


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _detect(project_dir: Path) -> str:
    return MCPDetector(str(project_dir)).detect().entry_point


# --------------------------------------------------------------------------- #
# 必须跳过：tests / test / testing
# --------------------------------------------------------------------------- #


def test_skips_tests_directory(tmp_path: Path) -> None:
    """`tests/test_*.py` 里 import fastmcp 不能被当成生产入口。"""
    _write(tmp_path / "tests" / "test_agent_surface.py", FASTMCP_SERVER)
    _write(tmp_path / "tests" / "conftest.py", "from fastmcp import FastMCP\n")
    _write(tmp_path / "src" / "demo" / "server.py", FASTMCP_SERVER)

    entry = _detect(tmp_path)

    assert "tests" not in entry, f"探测到了测试文件：{entry}"
    assert entry.endswith("server.py")


def test_skips_test_singular_directory(tmp_path: Path) -> None:
    _write(tmp_path / "test" / "test_server.py", FASTMCP_SERVER)
    _write(tmp_path / "app.py", FASTMCP_SERVER)

    assert "test/" not in _detect(tmp_path).replace("\\", "/")


def test_skips_testing_directory(tmp_path: Path) -> None:
    _write(tmp_path / "testing" / "helpers.py", FASTMCP_SERVER)
    _write(tmp_path / "app.py", FASTMCP_SERVER)

    assert _detect(tmp_path) == "app.py"


# --------------------------------------------------------------------------- #
# 必须跳过：点目录（含完整 checkout 的 .worktrees）
# --------------------------------------------------------------------------- #


def test_skips_worktrees_full_checkout(tmp_path: Path) -> None:
    """.worktrees/ 是完整仓库副本——不跳过会扫出几万个同名文件。"""
    # 模拟一个旧 worktree：里面也有 src/demo/server.py，且内容更新
    _write(tmp_path / ".worktrees" / "old" / "src" / "demo" / "server.py", FASTMCP_SERVER)
    _write(tmp_path / "src" / "demo" / "server.py", FASTMCP_SERVER)

    entry = _detect(tmp_path)

    assert ".worktrees" not in entry, f"探测进了 worktree：{entry}"


def test_skips_virtualenv_and_tox(tmp_path: Path) -> None:
    _write(tmp_path / ".venv" / "lib" / "fastmcp" / "example.py", FASTMCP_SERVER)
    _write(tmp_path / ".tox" / "py312" / "lib" / "example.py", FASTMCP_SERVER)
    _write(tmp_path / "app.py", FASTMCP_SERVER)

    assert _detect(tmp_path) == "app.py"


def test_skips_agentengine_build_dir(tmp_path: Path) -> None:
    """上一次构建解压出来的副本不能再被当成源码。"""
    _write(tmp_path / ".agentengine" / "mcp_build" / "deps" / "server.py", FASTMCP_SERVER)
    _write(tmp_path / "app.py", FASTMCP_SERVER)

    assert _detect(tmp_path) == "app.py"


def test_skips_node_modules_and_pycache(tmp_path: Path) -> None:
    _write(tmp_path / "node_modules" / "pkg" / "server.py", FASTMCP_SERVER)
    _write(tmp_path / "src" / "__pycache__" / "cached.py", FASTMCP_SERVER)
    _write(tmp_path / "app.py", FASTMCP_SERVER)

    assert _detect(tmp_path) == "app.py"


# --------------------------------------------------------------------------- #
# 反向保证：跳过规则不能误伤真入口
# --------------------------------------------------------------------------- #


def test_root_server_py_still_wins(tmp_path: Path) -> None:
    _write(tmp_path / "server.py", FASTMCP_SERVER)
    _write(tmp_path / "tests" / "test_server.py", FASTMCP_SERVER)

    assert _detect(tmp_path) == "server.py"


def test_root_test_prefixed_file_is_not_skipped(tmp_path: Path) -> None:
    """`parts[:-1]` 只看父目录——根目录下的 `test_server.py` 不该被跳过。"""
    _write(tmp_path / "test_server.py", FASTMCP_SERVER)

    assert _detect(tmp_path) == "test_server.py"


def test_subpackage_init_is_still_found(tmp_path: Path) -> None:
    _write(tmp_path / "demo" / "__init__.py", FASTMCP_SERVER)
    _write(tmp_path / "demo" / "other.py", "x = 1\n")

    assert _detect(tmp_path) == "demo/__init__.py"


def test_nested_package_entry_is_found(tmp_path: Path) -> None:
    """src/ 布局：入口在 src/demo/server.py，`src` 本身不是包。"""
    _write(tmp_path / "src" / "demo" / "server.py", FASTMCP_SERVER)

    assert _detect(tmp_path) == "src/demo/server.py"


def test_no_fastmcp_anywhere_returns_invalid(tmp_path: Path) -> None:
    _write(tmp_path / "app.py", "print('hello')\n")
    _write(tmp_path / "tests" / "test_app.py", "from fastmcp import FastMCP\n")

    result = MCPDetector(str(tmp_path)).detect()

    assert result.is_mcp is False, "只有 tests/ 里 import fastmcp 时不应该判定为 MCP 项目"


# --------------------------------------------------------------------------- #
# 显式声明优先于启发式扫描
# --------------------------------------------------------------------------- #


def test_explicit_config_wins_over_scanning(tmp_path: Path) -> None:
    """`agentengine.yaml` 里写了 `type: mcp` 就不该再猜。"""
    _write(
        tmp_path / "agentengine.yaml",
        "type: mcp\nname: demo\nentry_point: mcp_server.py\nmcp_variable: mcp\n",
    )
    _write(tmp_path / "mcp_server.py", FASTMCP_SERVER)
    _write(tmp_path / "tests" / "test_x.py", FASTMCP_SERVER)

    assert _detect(tmp_path) == "mcp_server.py"

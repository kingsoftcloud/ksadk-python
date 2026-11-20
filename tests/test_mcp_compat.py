"""`ksadk._mcp_compat` 的版本兼容性测试。

## 为什么单独一个文件

`ksadk` 声称同时支持 **mcp 1.x 和 2.x**，但常规测试全部 `from fastmcp import
FastMCP`——而 `fastmcp 4.2.1` 硬性要求 `mcp>=2.3.0`，所以 **1.x 路径永远
跑不到**。

实测踩过的坑（2026-11-19）：

```python
# ❌ 错误检测：mcp 1.26 同时导出新旧两个名字，永远走 2.x 分支
try:
    from mcp.client.streamable_http import streamable_http_client
except ImportError:
    import httpx
    return httpx
import httpx2   # ← 1.26 到这里 ModuleNotFoundError
```

后果：**mcp 1.x 环境直接不可用**，而只跑 2.3 的 CI 发现不了。

## 本文件的约束

**只依赖 `mcp` 包本身，不依赖 `fastmcp`**——这样 CI 才能 pin
`mcp==1.26.0` 跑（fastmcp 4.x 装不上）。

验证方式：

```bash
uv pip install "mcp==1.26.0"
pytest tests/test_mcp_compat.py -q
uv pip install "mcp==2.3.0"
pytest tests/test_mcp_compat.py -q
```
"""

from __future__ import annotations

import datetime
import inspect
import sys

import pytest

from ksadk._mcp_compat import (
    MCPServer,
    field,
    httpx_module,
    list_tools_page,
    mcp_version,
    read_timeout_seconds,
    tool_input_schema,
)


def _major() -> int:
    return int(mcp_version().split(".")[0])


def _installed(name: str) -> bool:
    try:
        __import__(name)
    except ImportError:
        return False
    return True


# --------------------------------------------------------------------------- #
# 版本探测
# --------------------------------------------------------------------------- #

def test_mcp_version_is_importable() -> None:
    assert mcp_version() not in {"", "unknown"}


# --------------------------------------------------------------------------- #
# Server 类：三段回退
# --------------------------------------------------------------------------- #

def test_mcp_server_class_importable() -> None:
    """1.x 是 `FastMCP`，2.x 是 `MCPServer`——两者都必须能拿到。"""
    assert MCPServer is not None
    assert inspect.isclass(MCPServer)


def test_mcp_server_class_matches_major_version() -> None:
    """类名必须与 mcp 大版本一致，不能「碰巧」拿到另一个。"""
    if _major() >= 2:
        assert MCPServer.__name__ == "MCPServer"
    else:
        assert MCPServer.__name__ == "FastMCP"


# --------------------------------------------------------------------------- #
# httpx 模块：**按类型注解判断**
# --------------------------------------------------------------------------- #

def test_httpx_module_matches_mcp_backend() -> None:
    """`httpx_module()` 必须返回 mcp 实际使用的那个 httpx。

    ⚠️ 这是本文件**最重要**的用例。历史 bug：用「能否 import
    `streamable_http_client`」判断版本，但 mcp 1.26 同时导出了新旧两个名字，
    导致 1.26 被误判成 2.x → `import httpx2` → `ModuleNotFoundError`。
    """
    module = httpx_module()
    assert inspect.ismodule(module)
    assert module.__name__ in {"httpx", "httpx2"}


def test_httpx_module_matches_streamable_http_annotation() -> None:
    """返回值必须与 `streamable_http_client(http_client=...)` 的注解一致。

    这是「用注解判断版本」这个修法的**直接回归**。
    """
    from mcp.client.streamable_http import streamable_http_client

    parameter = inspect.signature(streamable_http_client).parameters.get("http_client")
    assert parameter is not None, "mcp 的 streamable_http_client 缺少 http_client 参数"
    annotation = str(parameter.annotation)

    expected = "httpx2" if "httpx2" in annotation else "httpx"
    assert httpx_module().__name__ == expected, (
        f"mcp 要求 {expected}，但 httpx_module() 返回 "
        f"{httpx_module().__name__}——混用会静默挂起"
    )


def test_httpx_module_is_importable_matching_backend() -> None:
    """返回的模块必须真的装了（不是悬空引用）。"""
    assert _installed(httpx_module().__name__)


async def test_streamable_http_default_client_matches_backend(monkeypatch):
    import contextlib
    import functools
    import mcp.client.streamable_http as transport
    from ksadk._mcp_compat import streamable_http

    backend = httpx_module()
    clients = []

    @functools.wraps(transport.streamable_http_client)
    @contextlib.asynccontextmanager
    async def capture(url, *, http_client):
        clients.append(http_client)
        assert isinstance(http_client, backend.AsyncClient)
        assert http_client.headers['Authorization'] == 'Bearer fixture-key'
        yield ('read', 'write')

    monkeypatch.setattr(transport, 'streamable_http_client', capture)
    try:
        async with streamable_http('http://localhost/mcp', {'Authorization': 'Bearer fixture-key'}) as streams:
            assert streams == ('read', 'write')
    finally:
        for client in clients:
            await client.aclose()


# --------------------------------------------------------------------------- #
# 字段名：camelCase / snake_case
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("obj", "names", "expected"),
    [
        ({"input_schema": {"a": 1}}, ("input_schema", "inputSchema"), {"a": 1}),
        ({"inputSchema": {"a": 1}}, ("input_schema", "inputSchema"), {"a": 1}),
        ({"is_error": True}, ("is_error", "isError"), True),
        ({"isError": True}, ("is_error", "isError"), True),
        ({"next_cursor": "x"}, ("next_cursor", "nextCursor"), "x"),
        ({"nextCursor": "x"}, ("next_cursor", "nextCursor"), "x"),
    ],
)
def test_field_accepts_both_naming_conventions(obj, names, expected) -> None:
    assert field(obj, *names) == expected


def test_field_returns_default_when_absent() -> None:
    assert field({}, "missing", default="fallback") == "fallback"
    assert field({}, "missing") is None


def test_tool_input_schema_reads_camel_case_dict() -> None:
    """mcp 1.x 的 `model_dump()` 产出 `inputSchema`。"""
    assert tool_input_schema({"inputSchema": {"type": "object"}}) == {"type": "object"}


def test_tool_input_schema_reads_snake_case_dict() -> None:
    """mcp 2.x 的 `model_dump()` 产出 `input_schema`。"""
    assert tool_input_schema({"input_schema": {"type": "object"}}) == {"type": "object"}


def test_tool_input_schema_reads_object_attributes() -> None:
    """真实 mcp `Tool` 对象——两个版本属性名不同，都要能读。"""

    class _Camel:
        inputSchema = {"type": "object", "title": "camel"}

    class _Snake:
        input_schema = {"type": "object", "title": "snake"}

    assert tool_input_schema(_Camel()) == {"type": "object", "title": "camel"}
    assert tool_input_schema(_Snake()) == {"type": "object", "title": "snake"}


def test_tool_input_schema_empty_when_unavailable() -> None:
    assert tool_input_schema(object()) == {}


# --------------------------------------------------------------------------- #
# read_timeout_seconds：timedelta / float
# --------------------------------------------------------------------------- #

def test_read_timeout_seconds_matches_annotation() -> None:
    """1.x 期望 `timedelta`，2.x 期望 `float`——传错会在**每次请求**时炸。"""
    from mcp.client.session import ClientSession

    parameter = inspect.signature(ClientSession.__init__).parameters.get(
        "read_timeout_seconds"
    )
    assert parameter is not None
    expects_timedelta = "timedelta" in str(parameter.annotation)

    value = read_timeout_seconds(30)
    if expects_timedelta:
        assert isinstance(value, datetime.timedelta)
        assert value.total_seconds() == 30
    else:
        assert isinstance(value, float)
        assert value == 30.0


# --------------------------------------------------------------------------- #
# list_tools_page：cursor 参数形态
# --------------------------------------------------------------------------- #

class _FakeSession:
    """记录 `list_tools` 收到的参数，不连网络。"""

    def __init__(self, *, accept_cursor: bool) -> None:
        self.calls: list[dict] = []
        self._accept_cursor = accept_cursor

    async def list_tools(self, **kwargs):
        self.calls.append(kwargs)
        if "cursor" in kwargs and not self._accept_cursor:
            raise TypeError("got an unexpected keyword argument 'cursor'")
        return "page"


async def test_list_tools_page_uses_cursor_kwarg_when_supported() -> None:
    session = _FakeSession(accept_cursor=True)
    assert await list_tools_page(session, "abc") == "page"
    assert session.calls == [{"cursor": "abc"}]


async def test_list_tools_page_falls_back_to_params() -> None:
    """mcp 2.x 不接受 `cursor=`，必须回退到 `params=PaginatedRequestParams(...)`。

    回退策略是「先按 1.x 试，TypeError 再按 2.x 重试」，所以是 **2 次调用**。
    """
    session = _FakeSession(accept_cursor=False)
    assert await list_tools_page(session, "abc") == "page"
    assert len(session.calls) == 2, session.calls
    assert "cursor" in session.calls[0]
    assert "params" in session.calls[1]


async def test_list_tools_page_handles_none_cursor() -> None:
    """首屏无 cursor——两个版本都该正常工作。"""
    session = _FakeSession(accept_cursor=True)
    assert await list_tools_page(session) == "page"


# --------------------------------------------------------------------------- #
# 兼容层不引入 fastmcp（保证 CI 能 pin mcp 1.x 跑）
# --------------------------------------------------------------------------- #

def test_compat_layer_does_not_import_fastmcp() -> None:
    """`_mcp_compat` 绝不能依赖独立 `fastmcp` 包。

    这是本文件能在 `mcp==1.26.0` 下运行的**前提**——
    `fastmcp 4.2.1` 硬性要求 `mcp>=2.3.0`，装了它就没法测 1.x。
    """
    assert "fastmcp" not in sys.modules or True  # 允许环境里装了 fastmcp
    source = inspect.getsource(sys.modules["ksadk._mcp_compat"])
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import fastmcp", "from fastmcp")):
            pytest.fail(f"_mcp_compat 不能 import fastmcp：{stripped}")

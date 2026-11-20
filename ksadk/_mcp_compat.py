"""``mcp`` SDK 1.x / 2.x 的 Server 类兼容导入。

## 为什么需要

`mcp` **2.3.0** 把 ``FastMCP`` 改名成 ``MCPServer``，挪到
``mcp.server.mcpserver``，并**删除**了 ``mcp.server.fastmcp``。

平台镜像和用户环境里可能装着任一版本（ksadk 的 ``mcp`` 约束是
``>=2.3.0``，但下游可能锁旧版）。硬编码一条路径会在另一个版本上
``ModuleNotFoundError``——而 MCP Server 起不来时**只是工具不可用**，
错误信息也指向不到根因，很难定位。

## 用法

```python
from ksadk._mcp_compat import MCPServer
```
"""

from __future__ import annotations

__all__ = [
    "MCPServer",
    "httpx_module",
    "mcp_version",
    "read_timeout_seconds",
    "streamable_http",
    "field",
    "list_tools_page",
    "tool_input_schema",
]

import contextlib
import importlib.metadata as _md


def httpx_module():
    """返回 **mcp 当前使用的那个 httpx 模块**。

    ## 为什么要这个

    `mcp` 2.x 换成了独立的 `httpx2` 包（**不是** `httpx` 的子类）。
    任何给 `streamable_http_client(http_client=...)` 传客户端的地方都必须
    用**同一套** httpx，否则类型不匹配——实测表现是**静默挂起**（不是报错），
    排查成本极高。

    ## 为什么不能按函数名判断

    实测（2026-11-19）**mcp 1.26 同时导出了 `streamablehttp_client`（旧）
    和 `streamable_http_client`（新）**，所以「能不能 import 新名字」
    **不能**区分版本——1.26 会被误判成 2.x，进而要求不存在的 `httpx2`。

    改成看 `http_client` 参数的**类型注解**：

    | mcp | 注解 |
    |---|---|
    | 1.26 | ``httpx.AsyncClient`` |
    | 2.3  | ``httpx2.AsyncClient`` |
    """
    import inspect

    try:
        from mcp.client.streamable_http import streamable_http_client
    except ImportError:  # pragma: no cover - 极旧版本
        import httpx

        return httpx

    parameter = inspect.signature(streamable_http_client).parameters.get("http_client")
    annotation = str(getattr(parameter, "annotation", ""))
    if "httpx2" in annotation:
        import httpx2

        return httpx2

    import httpx

    return httpx


def field(obj: object, *names: str, default=None):
    """按顺序取第一个非 None 的属性/键，兼容 mcp 1.x 的 camelCase 与 2.x 的 snake_case。

    ## 为什么需要

    `mcp` 2.x 把**所有** Pydantic 模型字段从 camelCase 改成 snake_case：

    | 1.x | 2.x |
    |---|---|
    | ``result.isError`` | ``result.is_error`` |
    | ``result.structuredContent`` | ``result.structured_content`` |
    | ``page.nextCursor`` | ``page.next_cursor`` |
    | ``tool.inputSchema`` | ``tool.input_schema`` |

    这些**只在运行时炸**（Pydantic 不做静态检查），且错误信息（`AttributeError`）
    指向不到根因。

    ## 用法

    ```python
    field(result, "is_error", "isError", default=False)
    ```
    """
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    if isinstance(obj, dict):
        for name in names:
            value = obj.get(name)
            if value is not None:
                return value
    return default


async def list_tools_page(session, cursor: str | None = None):
    """分页列工具，兼容 1.x / 2.x 的分页参数形态。

    ## 为什么要这个

    | | 签名 |
    |---|---|
    | mcp 1.x | ``list_tools(cursor=None)`` |
    | mcp 2.x | ``list_tools(*, params=PaginatedRequestParams(cursor=...))`` |

    传错直接 ``TypeError: got an unexpected keyword argument 'cursor'``，
    而且被隧道兜底成 ``local_service_error``，看不到根因。
    """
    try:
        return await session.list_tools(cursor=cursor)
    except TypeError:
        from mcp.types import PaginatedRequestParams

        params = PaginatedRequestParams(cursor=cursor) if cursor else None
        return await session.list_tools(params=params)


def read_timeout_seconds(seconds: float):
    """把「秒数」转成 mcp 期望的 ``read_timeout_seconds`` 类型。

    ## 为什么要这个

    `ClientSession.read_timeout_seconds` 的类型**在 2.x 变了**：

    | | 类型 |
    |---|---|
    | mcp 1.x | ``datetime.timedelta`` |
    | mcp 2.x | ``float``（秒） |

    传错会在**每次请求**时炸在 mcp 内部：

        anyio.current_time() + self._budget
        TypeError: unsupported operand type(s) for +: 'float' and 'datetime.timedelta'

    注意这个错误**不在我们的栈里**（在 `mcp/shared/_request_clock.py`），
    不看完整 traceback 根本定位不到。

    ## 判定方式

    用**注解**而不是版本号——`inspect.signature` 拿到的类型是权威的，
    不受预发布版本字符串格式影响。
    """
    import inspect

    from mcp.client.session import ClientSession

    parameter = inspect.signature(ClientSession.__init__).parameters.get(
        "read_timeout_seconds"
    )
    annotation = str(getattr(parameter, "annotation", ""))

    if "timedelta" in annotation:
        from datetime import timedelta

        return timedelta(seconds=float(seconds))
    return float(seconds)


def tool_input_schema(tool: object) -> dict:
    """取 MCP 工具的 input schema，兼容 1.x / 2.x 的字段名。

    ## 为什么要这个

    `mcp` 2.x 把 Pydantic 模型字段从 **camelCase 改成 snake_case**：

    | | 1.x | 2.x |
    |---|---|---|
    | 属性 | ``tool.inputSchema`` | ``tool.input_schema`` |
    | ``model_dump()`` 键 | ``"inputSchema"`` | ``"input_schema"`` |

    直接取属性会 `AttributeError`，直接取 dict 键会 `KeyError`，
    而这两个都是**运行时**才炸（Pydantic 不做静态检查），只能在测试里发现。
    """
    for name in ("input_schema", "inputSchema"):
        value = getattr(tool, name, None)
        if value is not None:
            return dict(value)
    if isinstance(tool, dict):
        for name in ("input_schema", "inputSchema"):
            value = tool.get(name)
            if value is not None:
                return dict(value)
    return {}


def mcp_version() -> str:
    """当前安装的 ``mcp`` 版本（诊断用）。"""
    try:
        return _md.version("mcp")
    except _md.PackageNotFoundError:  # pragma: no cover
        return "unknown"


try:  # mcp >= 2.3
    from mcp.server.mcpserver import MCPServer
except ImportError:  # pragma: no cover - 兼容旧版
    try:  # mcp 2.0 ~ 2.2 的内部路径
        from mcp.server.mcpserver.server import MCPServer  # type: ignore[no-redef]
    except ImportError:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[no-redef]


def streamable_http(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    http_client: object | None = None,
    httpx_client_factory: object | None = None,
    timeout: float | None = None,
):
    """兼容 mcp 1.x / 2.x 的 Streamable HTTP 传输。

    ## 为什么要包

    两个版本的**函数名和签名都不同**：

    | | 1.x | 2.x |
    |---|---|---|
    | 函数名 | ``streamablehttp_client`` | ``streamable_http_client`` |
    | 请求头 | ``headers={...}`` | 无此参数，要用 ``http_client=httpx2.AsyncClient(headers=...)`` |
    | 返回值 | ``(read, write, get_session_id)`` | ``TransportStreams`` |

    ## 用法

    统一成 **``(read, write)`` 二元组**，两个版本写法一致：

    ```python
    async with streamable_http(url, {"Authorization": "..."}) as (read, write):
        async with ClientSession(read, write) as session:
            ...
    ```
    """
    if not url:
        raise ValueError("url 不能为空")

    try:  # mcp >= 2.3
        from mcp.client.streamable_http import streamable_http_client
    except ImportError:  # pragma: no cover - mcp 1.x
        from mcp.client.streamable_http import streamablehttp_client

        return _normalize(
            streamablehttp_client(
                url,
                headers=headers,
                httpx_client_factory=httpx_client_factory,  # type: ignore[arg-type]
                timeout=timeout if timeout is not None else 30,
            )
        )

    # mcp 2.x 没有 `headers` / `httpx_client_factory` / `timeout` 参数，
    # 统一收敛到「传一个配好的 http_client」。
    client = http_client
    if client is None and httpx_client_factory is not None:
        # 调用方按 1.x 习惯传工厂——直接调用拿到 client
        client = httpx_client_factory(headers=headers, timeout=timeout)  # type: ignore[operator]
    if client is None:
        # mcp 1.26 also exports this spelling, but still uses httpx.
        client = httpx_module().AsyncClient(
            headers=headers or {},
            **({"timeout": timeout} if timeout is not None else {}),
        )

    return _normalize(streamable_http_client(url, http_client=client))  # type: ignore[arg-type]


@contextlib.asynccontextmanager
async def _normalize(streams):
    """把传输流统一裁成 ``(read, write)`` 二元组。

    mcp 2.x 的 ``TransportStreams`` 在不同小版本里**既可能是具名对象**
    （``.read`` / ``.write``），**也可能是普通元组**——实测 2.3.0 是
    ``tuple[ReadStream, WriteStream]``。统一裁成二元组，调用方写法固定。
    """
    if hasattr(streams, "__aenter__"):
        async with streams as inner:
            if hasattr(inner, "read"):
                yield inner.read, inner.write
            else:
                # mcp 1.x 返回 3 元组 ``(read, write, get_session_id)``
                yield inner[0], inner[1]
    elif hasattr(streams, "read"):
        yield streams.read, streams.write
    else:
        # mcp 2.x 返回 2 元组；1.x 返回 3 元组——都只取前两个
        yield streams[0], streams[1]

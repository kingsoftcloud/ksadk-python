"""Google ADK 多版本兼容层 (goal-00)。

ksadk 业务代码**不直接** ``import google.adk``;统一从本模块取符号。
``google-adk`` 的版本差异只在兼容层内消化,业务代码对版本无感知。

版本窗口
--------
依赖约束 ``google-adk>=1.34.0,<3.0.0``:

- **1.34.x** 是最低支持锚点(不承诺 1.x 全系;1.0 缺 ``google.adk.apps`` 等,代价过大)。
- **2.x**(当前 2.5.x)是上界内最新主线。

实测(2026-07-21, goal-00 探测,解包对比 1.34.3 vs 2.5.0):ksadk 用到的
全部符号在两个版本**都存在**,差异仅为 2.x 的**向后兼容可选新增**:

- ``Runner.run_async`` 2.x 新增可选 ``yield_user_message``(默认 False)。
- ``RunConfig`` 2.x 新增若干可选字段(``http_options`` / ``telemetry`` /
  ``model_input_context`` 等),均为可选,不传不影响。
- ``App.root_agent`` 2.x 变为可选(1.34 必填);ksadk 始终显式传,两版皆可。
- ``DatabaseSessionService`` 2.x 新增可选 ``db_engine``(``db_url`` 变可选);
  ksadk 始终传 ``db_url``,两版皆可。
- ``McpToolset.header_provider`` 2.x 支持 awaitable;ksadk 未用该参数。

ksadk 现有调用**不依赖任何 2.x-only 参数**,因此 1.34 与 2.x 对 ksadk 能力对等,
无需降级 shim。能力降级矩阵的完整文字版见
``docs/adk-multi-version-compat.md``。

使用 2.x-only 新能力的纪律
--------------------------
若未来要用某个 2.x 才有的可选参数/符号,**必须**在本层用
:func:`adk_version_at_least` 判断后再下发,并对 1.34 做显式降级;不允许业务
代码自行 ``import google.adk`` 探测。

加载语义
--------
本模块 import 自身**不触发** ``google.adk`` 导入(惰性,PEP 562
``__getattr__``)。只有真正访问某个符号时才导入对应 ``google.adk`` 子模块;
未安装 ``google-adk`` 时抛出带安装提示的 :class:`ImportError`,便于
``memory/adk_tool`` 等调用方捕获并降级为原函数。
"""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - 仅供 mypy 静态检查,运行时不执行
    pass

#: 导出符号名 -> 提供该符号的 google.adk 子模块路径。
#: 这是 ksadk 全部 google.adk 依赖的唯一登记表;新增依赖先在这里登记。
_LAZY_SYMBOLS: dict[str, str] = {
    # agents
    "Agent": "google.adk.agents",
    "RunConfig": "google.adk.agents.run_config",
    "StreamingMode": "google.adk.agents.run_config",
    # apps
    "App": "google.adk.apps",
    "ResumabilityConfig": "google.adk.apps",
    # events
    "Event": "google.adk.events.event",
    # memory
    "BaseMemoryService": "google.adk.memory.base_memory_service",
    "SearchMemoryResponse": "google.adk.memory.base_memory_service",
    "MemoryEntry": "google.adk.memory.memory_entry",
    # models
    "LlmResponse": "google.adk.models",
    "LiteLlm": "google.adk.models.lite_llm",
    # runners
    "Runner": "google.adk.runners",
    # sessions
    "BaseSessionService": "google.adk.sessions",
    "InMemorySessionService": "google.adk.sessions",
    "Session": "google.adk.sessions",
    "DatabaseSessionService": "google.adk.sessions",
    "InvocationContext": "google.adk.agents",
    "ToolContext": "google.adk.tools",
    "GetSessionConfig": "google.adk.sessions.base_session_service",
    "ListSessionsResponse": "google.adk.sessions.base_session_service",
    # tools
    "FunctionTool": "google.adk.tools",
    "load_memory": "google.adk.tools",
    "McpTool": "google.adk.tools.mcp_tool.mcp_tool",
    "McpToolset": "google.adk.tools.mcp_tool.mcp_toolset",
    "CheckableMcpHttpClientFactory": "google.adk.tools.mcp_tool.mcp_session_manager",
    "StreamableHTTPConnectionParams": "google.adk.tools.mcp_tool.mcp_session_manager",
}

#: 以模块形式导出的名字 -> 模块路径(如 genai types、需要 monkeypatch 的 lite_llm)。
_LAZY_MODULES: dict[str, str] = {
    "genai_types": "google.genai.types",
}

_INSTALL_HINT = (
    "google-adk 未安装或版本不在支持窗口 (>=1.34.0,<3.0.0)。" "安装: pip install 'ksadk[adk]'"
)

_ADK_NOT_FOUND = "google-adk 未安装"


def _raise_missing(name: str, exc: BaseException) -> None:
    raise ImportError(f"无法导入 google.adk 符号 {name!r}。{_INSTALL_HINT}") from exc


def __getattr__(name: str) -> Any:
    """PEP 562 惰性解析:首次访问符号时才导入对应 google.adk 子模块。"""
    if name in _LAZY_SYMBOLS:
        module_path = _LAZY_SYMBOLS[name]
        try:
            module = import_module(module_path)
        except ImportError as exc:
            _raise_missing(name, exc)
        try:
            value = getattr(module, name)
        except AttributeError as exc:
            raise ImportError(
                f"google.adk 已安装但 {module_path} 缺少 {name!r};"
                f"当前版本 {adk_version() or '未知'} 可能不在支持窗口 (>=1.34.0,<3.0.0)。"
            ) from exc
        # 注意: 刻意不缓存到 globals()。测试常 monkeypatch ``google.adk.*``
        # 子模块符号(如把 Runner 换成 FakeRunner);若在此缓存,会把某次 patch
        # 后的值泄漏给后续解析,造成跨测试污染。每次经 getattr 重新解析,
        # 让既有 ``google.adk.*`` patch 点继续生效。开销可忽略(import_module
        # 命中 sys.modules 缓存)。
        return value
    if name in _LAZY_MODULES:
        module_path = _LAZY_MODULES[name]
        try:
            value = import_module(module_path)
        except ImportError as exc:
            _raise_missing(name, exc)
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_SYMBOLS) | set(_LAZY_MODULES))


def is_adk_available() -> bool:
    """google-adk 是否可导入(不关心版本)。"""
    try:
        import_module("google.adk")
    except ImportError:
        return False
    return True


def adk_version() -> str | None:
    """返回已安装的 google-adk 版本;未安装返回 None。"""
    try:
        return _dist_version("google-adk")
    except PackageNotFoundError:
        return None


def adk_version_at_least(min_version: str) -> bool:
    """已安装 google-adk 版本是否 >= ``min_version``;未安装或无法解析返回 False。

    用于下发 2.x-only 能力前的版本门禁,例如::

        if adk_version_at_least("2.0.0"):
            kwargs["yield_user_message"] = True
    """
    ver = adk_version()
    if ver is None:
        return False
    try:
        from packaging.version import Version

        return bool(Version(ver) >= Version(min_version))
    except Exception:
        return False


def lite_llm_module() -> Any:
    """返回 ``google.adk.models.lite_llm`` 模块对象。

    adk_runner 的 JSON 容错 patch 需要 MonkeyPatch 该模块内的
    ``_message_to_generate_content_response``(私有函数),因此需要模块本身
    而非单个符号。未安装 litellm/google-adk 时抛 :class:`ImportError`。
    """
    try:
        return import_module("google.adk.models.lite_llm")
    except ImportError as exc:
        _raise_missing("google.adk.models.lite_llm", exc)


__all__ = [
    # 版本/能力探测
    "is_adk_available",
    "adk_version",
    "adk_version_at_least",
    "lite_llm_module",
    # 惰性符号
    *_LAZY_SYMBOLS.keys(),
    *_LAZY_MODULES.keys(),
]

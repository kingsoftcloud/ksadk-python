"""env 型框架接线(v2.2):按需懒起 ProxyServer 并重定向 OPENAI_BASE_URL。

review v1 约束:setup_environment 在框架检测之前执行,无法判断 runtime。
故本模块用 :class:`ProxyGate`(模型白名单,不依赖 runtime 判断)控制:
gate.is_on(model) 为真时起一个进程内 ProxyServer,把 OPENAI_BASE_URL/
OPENAI_API_BASE 重定向到它;env 框架(ADK/langchain/deepagents 发 chat)经代理
的 /v1/chat/completions 直通上游(字节级,缓存不受影响),codex 走 v2.1 不经此。

默认 gate 关(ProxyGate.enabled=False),不重定向,历史路径零影响。
"""

from __future__ import annotations

import os
from typing import Optional

from .config import ProxyConfig
from .gate import ProxyGate
from .server import ProxyServer

# 进程级单例:setup_environment 多次调用只起一个 proxy
_proxy: Optional[ProxyServer] = None
_original_base: Optional[str] = None
_original_base_env: Optional[dict[str, str | None]] = None


def setup_proxy_redirect_if_enabled(
    gate: ProxyGate | None = None,
    model: str | None = None,
    upstream_base: str | None = None,
    api_key: str | None = None,
    local_token: str | None = None,
) -> str | None:
    """gate 开启且模型在白名单时起 ProxyServer 并重定向 OPENAI_BASE_URL。

    返回 proxy base_url(已重定向)或 None(未启用)。多次调用幂等(单例)。
    上游凭证来自 ProxyConfig(从 env 读),不下发给子进程(凭证闭合)。
    """
    global _proxy, _original_base, _original_base_env
    if _proxy is not None:
        return _proxy.base_url  # 幂等:已起
    gate = gate or ProxyGate.from_env()
    model = model or os.environ.get("OPENAI_MODEL_NAME") or os.environ.get("MODEL_NAME") or ""
    if not gate.is_on(model=model or None):
        return None
    upstream = (
        upstream_base
        or os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("OPENAI_API_BASE")
        or ""
    )
    key = api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY") or ""
    token = local_token or os.environ.get("KSADK_PROXY_TOKEN") or ""
    if not upstream or not key:
        return None  # 缺凭证/上游,不启用(保持原 env)
    cfg = ProxyConfig(upstream_base=upstream, api_key=key, local_token=token)
    srv = ProxyServer(cfg)
    srv.start()
    _proxy = srv
    # 记录原 base 并重定向(双别名都指向 proxy)
    _original_base_env = {
        "OPENAI_BASE_URL": os.environ.get("OPENAI_BASE_URL"),
        "OPENAI_API_BASE": os.environ.get("OPENAI_API_BASE"),
    }
    _original_base = _original_base_env["OPENAI_BASE_URL"] or _original_base_env["OPENAI_API_BASE"]
    os.environ["OPENAI_BASE_URL"] = srv.base_url
    os.environ["OPENAI_API_BASE"] = srv.base_url
    if not token:
        # 非 codex 路径(env 框架)无 token:代理 local_token 为空,仅回环监听才允许
        os.environ["KSADK_PROXY_TOKEN"] = ""
    return srv.base_url


def teardown_proxy_redirect() -> None:
    """回收 proxy 并恢复原 OPENAI_BASE_URL(进程退出/卸载时调)。"""
    global _proxy, _original_base, _original_base_env
    if _proxy is not None:
        _proxy.stop()
        _proxy = None
    if _original_base_env is not None:
        for key, value in _original_base_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _original_base_env = None
        _original_base = None

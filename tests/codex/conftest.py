"""tests/codex 环境归一化（与宿主 .env 解耦）。

宿主的 ``OPENAI_BASE_URL``（来自仓库根 .env 的自定义上游）在
``AsyncCodexClient`` 的 ``runtime_env = {**os.environ, **cfg.env}`` 合并中
优先于测试通过 ``cfg.env["OPENAI_API_BASE"]`` 注入的本地上游，会把本应
确定性的 proxy/transport 用例劫持到真实网络。本目录的测试要么用官方直连
假设、要么在 ``cfg.env`` 显式声明上游——因此统一清掉 os.environ 层的
上游路由变量；需要自定义上游的用例请写入 ``cfg.env``。
"""

from __future__ import annotations

import pytest

_HOST_ENV_ROUTES = (
    "KSADK_PROXY_UPSTREAM_BASE",
    "KSADK_PROXY_UPSTREAM_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
)


@pytest.fixture(autouse=True)
def _isolate_codex_upstream_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _HOST_ENV_ROUTES:
        monkeypatch.delenv(name, raising=False)

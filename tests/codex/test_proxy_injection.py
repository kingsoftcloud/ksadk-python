"""AsyncCodexClient 协议代理 opt-in 注入测试(v2.1 tracer bullet)。

不起真 codex 子进程:直接测 staticmethod ``_maybe_apply_proxy`` 的 config 构造、
合并保留、互斥与默认关闭;close 的 proxy 回收用 stub 验证。
"""

import asyncio

import pytest

codex_sdk = pytest.importorskip("openai_codex")  # 缺 ksadk[codex] 时跳过整个文件

from ksadk.codex.client import AsyncCodexClient  # noqa: E402  (importorskip 守卫后)
from ksadk.model_proxy.detect import ModelCapabilities  # noqa: E402


def test_proxy_default_off(monkeypatch):
    """未设 KSADK_CODEX_USE_PROXY 时原样返回,不注入(历史路径与基线测试不受影响)。"""
    from openai_codex import CodexConfig

    monkeypatch.delenv("KSADK_CODEX_USE_PROXY", raising=False)
    cfg = CodexConfig(codex_bin="/x")
    out, proxy = AsyncCodexClient._maybe_apply_proxy(cfg)
    assert proxy is None
    assert out is cfg  # 原对象原样


def test_proxy_overrides_injected_and_merged(monkeypatch):
    """opt-in 时注入 provider/禁 websocket/禁 multi_agent×2;合并保留原 config。"""
    from openai_codex import CodexConfig

    monkeypatch.setenv("KSADK_CODEX_USE_PROXY", "1")
    monkeypatch.setenv("OPENAI_API_BASE", "https://kspmas.ksyun.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg = CodexConfig(
        codex_bin="/x",
        cwd="/tmp",
        config_overrides=("existing=k",),
        env={
            "FOO": "bar",
            "OPENAI_API_KEY": "config-upstream-secret",
            "AGENTKIT_MODEL_API_KEY": "alias-upstream-secret",
            "KSADK_PROXY_UPSTREAM_KEY": "proxy-upstream-secret",
        },
    )
    out, proxy = AsyncCodexClient._maybe_apply_proxy(cfg)
    try:
        ov = list(out.config_overrides)
        # 精确 override 断言(review 要求的可复核产物)
        assert "model_provider=ksadk_proxy" in ov
        assert "model_providers.ksadk_proxy.env_key=KSADK_PROXY_TOKEN" in ov
        assert "model_providers.ksadk_proxy.wire_api=responses" in ov
        assert "model_providers.ksadk_proxy.supports_websockets=false" in ov
        assert "web_search=disabled" in ov
        assert "features.multi_agent=false" in ov
        assert "features.multi_agent_v2=false" in ov
        assert any("model_providers.ksadk_proxy.base_url=http://127.0.0.1:" in o for o in ov)
        # 合并不丢:原 override/字段/env 都在
        assert "existing=k" in ov
        assert out.codex_bin == "/x" and out.cwd == "/tmp"
        assert out.env["FOO"] == "bar"
        assert out.env["KSADK_PROXY_TOKEN"]  # 随机 token 下发子进程
        assert "OPENAI_API_KEY" not in out.env
        assert "AGENTKIT_MODEL_API_KEY" not in out.env
        assert "KSADK_PROXY_UPSTREAM_KEY" not in out.env
    finally:
        proxy.stop()


def test_proxy_launch_args_override_mutex(monkeypatch):
    """opt-in 与 launch_args_override 互斥:后者整体覆盖命令行,config_overrides 失效。"""
    from openai_codex import CodexConfig

    monkeypatch.setenv("KSADK_CODEX_USE_PROXY", "1")
    cfg = CodexConfig(launch_args_override=("codex", "app-server"))
    with pytest.raises(RuntimeError, match="互斥"):
        AsyncCodexClient._maybe_apply_proxy(cfg)


# ---- 智能探测 fallback(未设 env 时) ----


def test_capability_probe_requires_proxy_without_namespace(monkeypatch):
    from ksadk.codex import client as client_module

    client_module._CAPABILITY_CACHE.clear()
    monkeypatch.setattr(
        client_module,
        "probe_responses_capability",
        lambda *_args, **_kwargs: ModelCapabilities(
            responses_supported=True,
            tool_types=set(),
            preferred_protocol="chat",
            verdict="supported",
        ),
    )

    assert client_module._probe_requires_proxy(
        "responses-without-namespace",
        "https://models.example/v1",
        "key-a",
    )


def test_capability_probe_allows_direct_with_namespace(monkeypatch):
    from ksadk.codex import client as client_module

    client_module._CAPABILITY_CACHE.clear()
    monkeypatch.setattr(
        client_module,
        "probe_responses_capability",
        lambda *_args, **_kwargs: ModelCapabilities(
            responses_supported=True,
            tool_types={"namespace"},
            preferred_protocol="responses",
            verdict="supported",
        ),
    )

    assert not client_module._probe_requires_proxy(
        "responses-with-namespace",
        "https://models.example/v1",
        "key-b",
    )


def test_capability_probe_requires_proxy_when_namespace_is_unknown(monkeypatch):
    from ksadk.codex import client as client_module

    client_module._CAPABILITY_CACHE.clear()
    monkeypatch.setattr(
        client_module,
        "probe_responses_capability",
        lambda *_args, **_kwargs: ModelCapabilities(verdict="unknown"),
    )

    assert client_module._probe_requires_proxy(
        "responses-namespace-unknown",
        "https://models.example/v1",
        "key-c",
    )


def test_probe_openai_official_direct_no_probe(monkeypatch):
    """OpenAI 官方 base_url:直连,不探测(探测函数不应被调)。"""
    from openai_codex import CodexConfig

    monkeypatch.delenv("KSADK_CODEX_USE_PROXY", raising=False)
    monkeypatch.setenv("OPENAI_API_BASE", "https://api.openai.com/v1")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    def _forbid(*a, **k):
        raise AssertionError("OpenAI 官方不应探测")

    monkeypatch.setattr("ksadk.codex.client._probe_requires_proxy", _forbid)
    out, proxy = AsyncCodexClient._maybe_apply_proxy(CodexConfig())
    assert proxy is None and out is not None


def test_probe_unsupported_enables_proxy(monkeypatch):
    """自定义上游探测 unsupported(chat 模型):自动启用代理。"""
    from openai_codex import CodexConfig

    monkeypatch.delenv("KSADK_CODEX_USE_PROXY", raising=False)
    monkeypatch.setenv("OPENAI_API_BASE", "https://kspmas.ksyun.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-t")
    monkeypatch.setattr("ksadk.codex.client._probe_requires_proxy", lambda *a, **k: True)
    out, proxy = AsyncCodexClient._maybe_apply_proxy(CodexConfig())
    try:
        assert proxy is not None
        assert "model_provider=ksadk_proxy" in list(out.config_overrides)
    finally:
        proxy.stop()


def test_probe_uses_launch_config_environment(monkeypatch):
    """Break caught: Studio injects endpoint/key into CodexConfig but proxy ignores it."""
    from openai_codex import CodexConfig

    monkeypatch.delenv("KSADK_CODEX_USE_PROXY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    observed = {}

    def probe(model, base, key):
        observed.update(model=model, base=base, key=key)
        return False

    monkeypatch.setattr("ksadk.codex.client._probe_requires_proxy", probe)
    out, proxy = AsyncCodexClient._maybe_apply_proxy(
        CodexConfig(
            env={
                "OPENAI_BASE_URL": "https://model.example.com/v1",
                "OPENAI_API_KEY": "workspace-model-key",
                "OPENAI_MODEL_NAME": "glm-5.2",
            }
        )
    )

    assert proxy is None
    assert out is not None
    assert observed == {
        "model": "glm-5.2",
        "base": "https://model.example.com/v1",
        "key": "workspace-model-key",
    }


def test_probe_uses_the_same_https_url_as_managed_runtime_execution(monkeypatch):
    """An HTTP redirect must not hide an HTTPS /responses 404 from detection."""
    from openai_codex import CodexConfig

    monkeypatch.delenv("KSADK_CODEX_USE_PROXY", raising=False)
    observed = {}

    def probe(model, base, key):
        observed.update(model=model, base=base, key=key)
        return False

    monkeypatch.setattr("ksadk.codex.client._probe_requires_proxy", probe)
    out, proxy = AsyncCodexClient._maybe_apply_proxy(
        CodexConfig(
            env={
                "OPENAI_BASE_URL": "http://kspmas-internal.sdns.ksyun.com/v1",
                "OPENAI_API_KEY": "workspace-model-key",
                "OPENAI_MODEL_NAME": "qwen3.7-flash",
            }
        )
    )

    assert proxy is None
    assert out is not None
    assert observed == {
        "model": "qwen3.7-flash",
        "base": "https://kspmas-internal.sdns.ksyun.com/v1",
        "key": "workspace-model-key",
    }


def test_probe_supported_direct(monkeypatch):
    """自定义上游探测 supported:直连。"""
    from openai_codex import CodexConfig

    monkeypatch.delenv("KSADK_CODEX_USE_PROXY", raising=False)
    monkeypatch.setenv("OPENAI_API_BASE", "https://kspmas.ksyun.com/v1")
    monkeypatch.setattr("ksadk.codex.client._probe_requires_proxy", lambda *a, **k: False)
    out, proxy = AsyncCodexClient._maybe_apply_proxy(CodexConfig())
    assert proxy is None


def test_env_zero_forces_direct(monkeypatch):
    """env=0:强制直连(人工覆盖,即使自定义上游)。"""
    from openai_codex import CodexConfig

    monkeypatch.setenv("KSADK_CODEX_USE_PROXY", "0")
    monkeypatch.setenv("OPENAI_API_BASE", "https://kspmas.ksyun.com/v1")
    monkeypatch.setattr(
        "ksadk.codex.client._probe_requires_proxy",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("env=0 不应探测")),
    )
    out, proxy = AsyncCodexClient._maybe_apply_proxy(CodexConfig())
    assert proxy is None


def test_close_stops_proxy():
    """close() 必须回收 proxy(覆盖重复关闭:第二次 close 不报错)。"""

    class _FakeProxy:
        def __init__(self):
            self.stopped = 0

        def stop(self):
            self.stopped += 1

    class _FakeCodex:
        async def close(self):
            pass

    # 跳过 __init__(不起真 codex),手动装配 close 所需状态
    c = AsyncCodexClient.__new__(AsyncCodexClient)
    proxy = _FakeProxy()
    c._codex = _FakeCodex()
    c._proxy = proxy
    c._threads = {}
    c._active_handles = {}

    asyncio.run(c.close())
    assert proxy.stopped == 1
    assert c._proxy is None
    # 重复 close:proxy 已 None,不再 stop,不报错
    asyncio.run(c.close())
    assert proxy.stopped == 1


def test_proxy_observer_is_forwarded_to_proxy_config(monkeypatch):
    from openai_codex import CodexConfig

    monkeypatch.setenv("KSADK_CODEX_USE_PROXY", "1")
    observed = []

    def observer(event, data):
        observed.append((event, data))

    _config, proxy = AsyncCodexClient._maybe_apply_proxy(
        CodexConfig(),
        proxy_observer=observer,
    )
    try:
        assert proxy.config.event_callback is observer
    finally:
        proxy.stop()


def test_probe_supported_custom_base_injects_direct_provider(monkeypatch):
    """P1：探测 supported（直连）分支也要把自定义 OPENAI_API_BASE 配成 provider。

    否则 codex 子进程回落到默认 OpenAI 官方端点，自定义上游静默失效。
    """
    from openai_codex import CodexConfig

    monkeypatch.delenv("KSADK_CODEX_USE_PROXY", raising=False)
    monkeypatch.setenv("OPENAI_API_BASE", "http://kspmas.ksyun.com/v1")
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-5.3")
    monkeypatch.setattr(
        "ksadk.codex.client._probe_requires_proxy",
        lambda *a, **k: False,  # supported → 直连
    )
    out, proxy = AsyncCodexClient._maybe_apply_proxy(CodexConfig(codex_bin="/x"))
    assert proxy is None
    ov = list(out.config_overrides)
    assert "model_provider=ksadk_direct" in ov
    assert (
        "model_providers.ksadk_direct.base_url=https://kspmas.ksyun.com/v1" in ov
    )
    assert "model_providers.ksadk_direct.env_key=OPENAI_API_KEY" in ov
    assert "model_providers.ksadk_direct.wire_api=responses" in ov
    assert "model_providers.ksadk_direct.supports_websockets=false" in ov


def test_probe_unknown_custom_base_also_injects_direct_provider(monkeypatch):
    """unknown（探测故障）保守直连：同样要注入自定义 provider。"""
    from openai_codex import CodexConfig

    monkeypatch.delenv("KSADK_CODEX_USE_PROXY", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://x.example.com/v1")
    monkeypatch.setattr(
        "ksadk.codex.client._probe_requires_proxy",
        lambda *a, **k: False,
    )
    out, proxy = AsyncCodexClient._maybe_apply_proxy(CodexConfig(codex_bin="/x"))
    assert proxy is None
    assert any(o == "model_provider=ksadk_direct" for o in out.config_overrides)
    assert any(
        o == "model_providers.ksadk_direct.base_url=https://x.example.com/v1"
        for o in out.config_overrides
    )


def test_direct_provider_injection_respects_existing_model_provider(monkeypatch):
    """用户 config_overrides 已显式设 model_provider 时不覆盖。"""
    from openai_codex import CodexConfig

    monkeypatch.delenv("KSADK_CODEX_USE_PROXY", raising=False)
    monkeypatch.setenv("OPENAI_API_BASE", "https://x.example.com/v1")
    monkeypatch.setattr(
        "ksadk.codex.client._probe_requires_proxy",
        lambda *a, **k: False,
    )
    cfg = CodexConfig(
        codex_bin="/x", config_overrides=("model_provider=my_own",)
    )
    out, proxy = AsyncCodexClient._maybe_apply_proxy(cfg)
    assert proxy is None
    assert list(out.config_overrides) == ["model_provider=my_own"]


def test_direct_provider_skipped_for_official_openai_base(monkeypatch):
    """官方 OpenAI base：不注入（codex 默认行为正确）。"""
    from openai_codex import CodexConfig

    monkeypatch.delenv("KSADK_CODEX_USE_PROXY", raising=False)
    monkeypatch.setenv("OPENAI_API_BASE", "https://api.openai.com/v1")
    monkeypatch.setattr(
        "ksadk.codex.client._probe_requires_proxy",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("官方 base 不应探测")),
    )
    cfg = CodexConfig(codex_bin="/x")
    out, proxy = AsyncCodexClient._maybe_apply_proxy(cfg)
    assert proxy is None
    assert out is cfg

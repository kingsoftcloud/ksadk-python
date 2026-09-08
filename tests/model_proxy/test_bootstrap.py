"""env 型框架接线(v2.2)单测:gate 控制 + 幂等 + 恢复。"""

import os

from ksadk.model_proxy import bootstrap


def test_gate_off_no_redirect(monkeypatch):
    monkeypatch.delenv("KSADK_MODEL_PROXY_ENABLED", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://original.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    bootstrap._proxy = None
    bootstrap._original_base = None
    assert bootstrap.setup_proxy_redirect_if_enabled() is None
    assert os.environ["OPENAI_BASE_URL"] == "https://original.example/v1"  # 未改


def test_gate_on_redirects_base(monkeypatch):
    monkeypatch.setenv("KSADK_MODEL_PROXY_ENABLED", "1")
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-5.2")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://original.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    bootstrap._proxy = None
    bootstrap._original_base = None
    try:
        url = bootstrap.setup_proxy_redirect_if_enabled()
        assert url and url.startswith("http://127.0.0.1:")
        # env 已重定向到 proxy
        assert os.environ["OPENAI_BASE_URL"].startswith("http://127.0.0.1:")
        assert os.environ["OPENAI_API_BASE"].startswith("http://127.0.0.1:")
    finally:
        bootstrap.teardown_proxy_redirect()
    # 回收后恢复原 base
    assert os.environ["OPENAI_BASE_URL"] == "https://original.example/v1"
    assert bootstrap._proxy is None


def test_teardown_does_not_leak_missing_base_alias(monkeypatch):
    """Redirect cleanup restores each base alias to its original presence."""

    monkeypatch.setenv("KSADK_MODEL_PROXY_ENABLED", "1")
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-5.2")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://original.example/v1")
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    bootstrap._proxy = None
    bootstrap._original_base = None
    bootstrap._original_base_env = None
    try:
        assert bootstrap.setup_proxy_redirect_if_enabled() is not None
        assert "OPENAI_API_BASE" in os.environ
    finally:
        bootstrap.teardown_proxy_redirect()

    assert os.environ["OPENAI_BASE_URL"] == "https://original.example/v1"
    assert "OPENAI_API_BASE" not in os.environ


def test_idempotent_multiple_calls(monkeypatch):
    monkeypatch.setenv("KSADK_MODEL_PROXY_ENABLED", "1")
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-5.2")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://o.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    bootstrap._proxy = None
    bootstrap._original_base = None
    try:
        u1 = bootstrap.setup_proxy_redirect_if_enabled()
        u2 = bootstrap.setup_proxy_redirect_if_enabled()
        assert u1 == u2  # 单例幂等:第二次直接返回已起的 proxy
    finally:
        bootstrap.teardown_proxy_redirect()


def test_model_whitelist_only(monkeypatch):
    # 全局关,但模型白名单命中 -> 放行重定向
    monkeypatch.delenv("KSADK_MODEL_PROXY_ENABLED", raising=False)
    monkeypatch.setenv("KSADK_MODEL_PROXY_MODELS", "glm-5.2")
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-5.2")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://o.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    bootstrap._proxy = None
    bootstrap._original_base = None
    try:
        assert bootstrap.setup_proxy_redirect_if_enabled() is not None
    finally:
        bootstrap.teardown_proxy_redirect()


def test_denylist_blocks(monkeypatch):
    monkeypatch.setenv("KSADK_MODEL_PROXY_ENABLED", "1")
    monkeypatch.setenv("KSADK_MODEL_PROXY_DENY", "glm-5.2")
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-5.2")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://o.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    bootstrap._proxy = None
    bootstrap._original_base = None
    assert bootstrap.setup_proxy_redirect_if_enabled() is None  # denylist 一键回退

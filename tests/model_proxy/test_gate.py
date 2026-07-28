"""开关与灰度配置单测。"""

from ksadk.model_proxy.gate import ProxyGate


def test_default_off():
    g = ProxyGate()
    assert not g.is_on("any-agent", "any-model")


def test_global_on():
    g = ProxyGate(enabled=True)
    assert g.is_on("a", "m")
    assert g.is_on(None, None)


def test_denylist_overrides_global_on():
    g = ProxyGate(enabled=True, denylist={"bad-agent", "bad-model"})
    assert not g.is_on("bad-agent", "ok-model")
    assert not g.is_on("ok-agent", "bad-model")
    assert g.is_on("ok-agent", "ok-model")


def test_allowlist_grayscale_when_global_off():
    g = ProxyGate(
        agent_allowlist={"alpha-agent"}, model_allowlist={"glm-5.2"}
    )
    assert g.is_on("alpha-agent", None)  # agent 白名单
    assert g.is_on(None, "glm-5.2")  # model 白名单
    assert not g.is_on("other-agent", "other-model")


def test_denylist_overrides_allowlist():
    g = ProxyGate(agent_allowlist={"alpha"}, denylist={"alpha"})
    assert not g.is_on("alpha", None)


def test_from_env(monkeypatch):
    monkeypatch.setenv("KSADK_MODEL_PROXY_ENABLED", "1")
    monkeypatch.setenv("KSADK_MODEL_PROXY_AGENTS", "a, b")
    monkeypatch.setenv("KSADK_MODEL_PROXY_MODELS", "glm-5.2")
    monkeypatch.setenv("KSADK_MODEL_PROXY_DENY", "c")
    g = ProxyGate.from_env()
    assert g.enabled
    assert g.agent_allowlist == {"a", "b"}
    assert g.model_allowlist == {"glm-5.2"}
    assert g.denylist == {"c"}
    assert g.is_on("a", None)
    assert not g.is_on("c", None)

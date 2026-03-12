import re

from ksadk.cli import cmd_openclaw


def test_build_openclaw_env_vars_defaults_to_trusted_proxy(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.delenv("OPENCLAW_GATEWAY_AUTH_MODE", raising=False)
    monkeypatch.delenv("OPENCLAW_TRUSTED_PROXY_USER_HEADER", raising=False)
    monkeypatch.delenv("OPENCLAW_TRUSTED_PROXIES", raising=False)

    env = cmd_openclaw._build_openclaw_env_vars()

    assert env["OPENCLAW_GATEWAY_AUTH_MODE"] == "trusted-proxy"
    assert env["OPENCLAW_TRUSTED_PROXY_USER_HEADER"] == "x-forwarded-user"
    assert env["OPENCLAW_TRUSTED_PROXIES"] == "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,35.0.0.0/8"


def test_build_openclaw_env_vars_forces_trusted_proxy_when_token_configured(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.setenv("OPENCLAW_GATEWAY_AUTH_MODE", "token")

    env = cmd_openclaw._build_openclaw_env_vars()

    assert env["OPENCLAW_GATEWAY_AUTH_MODE"] == "trusted-proxy"
    assert env["OPENCLAW_TRUSTED_PROXY_USER_HEADER"] == "x-forwarded-user"
    assert env["OPENCLAW_TRUSTED_PROXIES"] == "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,35.0.0.0/8"


def test_build_openclaw_env_vars_uses_custom_trusted_proxy_env(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.setenv("OPENCLAW_GATEWAY_AUTH_MODE", "trusted-proxy")
    monkeypatch.setenv("OPENCLAW_TRUSTED_PROXY_USER_HEADER", "x-auth-request-user")
    monkeypatch.setenv("OPENCLAW_TRUSTED_PROXIES", '["10.244.0.0/16","10.96.0.0/12"]')

    env = cmd_openclaw._build_openclaw_env_vars()

    assert env["OPENCLAW_GATEWAY_AUTH_MODE"] == "trusted-proxy"
    assert env["OPENCLAW_TRUSTED_PROXY_USER_HEADER"] == "x-auth-request-user"
    assert env["OPENCLAW_TRUSTED_PROXIES"] == "10.244.0.0/16,10.96.0.0/12"


def test_generate_default_openclaw_name_is_high_entropy():
    name1 = cmd_openclaw._generate_default_openclaw_name()
    name2 = cmd_openclaw._generate_default_openclaw_name()

    assert name1 != name2
    assert len(name1) <= 64
    assert name1.startswith("openclaw-gateway-")
    assert re.fullmatch(r"openclaw-gateway-\d{10}-[0-9a-f]{6}", name1) is not None

import re
import json

from ksadk.cli import cmd_openclaw


def test_build_openclaw_env_vars_defaults_to_trusted_proxy(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.delenv("OPENCLAW_GATEWAY_AUTH_MODE", raising=False)
    monkeypatch.delenv("OPENCLAW_TRUSTED_PROXY_USER_HEADER", raising=False)
    monkeypatch.delenv("OPENCLAW_TRUSTED_PROXIES", raising=False)

    env = cmd_openclaw._build_openclaw_env_vars()

    assert env["OPENCLAW_GATEWAY_AUTH_MODE"] == "trusted-proxy"
    assert env["OPENCLAW_TRUSTED_PROXY_USER_HEADER"] == "x-forwarded-user"
    assert env["OPENCLAW_INTERNAL_TRUSTED_PROXY_USER"] == "openclaw-backend"
    assert env["OPENCLAW_INTERNAL_TRUSTED_PROXY_USER_HEADER"] == "x-forwarded-user"
    assert env["OPENCLAW_TRUSTED_PROXIES"] == "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,35.0.0.0/8"


def test_build_openclaw_env_vars_forces_trusted_proxy_when_token_configured(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.setenv("OPENCLAW_GATEWAY_AUTH_MODE", "token")

    env = cmd_openclaw._build_openclaw_env_vars()

    assert env["OPENCLAW_GATEWAY_AUTH_MODE"] == "trusted-proxy"
    assert env["OPENCLAW_TRUSTED_PROXY_USER_HEADER"] == "x-forwarded-user"
    assert env["OPENCLAW_INTERNAL_TRUSTED_PROXY_USER"] == "openclaw-backend"
    assert env["OPENCLAW_INTERNAL_TRUSTED_PROXY_USER_HEADER"] == "x-forwarded-user"
    assert env["OPENCLAW_TRUSTED_PROXIES"] == "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,35.0.0.0/8"


def test_build_openclaw_env_vars_uses_custom_trusted_proxy_env(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.setenv("OPENCLAW_GATEWAY_AUTH_MODE", "trusted-proxy")
    monkeypatch.setenv("OPENCLAW_TRUSTED_PROXY_USER_HEADER", "x-auth-request-user")
    monkeypatch.setenv("OPENCLAW_INTERNAL_TRUSTED_PROXY_USER", "internal-agent")
    monkeypatch.setenv("OPENCLAW_TRUSTED_PROXIES", '["10.244.0.0/16","10.96.0.0/12"]')

    env = cmd_openclaw._build_openclaw_env_vars()

    assert env["OPENCLAW_GATEWAY_AUTH_MODE"] == "trusted-proxy"
    assert env["OPENCLAW_TRUSTED_PROXY_USER_HEADER"] == "x-auth-request-user"
    assert env["OPENCLAW_INTERNAL_TRUSTED_PROXY_USER"] == "internal-agent"
    assert env["OPENCLAW_INTERNAL_TRUSTED_PROXY_USER_HEADER"] == "x-auth-request-user"
    assert env["OPENCLAW_TRUSTED_PROXIES"] == "10.244.0.0/16,10.96.0.0/12"


def test_build_openclaw_env_vars_defaults_to_auto_approval_first(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.delenv("OPENCLAW_EXEC_HOST", raising=False)
    monkeypatch.delenv("OPENCLAW_EXEC_ASK", raising=False)
    monkeypatch.delenv("OPENCLAW_EXEC_ASK_FALLBACK", raising=False)
    monkeypatch.delenv("OPENCLAW_EXEC_AUTO_ALLOW_SKILLS", raising=False)
    monkeypatch.delenv("OPENCLAW_EXEC_DEFAULT_ALLOWLIST_ENABLED", raising=False)
    monkeypatch.delenv("OPENCLAW_EXEC_ALLOWLIST", raising=False)
    monkeypatch.delenv("OPENCLAW_FS_WORKSPACE_ONLY", raising=False)
    monkeypatch.delenv("OPENCLAW_MODEL_API_KEY_SECRET_SOURCE", raising=False)
    monkeypatch.delenv("OPENCLAW_MODEL_API_KEY_SECRET_FILE_PATH", raising=False)

    env = cmd_openclaw._build_openclaw_env_vars()

    assert env["OPENCLAW_EXEC_HOST"] == "gateway"
    assert env["OPENCLAW_EXEC_ASK"] == "off"
    assert env["OPENCLAW_EXEC_ASK_FALLBACK"] == "allowlist"
    assert env["OPENCLAW_EXEC_AUTO_ALLOW_SKILLS"] == "false"
    assert env["OPENCLAW_EXEC_DEFAULT_ALLOWLIST_ENABLED"] == "true"
    assert env["OPENCLAW_FS_WORKSPACE_ONLY"] == "false"
    assert env["OPENCLAW_MODEL_API_KEY_SECRET_SOURCE"] == "file"
    assert "OPENCLAW_EXEC_ALLOWLIST" not in env
    assert "OPENCLAW_MODEL_API_KEY_SECRET_FILE_PATH" not in env


def test_build_openclaw_env_vars_exposes_exec_confirmation_controls(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.setenv("OPENCLAW_EXEC_HOST", "node")
    monkeypatch.setenv("OPENCLAW_EXEC_SECURITY", "deny")
    monkeypatch.setenv("OPENCLAW_EXEC_ASK", "on-miss")
    monkeypatch.setenv("OPENCLAW_EXEC_ASK_FALLBACK", "allowlist")
    monkeypatch.setenv("OPENCLAW_EXEC_AUTO_ALLOW_SKILLS", "true")
    monkeypatch.setenv("OPENCLAW_ELEVATED_ENABLED", "true")
    monkeypatch.setenv("OPENCLAW_EXEC_DEFAULT_ALLOWLIST_ENABLED", "false")
    monkeypatch.setenv("OPENCLAW_EXEC_ALLOWLIST", "/opt/tools/read-only")
    monkeypatch.setenv("OPENCLAW_FS_WORKSPACE_ONLY", "false")
    monkeypatch.setenv("OPENCLAW_MODEL_API_KEY_SECRET_SOURCE", "env")
    monkeypatch.setenv("OPENCLAW_MODEL_API_KEY_SECRET_FILE_PATH", "/tmp/runtime-secrets.json")

    env = cmd_openclaw._build_openclaw_env_vars()

    assert env["OPENCLAW_EXEC_HOST"] == "node"
    assert env["OPENCLAW_EXEC_SECURITY"] == "deny"
    assert env["OPENCLAW_EXEC_ASK"] == "on-miss"
    assert env["OPENCLAW_EXEC_ASK_FALLBACK"] == "allowlist"
    assert env["OPENCLAW_EXEC_AUTO_ALLOW_SKILLS"] == "true"
    assert env["OPENCLAW_ELEVATED_ENABLED"] == "true"
    assert env["OPENCLAW_EXEC_DEFAULT_ALLOWLIST_ENABLED"] == "false"
    assert env["OPENCLAW_EXEC_ALLOWLIST"] == "/opt/tools/read-only"
    assert env["OPENCLAW_FS_WORKSPACE_ONLY"] == "false"
    assert env["OPENCLAW_MODEL_API_KEY_SECRET_SOURCE"] == "env"
    assert env["OPENCLAW_MODEL_API_KEY_SECRET_FILE_PATH"] == "/tmp/runtime-secrets.json"


def test_build_openclaw_env_vars_omits_redundant_model_defaults(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.delenv("OPENCLAW_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL_NAME", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("OPENCLAW_MODEL_CATALOG_JSON", raising=False)
    monkeypatch.delenv("OPENCLAW_MODEL_PROVIDER_ID", raising=False)
    monkeypatch.delenv("OPENCLAW_MODEL_API", raising=False)

    env = cmd_openclaw._build_openclaw_env_vars()

    assert "OPENCLAW_DEFAULT_MODEL" not in env
    assert "OPENCLAW_MODEL_CATALOG_JSON" not in env
    assert "OPENCLAW_MODEL_BASE_URL" not in env
    assert "OPENCLAW_MODEL_PROVIDER_ID" not in env
    assert "OPENCLAW_MODEL_API" not in env


def test_build_openclaw_env_vars_global_model_preference_keeps_dual_catalog(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.delenv("OPENCLAW_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("OPENCLAW_MODEL_CATALOG_JSON", raising=False)
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-5")

    env = cmd_openclaw._build_openclaw_env_vars()

    assert env["OPENAI_MODEL_NAME"] == "ksyun/glm-5"
    assert "OPENCLAW_DEFAULT_MODEL" not in env
    assert "OPENCLAW_MODEL_CATALOG_JSON" not in env


def test_build_openclaw_env_vars_explicit_glm5_is_forwarded_without_catalog(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.delenv("OPENCLAW_MODEL_CATALOG_JSON", raising=False)
    monkeypatch.setenv("OPENCLAW_DEFAULT_MODEL", "ksyun/glm-5")

    env = cmd_openclaw._build_openclaw_env_vars()

    assert env["OPENCLAW_DEFAULT_MODEL"] == "ksyun/glm-5"
    assert "OPENCLAW_MODEL_CATALOG_JSON" not in env


def test_build_openclaw_env_vars_preserves_explicit_model_catalog(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.setenv("OPENCLAW_MODEL_CATALOG_JSON", '[{"id":"glm-5"}]')

    env = cmd_openclaw._build_openclaw_env_vars()

    assert env["OPENCLAW_MODEL_CATALOG_JSON"] == '[{"id":"glm-5"}]'


def test_generate_default_openclaw_name_is_high_entropy():
    name1 = cmd_openclaw._generate_default_openclaw_name()
    name2 = cmd_openclaw._generate_default_openclaw_name()

    assert name1 != name2
    assert len(name1) <= 64
    assert name1.startswith("openclaw-gateway-")
    assert re.fullmatch(r"openclaw-gateway-\d{10}-[0-9a-f]{6}", name1) is not None

import re
import json
import pytest

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


def test_build_openclaw_env_vars_switches_to_token_mode_when_token_configured(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "gateway-token-demo")

    env = cmd_openclaw._build_openclaw_env_vars()

    assert env["OPENCLAW_GATEWAY_AUTH_MODE"] == "token"
    assert env["OPENCLAW_GATEWAY_TOKEN"] == "gateway-token-demo"
    assert env["OPENCLAW_GATEWAY_PASSWORD"] == "gateway-token-demo"


def test_build_openclaw_env_vars_accepts_password_alias_for_token_mode(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.setenv("OPENCLAW_GATEWAY_AUTH_MODE", "token")
    monkeypatch.setenv("OPENCLAW_GATEWAY_PASSWORD", "gateway-password-demo")

    env = cmd_openclaw._build_openclaw_env_vars()

    assert env["OPENCLAW_GATEWAY_AUTH_MODE"] == "token"
    assert env["OPENCLAW_GATEWAY_TOKEN"] == "gateway-password-demo"
    assert env["OPENCLAW_GATEWAY_PASSWORD"] == "gateway-password-demo"


def test_build_openclaw_env_vars_rejects_mismatched_token_and_password(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.setenv("OPENCLAW_GATEWAY_AUTH_MODE", "token")
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "gateway-token-demo")
    monkeypatch.setenv("OPENCLAW_GATEWAY_PASSWORD", "gateway-password-other")

    with pytest.raises(ValueError, match="OPENCLAW_GATEWAY_TOKEN.*OPENCLAW_GATEWAY_PASSWORD"):
        cmd_openclaw._build_openclaw_env_vars()


def test_build_openclaw_env_vars_rejects_token_secret_outside_token_mode(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.setenv("OPENCLAW_GATEWAY_AUTH_MODE", "trusted-proxy")
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "gateway-token-demo")

    with pytest.raises(ValueError, match="仅在 OPENCLAW_GATEWAY_AUTH_MODE=token 时支持"):
        cmd_openclaw._build_openclaw_env_vars()


def test_build_openclaw_env_vars_requires_secret_for_token_mode(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.setenv("OPENCLAW_GATEWAY_AUTH_MODE", "token")
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("OPENCLAW_GATEWAY_PASSWORD", raising=False)

    with pytest.raises(ValueError, match="OPENCLAW_GATEWAY_TOKEN"):
        cmd_openclaw._build_openclaw_env_vars()


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
    monkeypatch.delenv("OPENCLAW_EXEC_STRICT_MODE", raising=False)
    monkeypatch.delenv("OPENCLAW_EXEC_SAFE_MODE", raising=False)

    env = cmd_openclaw._build_openclaw_env_vars()

    assert env["OPENCLAW_EXEC_HOST"] == "gateway"
    assert env["OPENCLAW_EXEC_STRICT_MODE"] == "false"
    assert env["OPENCLAW_EXEC_UNSAFE_MODE"] == "true"
    assert env["OPENCLAW_EXEC_SECURITY"] == "full"
    assert env["OPENCLAW_EXEC_ASK"] == "off"
    assert env["OPENCLAW_EXEC_ASK_FALLBACK"] == "full"
    assert env["OPENCLAW_EXEC_AUTO_ALLOW_SKILLS"] == "false"
    assert env["OPENCLAW_EXEC_DEFAULT_ALLOWLIST_ENABLED"] == "false"
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


def test_build_openclaw_env_vars_enables_strict_mode_when_requested(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.setenv("OPENCLAW_EXEC_STRICT_MODE", "true")
    monkeypatch.delenv("OPENCLAW_EXEC_SECURITY", raising=False)
    monkeypatch.delenv("OPENCLAW_EXEC_ASK_FALLBACK", raising=False)
    monkeypatch.delenv("OPENCLAW_EXEC_DEFAULT_ALLOWLIST_ENABLED", raising=False)

    env = cmd_openclaw._build_openclaw_env_vars()

    assert env["OPENCLAW_EXEC_STRICT_MODE"] == "true"
    assert env["OPENCLAW_EXEC_UNSAFE_MODE"] == "false"
    assert env["OPENCLAW_EXEC_SECURITY"] == "allowlist"
    assert env["OPENCLAW_EXEC_ASK"] == "off"
    assert env["OPENCLAW_EXEC_ASK_FALLBACK"] == "allowlist"
    assert env["OPENCLAW_EXEC_DEFAULT_ALLOWLIST_ENABLED"] == "true"


def test_build_openclaw_env_vars_applies_strict_security_profile(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.setenv("OPENCLAW_EXEC_STRICT_MODE", "false")
    monkeypatch.setenv("OPENCLAW_EXEC_SECURITY", "full")
    monkeypatch.setenv("OPENCLAW_EXEC_ASK_FALLBACK", "full")
    monkeypatch.setenv("OPENCLAW_EXEC_DEFAULT_ALLOWLIST_ENABLED", "false")
    monkeypatch.setenv("OPENCLAW_FS_WORKSPACE_ONLY", "true")

    env = cmd_openclaw._build_openclaw_env_vars(security_profile="strict")

    assert env["OPENCLAW_EXEC_STRICT_MODE"] == "true"
    assert env["OPENCLAW_EXEC_UNSAFE_MODE"] == "false"
    assert env["OPENCLAW_EXEC_SECURITY"] == "allowlist"
    assert env["OPENCLAW_EXEC_ASK"] == "off"
    assert env["OPENCLAW_EXEC_ASK_FALLBACK"] == "allowlist"
    assert env["OPENCLAW_EXEC_DEFAULT_ALLOWLIST_ENABLED"] == "true"
    assert env["OPENCLAW_FS_WORKSPACE_ONLY"] == "false"


def test_build_openclaw_env_vars_applies_strictest_security_profile(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.setenv("OPENCLAW_EXEC_SECURITY", "allowlist")
    monkeypatch.setenv("OPENCLAW_EXEC_ASK_FALLBACK", "allowlist")
    monkeypatch.setenv("OPENCLAW_EXEC_DEFAULT_ALLOWLIST_ENABLED", "true")
    monkeypatch.setenv("OPENCLAW_FS_WORKSPACE_ONLY", "false")

    env = cmd_openclaw._build_openclaw_env_vars(security_profile="strictest")

    assert env["OPENCLAW_EXEC_STRICT_MODE"] == "true"
    assert env["OPENCLAW_EXEC_UNSAFE_MODE"] == "false"
    assert env["OPENCLAW_EXEC_SECURITY"] == "deny"
    assert env["OPENCLAW_EXEC_ASK"] == "off"
    assert env["OPENCLAW_EXEC_ASK_FALLBACK"] == "deny"
    assert env["OPENCLAW_EXEC_DEFAULT_ALLOWLIST_ENABLED"] == "false"
    assert env["OPENCLAW_FS_WORKSPACE_ONLY"] == "true"


def test_build_openclaw_env_vars_defaults_exec_to_relaxed_without_explicit_profile(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.delenv("OPENCLAW_EXEC_STRICT_MODE", raising=False)
    monkeypatch.delenv("OPENCLAW_EXEC_SECURITY", raising=False)
    monkeypatch.delenv("OPENCLAW_EXEC_ASK_FALLBACK", raising=False)
    monkeypatch.delenv("OPENCLAW_EXEC_DEFAULT_ALLOWLIST_ENABLED", raising=False)

    env = cmd_openclaw._build_openclaw_env_vars()

    assert env["OPENCLAW_EXEC_STRICT_MODE"] == "false"
    assert env["OPENCLAW_EXEC_UNSAFE_MODE"] == "true"
    assert env["OPENCLAW_EXEC_SECURITY"] == "full"
    assert env["OPENCLAW_EXEC_ASK_FALLBACK"] == "full"
    assert env["OPENCLAW_EXEC_DEFAULT_ALLOWLIST_ENABLED"] == "false"


def test_build_openclaw_env_vars_injects_default_model_policy(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.delenv("OPENCLAW_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL_NAME", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("OPENCLAW_MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.delenv("OPENCLAW_MODEL_CATALOG_JSON", raising=False)
    monkeypatch.delenv("OPENCLAW_MODEL_PROVIDER_ID", raising=False)
    monkeypatch.delenv("OPENCLAW_MODEL_API", raising=False)

    env = cmd_openclaw._build_openclaw_env_vars()

    assert "OPENCLAW_DEFAULT_MODEL" not in env
    assert env["OPENAI_MODEL_NAME"] == "ksyun/glm-5.2"
    assert env["OPENCLAW_FALLBACK_MODEL"] == "ksyun/deepseek-v4-pro"
    assert env["OPENCLAW_IMAGE_MODEL"] == "ksyun/kimi-k2.7-code"
    assert "AGENTENGINE_MODEL_POLICY_JSON" in env
    catalog = json.loads(env["OPENCLAW_MODEL_CATALOG_JSON"])
    assert [item["id"] for item in catalog] == ["glm-5.2", "kimi-k2.7-code", "deepseek-v4-pro"]
    assert catalog[1]["options"] == {"temperature": 1}
    assert "OPENCLAW_MODEL_BASE_URL" not in env
    assert "OPENCLAW_MODEL_PROVIDER_ID" not in env
    assert "OPENCLAW_MODEL_API" not in env


def test_build_openclaw_env_vars_global_model_preference_keeps_dual_catalog(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.delenv("OPENCLAW_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("OPENCLAW_MODEL_CATALOG_JSON", raising=False)
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-5.1")

    env = cmd_openclaw._build_openclaw_env_vars()

    assert env["OPENAI_MODEL_NAME"] == "ksyun/glm-5.1"
    assert "OPENCLAW_DEFAULT_MODEL" not in env
    catalog = json.loads(env["OPENCLAW_MODEL_CATALOG_JSON"])
    assert [item["id"] for item in catalog] == ["glm-5.2", "kimi-k2.7-code", "deepseek-v4-pro"]


def test_build_openclaw_env_vars_explicit_glm5_is_forwarded_without_catalog(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.delenv("OPENCLAW_MODEL_CATALOG_JSON", raising=False)
    monkeypatch.setenv("OPENCLAW_DEFAULT_MODEL", "ksyun/glm-5.1")

    env = cmd_openclaw._build_openclaw_env_vars()

    assert env["OPENCLAW_DEFAULT_MODEL"] == "ksyun/glm-5.1"
    catalog = json.loads(env["OPENCLAW_MODEL_CATALOG_JSON"])
    assert [item["id"] for item in catalog] == ["glm-5.2", "kimi-k2.7-code", "deepseek-v4-pro"]


def test_build_openclaw_env_vars_preserves_explicit_model_catalog(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.setenv("OPENCLAW_MODEL_CATALOG_JSON", '[{"id":"glm-5.1"}]')

    env = cmd_openclaw._build_openclaw_env_vars()

    assert env["OPENCLAW_MODEL_CATALOG_JSON"] == '[{"id":"glm-5.1"}]'


def test_openclaw_provider_model_metadata_builds_catalog_for_creation(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.delenv("OPENCLAW_MODEL_CATALOG_JSON", raising=False)
    monkeypatch.setenv("OPENAI_MODEL_NAME", "deepseek-v4-pro")

    env = cmd_openclaw._build_openclaw_env_vars()
    changed = cmd_openclaw._apply_openclaw_provider_model_metadata(
        env,
        {
            "id": "deepseek-v4-pro",
            "context_window_tokens": 1_000_000,
            "max_output_tokens": 384_000,
        },
    )

    assert changed is True
    catalog = json.loads(env["OPENCLAW_MODEL_CATALOG_JSON"])
    assert [item["id"] for item in catalog] == [
        "glm-5.2",
        "kimi-k2.7-code",
        "deepseek-v4-pro",
    ]
    assert catalog[1]["options"] == {"temperature": 1}
    assert catalog[-1] == {
        "id": "deepseek-v4-pro",
        "name": "deepseek-v4-pro",
        "api": "openai-completions",
        "reasoning": True,
        "input": ["text", "image"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 1_000_000,
        "maxTokens": 384_000,
    }


def test_openclaw_provider_model_metadata_preserves_explicit_catalog_items(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.setenv(
        "OPENCLAW_MODEL_CATALOG_JSON",
        json.dumps(
            [
                {"id": "custom-model", "name": "custom-model"},
                {"id": "deepseek-v4-pro", "name": "old"},
            ]
        ),
    )

    env = cmd_openclaw._build_openclaw_env_vars()
    changed = cmd_openclaw._apply_openclaw_provider_model_metadata(
        env,
        {
            "id": "deepseek-v4-pro",
            "context_window_tokens": 1_000_000,
        }
    )

    assert changed is True
    catalog = json.loads(env["OPENCLAW_MODEL_CATALOG_JSON"])
    assert catalog[0]["id"] == "custom-model"
    assert catalog[1]["id"] == "deepseek-v4-pro"
    assert catalog[1]["contextWindow"] == 1_000_000


def test_build_openclaw_env_vars_forwards_explicit_web_tool_overrides(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.setenv("OPENCLAW_WEB_FETCH_ENABLED", "true")
    monkeypatch.setenv("OPENCLAW_WEB_SEARCH_PROVIDER", "perplexity")
    monkeypatch.setenv("OPENCLAW_WEB_SEARCH_BASE_URL", "https://search.example.com/v1")
    monkeypatch.setenv("OPENCLAW_WEB_SEARCH_MODEL", "sonar-pro")
    monkeypatch.setenv("OPENCLAW_WEB_SEARCH_API_KEY_SECRET_SOURCE", "env")
    monkeypatch.setenv("OPENCLAW_WEB_SEARCH_API_KEY_SECRET_PROVIDER", "default")
    monkeypatch.setenv("OPENCLAW_WEB_SEARCH_API_KEY_SECRET_ID", "OPENCLAW_WEB_SEARCH_API_KEY")

    env = cmd_openclaw._build_openclaw_env_vars()

    assert env["OPENCLAW_WEB_FETCH_ENABLED"] == "true"
    assert env["OPENCLAW_WEB_SEARCH_PROVIDER"] == "perplexity"
    assert env["OPENCLAW_WEB_SEARCH_BASE_URL"] == "https://search.example.com/v1"
    assert env["OPENCLAW_WEB_SEARCH_MODEL"] == "sonar-pro"
    assert env["OPENCLAW_WEB_SEARCH_API_KEY_SECRET_SOURCE"] == "env"
    assert env["OPENCLAW_WEB_SEARCH_API_KEY_SECRET_PROVIDER"] == "default"
    assert env["OPENCLAW_WEB_SEARCH_API_KEY_SECRET_ID"] == "OPENCLAW_WEB_SEARCH_API_KEY"


def test_build_openclaw_env_vars_forwards_explicit_builtin_browser_toggle(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.setenv("OPENCLAW_BROWSER_ENABLED", "true")

    env = cmd_openclaw._build_openclaw_env_vars()

    assert env["OPENCLAW_BROWSER_ENABLED"] == "true"
    assert env["OPENCLAW_BROWSER_NO_SANDBOX"] == "true"
    assert env["OPENCLAW_BROWSER_HEADLESS"] == "true"


def test_build_openclaw_env_vars_forwards_explicit_browser_ssrf_policy_json(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.setenv(
        "OPENCLAW_BROWSER_SSRF_POLICY_JSON",
        '{"dangerouslyAllowPrivateNetwork":false,"hostnameAllowlist":["docs.example.com"]}',
    )

    env = cmd_openclaw._build_openclaw_env_vars()

    assert env["OPENCLAW_BROWSER_SSRF_POLICY_JSON"] == (
        '{"dangerouslyAllowPrivateNetwork":false,"hostnameAllowlist":["docs.example.com"]}'
    )


def test_build_openclaw_env_vars_forwards_channel_bootstrap_json(monkeypatch):
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", {})
    monkeypatch.setenv(
        "OPENCLAW_CHANNEL_BOOTSTRAP_JSON",
        '{"wps-xiezuo":{"appId":"app-demo","appSecret":"secret-demo"},"feishu":{"appId":"app-demo"}}',
    )

    env = cmd_openclaw._build_openclaw_env_vars()

    assert env["OPENCLAW_CHANNEL_BOOTSTRAP_JSON"] == (
        '{"wps-xiezuo":{"appId":"app-demo","appSecret":"secret-demo"},"feishu":{"appId":"app-demo"}}'
    )


def test_parse_extra_openclaw_env_pairs_supports_custom_keys_and_explicit_override():
    parsed = cmd_openclaw._parse_extra_openclaw_env_pairs(
        (
            "FOO=bar",
            "OPENCLAW_GATEWAY_PORT=9090",
            "FOO=baz",
            "EMPTY_VALUE=",
        )
    )

    assert parsed == {
        "FOO": "baz",
        "OPENCLAW_GATEWAY_PORT": "9090",
        "EMPTY_VALUE": "",
    }


def test_parse_extra_openclaw_env_pairs_rejects_invalid_items():
    with pytest.raises(ValueError, match="KEY=VALUE"):
        cmd_openclaw._parse_extra_openclaw_env_pairs(("MISSING_EQUALS",))

    with pytest.raises(ValueError, match="合法的环境变量名"):
        cmd_openclaw._parse_extra_openclaw_env_pairs(("1BAD=value",))

    with pytest.raises(ValueError, match="trusted-proxy、token 或 none"):
        cmd_openclaw._parse_extra_openclaw_env_pairs(("OPENCLAW_GATEWAY_AUTH_MODE=password",))


def test_parse_extra_openclaw_env_pairs_accepts_token_auth_mode():
    parsed = cmd_openclaw._parse_extra_openclaw_env_pairs(("OPENCLAW_GATEWAY_AUTH_MODE=token",))

    assert parsed == {"OPENCLAW_GATEWAY_AUTH_MODE": "token"}


def test_generate_default_openclaw_name_is_high_entropy():
    name1 = cmd_openclaw._generate_default_openclaw_name()
    name2 = cmd_openclaw._generate_default_openclaw_name()

    assert name1 != name2
    assert len(name1) <= 64
    assert name1.startswith("openclaw-gateway-")
    assert re.fullmatch(r"openclaw-gateway-\d{10}-[0-9a-f]{6}", name1) is not None

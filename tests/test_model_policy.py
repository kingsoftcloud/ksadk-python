import os

from ksadk.configs.settings import DEFAULT_MODEL_NAME
from ksadk.model_policy import (
    DEFAULT_MODEL_POLICY,
    fallback_model_for_exception,
    model_policy_options_for_model,
    normalize_model_policy,
)


def test_default_model_policy_matches_release_defaults():
    policy = normalize_model_policy(DEFAULT_MODEL_POLICY)

    assert DEFAULT_MODEL_NAME == "glm-5.2"
    assert policy["primary"]["model"] == "glm-5.2"
    assert policy["multimodal"]["model"] == "kimi-k2.7-code"
    assert policy["fallback"]["model"] == "deepseek-v4-pro"
    assert policy["models"]["kimi-k2.7-code"]["options"]["temperature"] == 1


def test_model_policy_options_apply_kimi_temperature_constraint():
    assert model_policy_options_for_model("kimi-k2.7-code") == {"temperature": 1}
    assert model_policy_options_for_model("ksyun/kimi-k2.7-code") == {"temperature": 1}
    assert model_policy_options_for_model("glm-5.2") == {}


def test_model_policy_env_override_keeps_default_shape(monkeypatch):
    monkeypatch.setenv(
        "AGENTENGINE_MODEL_POLICY_JSON",
        '{"primary":{"model":"custom-primary"},"fallback":{"model":"custom-fallback"}}',
    )

    policy = normalize_model_policy(os.environ["AGENTENGINE_MODEL_POLICY_JSON"])

    assert policy["primary"]["model"] == "custom-primary"
    assert policy["fallback"]["model"] == "custom-fallback"
    assert policy["multimodal"]["model"] == "kimi-k2.7-code"


def test_fallback_model_for_exception_only_accepts_transient_errors():
    assert (
        fallback_model_for_exception(RuntimeError("model unavailable"), current_model="glm-5.2")
        == "deepseek-v4-pro"
    )
    assert (
        fallback_model_for_exception(RuntimeError("invalid request 400"), current_model="glm-5.2")
        is None
    )
    assert (
        fallback_model_for_exception(
            RuntimeError("model unavailable"),
            current_model="deepseek-v4-pro",
        )
        is None
    )

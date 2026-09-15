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

    assert DEFAULT_MODEL_NAME == "deepseek-v4.1-flash"
    assert policy["primary"]["model"] == "deepseek-v4.1-flash"
    assert policy["multimodal"]["model"] == "deepseek-v4.1-flash"
    assert policy["fallback"]["model"] == "glm-5.3-flash"
    assert policy["models"]["deepseek-v4.1-flash"]["reasoning"] is True
    assert policy["models"]["glm-5.3-flash"]["reasoning"] is True
    assert policy["models"]["deepseek-v4.1-flash"]["options"]["temperature"] == 1


def test_model_policy_options_apply_multimodal_temperature_constraint():
    # temperature=1 约束挂在 multimodal 默认模型上(fb8daf17:deepseek-v4.1-flash 取代 kimi)
    assert model_policy_options_for_model("deepseek-v4.1-flash") == {"temperature": 1}
    assert model_policy_options_for_model("ksyun/deepseek-v4.1-flash") == {"temperature": 1}
    assert model_policy_options_for_model("glm-5.3-flash") == {}
    # kimi 已不在默认 models 里(仅 family 约束注释保留)
    assert model_policy_options_for_model("kimi-k2.7-code") == {}


def test_model_policy_env_override_keeps_default_shape(monkeypatch):
    monkeypatch.setenv(
        "AGENTENGINE_MODEL_POLICY_JSON",
        '{"primary":{"model":"custom-primary"},"fallback":{"model":"custom-fallback"}}',
    )

    policy = normalize_model_policy(os.environ["AGENTENGINE_MODEL_POLICY_JSON"])

    assert policy["primary"]["model"] == "custom-primary"
    assert policy["fallback"]["model"] == "custom-fallback"
    assert policy["multimodal"]["model"] == "deepseek-v4.1-flash"


def test_fallback_model_for_exception_only_accepts_transient_errors():
    assert (
        fallback_model_for_exception(
            RuntimeError("model unavailable"), current_model="deepseek-v4.1-flash"
        )
        == "glm-5.3-flash"
    )
    assert (
        fallback_model_for_exception(
            RuntimeError("invalid request 400"), current_model="deepseek-v4.1-flash"
        )
        is None
    )
    assert (
        fallback_model_for_exception(
            RuntimeError("model unavailable"),
            current_model="glm-5.3-flash",
        )
        is None
    )

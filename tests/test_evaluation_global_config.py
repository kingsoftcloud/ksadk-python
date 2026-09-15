from __future__ import annotations

from ksadk.configs.global_config import (
    build_global_config_from_env,
    get_env_from_global_config,
)


def test_agent_eval_connection_can_round_trip_through_global_config(monkeypatch):
    config = build_global_config_from_env(
        {
            "AGENT_EVAL_BASE_URL": "https://agent-eval.example",
            "AGENT_EVAL_API_TOKEN": "token-secret",
            "AGENT_EVAL_ACCOUNT_ID": "account-001",
        }
    )
    monkeypatch.setattr(
        "ksadk.configs.global_config.load_global_config",
        lambda: config,
    )

    assert get_env_from_global_config() == {
        "AGENT_EVAL_BASE_URL": "https://agent-eval.example",
        "AGENT_EVAL_API_TOKEN": "token-secret",
        "AGENT_EVAL_ACCOUNT_ID": "account-001",
    }

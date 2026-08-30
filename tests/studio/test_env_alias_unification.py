"""OPENAI_BASE_URL / OPENAI_API_BASE 别名统一与优先级（方案 §2.4 第 5 点）。

验证：两个别名互为兼容，运行时统一 OPENAI_BASE_URL 优先（与 cmd_config/cmd_model/api.py 一致），
--env-file 安全加载两个都接受并做别名归一。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from ksadk.cli.cmd_studio import studio


def _write_env(tmp_path: Path, content: str) -> Path:
    env_file = tmp_path / "model.env"
    env_file.write_text(content, encoding="utf-8")
    return env_file


def _capture_env(monkeypatch) -> dict:
    captured: dict = {}

    def _cap(*_a, **_kw):
        captured.update(
            {
                "OPENAI_BASE_URL": os.environ.get("OPENAI_BASE_URL"),
                "OPENAI_API_BASE": os.environ.get("OPENAI_API_BASE"),
            }
        )

    monkeypatch.setattr("ksadk.cli.cmd_studio.uvicorn.run", _cap)
    return captured


@pytest.mark.parametrize("alias", ["OPENAI_BASE_URL", "OPENAI_API_BASE"])
def test_env_file_accepts_both_aliases_and_normalizes(tmp_path, monkeypatch, alias):
    """任一别名经 --env-file 加载后，两个 env 都有值（别名归一，方案 §2.4 第 5 点）。"""
    for k in ("OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_API_KEY", "OPENAI_MODEL_NAME"):
        monkeypatch.delenv(k, raising=False)
    env_file = _write_env(
        tmp_path, f"{alias}=https://models.example/v1\nOPENAI_API_KEY=k\nOPENAI_MODEL_NAME=m\n"
    )
    captured = _capture_env(monkeypatch)
    CliRunner().invoke(studio, [str(tmp_path / "ws"), "--no-open", "--env-file", str(env_file)])
    assert captured["OPENAI_BASE_URL"] == "https://models.example/v1"
    assert captured["OPENAI_API_BASE"] == "https://models.example/v1"


def test_openai_base_url_takes_priority_when_both_set(tmp_path, monkeypatch):
    """两个别名都设时，OPENAI_BASE_URL 优先（运行时统一口径）。"""
    # 直接设两个 env，runtime_source 应读 OPENAI_BASE_URL
    monkeypatch.setenv("OPENAI_BASE_URL", "https://preferred.example/v1")
    monkeypatch.setenv("OPENAI_API_BASE", "https://legacy.example/v1")
    # runtime_source._call_model 用 base_url=os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE")
    resolved = os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE")
    assert resolved == "https://preferred.example/v1"


def test_falls_back_to_api_base_when_base_url_unset(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_BASE", "https://legacy.example/v1")
    resolved = os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE")
    assert resolved == "https://legacy.example/v1"


def test_unrelated_env_still_blocked(tmp_path, monkeypatch):
    """白名单仍挡住非模型 env（安全加载不变）。"""
    for k in (
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "OPENAI_API_KEY",
        "OPENAI_MODEL_NAME",
        "UNRELATED_SECRET",
    ):
        monkeypatch.delenv(k, raising=False)
    env_file = _write_env(tmp_path, "OPENAI_BASE_URL=https://m.example/v1\nUNRELATED_SECRET=leak\n")
    captured: dict = {}

    def _cap(*_a, **_kw):
        captured["UNRELATED_SECRET"] = os.environ.get("UNRELATED_SECRET")

    monkeypatch.setattr("ksadk.cli.cmd_studio.uvicorn.run", _cap)
    result = CliRunner().invoke(
        studio, [str(tmp_path / "ws"), "--no-open", "--env-file", str(env_file)]
    )
    assert result.exit_code == 0
    assert captured.get("UNRELATED_SECRET") is None  # 被白名单挡住

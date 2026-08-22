from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from ksadk.cli.cmd_studio import studio


def test_studio_cli_binds_loopback_and_initializes_workspace(
    tmp_path: Path,
    monkeypatch,
):
    captured = {}

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured.update(kwargs)

    opened = []
    monkeypatch.setattr("ksadk.cli.cmd_studio.uvicorn.run", fake_run)
    monkeypatch.setattr(
        "ksadk.cli.cmd_studio.webbrowser.open",
        lambda url: opened.append(url),
    )

    result = CliRunner().invoke(studio, [str(tmp_path / "workspace"), "--port", "8899"])

    assert result.exit_code == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8899
    assert captured["access_log"] is False
    assert captured["app"].state.studio_service.runtime_executor.registered_runtime_types() == [
        "adk",
        "codex",
        "langgraph",
    ]
    assert opened[0].startswith("http://127.0.0.1:8899/#session=")
    assert (tmp_path / "workspace/agentkit.yaml").is_file()


def test_studio_cli_no_open_does_not_launch_browser(tmp_path: Path, monkeypatch):
    captured = {}

    def fake_run(_app, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("ksadk.cli.cmd_studio.uvicorn.run", fake_run)
    monkeypatch.setattr(
        "ksadk.cli.cmd_studio.webbrowser.open",
        lambda _url: (_ for _ in ()).throw(AssertionError("must not open")),
    )

    result = CliRunner().invoke(studio, [str(tmp_path), "--no-open"])

    assert result.exit_code == 0
    assert captured["port"] == 8080
    assert "127.0.0.1:8080" in result.output
    assert "#session=" in result.output


def test_studio_cli_loads_allowlisted_model_and_cloud_control_env_and_forces_codex_proxy(
    tmp_path: Path,
    monkeypatch,
):
    env_file = tmp_path / "model.env"
    env_file.write_text(
        "OPENAI_API_BASE=https://models.example/v1\n"
        "OPENAI_API_KEY=top-secret-value\n"
        "OPENAI_MODEL_NAME=glm-5.2\n"
        "KSYUN_ACCESS_KEY=cloud-access\n"
        "KSYUN_SECRET_KEY=cloud-secret\n"
        "KSYUN_REGION=cn-beijing-6\n"
        "UNRELATED_SECRET=must-not-be-loaded\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL_NAME", raising=False)
    monkeypatch.delenv("UNRELATED_SECRET", raising=False)
    monkeypatch.delenv("KSYUN_ACCESS_KEY", raising=False)
    monkeypatch.delenv("KSYUN_SECRET_KEY", raising=False)
    monkeypatch.delenv("KSYUN_REGION", raising=False)
    monkeypatch.delenv("KSADK_CODEX_USE_PROXY", raising=False)
    active_environment = {}

    def capture_runtime_environment(*_args, **_kwargs):
        active_environment.update(
            {
                key: __import__("os").environ.get(key)
                for key in (
                    "OPENAI_API_BASE",
                    "OPENAI_BASE_URL",
                    "OPENAI_API_KEY",
                    "OPENAI_MODEL_NAME",
                    "KSYUN_ACCESS_KEY",
                    "KSYUN_SECRET_KEY",
                    "KSYUN_REGION",
                    "KSADK_CODEX_USE_PROXY",
                )
            }
        )

    monkeypatch.setattr("ksadk.cli.cmd_studio.uvicorn.run", capture_runtime_environment)

    result = CliRunner().invoke(
        studio,
        [
            str(tmp_path / "workspace"),
            "--no-open",
            "--env-file",
            str(env_file),
            "--codex-proxy",
            "forced",
        ],
    )

    assert result.exit_code == 0
    assert "模型环境" in result.output
    # 别名归一：OPENAI_API_BASE 被加载并归一到 OPENAI_BASE_URL，两个都有值（方案 §2.4 第 5 点）
    assert active_environment["OPENAI_API_BASE"] == "https://models.example/v1"
    assert active_environment["OPENAI_BASE_URL"] == "https://models.example/v1"
    assert active_environment["OPENAI_API_KEY"] == "top-secret-value"
    assert active_environment["OPENAI_MODEL_NAME"] == "glm-5.2"
    assert active_environment["KSYUN_ACCESS_KEY"] == "cloud-access"
    assert active_environment["KSYUN_SECRET_KEY"] == "cloud-secret"
    assert active_environment["KSYUN_REGION"] == "cn-beijing-6"
    # 白名单仍挡住非模型 env
    assert active_environment.get("KSADK_CODEX_USE_PROXY") == "1"
    assert "top-secret-value" not in result.output
    assert "cloud-access" not in result.output
    assert "cloud-secret" not in result.output
    assert "must-not-be-loaded" not in result.output
    assert "models.example" not in result.output
    assert active_environment == {
        "OPENAI_API_BASE": "https://models.example/v1",
        "OPENAI_BASE_URL": "https://models.example/v1",
        "OPENAI_API_KEY": "top-secret-value",
        "OPENAI_MODEL_NAME": "glm-5.2",
        "KSYUN_ACCESS_KEY": "cloud-access",
        "KSYUN_SECRET_KEY": "cloud-secret",
        "KSYUN_REGION": "cn-beijing-6",
        "KSADK_CODEX_USE_PROXY": "1",
    }
    assert "OPENAI_API_BASE" not in __import__("os").environ
    assert "OPENAI_BASE_URL" not in __import__("os").environ
    assert "OPENAI_API_KEY" not in __import__("os").environ
    assert "OPENAI_MODEL_NAME" not in __import__("os").environ
    assert "KSYUN_ACCESS_KEY" not in __import__("os").environ
    assert "KSYUN_SECRET_KEY" not in __import__("os").environ
    assert "KSYUN_REGION" not in __import__("os").environ
    assert "UNRELATED_SECRET" not in __import__("os").environ
    assert "KSADK_CODEX_USE_PROXY" not in __import__("os").environ
    for path in (tmp_path / "workspace").rglob("*"):
        if path.is_file():
            assert "top-secret-value" not in path.read_text(
                encoding="utf-8",
                errors="ignore",
            )
            assert "cloud-secret" not in path.read_text(
                encoding="utf-8",
                errors="ignore",
            )

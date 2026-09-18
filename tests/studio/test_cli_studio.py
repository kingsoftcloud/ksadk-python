from __future__ import annotations

import time
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
        "ksadk.cli.cmd_studio._open_browser_when_ready",
        lambda url, _port: opened.append(url),
    )

    result = CliRunner().invoke(studio, [str(tmp_path / "workspace"), "--port", "8899"])

    assert result.exit_code == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8899
    assert captured["timeout_graceful_shutdown"] == 10
    # Access log 必须开启（历史上被 access_log=False 关闭过），且日志格式带
    # filename:lineno（veadk 风格），便于本地排障定位代码。
    assert "access_log" not in captured or captured["access_log"] is not False
    log_config = captured["log_config"]
    assert log_config["loggers"]["uvicorn.access"]["level"] == "INFO"
    assert "%(filename)s:%(lineno)d" in log_config["formatters"]["access"]["format"]
    assert "%(filename)s:%(lineno)d" in log_config["formatters"]["default"]["format"]
    assert captured["app"].state.studio_service.runtime_executor.registered_runtime_types() == [
        "adk",
        "codex",
        "harness",
        "langgraph",
    ]
    # The production launcher waits for Uvicorn to bind before opening the
    # browser; the helper is replaced with a test double here.
    for _ in range(100):
        if opened:
            break
        time.sleep(0.01)
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


def test_explicit_env_file_overrides_inherited_configuration_only_for_studio_process(
    tmp_path: Path,
    monkeypatch,
):
    env_file = tmp_path / "studio.env"
    env_file.write_text(
        "OPENAI_API_KEY=file-model-key\n"
        "KSYUN_ACCESS_KEY=file-cloud-access\n"
        "KSYUN_SECRET_KEY=file-cloud-secret\n"
        "KSYUN_REGION=pre-online\n"
        "KSADK_WEB_SEARCH_PROVIDER=ksyun\n"
        "KSADK_WEB_SEARCH_API_KEY=fixture-search-key\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "shell-model-key")
    monkeypatch.setenv("KSYUN_ACCESS_KEY", "shell-cloud-access")
    monkeypatch.setenv("KSYUN_SECRET_KEY", "shell-cloud-secret")
    monkeypatch.setenv("KSYUN_REGION", "online")
    monkeypatch.setenv("KSADK_WEB_SEARCH_API_KEY", "old-search-key")
    active_environment: dict[str, str | None] = {}

    def capture_runtime_environment(*_args, **_kwargs):
        active_environment.update(
            {
                "OPENAI_API_KEY": __import__("os").environ.get("OPENAI_API_KEY"),
                "KSYUN_ACCESS_KEY": __import__("os").environ.get("KSYUN_ACCESS_KEY"),
                "KSYUN_SECRET_KEY": __import__("os").environ.get("KSYUN_SECRET_KEY"),
                "KSYUN_REGION": __import__("os").environ.get("KSYUN_REGION"),
                "KSADK_WEB_SEARCH_API_KEY": __import__("os").environ.get(
                    "KSADK_WEB_SEARCH_API_KEY"
                ),
            }
        )

    monkeypatch.setattr("ksadk.cli.cmd_studio.uvicorn.run", capture_runtime_environment)

    result = CliRunner().invoke(
        studio,
        [str(tmp_path / "workspace"), "--no-open", "--env-file", str(env_file)],
    )

    assert result.exit_code == 0
    assert active_environment == {
        "OPENAI_API_KEY": "file-model-key",
        "KSYUN_ACCESS_KEY": "file-cloud-access",
        "KSYUN_SECRET_KEY": "file-cloud-secret",
        "KSYUN_REGION": "pre-online",
        "KSADK_WEB_SEARCH_API_KEY": "fixture-search-key",
    }
    assert __import__("os").environ["OPENAI_API_KEY"] == "shell-model-key"
    assert __import__("os").environ["KSYUN_ACCESS_KEY"] == "shell-cloud-access"
    assert __import__("os").environ["KSYUN_SECRET_KEY"] == "shell-cloud-secret"
    assert __import__("os").environ["KSYUN_REGION"] == "online"
    assert __import__("os").environ["KSADK_WEB_SEARCH_API_KEY"] == "old-search-key"
    assert "fixture-search-key" not in result.output


def test_explicit_proxy_and_base_url_alias_override_saved_and_inherited_values(
    tmp_path, monkeypatch
):
    from ksadk.studio.configuration import WorkspaceConfiguration
    from ksadk.studio.workspace import Workspace

    WorkspaceConfiguration(Workspace(tmp_path)).update_settings({"codexProxy": "direct"})
    env_file = tmp_path / "override.env"
    env_file.write_text("OPENAI_API_BASE=https://explicit.example/v1\n")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://inherited.example/v1")
    captured = {}

    def capture(app, **kwargs):
        import os

        service = app.state.studio_service
        captured["proxy"] = service.get_settings()["codexProxy"]
        captured["base"] = os.environ["OPENAI_BASE_URL"]
        captured["alias"] = os.environ["OPENAI_API_BASE"]
        captured["saved"] = service.configuration.settings()["codexProxy"]

    monkeypatch.setattr("ksadk.cli.cmd_studio.uvicorn.run", capture)
    for mode in ("forced", "auto"):
        result = CliRunner().invoke(
            studio, [str(tmp_path), "--no-open", "--env-file", str(env_file), "--codex-proxy", mode]
        )
        assert result.exit_code == 0, result.output
        assert captured == {
            "proxy": mode,
            "base": "https://explicit.example/v1",
            "alias": "https://explicit.example/v1",
            "saved": "direct",
        }

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
    assert opened[0].startswith("http://127.0.0.1:8899/#session=")
    assert (tmp_path / "workspace/agentkit.yaml").is_file()


def test_studio_cli_no_open_does_not_launch_browser(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("ksadk.cli.cmd_studio.uvicorn.run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "ksadk.cli.cmd_studio.webbrowser.open",
        lambda _url: (_ for _ in ()).throw(AssertionError("must not open")),
    )

    result = CliRunner().invoke(studio, [str(tmp_path), "--no-open"])

    assert result.exit_code == 0
    assert "127.0.0.1:7831" in result.output
    assert "#session=" in result.output

from __future__ import annotations

from pathlib import Path
import sys

from click.testing import CliRunner

from ksadk.cli import _register_commands, cli


def _assert_bootstrap_args(captured: dict[str, object], venv_python: Path, command_args: list[str]) -> str:
    assert captured["file"] == str(venv_python)
    args = captured["args"]
    assert isinstance(args, list)
    assert args[:2] == [str(venv_python), "-c"]
    assert "from ksadk.cli import main; main()" in args[2]
    assert args[3:] == command_args
    return args[2]


def _write_project_venv(project_dir: Path) -> Path:
    venv_bin = project_dir / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    venv_python = venv_bin / "python"
    venv_python.write_text("#!/bin/sh\n", encoding="utf-8")
    return venv_python


def _capture_reexec(monkeypatch):
    import ksadk.cli.local_runtime as local_runtime

    captured: dict[str, object] = {}

    def _fake_execvpe(file: str, args: list[str], env: dict[str, str]) -> None:
        captured["file"] = file
        captured["args"] = args
        captured["env"] = env
        raise SystemExit(23)

    monkeypatch.delenv("AGENTENGINE_LOCAL_RUNTIME_VENV_REEXEC", raising=False)
    monkeypatch.setattr(local_runtime.sys, "executable", sys.executable, raising=False)
    monkeypatch.setattr(local_runtime.os, "execvpe", _fake_execvpe, raising=False)
    return local_runtime, captured


def test_run_reexecs_with_project_venv_python(monkeypatch, tmp_path: Path):
    project_dir = tmp_path / "demo-agent"
    project_dir.mkdir()
    venv_python = _write_project_venv(project_dir)
    local_runtime, captured = _capture_reexec(monkeypatch)
    _register_commands()

    result = CliRunner().invoke(
        cli,
        [
            "run",
            str(project_dir),
            "--port",
            "8899",
            "--interactive",
            "--no-trace",
            "--model",
            "demo-model",
            "--show-thinking",
            "--no-stream",
        ],
    )

    assert result.exit_code == 23
    bootstrap_code = _assert_bootstrap_args(
        captured,
        venv_python,
        [
            "run",
            str(project_dir.resolve()),
            "--port",
            "8899",
            "--interactive",
            "--no-trace",
            "--model",
            "demo-model",
            "--show-thinking",
            "--no-stream",
        ],
    )
    assert str(Path(local_runtime.__file__).resolve().parents[2]) in bootstrap_code
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["AGENTENGINE_LOCAL_RUNTIME_VENV_REEXEC"] == "1"


def test_run_reexec_bootstrap_includes_current_site_packages(monkeypatch, tmp_path: Path):
    project_dir = tmp_path / "demo-agent"
    project_dir.mkdir()
    venv_python = _write_project_venv(project_dir)
    local_runtime, captured = _capture_reexec(monkeypatch)
    fake_site_packages = str(tmp_path / "current-site-packages")
    Path(fake_site_packages).mkdir()
    monkeypatch.setattr(local_runtime, "_current_site_package_paths", lambda: [fake_site_packages])
    _register_commands()

    result = CliRunner().invoke(cli, ["run", str(project_dir), "--no-trace"])

    assert result.exit_code == 23
    bootstrap_code = _assert_bootstrap_args(
        captured,
        venv_python,
        [
            "run",
            str(project_dir.resolve()),
            "--port",
            "8080",
            "--no-trace",
        ],
    )
    assert fake_site_packages in bootstrap_code


def test_run_reexecs_when_venv_python_symlinks_to_current_python(monkeypatch, tmp_path: Path):
    project_dir = tmp_path / "demo-agent"
    project_dir.mkdir()
    venv_bin = project_dir / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    venv_python = venv_bin / "python"
    try:
        venv_python.symlink_to(sys.executable)
    except OSError:
        venv_python.write_text("#!/bin/sh\n", encoding="utf-8")
    _local_runtime, captured = _capture_reexec(monkeypatch)
    _register_commands()

    result = CliRunner().invoke(cli, ["run", str(project_dir), "--no-trace"])

    assert result.exit_code == 23
    _assert_bootstrap_args(
        captured,
        venv_python,
        [
            "run",
            str(project_dir.resolve()),
            "--port",
            "8080",
            "--no-trace",
        ],
    )


def test_a2a_serve_reexecs_with_project_venv_python(monkeypatch, tmp_path: Path):
    project_dir = tmp_path / "demo-agent"
    project_dir.mkdir()
    venv_python = _write_project_venv(project_dir)
    _local_runtime, captured = _capture_reexec(monkeypatch)
    _register_commands()

    result = CliRunner().invoke(
        cli,
        [
            "a2a",
            "serve",
            str(project_dir),
            "--host",
            "127.0.0.1",
            "--port",
            "9091",
            "--url",
            "http://example.test/a2a",
            "--name",
            "demo",
            "--description",
            "local a2a",
            "--skill",
            "echo",
            "--no-trace",
        ],
    )

    assert result.exit_code == 23
    _assert_bootstrap_args(
        captured,
        venv_python,
        [
            "a2a",
            "serve",
            str(project_dir.resolve()),
            "--host",
            "127.0.0.1",
            "--port",
            "9091",
            "--url",
            "http://example.test/a2a",
            "--name",
            "demo",
            "--description",
            "local a2a",
            "--skill",
            "echo",
            "--no-trace",
        ],
    )

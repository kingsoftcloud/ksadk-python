from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from ksadk.cli import _register_commands, cli
from ksadk.cli.cmd_managed_runtime import managed_runtime


def _manifest(path: Path, *, artifact_type: str = "ManagedRuntime") -> Path:
    manifest = path / "agentengine.yaml"
    manifest.write_text(
        "\n".join(
            [
                "name: hosted-codex",
                "version: 1.0.0",
                "framework: codex",
                f"artifact_type: {artifact_type}",
                "runtime:",
                "  name: codex",
                "  version: 0.147.0",
                "model: qwen3.7-flash",
                "prompt: hosted test",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return manifest


def test_managed_runtime_delegates_mounted_manifest_to_runtime_web(monkeypatch, tmp_path):
    calls: list[tuple[str, int, str, object, bool]] = []
    monkeypatch.setattr(
        "ksadk.cli.cmd_managed_runtime.web.callback",
        lambda *args: calls.append(args),
    )
    manifest = _manifest(tmp_path)

    result = CliRunner().invoke(
        managed_runtime,
        [str(manifest), "--port", "8088", "--host", "0.0.0.0"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [(str(tmp_path.resolve()), 8088, "0.0.0.0", None, True)]


def test_managed_runtime_rejects_non_declarative_manifest(monkeypatch, tmp_path):
    called = False

    def _unexpected(*_args):
        nonlocal called
        called = True

    monkeypatch.setattr("ksadk.cli.cmd_managed_runtime.web.callback", _unexpected)
    result = CliRunner().invoke(managed_runtime, [str(_manifest(tmp_path, artifact_type="Code"))])

    assert result.exit_code != 0
    assert "artifact_type=ManagedRuntime" in result.output
    assert called is False


def test_managed_runtime_is_registered_as_a_public_cli_command():
    _register_commands()
    result = CliRunner().invoke(cli, ["managed-runtime", "--help"])

    assert result.exit_code == 0, result.output
    assert "mounted, declarative" in result.output

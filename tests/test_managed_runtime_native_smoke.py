"""Cross-platform native Codex ManagedRuntime smoke test.

This test is intentionally self-contained so CI can run the same contract on
macOS, Windows, and Linux without Docker.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from importlib.metadata import version
from pathlib import Path

import pytest


def _unused_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _process_diagnostics(process: subprocess.Popen[str]) -> str:
    if process.stdout is None:
        return ""
    return process.stdout.read().strip()


def _wait_for_health(port: int, process: subprocess.Popen[str]) -> dict:
    deadline = time.monotonic() + 30
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            details = _process_diagnostics(process)
            pytest.fail(
                "ksadk web exited before health check "
                f"(code={process.returncode}): {details or '(no output)'}"
            )
        try:
            with urllib.request.urlopen(url, timeout=1) as response:  # noqa: S310
                import json

                return json.loads(response.read())
        except (OSError, urllib.error.URLError):
            time.sleep(0.25)
    pytest.fail("ksadk web did not become healthy within 30 seconds")


def test_codex_native_binary_and_web_start_on_current_os(tmp_path: Path) -> None:
    codex_cli_bin = pytest.importorskip(
        "codex_cli_bin",
        reason="install the codex extra to run native ManagedRuntime smoke",
    )
    runtime_version = version("openai-codex")
    codex_bin = codex_cli_bin.bundled_codex_path()
    completed = subprocess.run(
        [str(codex_bin), "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert runtime_version in f"{completed.stdout}\n{completed.stderr}"

    (tmp_path / "agentengine.yaml").write_text(
        (
            "name: native-codex-smoke\n"
            'version: "1.0.0"\n'
            "framework: codex\n"
            "artifact_type: ManagedRuntime\n"
            "runtime:\n"
            "  name: codex\n"
            f'  version: "{runtime_version}"\n'
            "model: gpt-5.1-codex\n"
            "prompt: |\n"
            "  Native ManagedRuntime smoke test.\n"
        ),
        encoding="utf-8",
    )
    port = _unused_local_port()
    env = dict(os.environ)
    env.update(
        {
            "OPENAI_API_KEY": "native-smoke-no-request",
            "OPENAI_BASE_URL": "https://api.openai.com/v1",
            "OPENAI_MODEL_NAME": "gpt-5.1-codex",
            # Keep the spawned CLI's Chinese diagnostic output portable on
            # non-interactive Windows runners as well as local terminals.
            "PYTHONUTF8": "1",
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "ksadk.cli",
            "web",
            str(tmp_path),
            "--port",
            str(port),
            "--no-open",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        health = _wait_for_health(port, process)
        assert health["status"] == "ok"
        assert health["framework"] == "codex"
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

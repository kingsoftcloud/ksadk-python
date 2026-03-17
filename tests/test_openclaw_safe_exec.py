import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory


REPO_ROOT = Path(__file__).resolve().parents[1]
SAFE_EXEC_SCRIPT = REPO_ROOT / "deploy" / "openclaw" / "safe-bin" / "openclaw-safe-exec"


def _run_sh_safe(workspace: Path, state_dir: Path, safe_bin_dir: Path, *args: str, extra_env: dict | None = None):
    env = os.environ.copy()
    env["OPENCLAW_SAFE_EXEC_COMMAND"] = "sh-safe"
    env["OPENCLAW_EXEC_SAFE_WORKSPACE_ROOT"] = str(workspace)
    env["OPENCLAW_EXEC_SAFE_STATE_DIR"] = str(state_dir)
    env["OPENCLAW_SAFE_BIN_DIR"] = str(safe_bin_dir)
    env["OPENCLAW_EXEC_STRICT_MODE"] = "true"
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        ["bash", str(SAFE_EXEC_SCRIPT), *args],
        cwd=str(workspace),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_bash_safe(workspace: Path, state_dir: Path, safe_bin_dir: Path, *args: str, extra_env: dict | None = None):
    env = os.environ.copy()
    env["OPENCLAW_SAFE_EXEC_COMMAND"] = "bash-safe"
    env["OPENCLAW_EXEC_SAFE_WORKSPACE_ROOT"] = str(workspace)
    env["OPENCLAW_EXEC_SAFE_STATE_DIR"] = str(state_dir)
    env["OPENCLAW_SAFE_BIN_DIR"] = str(safe_bin_dir)
    env["OPENCLAW_EXEC_STRICT_MODE"] = "true"
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        ["bash", str(SAFE_EXEC_SCRIPT), *args],
        cwd=str(workspace),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_web_safe(workspace: Path, state_dir: Path, safe_bin_dir: Path, *args: str):
    env = os.environ.copy()
    env["OPENCLAW_SAFE_EXEC_COMMAND"] = "web-safe"
    env["OPENCLAW_EXEC_SAFE_WORKSPACE_ROOT"] = str(workspace)
    env["OPENCLAW_EXEC_SAFE_STATE_DIR"] = str(state_dir)
    env["OPENCLAW_SAFE_BIN_DIR"] = str(safe_bin_dir)
    env["OPENCLAW_EXEC_STRICT_MODE"] = "true"

    return subprocess.run(
        ["bash", str(SAFE_EXEC_SCRIPT), *args],
        cwd=str(workspace),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_mcporter_safe(
    workspace: Path,
    state_dir: Path,
    safe_bin_dir: Path,
    fake_bin_dir: Path,
    *args: str,
):
    env = os.environ.copy()
    env["OPENCLAW_SAFE_EXEC_COMMAND"] = "mcporter"
    env["OPENCLAW_EXEC_SAFE_WORKSPACE_ROOT"] = str(workspace)
    env["OPENCLAW_EXEC_SAFE_STATE_DIR"] = str(state_dir)
    env["OPENCLAW_SAFE_BIN_DIR"] = str(safe_bin_dir)
    env["OPENCLAW_EXEC_STRICT_MODE"] = "true"
    env["PATH"] = f"{fake_bin_dir}:{env['PATH']}"

    return subprocess.run(
        ["bash", str(SAFE_EXEC_SCRIPT), *args],
        cwd=str(workspace),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_sh_safe_runs_workspace_script_with_scrubbed_env():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        state_dir = root / "state"
        safe_bin_dir = root / "safe-bin"
        script_path = workspace / "env-check.sh"

        workspace.mkdir()
        state_dir.mkdir()
        safe_bin_dir.mkdir()
        script_path.write_text(
            "#!/bin/sh\n"
            "printf 'api=%s\\n' \"${OPENCLAW_MODEL_API_KEY:-missing}\"\n"
            "printf 'kdocs=%s\\n' \"${KDOCS_TOKEN:-missing}\"\n"
            "printf 'path=%s\\n' \"$PATH\"\n"
            "printf 'home=%s\\n' \"$HOME\"\n"
            "printf 'shell=%s\\n' \"$SHELL\"\n"
        )

        result = _run_sh_safe(
            workspace,
            state_dir,
            safe_bin_dir,
            str(script_path),
            extra_env={
                "OPENCLAW_MODEL_API_KEY": "top-secret",
                "KDOCS_TOKEN": "kdocs-secret",
            },
        )

        assert result.returncode == 0, result.stderr or result.stdout
        assert "api=missing" in result.stdout
        assert "kdocs=missing" in result.stdout
        assert f"path={safe_bin_dir.resolve()}" in result.stdout
        assert f"home={workspace.resolve()}" in result.stdout
        assert "shell=/bin/sh" in result.stdout or "shell=/usr/bin/sh" in result.stdout


def test_sh_safe_rejects_shell_options():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        state_dir = root / "state"
        safe_bin_dir = root / "safe-bin"

        workspace.mkdir()
        state_dir.mkdir()
        safe_bin_dir.mkdir()

        result = _run_sh_safe(workspace, state_dir, safe_bin_dir, "-c", "echo hi")

        assert result.returncode == 126
        assert "does not accept shell options" in result.stderr


def test_sh_safe_rejects_scripts_outside_workspace():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        state_dir = root / "state"
        safe_bin_dir = root / "safe-bin"
        outside_script = root / "outside.sh"

        workspace.mkdir()
        state_dir.mkdir()
        safe_bin_dir.mkdir()
        outside_script.write_text("#!/bin/sh\nexit 0\n")

        result = _run_sh_safe(workspace, state_dir, safe_bin_dir, str(outside_script))

        assert result.returncode == 126
        assert "path must remain inside workspace" in result.stderr


def test_bash_safe_runs_workspace_script_with_scrubbed_env():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        state_dir = root / "state"
        safe_bin_dir = root / "safe-bin"
        script_path = workspace / "env-check.sh"

        workspace.mkdir()
        state_dir.mkdir()
        safe_bin_dir.mkdir()
        script_path.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'api=%s\\n' \"${OPENCLAW_MODEL_API_KEY:-missing}\"\n"
            "printf 'home=%s\\n' \"$HOME\"\n"
            "printf 'shell=%s\\n' \"$SHELL\"\n"
        )

        result = _run_bash_safe(
            workspace,
            state_dir,
            safe_bin_dir,
            str(script_path),
            extra_env={"OPENCLAW_MODEL_API_KEY": "top-secret"},
        )

        assert result.returncode == 0, result.stderr or result.stdout
        assert "api=missing" in result.stdout
        assert f"home={workspace.resolve()}" in result.stdout
        assert "shell=/bin/bash" in result.stdout or "shell=/usr/bin/bash" in result.stdout


def test_bash_safe_rejects_shell_options():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        state_dir = root / "state"
        safe_bin_dir = root / "safe-bin"

        workspace.mkdir()
        state_dir.mkdir()
        safe_bin_dir.mkdir()

        result = _run_bash_safe(workspace, state_dir, safe_bin_dir, "-c", "echo hi")

        assert result.returncode == 126
        assert "does not accept shell options" in result.stderr


def test_bash_safe_relaxed_mode_allows_shell_options_and_state_access():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        state_dir = root / "state"
        safe_bin_dir = root / "safe-bin"
        marker_path = state_dir / "relaxed-ok.txt"

        workspace.mkdir()
        state_dir.mkdir()
        safe_bin_dir.mkdir()

        result = _run_bash_safe(
            workspace,
            state_dir,
            safe_bin_dir,
            "-lc",
            f"echo ok > {marker_path}",
            extra_env={"OPENCLAW_EXEC_STRICT_MODE": "false"},
        )

        assert result.returncode == 0, result.stderr or result.stdout
        assert marker_path.read_text().strip() == "ok"


def test_bash_safe_allows_allowlisted_state_kdocs_setup_script():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        state_dir = root / "state"
        safe_bin_dir = root / "safe-bin"
        script_path = state_dir / "skills" / "kdocs" / "setup.sh"

        workspace.mkdir()
        state_dir.mkdir()
        safe_bin_dir.mkdir()
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'home=%s\\n' \"$HOME\"\n"
            "if command -v grep >/dev/null 2>&1; then printf 'grep=ok\\n'; fi\n"
        )
        script_path.chmod(0o755)

        result = _run_bash_safe(workspace, state_dir, safe_bin_dir, str(script_path))

        assert result.returncode == 0, result.stderr or result.stdout
        assert f"home={state_dir.resolve()}" in result.stdout
        assert "grep=ok" in result.stdout


def test_bash_safe_rejects_non_allowlisted_state_scripts():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        state_dir = root / "state"
        safe_bin_dir = root / "safe-bin"
        script_path = state_dir / "skills" / "other-skill" / "setup.sh"

        workspace.mkdir()
        state_dir.mkdir()
        safe_bin_dir.mkdir()
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text("#!/usr/bin/env bash\nexit 0\n")
        script_path.chmod(0o755)

        result = _run_bash_safe(workspace, state_dir, safe_bin_dir, str(script_path))

        assert result.returncode == 126
        assert "state directory script is blocked unless allowlisted" in result.stderr


def test_web_safe_rejects_private_targets():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        state_dir = root / "state"
        safe_bin_dir = root / "safe-bin"

        workspace.mkdir()
        state_dir.mkdir()
        safe_bin_dir.mkdir()

        result = _run_web_safe(
            workspace,
            state_dir,
            safe_bin_dir,
            "read",
            "http://127.0.0.1:8080/health",
        )

        assert result.returncode == 126
        assert "non-public address" in result.stderr or "host is blocked" in result.stderr


def test_web_safe_requires_query_for_search():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        state_dir = root / "state"
        safe_bin_dir = root / "safe-bin"

        workspace.mkdir()
        state_dir.mkdir()
        safe_bin_dir.mkdir()

        result = _run_web_safe(workspace, state_dir, safe_bin_dir, "search")

        assert result.returncode == 2
        assert "search requires a query" in result.stderr


def test_mcporter_allows_agent_reach_namespaces():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        state_dir = root / "state"
        safe_bin_dir = root / "safe-bin"
        fake_bin_dir = root / "bin"
        fake_mcporter = fake_bin_dir / "mcporter"

        workspace.mkdir()
        state_dir.mkdir()
        safe_bin_dir.mkdir()
        fake_bin_dir.mkdir()
        fake_mcporter.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\"\n")
        fake_mcporter.chmod(0o755)

        result = _run_mcporter_safe(
            workspace,
            state_dir,
            safe_bin_dir,
            fake_bin_dir,
            "call",
            'exa.web_search_exa(query: "OpenClaw", numResults: 1)',
        )

        assert result.returncode == 0, result.stderr or result.stdout
        assert 'exa.web_search_exa(query: "OpenClaw", numResults: 1)' in result.stdout


def test_mcporter_rejects_non_allowlisted_namespaces():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        state_dir = root / "state"
        safe_bin_dir = root / "safe-bin"
        fake_bin_dir = root / "bin"
        fake_mcporter = fake_bin_dir / "mcporter"

        workspace.mkdir()
        state_dir.mkdir()
        safe_bin_dir.mkdir()
        fake_bin_dir.mkdir()
        fake_mcporter.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\"\n")
        fake_mcporter.chmod(0o755)

        result = _run_mcporter_safe(
            workspace,
            state_dir,
            safe_bin_dir,
            fake_bin_dir,
            "call",
            'evil.do_thing()',
        )

        assert result.returncode == 126
        assert "restricted to built-in namespaces" in result.stderr

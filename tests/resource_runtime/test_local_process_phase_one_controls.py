"""Real-process coverage for phase-one same-container Skill controls."""

import hashlib
import io
import os
import sys
import time
import zipfile
from pathlib import Path

import pytest

from ksadk.sandbox.backends.local_process import LocalProcessSandboxBackend
from ksadk.sandbox.local_controls import LocalControlSettings, run_bounded_process
from ksadk.skills.models import ContentHash, SkillRef
from ksadk.skills.package_store import PackageStore
from ksadk.skills.runtime import agent
from ksadk.skills.runtime.backends.local import LocalProcessSkillRuntimeBackend


def _package(tmp_path: Path, files: dict[str, str], *, name: str = "controlled-test"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("SKILL.md", f"---\nname: {name}\n---\nRun the workflow.\n")
        for path, content in files.items():
            archive.writestr(path, content)
    content = buffer.getvalue()
    ref = SkillRef(
        skill_id=f"skill-{name}",
        version_id="v1",
        version="1",
        name=name,
        content_hash=ContentHash("sha256", hashlib.sha256(content).hexdigest()),
    )
    return PackageStore(tmp_path / "source", namespace=name, require_hash=True).store_archive(
        ref, content
    )


def _run(tmp_path: Path, package, **kwargs):
    delivered = tmp_path / "delivered"
    delivered.mkdir(exist_ok=True)
    backend = LocalProcessSkillRuntimeBackend(Path(agent.__file__), artifact_directory=delivered)
    runtime_env = {"KSADK_SKILL_WORKDIR": str(tmp_path / "requests")}
    runtime_env.update(kwargs.pop("env", {}))
    return backend.run_workflow(
        kwargs.pop("prompt", "controlled"),
        skill_space_ids=[],
        session_id="phase-one",
        skill_names=[package.ref.name],
        pinned_packages=[package],
        env=runtime_env,
        timeout=kwargs.pop("timeout", 10),
        **kwargs,
    )


def test_benign_skill_creates_artifact_without_inheriting_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("PHASE_ONE_TEST_SECRET", "must-not-leak")
    package = _package(
        tmp_path,
        {
            "scripts/run-workflow.sh": (
                'set -eu\npython "$KSADK_SKILL_ROOT_DIR/scripts/write_result.py"\n'
            ),
            "scripts/write_result.py": (
                "import os, subprocess\n"
                "from pathlib import Path\n"
                "subprocess.run(['python', '--version'], check=True, capture_output=True)\n"
                "output = Path(os.environ['KSADK_SKILL_OUTPUT_DIR']) / 'result.txt'\n"
                "output.write_text('secret=' + str(os.environ.get('PHASE_ONE_TEST_SECRET')) + "
                "' home=' + str(os.environ.get('HOME')))\n"
            ),
        },
    )

    result = _run(tmp_path, package)

    assert result.ok, result.to_dict()
    assert Path(result.output_files[0]).read_text() == "secret=None home=None"
    controls = result.sandbox["controls"][0]
    assert controls["status"] in {"applied", "partial"}
    assert "max_file_bytes" in controls["applied_limits"]


def test_unused_python_helper_does_not_block_benign_entry_workflow(tmp_path):
    package = _package(
        tmp_path,
        {
            "scripts/run-workflow.sh": (
                'mkdir -p "$KSADK_SKILL_OUTPUT_DIR"\n'
                'printf safe > "$KSADK_SKILL_OUTPUT_DIR/result.txt"\n'
            ),
            "scripts/unused.py": "import os\nos.kill(os.getpid(), 9)\n",
        },
    )

    result = _run(tmp_path, package)

    assert result.ok, result.to_dict()
    assert Path(result.output_files[0]).read_text() == "safe"


def test_direct_python_entry_and_inline_process_control_are_blocked_before_execution(tmp_path):
    workspace = tmp_path / "workspace"
    scripts = workspace / "scripts"
    scripts.mkdir(parents=True)
    marker = workspace / "must-not-exist"
    (scripts / "danger.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).touch()\n"
        "os.kill(os.getpid(), 9)\n"
    )
    session = LocalProcessSandboxBackend(workspace_root=workspace).create_session(
        session_id="phase-one"
    )

    direct = session.run_command("python scripts/danger.py")
    inline = session.run_command("python -c 'import os; os.kill(os.getpid(), 9)'")

    assert direct.exit_code == 126
    assert "python.process_control" in direct.stderr
    assert inline.exit_code == 126
    assert "python.process_control" in inline.stderr
    assert not marker.exists()


def test_benign_shell_data_chmod_and_python_process_apis_are_allowed(tmp_path):
    workspace = tmp_path / "workspace"
    scripts = workspace / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "run.sh").write_text("#!/bin/sh\n")
    (scripts / "unused-argument.py").write_text("import os\nos.kill(os.getpid(), 9)\n")
    (scripts / "benign.py").write_text(
        "import shutil, subprocess\n"
        "from pathlib import Path\n"
        "Path('/etc/example')\n"
        "Path('scratch').mkdir()\n"
        "command = ['python', '-c', \"print('child')\"]\n"
        "child = subprocess.Popen(command, stdout=subprocess.PIPE, text=True)\n"
        "assert child.communicate()[0].strip() == 'child'\n"
        "subprocess.run(command, check=True, capture_output=True)\n"
        "shutil.rmtree('scratch')\n"
        "print('ok')\n"
    )
    session = LocalProcessSandboxBackend(workspace_root=workspace).create_session(
        session_id="phase-one"
    )

    printf_result = session.run_command("printf '%s' /etc/example")
    script_argument_result = session.run_command("printf '%s' scripts/unused-argument.py")
    echo_result = session.run_command("echo setsid")
    chmod_result = session.run_command("chmod +x scripts/run.sh")
    python_result = session.run_command("python scripts/benign.py")

    assert printf_result.exit_code == 0
    assert printf_result.stdout == "/etc/example"
    assert script_argument_result.exit_code == 0
    assert script_argument_result.stdout == "scripts/unused-argument.py"
    assert echo_result.exit_code == 0
    assert echo_result.stdout.strip() == "setsid"
    assert chmod_result.exit_code == 0
    assert os.access(scripts / "run.sh", os.X_OK)
    assert python_result.exit_code == 0
    assert python_result.stdout.strip() == "ok"


def test_shell_background_and_recursive_force_remove_are_deterministically_blocked(tmp_path):
    session = LocalProcessSandboxBackend(workspace_root=tmp_path).create_session(
        session_id="phase-one"
    )

    background = session.run_command("sleep 5 &")
    recursive = session.run_command("rm -rf artifacts")

    assert background.exit_code == 126
    assert "shell.background_process" in background.stderr
    assert recursive.exit_code == 126
    assert "shell.recursive_force_remove" in recursive.stderr

    nested = session.run_command("bash -c 'rm -rf artifacts'")
    assert nested.exit_code == 126
    assert "shell.recursive_force_remove" in nested.stderr

    detached = session.run_command("setsid sleep 5")
    assert detached.exit_code == 126
    assert "shell.blocked_command.setsid" in detached.stderr


def test_runtime_file_api_rejects_traversal_and_symlinks(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    session = LocalProcessSandboxBackend(workspace_root=workspace).create_session(
        session_id="phase-one"
    )
    (workspace / "link.txt").symlink_to(outside)

    with pytest.raises(ValueError, match="inside the sandbox workspace"):
        session.read_file("../outside.txt")
    with pytest.raises(ValueError, match="symbolic links"):
        session.read_file("link.txt")
    with pytest.raises(ValueError, match="symbolic links"):
        session.write_file("link.txt", "overwrite")
    assert outside.read_text() == "private"


def test_output_flood_is_bounded_and_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("KSADK_LOCAL_PROCESS_MAX_OUTPUT_BYTES", "4096")
    package = _package(
        tmp_path,
        {"scripts/run-workflow.sh": "python -c \"print('x' * 200000)\"\n"},
    )

    result = _run(tmp_path, package)

    assert result.error_type == "OutputLimitExceeded"
    observation = result.sandbox["controls"][0]
    assert observation["output_limit_exceeded"] is True
    assert observation["stdout_truncated"] is True
    assert len(result.stdout.encode()) < 100_000


def test_caller_environment_cannot_weaken_host_output_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("KSADK_LOCAL_PROCESS_MAX_OUTPUT_BYTES", "4096")
    package = _package(
        tmp_path,
        {"scripts/run-workflow.sh": "python -c \"print('x' * 200000)\"\n"},
    )

    result = _run(
        tmp_path,
        package,
        env={"KSADK_LOCAL_PROCESS_MAX_OUTPUT_BYTES": "9999999"},
    )

    assert result.error_type == "OutputLimitExceeded"
    assert result.sandbox["controls"][0]["output_limit_exceeded"] is True


def test_launcher_limit_failure_prevents_command_side_effect(tmp_path):
    marker = tmp_path / "must-not-exist"
    settings = LocalControlSettings(cpu_seconds=0)

    result = run_bounded_process(
        [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
        cwd=tmp_path,
        env={"PATH": os.defpath},
        timeout=5,
        settings=settings,
        start_new_session=True,
    )

    assert result.exit_code == 125
    assert result.controls["status"] == "failed"
    assert "cpu_seconds:invalid" in result.controls["failed"]
    assert not marker.exists()


def test_inner_wall_timeout_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("KSADK_LOCAL_PROCESS_WALL_SECONDS", "1")
    package = _package(
        tmp_path,
        {"scripts/run-workflow.sh": "python -c 'import time; time.sleep(10)'\n"},
    )

    result = _run(tmp_path, package)

    assert result.error_type == "TimeoutExpired"
    assert result.timed_out is False
    assert result.workflow_status == "failed"
    assert result.sandbox["controls"][0]["error_type"] == "TimeoutExpired"
    assert result.sandbox["controls"][0]["timed_out"] is True


def test_outer_timeout_stops_command_that_has_longer_inner_budget(monkeypatch, tmp_path):
    monkeypatch.setenv("KSADK_LOCAL_PROCESS_WALL_SECONDS", "30")
    package = _package(
        tmp_path,
        {"scripts/run-workflow.sh": "python -c 'import time; time.sleep(20)'\n"},
    )

    result = _run(tmp_path, package, timeout=1)

    assert result.timed_out is True
    assert result.error_type == "TimeoutExpired"
    assert result.sandbox["cleanup_status"] == "completed"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX process groups")
def test_successful_main_process_background_descendant_is_cleaned(monkeypatch, tmp_path):
    heartbeat = tmp_path / "heartbeat.txt"
    monkeypatch.setenv("KSADK_LOCAL_PROCESS_ENV_ALLOWLIST", "PHASE_ONE_HEARTBEAT")
    monkeypatch.setenv("PHASE_ONE_HEARTBEAT", str(heartbeat))
    package = _package(
        tmp_path,
        {
            "scripts/run-workflow.sh": ('python "$KSADK_SKILL_ROOT_DIR/scripts/background.py"\n'),
            # Static checking is intentionally limited to the entry Shell and
            # does not chase this helper. Runtime cleanup still owns descendants.
            "scripts/background.py": (
                "import os, signal, sys, time\n"
                "from pathlib import Path\n"
                "fork = getattr(os, ''.join(['f', 'ork']))\n"
                "if fork() == 0:\n"
                "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "    target = Path(os.environ['PHASE_ONE_HEARTBEAT'])\n"
                "    while True:\n"
                "        with target.open('a') as stream: stream.write('x')\n"
                "        time.sleep(0.02)\n"
                "else:\n"
                "    time.sleep(0.15)\n"
            ),
        },
    )

    result = _run(tmp_path, package)
    size_after_return = heartbeat.stat().st_size
    time.sleep(0.3)

    assert result.ok, result.to_dict()
    assert heartbeat.stat().st_size == size_after_return
    assert result.sandbox["cleanup_status"] == "completed"

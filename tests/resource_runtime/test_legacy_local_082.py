from __future__ import annotations

import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from ksadk.sandbox.base import SandboxCommandResult
from ksadk.skills.models import ContentHash, SkillRef
from ksadk.skills.package_store import PackageStore
from ksadk.skills.runtime.backends.e2b import E2BSkillRuntimeBackend
from ksadk.skills.runtime.legacy_local import LEGACY_LOCAL_PROTOCOL


def _package(tmp_path: Path, *, fail: bool = False, symlink: bool = False):
    buffer = io.BytesIO()
    exit_line = "exit 7\n" if fail else ""
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("SKILL.md", "---\nname: legacy-test\n---\nUse the locked script.")
        archive.writestr("reference.txt", "locked-version-one")
        artifact_command = (
            'ln -s "$KSADK_SKILL_ROOT_DIR/reference.txt" '
            '"$KSADK_SKILL_OUTPUT_DIR/result.txt"\n'
            if symlink
            else 'cat "$KSADK_SKILL_ROOT_DIR/reference.txt" '
            '> "$KSADK_SKILL_OUTPUT_DIR/result.txt"\n'
        )
        archive.writestr("scripts/run-workflow.sh", "set -eu\n" + artifact_command + exit_line)
    content = buffer.getvalue()
    ref = SkillRef(
        skill_id="skill-a",
        version_id="version-a",
        version="1",
        name="legacy-test",
        content_hash=ContentHash("sha256", hashlib.sha256(content).hexdigest()),
    )
    return PackageStore(
        tmp_path / ("source-fail" if fail else "source"),
        namespace="test",
        require_hash=True,
    ).store_archive(ref, content)


class CommandExitException(RuntimeError):
    def __init__(self, exit_code: int):
        super().__init__(f"exit {exit_code}")
        self.exit_code = exit_code
        self.stdout = ""
        self.stderr = ""


class TimeoutException(RuntimeError):
    pass


class LegacySandboxDouble:
    sandbox_id = "legacy-sandbox"

    def __init__(
        self,
        root: Path,
        *,
        version: str = "0.8.2",
        agent_python: str | None = None,
    ):
        self.root = root
        self.root.mkdir()
        self.version = version
        self.agent_python = agent_python or sys.executable
        self.commands: list[tuple[str, dict[str, str]]] = []
        self.writes: dict[str, bytes] = {}
        self.killed = False

    def _path(self, remote: str) -> Path:
        return self.root / remote.lstrip("/")

    def write_file(self, path: str, data: str | bytes) -> None:
        content = data.encode() if isinstance(data, str) else data
        target = self._path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        self.writes[path] = content

    def read_file(self, path: str) -> str:
        return self._path(path).read_text()

    def read_file_bytes(self, path: str, *, max_bytes: int) -> bytes:
        content = self._path(path).read_bytes()
        assert len(content) <= max_bytes
        return content

    def run_command(self, command: str, **kwargs):
        env = dict(kwargs.get("env") or {})
        self.commands.append((command, env))
        if "load_local_skills" in command and "parse_workflow_request" in command:
            return SandboxCommandResult(stdout=self.version + "\n", exit_code=0)
        if command.startswith("mkdir "):
            args = shlex.split(command)
            paths = [item for item in args[1:] if item not in {"-m", "700", "-p"}]
            for path in paths:
                self._path(path).mkdir(parents=True, exist_ok=True)
            return SandboxCommandResult(exit_code=0)
        if command.startswith("chmod 700 "):
            for path in shlex.split(command)[2:]:
                self._path(path).chmod(0o700)
            return SandboxCommandResult(exit_code=0)
        if "ksadk.skills.runtime.agent --request-file" in command:
            tokens = shlex.split(command)
            request_path = tokens[tokens.index("--request-file") + 1]
            stdout_path = tokens[tokens.index(">") + 1]
            stderr_path = tokens[tokens.index("2>") + 1]
            process_env = {
                "PATH": "/usr/bin:/bin",
                **{
                    key: (str(self._path(value)) if value.startswith("/tmp/") else value)
                    for key, value in env.items()
                },
            }
            result = subprocess.run(
                [
                    self.agent_python,
                    "-I",
                    "-m",
                    "ksadk.skills.runtime.agent",
                    "--request-file",
                    str(self._path(request_path)),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
                env=process_env,
            )
            self.write_file(stdout_path, result.stdout)
            self.write_file(stderr_path, result.stderr)
            if result.returncode:
                raise CommandExitException(result.returncode)
            return SandboxCommandResult(exit_code=0)
        if command.startswith("python -I "):
            args = shlex.split(command)
            script, work, manifest, destination, recover = args[2:]
            manifest_path = self._path(manifest)
            payload = json.loads(manifest_path.read_text())
            payload["output_files"] = [
                str(self._path(item)) if item.startswith("/tmp/") else item
                for item in payload["output_files"]
            ]
            manifest_path.write_text(json.dumps(payload))
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(self._path(script)),
                    str(self._path(work)),
                    str(manifest_path),
                    str(self._path(destination)),
                    recover,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            return SandboxCommandResult(
                stdout=result.stdout, stderr=result.stderr, exit_code=result.returncode
            )
        raise AssertionError(command)

    def kill(self):
        self.killed = True


def _backend(session: LegacySandboxDouble, artifact_directory: Path):
    backend = E2BSkillRuntimeBackend(
        template_id="fixture-template", artifact_directory=artifact_directory
    )
    backend.sandbox_backend = SimpleNamespace(create_session=lambda **kwargs: session)
    return backend


def test_legacy_mode_uploads_verified_tree_and_downloads_artifact(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("KSADK_SKILL_SANDBOX_PROTOCOL", LEGACY_LOCAL_PROTOCOL)
    pinned = _package(tmp_path)
    (pinned.root_dir / "reference.txt").write_text("mutated-cache-content")
    session = LegacySandboxDouble(tmp_path / "sandbox")

    result = _backend(session, tmp_path / "host-artifacts").run_workflow(
        "run legacy-test",
        skill_space_ids=["must-not-reach-sandbox"],
        session_id="fixture",
        skill_names=["legacy-test"],
        pinned_packages=[pinned],
        env={"KSADK_SKILL_SERVICE_TOKEN": "must-be-cleared"},
        timeout=10,
    )

    assert result.ok, (result.stdout, result.stderr, result.error_message)
    assert Path(result.output_files[0]).read_text() == "locked-version-one"
    assert session.killed
    assert result.sandbox["skill_delivery_protocol"] == LEGACY_LOCAL_PROTOCOL
    assert result.sandbox["artifact_collection_status"] == "completed"
    assert result.sandbox["artifact_collection_source"] == "workflow_result"
    workflow_request = json.loads(
        next(
            data
            for path, data in session.writes.items()
            if path.endswith("workflow-request.json")
        )
    )
    assert workflow_request == {
        "workflow_prompt": "run legacy-test",
        "skill_names": ["legacy-test"],
    }
    execution_env = next(
        env for command, env in session.commands if "ksadk.skills.runtime.agent" in command
    )
    assert execution_env["KSADK_SKILL_SPACE_IDS"] == ""
    assert execution_env["SKILL_SPACE_ID"] == ""
    assert execution_env["KSADK_SKILL_SERVICE_TOKEN"] == ""
    assert execution_env["KSADK_LOCAL_SKILLS_DIR"].startswith("/tmp/ksadk-legacy-")
    shutil.rmtree(Path(result.output_files[0]).parents[1])


def test_legacy_mode_recovers_artifact_after_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("KSADK_SKILL_SANDBOX_PROTOCOL", LEGACY_LOCAL_PROTOCOL)
    session = LegacySandboxDouble(tmp_path / "sandbox")

    result = _backend(session, tmp_path / "host-artifacts").run_workflow(
        "run legacy-test",
        skill_space_ids=[],
        session_id="fixture",
        skill_names=["legacy-test"],
        pinned_packages=[_package(tmp_path, fail=True)],
        timeout=10,
    )

    assert not result.ok
    # The 0.8.2 agent normalizes a failed workflow process to agent exit code 1.
    assert result.exit_code == 1
    assert Path(result.output_files[0]).read_text() == "locked-version-one"
    assert result.sandbox["artifact_collection_status"] == "completed"
    assert result.sandbox["artifact_collection_source"] == "workflow_result"
    assert result.sandbox["cleanup_status"] == "completed"
    shutil.rmtree(Path(result.output_files[0]).parents[1])


def test_legacy_mode_scans_owned_artifact_directory_after_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("KSADK_SKILL_SANDBOX_PROTOCOL", LEGACY_LOCAL_PROTOCOL)

    class TimeoutSandbox(LegacySandboxDouble):
        def run_command(self, command: str, **kwargs):
            if "ksadk.skills.runtime.agent --request-file" not in command:
                return super().run_command(command, **kwargs)
            env = dict(kwargs.get("env") or {})
            self.commands.append((command, env))
            tokens = shlex.split(command)
            stdout_path = tokens[tokens.index(">") + 1]
            stderr_path = tokens[tokens.index("2>") + 1]
            artifact = self._path(env["KSADK_SKILL_WORKDIR"]) / "artifacts/partial.txt"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("partial-before-timeout")
            self.write_file(stdout_path, "")
            self.write_file(stderr_path, "timed out")
            raise TimeoutException("fixture timeout")

    session = TimeoutSandbox(tmp_path / "sandbox")
    result = _backend(session, tmp_path / "host-artifacts").run_workflow(
        "run legacy-test",
        skill_space_ids=[],
        session_id="fixture",
        skill_names=["legacy-test"],
        pinned_packages=[_package(tmp_path)],
        timeout=10,
    )

    assert result.timed_out
    assert Path(result.output_files[0]).read_text() == "partial-before-timeout"
    assert result.sandbox["artifact_collection_status"] == "completed"
    assert result.sandbox["artifact_collection_source"] == "recovery_scan"
    assert result.sandbox["cleanup_status"] == "completed"
    shutil.rmtree(Path(result.output_files[0]).parents[1])


def test_legacy_mode_rejects_symlink_artifact_and_still_cleans_sandbox(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("KSADK_SKILL_SANDBOX_PROTOCOL", LEGACY_LOCAL_PROTOCOL)
    session = LegacySandboxDouble(tmp_path / "sandbox")

    result = _backend(session, tmp_path / "host-artifacts").run_workflow(
        "run legacy-test",
        skill_space_ids=[],
        session_id="fixture",
        skill_names=["legacy-test"],
        pinned_packages=[_package(tmp_path, symlink=True)],
        timeout=10,
    )

    assert not result.ok
    assert result.output_files == []
    assert result.sandbox["artifact_collection_status"] == "failed"
    assert result.sandbox["cleanup_status"] == "completed"
    assert session.killed


def test_legacy_mode_rejects_other_sandbox_version_before_upload(tmp_path, monkeypatch):
    monkeypatch.setenv("KSADK_SKILL_SANDBOX_PROTOCOL", LEGACY_LOCAL_PROTOCOL)
    session = LegacySandboxDouble(tmp_path / "sandbox", version="0.8.4")

    result = _backend(session, tmp_path / "host-artifacts").run_workflow(
        "run",
        skill_space_ids=[],
        session_id="fixture",
        pinned_packages=[_package(tmp_path)],
    )

    assert not result.ok
    assert "does not match legacy local KsADK 0.8.2" in result.error_message
    assert not any(path.endswith("SKILL.md") for path in session.writes)
    assert session.killed


@pytest.mark.skipif(
    not os.environ.get("KSADK_TEST_LEGACY_082_PYTHON"),
    reason="set KSADK_TEST_LEGACY_082_PYTHON to an isolated KsADK 0.8.2 interpreter",
)
def test_legacy_mode_executes_with_isolated_real_082_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("KSADK_SKILL_SANDBOX_PROTOCOL", LEGACY_LOCAL_PROTOCOL)
    session = LegacySandboxDouble(
        tmp_path / "sandbox",
        agent_python=os.environ["KSADK_TEST_LEGACY_082_PYTHON"],
    )

    result = _backend(session, tmp_path / "host-artifacts").run_workflow(
        "run legacy-test",
        skill_space_ids=[],
        session_id="fixture",
        skill_names=["legacy-test"],
        pinned_packages=[_package(tmp_path)],
        timeout=10,
    )

    assert result.ok, (result.stdout, result.stderr, result.error_message)
    assert Path(result.output_files[0]).read_text() == "locked-version-one"
    assert result.sandbox["artifact_collection_status"] == "completed"
    shutil.rmtree(Path(result.output_files[0]).parents[1])


@pytest.mark.parametrize("value", ["auto", "0.8.2", "legacy"])
def test_unknown_protocol_is_rejected_before_sandbox_creation(tmp_path, monkeypatch, value):
    monkeypatch.setenv("KSADK_SKILL_SANDBOX_PROTOCOL", value)
    session = LegacySandboxDouble(tmp_path / "sandbox")

    result = _backend(session, tmp_path / "host-artifacts").run_workflow(
        "run",
        skill_space_ids=[],
        session_id="fixture",
        pinned_packages=[_package(tmp_path)],
    )
    assert not result.ok
    assert "KSADK_SKILL_SANDBOX_PROTOCOL" in result.error_message
    assert result.sandbox["instance_status"] == "not_created"
    assert session.commands == []

import hashlib
import io
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ksadk.sandbox.base import SandboxCommandResult
from ksadk.skills.models import ContentHash, SkillRef
from ksadk.skills.package_store import PackageStore, SkillPackageError
from ksadk.skills.runtime import agent
from ksadk.skills.runtime.backends.e2b import E2BSkillRuntimeBackend
from ksadk.skills.runtime.backends.local import LocalProcessSkillRuntimeBackend
from ksadk.skills.runtime.pinned import PinnedSkillArchive, load_pinned_packages, stage_packages
from ksadk.skills.runtime.request import SkillWorkflowRequestError, parse_workflow_request


def package(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("SKILL.md", "---\nname: pinned-test\n---\nUse the pinned script.")
        archive.writestr("reference.txt", "locked-version-one")
        archive.writestr(
            "scripts/run-workflow.sh",
            'set -eu\ntest -z "${KSADK_TEST_UNRELATED_SECRET:-}"\n'
            'cat "$KSADK_SKILL_ROOT_DIR/reference.txt" > "$KSADK_SKILL_OUTPUT_DIR/result.txt"\n',
        )
    content = buffer.getvalue()
    ref = SkillRef(
        skill_id="skill-a",
        version_id="v1",
        version="1",
        name="pinned-test",
        content_hash=ContentHash("sha256", hashlib.sha256(content).hexdigest()),
    )
    return PackageStore(tmp_path / "source", namespace="test", require_hash=True).store_archive(
        ref, content
    )


def test_real_subprocess_executes_locked_script_with_reference_file(tmp_path, monkeypatch):
    pinned = package(tmp_path)
    # Mutated extracted files must not become execution inputs: transport the ZIP.
    (pinned.root_dir / "reference.txt").write_text("unlocked-version-two")
    monkeypatch.setenv("KSADK_TEST_UNRELATED_SECRET", "fake-secret")
    monkeypatch.setenv("KSADK_LOCAL_SKILLS_DIR", str(pinned.root_dir.parent))
    monkeypatch.setenv("KSADK_SKILL_SERVICE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("KSADK_SELECTED_SKILL_NAMES", "unbound-skill")
    result = LocalProcessSkillRuntimeBackend(Path(agent.__file__)).run_workflow(
        "run pinned-test",
        skill_space_ids=[],
        session_id="test-session",
        pinned_packages=[pinned],
        env={"KSADK_SKILL_WORKDIR": str(tmp_path / "work")},
        timeout=10,
    )
    assert result.ok, (result.stdout, result.stderr)
    assert len(result.output_files) == 1
    assert Path(result.output_files[0]).read_text() == "locked-version-one"
    payload = json.loads(
        next(
            line.split("=", 1)[1]
            for line in result.stdout.splitlines()
            if line.startswith("workflow_result=")
        )
    )
    assert payload["loaded_skills"] == ["pinned-test"]
    assert payload["executed_skill"] == "pinned-test"


def test_consumer_rejects_changed_archive(tmp_path):
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    entries = stage_packages([package(tmp_path)], delivery)
    (delivery / entries[0].archive_name).write_bytes(b"changed")
    with pytest.raises(SkillPackageError):
        load_pinned_packages(entries, delivery)


def test_consumer_rejects_archive_link(tmp_path):
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    pinned = package(tmp_path)
    entries = stage_packages([pinned], delivery)
    archive = delivery / entries[0].archive_name
    archive.unlink()
    archive.symlink_to(pinned.archive_path)
    with pytest.raises(OSError):
        load_pinned_packages(entries, delivery)


@pytest.mark.parametrize("name", ["../archive.zip", "/archive.zip", "a.zip", "a\\b.zip"])
def test_manifest_rejects_noncanonical_archive_paths(name):
    with pytest.raises(ValidationError):
        PinnedSkillArchive(
            skill_id="a",
            version_id="v1",
            name="a",
            content_hash="sha256:" + "a" * 64,
            archive_name=name,
        )


def test_empty_manifest_does_not_fall_back_to_discovery(tmp_path, monkeypatch):
    monkeypatch.setenv("KSADK_LOCAL_SKILLS_DIR", str(package(tmp_path).root_dir.parent))
    assert load_pinned_packages((), tmp_path) == []


def test_request_rejects_selection_outside_manifest(tmp_path):
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {"pinned_packages": [], "pinned_protocol_version": 1, "skill_names": ["unbound"]}
        )
    )
    with pytest.raises(SkillWorkflowRequestError, match="not in"):
        parse_workflow_request(["--request-file", str(path)])


def test_duplicate_packages_rejected_before_execution(tmp_path):
    pinned = package(tmp_path)
    with pytest.raises(SkillPackageError, match="unique"):
        stage_packages([pinned, pinned], tmp_path)


@pytest.mark.parametrize("version", [None, 0, 2, True, "1"])
def test_request_rejects_unknown_pinned_protocol(tmp_path, version):
    path = tmp_path / "request.json"
    path.write_text(json.dumps({"pinned_packages": [], "pinned_protocol_version": version}))
    with pytest.raises(SkillWorkflowRequestError, match="protocol"):
        parse_workflow_request(["--request-file", str(path)])


def test_request_cannot_drop_manifest_and_fall_back_to_discovery(tmp_path):
    path = tmp_path / "request.json"
    path.write_text(json.dumps({"pinned_protocol_version": 1}))
    with pytest.raises(SkillWorkflowRequestError, match="manifest is missing"):
        parse_workflow_request(["--request-file", str(path)])


class SandboxTransportDouble:
    """Emulate file/command transport; execute the consumer in a real local process."""

    sandbox_id = "fixture-sandbox"

    def __init__(self, directory, *, version="1:1", fail_write=False):
        self.directory = directory
        directory.mkdir()
        self.version = version
        self.fail_write = fail_write
        self.writes = {}
        self.commands = []
        self.killed = False

    def write_file(self, path, data):
        if self.fail_write:
            raise OSError("fixture upload failed")
        self.writes[path] = data
        (self.directory / Path(path).name).write_bytes(data)

    def read_file_bytes(self, path, *, max_bytes):
        content = (self.directory / Path(path).name).read_bytes()
        assert len(content) <= max_bytes
        return content

    def run_command(self, command, **kwargs):
        self.commands.append(command)
        if "PINNED_PACKAGE_PROTOCOL_VERSION" in command:
            return SandboxCommandResult(stdout=self.version, exit_code=0)
        if command.startswith("mkdir -m 700 "):
            return SandboxCommandResult(exit_code=0)
        assert command.startswith("python -I -u -m ksadk.skills.runtime.agent --request-file ")
        request = self.directory / "workflow-request.json"
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "ksadk.skills.runtime.agent",
                "--request-file",
                str(request),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env={"PATH": "/usr/bin:/bin", "KSADK_SKILL_WORKDIR": str(self.directory / "work")},
        )
        return SandboxCommandResult(
            stdout=result.stdout, stderr=result.stderr, exit_code=result.returncode
        )

    def kill(self):
        self.killed = True
        shutil.rmtree(self.directory)


def remote_backend(session):
    backend = E2BSkillRuntimeBackend(template_id="fixture-template")
    backend.sandbox_backend = SimpleNamespace(create_session=lambda **kwargs: session)
    return backend


def test_remote_transport_delivers_exact_archive_to_real_consumer(tmp_path):
    pinned = package(tmp_path)
    session = SandboxTransportDouble(tmp_path / "remote")
    result = remote_backend(session).run_workflow(
        "run pinned-test",
        skill_space_ids=[],
        session_id="fixture",
        pinned_packages=[pinned],
        timeout=10,
    )
    assert result.ok, (result.stdout, result.stderr)
    assert session.killed
    archive_bytes = next(data for path, data in session.writes.items() if path.endswith(".zip"))
    assert archive_bytes == pinned.archive_path.read_bytes()
    request = json.loads(
        next(data for path, data in session.writes.items() if path.endswith(".json"))
    )
    assert request["pinned_protocol_version"] == 1
    assert request["pinned_packages"][0]["content_hash"] == pinned.ref.content_hash.render()
    assert Path(result.output_files[0]).read_text() == "locked-version-one"
    assert not session.directory.exists()
    shutil.rmtree(Path(result.output_files[0]).parents[1])


@pytest.mark.parametrize("version", ["", "0", "2", "1", "1\nextra"])
def test_remote_old_runtime_refused_before_upload_or_execution(tmp_path, version):
    session = SandboxTransportDouble(tmp_path / "remote", version=version)
    result = remote_backend(session).run_workflow(
        "run",
        skill_space_ids=[],
        session_id="fixture",
        pinned_packages=[package(tmp_path)],
    )
    assert not result.ok
    assert "protocol v1" in result.error_message
    assert len(session.commands) == 1
    assert session.writes == {}
    assert session.killed


def test_remote_transfer_failure_never_executes_and_closes_session(tmp_path):
    session = SandboxTransportDouble(tmp_path / "remote", fail_write=True)
    result = remote_backend(session).run_workflow(
        "run",
        skill_space_ids=[],
        session_id="fixture",
        pinned_packages=[package(tmp_path)],
    )
    assert not result.ok
    assert len(session.commands) == 2
    assert session.killed

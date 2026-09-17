import hashlib
import io
import shutil
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ksadk.skills.models import ContentHash, SkillRef
from ksadk.skills.package_store import PackageStore
from ksadk.skills.runtime import agent
from ksadk.skills.runtime.backends.local import LocalProcessSkillRuntimeBackend


def _prompt_package(tmp_path: Path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("SKILL.md", "---\nname: lifecycle-test\n---\nRun the script.")
        archive.writestr(
            "scripts/run-workflow.sh",
            "set -eu\nprintf '%s' \"$KSADK_WORKFLOW_PROMPT\" > "
            '"$KSADK_SKILL_OUTPUT_DIR/result.txt"\n',
        )
    content = buffer.getvalue()
    ref = SkillRef(
        skill_id="skill-lifecycle",
        version_id="v1",
        version="1",
        name="lifecycle-test",
        content_hash=ContentHash("sha256", hashlib.sha256(content).hexdigest()),
    )
    return PackageStore(
        tmp_path / "source", namespace="lifecycle", require_hash=True
    ).store_archive(ref, content)


def _backend(tmp_path: Path) -> LocalProcessSkillRuntimeBackend:
    artifacts = tmp_path / "delivered"
    artifacts.mkdir()
    return LocalProcessSkillRuntimeBackend(Path(agent.__file__), artifact_directory=artifacts)


def _run_pinned(backend, package, parent: Path, prompt: str):
    return backend.run_workflow(
        prompt,
        skill_space_ids=[],
        session_id="same-session",
        skill_names=["lifecycle-test"],
        pinned_packages=[package],
        env={"KSADK_SKILL_WORKDIR": str(parent)},
        timeout=10,
    )


def test_sequential_same_session_delivers_distinct_snapshots_and_removes_requests(tmp_path):
    backend = _backend(tmp_path)
    package = _prompt_package(tmp_path)
    request_parent = tmp_path / "requests"

    first = _run_pinned(backend, package, request_parent, "first")
    second = _run_pinned(backend, package, request_parent, "second")

    assert Path(first.output_files[0]).read_text() == "first"
    assert Path(second.output_files[0]).read_text() == "second"
    assert Path(first.output_files[0]).parent != Path(second.output_files[0]).parent
    assert first.runtime_id != second.runtime_id
    assert list(request_parent.iterdir()) == []
    assert package.archive_path.exists()


def test_concurrent_same_session_calls_use_isolated_request_directories(tmp_path):
    backend = _backend(tmp_path)
    package = _prompt_package(tmp_path)
    request_parent = tmp_path / "requests"

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(_run_pinned, backend, package, request_parent, f"value-{index}")
            for index in range(4)
        ]
    results = [future.result() for future in futures]

    assert {Path(result.output_files[0]).read_text() for result in results} == {
        "value-0",
        "value-1",
        "value-2",
        "value-3",
    }
    assert len({result.runtime_id for result in results}) == 4
    assert len({Path(result.output_files[0]).parent for result in results}) == 4
    assert list(request_parent.iterdir()) == []


def test_timeout_stops_descendant_writers_before_partial_recovery_and_cleanup(tmp_path):
    runtime_agent = tmp_path / "timeout-agent.py"
    heartbeat = tmp_path / "heartbeat.txt"
    runtime_agent.write_text(
        """import os, signal, subprocess, sys, time
from pathlib import Path
work = Path(os.environ['KSADK_SKILL_WORKDIR'])
output = work / 'artifacts' / 'partial.txt'
output.parent.mkdir(parents=True)
child = """
        + repr(
            "import signal,sys,time\n"
            "from pathlib import Path\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "heartbeat, output = map(Path, sys.argv[1:])\n"
            "while True:\n"
            "    with heartbeat.open('a') as stream: stream.write('x')\n"
            "    output.write_text('partial')\n"
            "    time.sleep(0.02)\n"
        )
        + """
subprocess.Popen([sys.executable, '-c', child, os.environ['HEARTBEAT'], str(output)])
time.sleep(30)
""",
        encoding="utf-8",
    )
    request_parent = tmp_path / "requests"
    artifact_parent = tmp_path / "delivered"
    artifact_parent.mkdir()
    result = LocalProcessSkillRuntimeBackend(
        runtime_agent, artifact_directory=artifact_parent
    ).run_workflow(
        "timeout",
        skill_space_ids=[],
        session_id="timeout",
        env={
            "KSADK_SKILL_WORKDIR": str(request_parent),
            "HEARTBEAT": str(heartbeat),
        },
        timeout=1,
    )

    assert result.timed_out is True
    assert result.error_type == "TimeoutExpired"
    assert result.sandbox["failure_stage"] == "execute"
    assert result.sandbox["artifact_collection_status"] == "recovered_partial"
    assert Path(result.output_files[0]).read_text() == "partial"
    assert list(request_parent.iterdir()) == []
    size_after_return = heartbeat.stat().st_size
    time.sleep(0.15)
    assert heartbeat.stat().st_size == size_after_return


def test_cleanup_failure_does_not_replace_workflow_failure(tmp_path, monkeypatch):
    runtime_agent = tmp_path / "failed-agent.py"
    runtime_agent.write_text(
        "import json\n"
        "print('workflow_result=' + json.dumps({'output_files': [], 'status': 'failed'}))\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )
    request_parent = tmp_path / "requests"
    leaked: list[Path] = []

    def fail_cleanup(path: Path):
        leaked.append(path)
        raise OSError("injected cleanup failure")

    monkeypatch.setattr(
        "ksadk.skills.runtime.backends.local._remove_request_directory", fail_cleanup
    )
    result = LocalProcessSkillRuntimeBackend(runtime_agent).run_workflow(
        "fail",
        skill_space_ids=[],
        session_id="failed",
        env={"KSADK_SKILL_WORKDIR": str(request_parent)},
    )
    try:
        assert result.exit_code == 7
        assert result.error_type is None
        assert result.sandbox["failure_stage"] == "execute"
        assert result.sandbox["cleanup_status"] == "failed"
        assert result.sandbox["cleanup_error"] == "cleanup_failed"
        assert result.skill_events[-1].event_type == "sandbox.session.cleanup_failed"
        assert all(
            event.event_type != "sandbox.session.cleaned_up" for event in result.skill_events
        )
    finally:
        shutil.rmtree(leaked[0])


def test_no_artifacts_leave_caller_directory_empty(tmp_path):
    runtime_agent = tmp_path / "no-artifacts-agent.py"
    runtime_agent.write_text(
        "import json\n"
        "print('workflow_result=' + json.dumps({'output_files': [], 'status': 'ok'}))\n",
        encoding="utf-8",
    )
    request_parent = tmp_path / "requests"
    artifact_parent = tmp_path / "delivered"
    artifact_parent.mkdir()

    result = LocalProcessSkillRuntimeBackend(
        runtime_agent, artifact_directory=artifact_parent
    ).run_workflow(
        "none",
        skill_space_ids=[],
        session_id="none",
        env={"KSADK_SKILL_WORKDIR": str(request_parent)},
    )

    assert result.ok
    assert result.output_files == []
    assert list(artifact_parent.iterdir()) == []
    assert list(request_parent.iterdir()) == []


def test_pinned_startup_ignores_hostile_pythonpath_and_cwd(tmp_path, monkeypatch):
    hostile = tmp_path / "hostile"
    (hostile / "ksadk").mkdir(parents=True)
    (hostile / "ksadk" / "__init__.py").write_text(
        "raise RuntimeError('hostile package imported')\n", encoding="utf-8"
    )
    monkeypatch.setenv("PYTHONPATH", str(hostile))
    monkeypatch.chdir(hostile)
    backend = _backend(tmp_path)

    result = _run_pinned(
        backend,
        _prompt_package(tmp_path),
        tmp_path / "requests",
        "trusted",
    )

    assert result.ok, result.stderr
    assert Path(result.output_files[0]).read_text() == "trusted"
    assert "hostile package imported" not in result.stderr

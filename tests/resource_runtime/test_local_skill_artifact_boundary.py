"""Host validation of untrusted local workflow output before artifact publication."""

import json
import shutil
from pathlib import Path

import pytest

from ksadk.skills.runtime.artifact_delivery import ArtifactDeliveryError
from ksadk.skills.runtime.backends.local import LocalProcessSkillRuntimeBackend
from ksadk.skills.runtime.base import SkillRuntimeError


def run(tmp_path, monkeypatch, paths, *, duplicate=False, setup=""):
    runtime_agent = tmp_path / "fixture-agent.py"
    runtime_agent.write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "work = Path(os.environ['KSADK_SKILL_WORKDIR'])\n"
        + setup
        + f"payload = {{'output_files': {paths!r}}}\n"
        + "print('workflow_result=' + json.dumps(payload))\n"
        + ("print('workflow_result=' + json.dumps(payload))\n" if duplicate else ""),
        encoding="utf-8",
    )
    return LocalProcessSkillRuntimeBackend(runtime_agent).run_workflow(
        "produce a report",
        skill_space_ids=[],
        session_id="fixture",
        env={"KSADK_SKILL_WORKDIR": str(tmp_path / "requests")},
    )


def test_output_is_verified_snapshot_not_live_workflow_file(tmp_path, monkeypatch):
    result = run(
        tmp_path,
        monkeypatch,
        ["report.txt"],
        setup="source = work / 'report.txt'\nsource.write_text('original')\n",
    )
    delivered = Path(result.output_files[0])
    try:
        assert delivered.read_text() == "original"
        payload = json.loads(result.stdout.split("=", 1)[1])
        assert payload["artifacts"] == result.output_files == payload["output_files"]
        assert payload["artifact_bundle"]["file_count"] == 1
        assert str(tmp_path / "requests") not in result.stdout
        assert list((tmp_path / "requests").iterdir()) == []
    finally:
        shutil.rmtree(delivered.parent)


@pytest.mark.parametrize("attack", ["outside", "symlink", "traversal", "oversize"])
def test_untrusted_workflow_cannot_publish_invalid_host_files(tmp_path, monkeypatch, attack):
    outside = tmp_path / "private.txt"
    outside.write_text("fixture confidential data")
    path = str(outside)
    setup = ""
    if attack == "symlink":
        path = "link.txt"
        setup = f"(work / 'link.txt').symlink_to({str(outside)!r})\n"
    elif attack == "traversal":
        path = "../private.txt"
        setup = "(work.parent / 'private.txt').write_text('request-private')\n"
    elif attack == "oversize":
        path = "large.bin"
        setup = (
            "with (work / 'large.bin').open('wb') as output:\n"
            "    output.truncate(20 * 1024 * 1024 + 1)\n"
        )
    with pytest.raises(ArtifactDeliveryError) as raised:
        run(tmp_path, monkeypatch, [path], setup=setup)
    assert raised.value.sandbox["cleanup_status"] == "completed"
    assert raised.value.sandbox["failure_stage"] == "collect"
    assert list((tmp_path / "requests").iterdir()) == []
    assert outside.read_text() == "fixture confidential data"


def test_duplicate_result_is_not_silently_accepted(tmp_path, monkeypatch):
    with pytest.raises(SkillRuntimeError, match="unique") as raised:
        run(tmp_path, monkeypatch, [], duplicate=True)
    assert raised.value.sandbox["cleanup_status"] == "completed"


@pytest.mark.parametrize("paths", [None, "report.txt", [42]])
def test_invalid_output_shape_is_not_silently_empty(tmp_path, monkeypatch, paths):
    with pytest.raises(SkillRuntimeError, match="invalid artifact") as raised:
        run(tmp_path, monkeypatch, paths)
    assert raised.value.sandbox["cleanup_status"] == "completed"


def test_cleanup_failure_does_not_replace_artifact_rejection(tmp_path, monkeypatch):
    outside = tmp_path / "private.txt"
    outside.write_text("fixture confidential data")
    leaked: list[Path] = []

    def fail_cleanup(path: Path):
        leaked.append(path)
        raise OSError("injected cleanup failure")

    monkeypatch.setattr(
        "ksadk.skills.runtime.backends.local._remove_request_directory", fail_cleanup
    )
    with pytest.raises(ArtifactDeliveryError) as raised:
        run(tmp_path, monkeypatch, [str(outside)])
    try:
        assert raised.value.sandbox["failure_stage"] == "collect"
        assert raised.value.sandbox["cleanup_status"] == "failed"
        assert raised.value.sandbox["cleanup_error"] == "cleanup_failed"
        assert raised.value.skill_events[-1].event_type == "sandbox.session.cleanup_failed"
    finally:
        shutil.rmtree(leaked[0])

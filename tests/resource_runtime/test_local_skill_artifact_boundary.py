"""Host validation of untrusted local workflow output before artifact publication."""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from ksadk.skills.runtime import agent
from ksadk.skills.runtime.artifact_delivery import ArtifactDeliveryError
from ksadk.skills.runtime.backends.local import LocalProcessSkillRuntimeBackend
from ksadk.skills.runtime.base import SkillRuntimeError


def run(tmp_path, monkeypatch, paths, *, duplicate=False):
    line = "workflow_result=" + json.dumps({"output_files": paths}) + "\n"
    monkeypatch.setattr(
        "ksadk.skills.runtime.backends.local.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=line * (2 if duplicate else 1), stderr="", returncode=0
        ),
    )
    return LocalProcessSkillRuntimeBackend(Path(agent.__file__)).run_workflow(
        "produce a report",
        skill_space_ids=[],
        session_id="fixture",
        pinned_packages=[],
        env={"KSADK_SKILL_WORKDIR": str(tmp_path / "work")},
    )


def test_output_is_verified_snapshot_not_live_workflow_file(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    source = work / "report.txt"
    source.write_text("original")
    result = run(tmp_path, monkeypatch, [str(source)])
    delivered = Path(result.output_files[0])
    try:
        source.write_text("changed after delivery")
        assert delivered.read_text() == "original"
        payload = json.loads(result.stdout.split("=", 1)[1])
        assert payload["artifacts"] == result.output_files == payload["output_files"]
        assert payload["artifact_bundle"]["file_count"] == 1
        assert str(source) not in result.stdout
    finally:
        shutil.rmtree(delivered.parent)


@pytest.mark.parametrize("attack", ["outside", "symlink", "traversal", "oversize"])
def test_untrusted_workflow_cannot_publish_invalid_host_files(tmp_path, monkeypatch, attack):
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("fixture confidential data")
    path = outside
    if attack == "symlink":
        path = work / "link.txt"
        path.symlink_to(outside)
    elif attack == "traversal":
        path = work / ".." / outside.name
    elif attack == "oversize":
        path = work / "large.bin"
        with path.open("wb") as output:
            output.truncate(20 * 1024 * 1024 + 1)
    with pytest.raises(ArtifactDeliveryError):
        run(tmp_path, monkeypatch, [str(path)])
    assert outside.read_text() == "fixture confidential data"


def test_duplicate_result_is_not_silently_accepted(tmp_path, monkeypatch):
    with pytest.raises(SkillRuntimeError, match="unique"):
        run(tmp_path, monkeypatch, [], duplicate=True)


@pytest.mark.parametrize("paths", [None, "report.txt", [42]])
def test_invalid_output_shape_is_not_silently_empty(tmp_path, monkeypatch, paths):
    with pytest.raises(SkillRuntimeError, match="invalid artifact"):
        run(tmp_path, monkeypatch, paths)

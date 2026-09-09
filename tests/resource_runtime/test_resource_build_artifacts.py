import hashlib
import io
import json
import shutil
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from ksadk.resource_runtime.build_artifacts import restore_resource_build, write_resource_build
from ksadk.resource_runtime.snapshots import ResourceSnapshot
from ksadk.skills.models import ContentHash, SkillRef
from ksadk.skills.package_store import PackageStore, SkillPackageError
from ksadk.skills.runtime import agent
from ksadk.skills.runtime.backends.local import LocalProcessSkillRuntimeBackend


@pytest.fixture
def frozen(tmp_path):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            "SKILL.md",
            "---\nname: build-skill\n"
            "description: Generates reports from references\n---\nUse reference.txt",
        )
        archive.writestr("reference.txt", "version-one")
        archive.writestr(
            "scripts/run-workflow.sh",
            'cat "$KSADK_SKILL_ROOT_DIR/reference.txt" > "$KSADK_SKILL_OUTPUT_DIR/result.txt"\n',
        )
    content = stream.getvalue()
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    ref = SkillRef(
        skill_id="skill-a",
        version_id="v1",
        version="1",
        name="build-skill",
        content_hash=ContentHash.parse(digest),
    )
    package = PackageStore(
        tmp_path / "source", namespace="tenant-a", require_hash=True
    ).store_archive(ref, content)
    snapshot = ResourceSnapshot.model_validate(
        {
            "pluginLockDigest": "sha256:" + "a" * 64,
            "bindings": [
                {
                    "config": {
                        "binding": {
                            "id": "skill-binding",
                            "connectionRef": "connection-a",
                            "resource": {
                                "kind": "skill-space",
                                "id": "space-a",
                                "region": "region-a",
                            },
                        },
                        "selectionMode": "pinned",
                        "selectedSkills": [
                            {"skillId": "skill-a", "versionId": "v1", "contentHash": digest}
                        ],
                    },
                    "connection": {
                        "connectionRef": "connection-a",
                        "tenantRef": "tenant-a",
                        "principalRef": "account-a",
                        "endpoint": "https://skills.example.test",
                        "authMode": "token",
                    },
                }
            ],
        }
    )
    return snapshot, package


def test_build_restores_and_executes_offline_after_source_is_removed(tmp_path, frozen, monkeypatch):
    snapshot, package = frozen
    directory = tmp_path / "build"
    reference = write_resource_build(directory, snapshot, {"skill-binding": [package]})
    shutil.rmtree(tmp_path / "source")
    monkeypatch.setenv("KSADK_SKILL_SERVICE_URL", "http://127.0.0.1:1")
    manifest, packages = restore_resource_build(
        directory, reference, cache_directory=tmp_path / "runtime"
    )
    assert manifest.snapshot.digest == snapshot.digest
    result = LocalProcessSkillRuntimeBackend(Path(agent.__file__)).run_workflow(
        "execute",
        skill_space_ids=[],
        session_id="fixture",
        pinned_packages=packages["skill-binding"],
        env={"KSADK_SKILL_WORKDIR": str(tmp_path / "work")},
        timeout=10,
    )
    assert result.ok, result.stderr
    assert Path(result.output_files[0]).read_text() == "version-one"
    assert str(tmp_path) not in (directory / "resource-build.json").read_text()


def test_invalid_archive_cannot_be_admitted_even_with_matching_hash(tmp_path, frozen):
    snapshot, package = frozen
    invalid = b"not a zip archive"
    package.archive_path.write_bytes(invalid)
    digest = "sha256:" + hashlib.sha256(invalid).hexdigest()
    payload = snapshot.model_dump(by_alias=True, mode="json")
    payload["bindings"][0]["config"]["selectedSkills"][0]["contentHash"] = digest
    snapshot = ResourceSnapshot.model_validate(payload)
    package = replace(package, ref=replace(package.ref, content_hash=ContentHash.parse(digest)))
    with pytest.raises(SkillPackageError):
        write_resource_build(tmp_path / "build", snapshot, {"skill-binding": [package]})
    assert not (tmp_path / "build").exists()


def test_missing_selected_package_does_not_publish_build(tmp_path, frozen):
    snapshot, _ = frozen
    with pytest.raises(ValueError, match="exactly match"):
        write_resource_build(tmp_path / "build", snapshot, {})
    assert not (tmp_path / "build").exists()


@pytest.mark.parametrize("change", ["manifest", "archive", "extra", "link"])
def test_build_tampering_is_rejected(tmp_path, frozen, change):
    snapshot, package = frozen
    directory = tmp_path / "build"
    reference = write_resource_build(directory, snapshot, {"skill-binding": [package]})
    path = directory / "resource-build.json"
    if change == "manifest":
        data = json.loads(path.read_text())
        data["snapshot"]["bindings"][0]["connection"]["principalRef"] = "another-account"
        path.write_text(json.dumps(data))
    elif change == "archive":
        next(directory.glob("*.zip")).write_bytes(b"changed")
    elif change == "extra":
        (directory / "unlocked.zip").write_bytes(b"extra")
    else:
        archive = next(directory.glob("*.zip"))
        archive.unlink()
        archive.symlink_to(package.archive_path)
    with pytest.raises((ValueError, SkillPackageError, OSError)):
        restore_resource_build(directory, reference, cache_directory=tmp_path / "runtime")


def test_build_never_overwrites_existing_artifact(tmp_path, frozen):
    snapshot, package = frozen
    directory = tmp_path / "build"
    reference = write_resource_build(directory, snapshot, {"skill-binding": [package]})
    with pytest.raises(FileExistsError):
        write_resource_build(directory, snapshot, {"skill-binding": [package]})
    assert (
        restore_resource_build(directory, reference, cache_directory=tmp_path / "runtime")[0].digest
        == reference.digest
    )

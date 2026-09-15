import hashlib
import io
import shutil
import subprocess
import sys
import zipfile
from dataclasses import replace

import pytest

from ksadk.resource_runtime.build_artifacts import write_resource_build
from ksadk.resource_runtime.skill_workspace import prepare_pinned_skill_workspace
from ksadk.resource_runtime.snapshots import ResourceSnapshot
from ksadk.skills.models import ContentHash
from ksadk.skills.package_store import PackageStore, SkillPackageError
from tests.resource_runtime.test_resource_build_artifacts import frozen as frozen


def build(tmp_path, frozen):
    snapshot, package = frozen
    source = tmp_path / "build"
    reference = write_resource_build(source, snapshot, {"skill-binding": [package]})
    return source, reference


def test_consumer_reads_complete_pinned_files_offline_and_cleanup_is_owned(tmp_path, frozen):
    source, reference = build(tmp_path, frozen)
    shutil.rmtree(tmp_path / "source")
    user_file = tmp_path / "user-file.txt"
    user_file.write_text("keep")
    target = tmp_path / "activation"
    with prepare_pinned_skill_workspace(
        source, reference, binding_id="skill-binding", workspace_directory=target
    ) as workspace:
        selected = workspace.skills[0]
        assert selected.version_id == "v1"
        assert selected.content_hash == frozen[1].ref.content_hash.render()
        assert selected.relative_directory == ".agents/skills/build-skill"
        assert (
            workspace.directory / selected.relative_directory / "scripts/run-workflow.sh"
        ).is_file()
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                "from pathlib import Path; "
                "print(Path('.agents/skills/build-skill/reference.txt').read_text())",
            ],
            cwd=workspace.directory,
            env={},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "version-one"
    assert not target.exists()
    assert user_file.read_text() == "keep"
    assert not list(tmp_path.glob(".skill-consumer-*"))


@pytest.mark.parametrize("link", [False, True])
def test_existing_consumer_directory_is_never_overwritten(tmp_path, frozen, link):
    source, reference = build(tmp_path, frozen)
    target = tmp_path / "activation"
    existing = tmp_path / "existing"
    existing.mkdir()
    if link:
        target.symlink_to(existing, target_is_directory=True)
    else:
        target.mkdir()
    marker = target / "user.txt"
    marker.write_text("keep")
    with pytest.raises(FileExistsError):
        with prepare_pinned_skill_workspace(
            source, reference, binding_id="skill-binding", workspace_directory=target
        ):
            pytest.fail("Existing workspace was admitted")
    assert marker.read_text() == "keep"


def test_consumer_exception_cleans_only_activation_workspace(tmp_path, frozen):
    source, reference = build(tmp_path, frozen)
    target = tmp_path / "activation"
    with pytest.raises(RuntimeError, match="consumer failed"):
        with prepare_pinned_skill_workspace(
            source, reference, binding_id="skill-binding", workspace_directory=target
        ):
            raise RuntimeError("consumer failed")
    assert not target.exists()
    assert source.is_dir()


def test_replaced_workspace_root_is_not_deleted(tmp_path, frozen):
    source, reference = build(tmp_path, frozen)
    target = tmp_path / "activation"
    with pytest.raises(SkillPackageError, match="ownership changed"):
        with prepare_pinned_skill_workspace(
            source, reference, binding_id="skill-binding", workspace_directory=target
        ):
            target.rename(tmp_path / "moved-owned-workspace")
            target.mkdir()
            (target / "user.txt").write_text("keep")
    assert (target / "user.txt").read_text() == "keep"


def test_isolated_binding_does_not_expose_scripts_to_outer_agent(tmp_path, frozen):
    snapshot, package = frozen
    data = snapshot.model_dump(by_alias=True)
    data["bindings"][0]["config"]["executionMode"] = "isolated"
    source, reference = build(tmp_path, (ResourceSnapshot.model_validate(data), package))
    target = tmp_path / "activation"
    with pytest.raises(SkillPackageError, match="outer-agent"):
        with prepare_pinned_skill_workspace(
            source, reference, binding_id="skill-binding", workspace_directory=target
        ):
            pytest.fail("Isolated Skill exposed to outer agent")
    assert not target.exists()


@pytest.mark.parametrize("outside", [False, True])
def test_wrapped_package_preserves_binary_files_or_rejects_outside_files(tmp_path, frozen, outside):
    snapshot, original = frozen
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("wrapped/SKILL.md", "---\nname: build-skill\n---\nRead data.bin")
        archive.writestr("wrapped/data.bin", b"\x00\xff\x01")
        if outside:
            archive.writestr("shared.txt", "not under the Skill root")
    content = stream.getvalue()
    content_hash = ContentHash("sha256", hashlib.sha256(content).hexdigest())
    package = PackageStore(
        tmp_path / "wrapped-cache", namespace="test", require_hash=True
    ).store_archive(
        replace(original.ref, content_hash=content_hash),
        content,
    )
    data = snapshot.model_dump(by_alias=True)
    data["bindings"][0]["config"]["selectedSkills"][0]["contentHash"] = content_hash.render()
    source, reference = build(tmp_path, (ResourceSnapshot.model_validate(data), package))
    target = tmp_path / "activation"
    if outside:
        with pytest.raises(SkillPackageError, match="outside"):
            with prepare_pinned_skill_workspace(
                source, reference, binding_id="skill-binding", workspace_directory=target
            ):
                pytest.fail("Package file silently omitted")
        assert not target.exists()
    else:
        with prepare_pinned_skill_workspace(
            source, reference, binding_id="skill-binding", workspace_directory=target
        ) as workspace:
            assert (
                workspace.directory / workspace.skills[0].relative_directory / "data.bin"
            ).read_bytes() == b"\x00\xff\x01"

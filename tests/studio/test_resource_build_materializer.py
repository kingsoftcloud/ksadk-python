import shutil

import httpx
import pytest

from ksadk.plugins.bridges.dsh import DshProfileBuildSnapshot
from ksadk.resource_runtime.build_artifacts import restore_resource_build
from ksadk.skills.package_store import SkillPackageError
from ksadk.skills.service_client import SkillServiceClient
from ksadk.studio import resource_build_materializer as materializer
from ksadk.studio.errors import StudioError
from ksadk.studio.model_client import CredentialResolver
from ksadk.studio.resource_connections import (
    ResourceConnectionDeclaration,
    ResourceConnectionRepository,
)
from ksadk.studio.workspace import Workspace
from tests.resource_runtime.test_resource_build_artifacts import frozen as frozen
from tests.resource_runtime.test_studio_resource_config import binding


def setup(tmp_path, frozen, monkeypatch, *, failure=None):
    snapshot, package = frozen
    profile = DshProfileBuildSnapshot.model_validate({
        "projection": {
            "profile": "studio", "bundles": ["fixture-skill-bundle"],
            "configDigest": "sha256:" + "a" * 64, "configBytes": 12,
            "hostVersion": "0.1.1-rc.2",
        },
        "dependencyLockDigest": "sha256:" + "b" * 64,
        "installationDigest": "sha256:" + "c" * 64,
    })
    snapshot = snapshot.model_copy(update={"dsh_profile": profile})
    credentials = CredentialResolver()
    credentials.put_session("SKILL_TOKEN", "fake-private-token")
    connections = ResourceConnectionRepository(Workspace(tmp_path), credentials)
    declaration = ResourceConnectionDeclaration(
        label="Skill",
        target=snapshot.bindings[0].connection,
        credentials={"tokenRef": "env://SKILL_TOKEN"},
    )
    connections.save(declaration, expected_revision=0)
    content = package.archive_path.read_bytes()
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path.endswith("ListSkillsBySpaceId"):
            assert request.url.params["SpaceId"] == "space-a"
            assert request.headers["Authorization"] == "Bearer fake-private-token"
            # The directory has already upgraded; retain the explicitly selected v1.
            return httpx.Response(
                200,
                json={
                    "Data": {
                        "SpaceId": "space-a",
                        "Skills": [
                            {
                                "SkillId": "other" if failure == "missing" else "skill-a",
                                "VersionId": "v2",
                                "Name": "build-skill",
                            }
                        ],
                    }
                },
            )
        if request.url.path.endswith("GetSkillDownloadUrl"):
            assert request.url.params["VersionId"] == "v1"
            return httpx.Response(
                200,
                json={
                    "Data": {
                        "DownloadUrl": "https://download.example.test/package?signature=fake-signature"
                    }
                },
            )
        if failure == "drift":
            changed = declaration.model_dump()
            changed["target"]["principal_ref"] = "different-account"
            connections.save(
                ResourceConnectionDeclaration.model_validate(changed), expected_revision=1
            )
        return httpx.Response(200, content=b"wrong hash" if failure == "hash" else content)

    monkeypatch.setattr(
        materializer,
        "SkillServiceClient",
        lambda **kwargs: SkillServiceClient(**kwargs, transport=httpx.MockTransport(handle)),
    )
    selected = binding("skill-space", config=snapshot.bindings[0].config.model_dump(by_alias=True))
    return snapshot, selected, connections, calls


def test_build_downloads_exact_pin_and_restores_without_live_sources(tmp_path, frozen, monkeypatch):
    snapshot, selected, connections, calls = setup(tmp_path, frozen, monkeypatch)
    directory = tmp_path / "build" / "resources"
    reference = materializer.materialize_resource_build(
        directory,
        bindings=[selected],
        memory=None,
        admitted_snapshot=snapshot,
        connections=connections,
    )
    assert len(calls) == 3
    shutil.rmtree(tmp_path / "source")
    shutil.rmtree(connections.root)
    manifest, packages = restore_resource_build(
        directory, reference, cache_directory=tmp_path / "restored"
    )
    assert manifest.snapshot == snapshot
    package = packages["skill-binding"][0]
    assert package.ref.version_id == "v1"
    assert (package.root_dir / "reference.txt").read_text() == "version-one"
    serialized = (directory / "resource-build.json").read_text()
    for secret in ("fake-private-token", "fake-signature", str(tmp_path)):
        assert secret not in serialized
    assert not list(directory.parent.glob(".resource-packages-*"))


@pytest.mark.parametrize("failure", ["missing", "hash", "drift", "declaration"])
def test_failed_build_publishes_no_resource_artifact(tmp_path, frozen, monkeypatch, failure):
    snapshot, selected, connections, calls = setup(tmp_path, frozen, monkeypatch, failure=failure)
    if failure == "declaration":
        selected.config["binding"]["resource"]["id"] = "different-space"
    directory = tmp_path / "build" / "resources"
    expected_error = {
        "missing": SkillPackageError,
        "hash": SkillPackageError,
        "drift": StudioError,
        "declaration": ValueError,
    }[failure]
    with pytest.raises(expected_error):
        materializer.materialize_resource_build(
            directory,
            bindings=[selected],
            memory=None,
            admitted_snapshot=snapshot,
            connections=connections,
        )
    assert not directory.exists()
    assert not list(directory.parent.glob(".resource-packages-*"))
    if failure == "declaration":
        assert not calls


def test_build_requires_profile_and_retained_bytes_reject_host_substitution(
    tmp_path, frozen, monkeypatch,
):
    snapshot, selected, connections, calls = setup(tmp_path, frozen, monkeypatch)
    directory = tmp_path / "build" / "resources"
    with pytest.raises(ValueError, match="DSH profile snapshot"):
        materializer.materialize_resource_build(
            directory, bindings=[selected], memory=None, connections=connections,
            admitted_snapshot=snapshot.model_copy(update={"dsh_profile": None}),
        )
    assert not calls
    assert not directory.exists()
    reference = materializer.materialize_resource_build(
        directory, bindings=[selected], memory=None, connections=connections,
        admitted_snapshot=snapshot,
    )
    import json

    path = directory / "resource-build.json"
    payload = json.loads(path.read_bytes())
    payload["snapshot"]["dshProfile"]["installationDigest"] = "sha256:" + "f" * 64
    path.write_text(json.dumps(payload))
    with pytest.raises(SkillPackageError, match="digest mismatch"):
        restore_resource_build(directory, reference, cache_directory=tmp_path / "restored")

"""Check pinned default builds and explicit local npm archives independently."""
import base64
import hashlib
import importlib.util
import io
import json
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "reviewed_web_assets", ROOT / "scripts/reviewed_web_assets.py")
assets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(assets)


@pytest.fixture
def candidate(tmp_path):
    root = tmp_path / "source"
    vendor = root / assets.MANIFEST.parent
    vendor.mkdir(parents=True)
    artifact = local_npm_archive(tmp_path, source_commit="a" * 40)
    actual = assets.selected_source(root, artifact)
    actual.pop("source_type")
    actual["artifact"] = artifact.name
    (vendor / assets.MANIFEST.name).write_text(json.dumps(actual))
    (vendor / actual["artifact"]).write_bytes(
        artifact.read_bytes())
    assets.sync(root)
    studio = root / "ksadk/studio/static"
    studio.mkdir(parents=True)
    (studio / "index.html").write_text('<script src="/static/assets/test.js"></script>')
    (studio / "assets").mkdir()
    (studio / "assets/test.js").write_text("reviewed studio")
    assets.stamp(root, studio, "studio", actual)
    return root


def wheel(root, target):
    with zipfile.ZipFile(target, "w") as archive:
        for prefix in ["ksadk/server/static", "ksadk/studio/static"]:
            for path in (root / prefix).rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(root).as_posix())
    return target


def local_npm_archive(tmp_path, *, name=assets.PACKAGE_NAME, source_commit=None):
    """A normal npm pack has no private source manifest or candidate metadata."""
    target = tmp_path / "local-web.tgz"
    files = {
        "package/package.json": json.dumps({"name": name, "version": "9.0.0-local"}),
        "package/dist-lib/runtime.js": "export const local = true;",
        "package/dist-ksadk/index.html": '<script src="./assets/local.js"></script>',
        "package/dist-ksadk/assets/local.js": "console.log('local build');",
    }
    if source_commit:
        files["package/SOURCE_CANDIDATE.json"] = json.dumps({"source_commit": source_commit})
    with tarfile.open(target, "w:gz") as archive:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return target


def test_published_release_uses_provenance_integrity_and_verified_cache(candidate, monkeypatch):
    value = assets.manifest(candidate)
    artifact = candidate / assets.MANIFEST.parent / value["artifact"]
    with tarfile.open(artifact) as archive:
        files = {item.name: archive.extractfile(item).read()
                 for item in archive.getmembers() if item.isfile()}
    files.pop("package/SOURCE_CANDIDATE.json")
    files["package/RELEASE_PROVENANCE.json"] = json.dumps({
        "package": value["package_name"], "version": value["package_version"],
        "source_commit": value["source_commit"],
    }).encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    data = stream.getvalue()
    value.update(
        registry_release=True,
        artifact_url="https://registry.npmjs.org/@kingsoftcloud/ksadk-web/-/"
                     f"ksadk-web-{value['package_version']}.tgz",
        artifact_sha256=hashlib.sha256(data).hexdigest(),
        artifact_integrity="sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode(),
    )
    manifest = candidate / assets.MANIFEST
    manifest.write_text(json.dumps(value))
    requests = []

    def download(url, timeout):
        assert url == value["artifact_url"] and timeout == 60
        requests.append(url)
        return io.BytesIO(data)

    monkeypatch.setattr(assets, "urlopen", download)
    artifact.unlink()  # A published release does not require a checked-in archive.
    assets.sync(candidate)
    assets.sync(candidate)
    assert len(requests) == 1
    marker = json.loads((candidate / "ksadk/server/static" / assets.MARKER).read_text())
    assert marker["registry_release"] is True
    before = assets.tree_digest(candidate / "ksadk/server/static")
    value["artifact_integrity"] = "sha512-tampered"
    manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="npm integrity"):
        assets.sync(candidate)
    assert assets.tree_digest(candidate / "ksadk/server/static") == before
    value["artifact_url"] = "https://untrusted.example.test/package.tgz"
    manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="pinned public npm URL"):
        assets.sync(candidate)


def test_default_archive_and_both_wheel_applications_share_identity(candidate, tmp_path):
    direct = assets.verify(candidate)
    packed = assets.verify(candidate, wheel(candidate, tmp_path / "candidate.whl"))
    assert packed == direct
    assert packed["hosted"]["source_commit"] == packed["studio"]["source_commit"]
    assert packed["hosted"]["artifact_sha256"] == packed["studio"]["artifact_sha256"]


@pytest.mark.parametrize("role,directory", [
    ("hosted", "ksadk/server/static"), ("studio", "ksadk/studio/static"),
])
def test_changed_payload_rejected_in_directory_and_wheel(candidate, tmp_path, role, directory):
    (candidate / directory / "index.html").write_text("old application")
    with pytest.raises(ValueError, match=f"{role} static assets changed"):
        assets.verify(candidate)
    with pytest.raises(ValueError, match=f"{role} static assets changed"):
        assets.verify(candidate, wheel(candidate, tmp_path / "mixed.whl"))


def test_same_version_other_source_rejected(candidate):
    marker = candidate / "ksadk/studio/static" / assets.MARKER
    value = json.loads(marker.read_text())
    value["source_commit"] = "0" * 40
    marker.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="studio source differs"):
        assets.verify(candidate)


def test_missing_static_assets_explain_the_build_prerequisite(candidate):
    (candidate / "ksadk/studio/static" / assets.MARKER).unlink()
    with pytest.raises(ValueError, match="run make build-frontend before packaging"):
        assets.verify(candidate)


def test_wrong_archive_does_not_replace_existing_hosted_assets(candidate, tmp_path):
    before = assets.tree_digest(candidate / "ksadk/server/static")
    old = tmp_path / "old.tgz"
    old.write_bytes(b"old npm package")
    with pytest.raises(tarfile.ReadError):
        assets.sync(candidate, old)
    assert assets.tree_digest(candidate / "ksadk/server/static") == before


def test_default_archive_still_requires_its_pinned_digest(candidate):
    value = assets.manifest(candidate)
    target = candidate / assets.MANIFEST.parent / value["artifact"]
    target.write_bytes(b"unreviewed package")
    before = assets.tree_digest(candidate / "ksadk/server/static")
    with pytest.raises(ValueError, match="no fallback"):
        assets.sync(candidate)
    assert assets.tree_digest(candidate / "ksadk/server/static") == before


def test_explicit_local_pack_needs_no_default_manifest_or_source_candidate(tmp_path):
    artifact = local_npm_archive(tmp_path)
    root = tmp_path / "without-vendor"
    assets.sync(root, artifact)
    marker = json.loads((root / "ksadk/server/static" / assets.MARKER).read_text())
    assert marker["package_version"] == "9.0.0-local"
    assert marker["artifact_sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert marker["source_commit"] is None
    assert marker["source_type"] == "local"
    assert marker["registry_release"] is False


def test_local_identity_survives_both_apps_and_wheel_checks(candidate, tmp_path):
    artifact = local_npm_archive(tmp_path)
    assets.sync(candidate, artifact)
    value = assets.selected_source(candidate, artifact)
    assets.stamp(candidate, candidate / "ksadk/studio/static", "studio", value)
    direct = assets.verify(candidate, artifact=artifact)
    packed = assets.verify(candidate, wheel(candidate, tmp_path / "local.whl"), artifact)
    assert packed == direct
    assert packed["hosted"]["artifact_sha256"] == packed["studio"]["artifact_sha256"]
    # A local marker must not silently opt a later default build out of its pin.
    with pytest.raises(ValueError, match="hosted source differs"):
        assets.verify(candidate)
    (candidate / "ksadk/server/static/index.html").write_text("changed after sync")
    with pytest.raises(ValueError, match="hosted static assets changed"):
        assets.verify(candidate, artifact=artifact)


def test_local_package_name_is_checked_before_replacing_hosted_assets(candidate, tmp_path):
    artifact = local_npm_archive(tmp_path, name="unrelated-package")
    before = assets.tree_digest(candidate / "ksadk/server/static")
    with pytest.raises(ValueError, match="KsADK Web npm package"):
        assets.sync(candidate, artifact)
    assert assets.tree_digest(candidate / "ksadk/server/static") == before


def test_missing_explicit_path_does_not_fall_back_to_default_archive(candidate, tmp_path):
    with pytest.raises(FileNotFoundError):
        assets.sync(candidate, tmp_path / "missing.tgz")


def test_cli_uses_local_path_from_make_environment_without_pinned_version(
    tmp_path, monkeypatch, capsys,
):
    artifact = local_npm_archive(tmp_path)
    monkeypatch.setenv("KSADK_WEB_TARBALL", str(artifact))
    monkeypatch.setattr(sys, "argv", ["reviewed_web_assets.py", "check-archive",
                        "--root", str(tmp_path / "without-vendor"), "--expected-version", "0.3.13"])
    assert assets.main() == 0
    assert json.loads(capsys.readouterr().out)["package_version"] == "9.0.0-local"


def test_cli_default_keeps_version_check(candidate, monkeypatch, capsys):
    monkeypatch.delenv("KSADK_WEB_TARBALL", raising=False)
    monkeypatch.setattr(sys, "argv", ["reviewed_web_assets.py", "check-archive",
                        "--root", str(candidate), "--expected-version", "different-version"])
    assert assets.main() == 1
    assert "requested Web version differs" in capsys.readouterr().err


@pytest.mark.parametrize("member,kind", [("package/../../escape", "file"),
                                         ("package/link", "symlink")])
def test_unsafe_archive_never_writes_outside_staging(candidate, tmp_path, member, kind):
    artifact = tmp_path / "unsafe.tgz"
    with tarfile.open(artifact, "w:gz") as archive:
        info = tarfile.TarInfo(member)
        if kind == "symlink":
            info.type = tarfile.SYMTYPE
            info.linkname = "/tmp/escape"
            archive.addfile(info)
        else:
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
    path = candidate / assets.MANIFEST
    value = json.loads(path.read_text())
    value["artifact_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="unsafe Web archive"):
        assets.sync(candidate, artifact)
    assert not (tmp_path / "escape").exists()


def test_missing_hosted_marker_blocks_packaged_mixed_artifact(candidate, tmp_path):
    (candidate / "ksadk/server/static" / assets.MARKER).unlink()
    with pytest.raises(ValueError, match="hosted static assets are missing"):
        assets.verify(candidate, wheel(candidate, tmp_path / "unmarked.whl"))


def test_ci_uses_the_manifest_archive_and_export_includes_its_verifier():
    import runpy
    version = assets.manifest(ROOT)["package_version"]
    action = (ROOT / ".github/actions/prepare-web-candidate/action.yml").read_text()
    assert "check-archive" in action
    assert "npm pack" not in action
    for name in ("ci.yml", "release-check.yml", "publish-pypi.yml"):
        workflow = (ROOT / ".github/workflows" / name).read_text()
        assert "0.3.12" not in workflow
        assert version in workflow
    policy = runpy.run_path(str(ROOT / "scripts/prepare_ksadk_python_export.py"))
    assert policy["is_included_by_policy"]("scripts/reviewed_web_assets.py")

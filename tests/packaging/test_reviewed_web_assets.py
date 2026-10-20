"""Exercise mixed-source rejection at the archive, directory and wheel boundaries."""
import hashlib
import importlib.util
import io
import json
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
    actual = assets.manifest(ROOT)
    (vendor / assets.MANIFEST.name).write_text(json.dumps(actual))
    (vendor / actual["artifact"]).write_bytes(
        (ROOT / assets.MANIFEST.parent / actual["artifact"]).read_bytes())
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
    with pytest.raises(ValueError, match="no fallback"):
        assets.sync(candidate, old)
    assert assets.tree_digest(candidate / "ksadk/server/static") == before


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
    for name in ("ci.yml", "release-check.yml", "release-studio-app.yml", "publish-pypi.yml"):
        workflow = (ROOT / ".github/workflows" / name).read_text()
        assert "0.3.12" not in workflow
        assert version in workflow
    policy = runpy.run_path(str(ROOT / "scripts/prepare_ksadk_python_export.py"))
    assert policy["is_included_by_policy"]("scripts/reviewed_web_assets.py")

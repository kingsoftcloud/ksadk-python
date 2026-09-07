from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile

import pytest

from ksadk.plugins.artifacts import export_plugin_artifact, restore_plugin_artifact


def _export(tmp_path):
    root = tmp_path / "selected"
    skill = root / "plugins/demo/skills/demo/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\ndescription: Example\n---\nDemo")
    script = root / "plugins/demo/run.sh"
    script.write_text("#!/bin/sh\nprintf demo")
    script.chmod(0o755)
    receipt, archive = export_plugin_artifact(
        root, tmp_path / "out", runtime_version="0.147.0", plugin_lock_digest="sha256:" + "a" * 64
    )
    return root, receipt, archive


def test_artifact_is_deterministic_and_restores_without_source_workspace(tmp_path):
    root, receipt, archive = _export(tmp_path)
    copied = tmp_path / "other-machine"
    shutil.copytree(root, copied)
    for path in copied.rglob("*"):
        os.utime(path, (12345, 12345))
    receipt2, _ = export_plugin_artifact(
        copied,
        tmp_path / "out2",
        runtime_version="0.147.0",
        plugin_lock_digest="sha256:" + "a" * 64,
    )
    assert receipt2 == receipt
    shutil.rmtree(root)
    shutil.rmtree(copied)
    target = restore_plugin_artifact(archive, receipt, tmp_path / "restored")
    assert (target / "plugins/demo/run.sh").stat().st_mode & 0o111 == 0o111
    assert (target / "plugins/demo/skills/demo/SKILL.md").read_text().endswith("Demo")


@pytest.mark.parametrize(
    "name", [".credentials.json", ".env", "nested/auth.json", "nested/.env.production"]
)
def test_artifact_rejects_credential_paths(tmp_path, name):
    root, _, _ = _export(tmp_path)
    secret = root / name
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("fake credential")
    with pytest.raises(ValueError, match="credential"):
        export_plugin_artifact(
            root,
            tmp_path / "out",
            runtime_version="0.147.0",
            plugin_lock_digest="sha256:" + "a" * 64,
        )


def test_artifact_rejects_symlink_and_corruption(tmp_path):
    root, receipt, archive = _export(tmp_path)
    (root / "escape").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        export_plugin_artifact(
            root,
            tmp_path / "out",
            runtime_version="0.147.0",
            plugin_lock_digest="sha256:" + "a" * 64,
        )
    raw = bytearray(archive.read_bytes())
    raw[10] ^= 1
    archive.write_bytes(raw)
    with pytest.raises(ValueError, match="digest"):
        restore_plugin_artifact(archive, receipt, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize("attack", ["traversal", "duplicate", "extra", "digest"])
def test_artifact_validates_archive_even_with_matching_outer_digest(tmp_path, attack):
    _, receipt, archive = _export(tmp_path)
    with zipfile.ZipFile(archive) as src:
        entries = [(i, src.read(i.filename)) for i in src.infolist()]
    manifest = json.loads(entries[-1][1])
    if attack == "traversal":
        manifest["files"][0]["path"] = "../escaped"
    if attack == "duplicate":
        manifest["files"].append(manifest["files"][0])
    if attack == "digest":
        manifest["files"][0]["digest"] = "sha256:" + "0" * 64
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as dst:
        for info, raw in entries[:-1]:
            dst.writestr(info, raw)
        dst.writestr(entries[-1][0], json.dumps(manifest))
        if attack == "extra":
            dst.writestr("unexpected", b"not indexed")
    receipt = receipt.model_copy(
        update={
            "size_bytes": archive.stat().st_size,
            "artifact_digest": "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest(),
        }
    )
    with pytest.raises(ValueError):
        restore_plugin_artifact(archive, receipt, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_artifact_rejects_expansion_above_limit(tmp_path, monkeypatch):
    from ksadk.plugins import artifacts

    _, receipt, archive = _export(tmp_path)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as out:
        out.writestr("manifest.json", " " * 20000)
    receipt = receipt.model_copy(
        update={
            "size_bytes": archive.stat().st_size,
            "artifact_digest": "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest(),
        }
    )
    monkeypatch.setattr(artifacts, "MAX_BYTES", 10000)
    with pytest.raises(ValueError, match="limit"):
        restore_plugin_artifact(archive, receipt, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_artifact_preserves_exact_executable_bits(tmp_path):
    root, _, _ = _export(tmp_path)
    (root / "plugins/demo/run.sh").chmod(0o744)
    receipt, archive = export_plugin_artifact(
        root, tmp_path / "modes", runtime_version="0.147.0", plugin_lock_digest="sha256:" + "a" * 64
    )
    restored = restore_plugin_artifact(archive, receipt, tmp_path / "mode-restored")
    assert (restored / "plugins/demo/run.sh").stat().st_mode & 0o111 == 0o100
    with pytest.raises(ValueError, match="outside"):
        export_plugin_artifact(
            root, root / "out", runtime_version="0.147.0", plugin_lock_digest="sha256:" + "a" * 64
        )

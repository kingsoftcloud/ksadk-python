from __future__ import annotations

import io
import json
import os
import stat
import zipfile

import pytest

from ksadk.plugins.teams.build_artifacts import (
    MANIFEST_PATH,
    MAX_FILE_BYTES,
    build_archive,
    stamp_code_archive,
    verify_build_archive,
    verify_loaded_build,
)
from ksadk.plugins.teams.cloud_contracts import canonical_bytes


def package():
    return build_archive(
        {"main.py": b"print('immutable')\n", "assets/中文.txt": b"resource"},
        entrypoint="main.py",
        agent_definition={"name": "fixed"},
        tools_policy={"allowed": ["read_file"]},
        behavior_config={"model": "pinned"},
        dependency_lock=b"exact==1.0.0 --hash=sha256:fixture\n",
    )


def rewrite(archive, *, change=None, extra=None):
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(archive)) as original, zipfile.ZipFile(out, "w") as target:
        for item in original.infolist():
            data = original.read(item)
            if change:
                item, data = change(item, data)
            target.writestr(item, data)
        if extra:
            target.writestr(*extra)
    return out.getvalue()


def test_deterministic_manifest_and_all_archive_bytes_are_verified():
    first = package()
    assert first == package()
    verified = verify_build_archive(io.BytesIO(first.archive))
    assert verified.code_digest == first.code_digest
    assert [item.path for item in first.manifest.files] == sorted(
        item.path for item in first.manifest.files
    )
    changed = rewrite(
        first.archive,
        change=lambda item, data: (item, data + b"x" if item.filename == "main.py" else data),
    )
    with pytest.raises(ValueError, match="digest or size"):
        verify_build_archive(changed)
    with pytest.raises(ValueError, match="differ"):
        verify_build_archive(rewrite(first.archive, extra=("hidden.py", b"unlisted")))


@pytest.mark.parametrize(
    "path", ["../escape", "/absolute", "a/../../escape", "a\\escape", ".", "a//b"]
)
def test_rejects_unsafe_archive_paths(path):
    with pytest.raises(ValueError):
        verify_build_archive(rewrite(package().archive, extra=(path, b"x")))


@pytest.mark.parametrize("mode", [stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o644, stat.S_IFCHR | 0o600])
def test_rejects_links_and_special_zip_entries(mode):
    entry = zipfile.ZipInfo("special")
    entry.create_system = 3
    entry.external_attr = mode << 16
    with pytest.raises(ValueError, match="unsupported"):
        verify_build_archive(rewrite(package().archive, extra=(entry, b"main.py")))


def test_rejects_duplicate_files_noncanonical_json_floats_and_zip_bombs():
    with pytest.warns(UserWarning), pytest.raises(ValueError, match="duplicate"):
        verify_build_archive(rewrite(package().archive, extra=("main.py", b"duplicate")))
    with pytest.raises(ValueError, match="canonical"):
        verify_build_archive(
            rewrite(
                package().archive,
                change=lambda item, data: (
                    item,
                    data + b"\n" if item.filename == MANIFEST_PATH else data,
                ),
            )
        )
    with pytest.raises(ValueError, match="floating"):
        verify_build_archive(
            rewrite(
                package().archive,
                change=lambda item, data: (
                    item,
                    canonical_bytes(json.loads(data) | {"unexpected": 0.7})
                    if item.filename == MANIFEST_PATH
                    else data,
                ),
            )
        )
    with pytest.raises(ValueError):
        build_archive(
            {"main.py": b"x" * (MAX_FILE_BYTES + 1)},
            entrypoint="main.py",
            agent_definition={},
            tools_policy={},
            behavior_config={},
            dependency_lock=b"lock",
        )
    with pytest.raises(ValueError, match="20 MiB"):
        verify_build_archive(io.BytesIO(b"x" * (MAX_FILE_BYTES + 1)))


def test_can_stamp_existing_codebuilder_payload_without_rebuilding_dependencies():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as target:
        target.writestr("entrypoint.py", b"existing code")
        target.writestr("ksadk/BUILD-INFO.json", canonical_bytes({"schema": "existing"}))
    result = stamp_code_archive(
        out.getvalue(),
        entrypoint="entrypoint.py",
        agent_definition={},
        tools_policy={},
        behavior_config={},
        dependency_lock=b"lock",
    )
    with zipfile.ZipFile(io.BytesIO(result.archive)) as target:
        assert target.read("entrypoint.py") == b"existing code"
        assert json.loads(target.read("ksadk/BUILD-INFO.json")) == {"schema": "existing"}


def materialize(root, archive):
    root.mkdir()
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        for item in source.infolist():
            target = root / item.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read(item))
            target.chmod(0o444)
    for directory, _, _ in os.walk(root, topdown=False):
        os.chmod(directory, 0o555)


def probe(archive, root, **overrides):
    args = dict(
        effective_agent_definition={"name": "fixed"},
        effective_tools_policy={"allowed": ["read_file"]},
        effective_behavior_config={"model": "pinned"},
        external_mutable_inputs=False,
    )
    args.update(overrides)
    return verify_loaded_build(archive, root, **args)


def test_loaded_build_requires_real_readonly_bytes_and_effective_policy(tmp_path):
    archive = package()
    root = tmp_path / "immutable"
    materialize(root, archive.archive)
    proof = probe(archive.archive, root)
    assert proof.codeArtifactDigest == archive.code_digest
    assert proof.buildManifestDigest == archive.manifest_digest
    with pytest.raises(ValueError, match="configuration"):
        probe(archive.archive, root, effective_tools_policy={"allowed": ["shell"]})
    with pytest.raises(ValueError, match="mutable"):
        probe(archive.archive, root, external_mutable_inputs=True)
    root.chmod(0o755)
    with pytest.raises(ValueError, match="read-only"):
        probe(archive.archive, root)
    root.chmod(0o555)
    (root / "main.py").chmod(0o644)
    with pytest.raises(ValueError, match="writable"):
        probe(archive.archive, root)
    (root / "main.py").write_bytes(b"changed")
    (root / "main.py").chmod(0o444)
    with pytest.raises(ValueError):
        probe(archive.archive, root)


def test_trusted_provider_revalidates_archive_workspace_and_effective_config(tmp_path):
    from tests.kernel.loaded_build_harness import loaded_build

    build = loaded_build(tmp_path / "provider")
    proof = build.provider()
    assert proof.codeArtifactDigest == build.archive.code_digest
    # Finite JCS behavior numbers are legal; only the build manifest forbids floats.
    build.configuration.behavior_config["temperature"] = 0.9
    with pytest.raises(ValueError, match="configuration"):
        build.provider()
    build.configuration.behavior_config["temperature"] = 0.7
    build.archive_path.chmod(0o644)
    with pytest.raises(ValueError, match="read-only"):
        build.provider()
    build.archive_path.chmod(0o444)
    assert build.provider() == proof
    path = build.workspace / "entrypoint.py"
    path.chmod(0o644)
    path.write_bytes(b"changed")
    path.chmod(0o444)
    with pytest.raises(ValueError):
        build.provider()


def test_trusted_provider_refuses_linked_archives_and_inflight_config_change(tmp_path):
    from tests.kernel.loaded_build_harness import loaded_build

    build = loaded_build(tmp_path / "provider")
    os.link(build.archive_path, tmp_path / "archive-link")
    with pytest.raises(ValueError, match="unlinked"):
        build.provider()
    (tmp_path / "archive-link").unlink()
    calls = 0

    def current():
        nonlocal calls
        calls += 1
        if calls == 2:
            build.configuration.tools_policy["tools"].append("shell")
        return build.configuration

    build.provider.configuration = current
    with pytest.raises(ValueError, match="changed during"):
        build.provider()


def test_loaded_build_rejects_symlinks_hardlinks_missing_extra_and_replaced_roots(tmp_path):
    archive = package()
    root = tmp_path / "immutable"
    materialize(root, archive.archive)
    root.chmod(0o755)
    (root / "main.py").unlink()
    (root / "main.py").symlink_to(tmp_path / "elsewhere")
    root.chmod(0o555)
    with pytest.raises(ValueError):
        probe(archive.archive, root)
    root.chmod(0o755)
    (root / "main.py").unlink()
    external = tmp_path / "elsewhere"
    external.write_bytes(b"print('immutable')\n")
    external.chmod(0o444)
    os.link(external, root / "main.py")
    root.chmod(0o555)
    with pytest.raises(ValueError):
        probe(archive.archive, root)

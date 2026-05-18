from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from ksadk.skills.models import ContentHash, SkillRef
from ksadk.skills.package_store import PackageStore, SkillPackageError


def _make_zip(entries: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _ref(digest: str) -> SkillRef:
    return SkillRef(
        skill_id="sk-web",
        version_id="sv-web-v1",
        version="v1",
        name="web-artifacts-builder",
        content_hash=ContentHash.parse(f"sha256:{digest}"),
    )


def test_package_store_verifies_hash_and_extracts_skill_root(tmp_path: Path):
    payload = _make_zip({"web-artifacts-builder/SKILL.md": "---\nname: web-artifacts-builder\n---\n# Skill\n"})
    digest = hashlib.sha256(payload).hexdigest()
    store = PackageStore(cache_dir=tmp_path)

    package = store.store_archive(_ref(digest), payload)

    assert package.root_dir.name == "web-artifacts-builder"
    assert (package.root_dir / "SKILL.md").exists()
    assert package.archive_path.exists()
    assert package.cache_hit is False

    second = store.store_archive(_ref(digest), payload)
    assert second.cache_hit is True
    assert second.root_dir == package.root_dir


def test_package_store_rejects_hash_mismatch(tmp_path: Path):
    payload = _make_zip({"skill/SKILL.md": "# Skill\n"})
    store = PackageStore(cache_dir=tmp_path)

    with pytest.raises(SkillPackageError, match="ContentHash mismatch"):
        store.store_archive(_ref("0" * 64), payload)


def test_package_store_rejects_zip_slip(tmp_path: Path):
    payload = _make_zip({"../escape.txt": "nope", "skill/SKILL.md": "# Skill\n"})
    digest = hashlib.sha256(payload).hexdigest()
    store = PackageStore(cache_dir=tmp_path)

    with pytest.raises(SkillPackageError, match="unsafe zip member"):
        store.store_archive(_ref(digest), payload)


def test_package_store_returns_none_for_corrupted_cache(tmp_path: Path):
    payload = _make_zip({"skill/SKILL.md": "# Skill\n"})
    digest = hashlib.sha256(payload).hexdigest()
    store = PackageStore(cache_dir=tmp_path)
    package = store.store_archive(_ref(digest), payload)
    package.archive_path.write_bytes(b"corrupted")

    assert store.get_cached(_ref(digest)) is None

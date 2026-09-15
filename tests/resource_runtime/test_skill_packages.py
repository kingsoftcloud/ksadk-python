import asyncio
import base64
import hashlib
import io
import json
import stat
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from ksadk.skills.models import ContentHash, SkillRef
from ksadk.skills.package_store import PackageStore, SkillPackageError


def archive(entries=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for name, content in (
            entries or {"SKILL.md": "original", "scripts/run.sh": "echo test"}
        ).items():
            output.writestr(name, content)
    return buffer.getvalue()


def ref(content):
    return SkillRef(
        skill_id="skill-a",
        version_id="version-a",
        version="1",
        name="test",
        content_hash=ContentHash("sha256", hashlib.sha256(content).hexdigest()),
    )


def test_modified_extracted_file_is_rejected_and_rebuilt(tmp_path):
    content = archive()
    store = PackageStore(tmp_path, namespace="tenant-a/account-a", require_hash=True)
    package = store.store_archive(ref(content), content)
    (package.root_dir / "SKILL.md").write_text("modified")
    assert store.get_cached(ref(content)) is None
    restored = store.store_archive(ref(content), content)
    assert (restored.root_dir / "SKILL.md").read_text() == "original"
    assert store.get_cached(ref(content)).cache_hit


@pytest.mark.parametrize("change", ["add", "delete", "link", "parent-link"])
def test_file_set_and_links_are_verified(tmp_path, change):
    store = PackageStore(tmp_path / "cache")
    content = archive()
    package = store.store_archive(ref(content), content)
    path = package.root_dir / "SKILL.md"
    if change == "add":
        (package.root_dir / "extra.py").write_text("unlocked")
    elif change == "delete":
        path.unlink()
    elif change == "link":
        path.unlink()
        path.symlink_to(tmp_path / "outside")
    else:
        script = package.root_dir / "scripts/run.sh"
        script.unlink()
        script.parent.rmdir()
        script.parent.symlink_to(tmp_path)
    assert store.get_cached(ref(content)) is None


@pytest.mark.parametrize("name", ["../outside", "/absolute", "C:/drive", "a\\b"])
def test_archive_traversal_is_rejected_without_publishing(tmp_path, name):
    content = archive({"SKILL.md": "ok", name: "bad"})
    store = PackageStore(tmp_path / "cache")
    with pytest.raises(SkillPackageError):
        store.store_archive(ref(content), content)
    assert store.get_cached(ref(content)) is None
    assert not list(store.cache_dir.glob(".stage-*"))


def test_archive_link_is_rejected(tmp_path):
    entry = zipfile.ZipInfo("link")
    entry.create_system = 3
    entry.external_attr = (stat.S_IFLNK | 0o777) << 16
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as output:
        output.writestr("SKILL.md", "ok")
        output.writestr(entry, "../../outside")
    content = buffer.getvalue()
    with pytest.raises(SkillPackageError):
        PackageStore(tmp_path).store_archive(ref(content), content)


@pytest.mark.parametrize(
    "limits", [{"max_archive_bytes": 10}, {"max_extracted_bytes": 3}, {"max_files": 1}]
)
def test_package_limits_are_enforced(tmp_path, limits):
    content = archive()
    with pytest.raises(SkillPackageError):
        PackageStore(tmp_path, **limits).store_archive(ref(content), content)


def test_namespace_hash_and_untrusted_ids_do_not_share_paths(tmp_path):
    content = archive()
    a = PackageStore(tmp_path, namespace="tenant-a", require_hash=True)
    b = PackageStore(tmp_path, namespace="tenant-b", require_hash=True)
    assert (
        a.store_archive(ref(content), content).root_dir
        != b.store_archive(ref(content), content).root_dir
    )
    unsafe = SkillRef(skill_id="..", version_id="", version="", name="..")
    path = PackageStore(tmp_path)._skill_dir(unsafe)
    assert path.parent == tmp_path
    assert len(path.name) == 64
    with pytest.raises(SkillPackageError, match="ContentHash"):
        a.store_archive(unsafe, content)


def test_concurrent_store_instances_publish_complete_packages(tmp_path):
    content = archive()

    def store(_):
        cache = PackageStore(tmp_path, namespace="tenant-a", require_hash=True)
        package = cache.store_archive(ref(content), content)
        return (package.root_dir / "SKILL.md").read_text()

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(store, range(8))) == ["original"] * 8


async def test_worker_processes_share_only_complete_cache_entries(tmp_path):
    content = archive()
    script = """
import base64, hashlib, json, sys, time
from ksadk.skills.models import SkillRef, ContentHash
from ksadk.skills.package_store import PackageStore
class SlowStore(PackageStore):
    def _safe_extract(self, source, target):
        time.sleep(0.05)
        return super()._safe_extract(source, target)
data = json.load(sys.stdin)
content = base64.b64decode(data['content'])
ref = SkillRef(skill_id='skill-a', version_id='version-a', version='1', name='test',
               content_hash=ContentHash('sha256', hashlib.sha256(content).hexdigest()))
store = SlowStore(data['cache'], namespace='tenant-a', require_hash=True)
package = store.store_archive(ref, content)
assert (package.root_dir / 'SKILL.md').read_text() == 'original'
print('ok')
"""
    processes = []
    try:
        for _ in range(4):
            processes.append(
                await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-I",
                    "-c",
                    script,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            )
        payload = json.dumps(
            {"cache": str(tmp_path), "content": base64.b64encode(content).decode()}
        ).encode()
        results = await asyncio.wait_for(
            asyncio.gather(*(process.communicate(payload) for process in processes)), 10
        )
        for process, (stdout, stderr) in zip(processes, results):
            assert process.returncode == 0, stderr.decode()
            assert stdout.strip() == b"ok"
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.wait()

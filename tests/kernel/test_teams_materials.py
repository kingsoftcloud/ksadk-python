import asyncio
import hashlib
import os
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from ksadk.kernel.teams_execution_context import ContextConflict
from ksadk.kernel.teams_materials import TeamsMaterializer
from ksadk.plugins.teams.cloud_contracts import MaterialManifest
from tests.kernel.teams_host_harness import host_harness as host_harness
from tests.kernel.teams_host_harness import temporary_postgres as temporary_postgres


class BytesTransport:
    def __init__(self, files):
        self.files, self.downloads, self.manifests = files, 0, 0
        self.allowed, self.tamper = True, False
        self.wait = None
        self.body = MaterialManifest(
            kind="files",
            entries=[
                {
                    "path": path,
                    "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                    "sizeBytes": len(data),
                    "mediaType": "text/plain",
                }
                for path, data in files.items()
            ],
        )

    async def manifest(self, context, ref):
        assert context.material_manifest_ref == ref
        self.manifests += 1
        if not self.allowed:
            raise ContextConflict("current material grant revoked")
        return {
            "materialId": ref,
            "manifestDigest": self.body.manifest_digest,
            "state": "ready",
            "sizeBytes": sum(len(data) for data in self.files.values()),
            "manifest": self.body.model_dump(exclude_none=True),
        }

    @asynccontextmanager
    async def blob(self, context, ref, digest):
        assert context.material_manifest_ref == ref
        self.downloads += 1
        data = next(
            value
            for value in self.files.values()
            if "sha256:" + hashlib.sha256(value).hexdigest() == digest
        )
        if self.tamper:
            data = b"wrong"

        async def stream():
            if self.wait:
                await self.wait.wait()
            for start in range(0, len(data), 4096):
                await asyncio.sleep(0)
                yield data[start : start + 4096]

        yield stream()


def configured(h, tmp_path, files=None):
    transport = BytesTransport(files or {"目录/readme.txt": "真实原始字节".encode(), "a.txt": b"a"})
    client = TeamsMaterializer(
        tmp_path / "private-cache", fetch_manifest=transport.manifest, open_blob=transport.blob
    )
    context = h.context.model_copy(
        update={
            "material_manifest_ref": "material-frozen",
            "material_manifest_digest": transport.body.manifest_digest,
        }
    )
    return client, transport, context


async def test_material_prepare_drives_real_kernel_admission_and_rehashes_restart_cache(
    host_harness, tmp_path
):
    h = host_harness
    client, transport, context = configured(h, tmp_path)
    h.host.material_preparer = client.prepare
    context = context.model_copy(
        update={
            "ref": context.ref.model_copy(update={"capabilitiesDigest": h.host.capabilities_digest})
        }
    )
    h.context, h.preparation = context, replace(h.preparation, context=context)
    await h.prepare()
    assert await h.host.kernel.get_execution_grant(context.grant) is not None
    result = await client.materialize(context)
    assert result.proof.digest == transport.body.manifest_digest
    assert (result.directory / "目录/readme.txt").read_bytes() == "真实原始字节".encode()
    downloaded = transport.downloads
    restarted = TeamsMaterializer(
        client.root, fetch_manifest=transport.manifest, open_blob=transport.blob
    )
    assert await restarted.prepare(context) == result.proof
    assert transport.downloads == downloaded and transport.manifests >= 3
    os.chmod(result.directory / "a.txt", 0o600)
    (result.directory / "a.txt").write_bytes(b"x")
    with pytest.raises(ContextConflict, match="checksum"):
        await restarted.prepare(context)


async def test_bad_material_never_admits_native_execution(host_harness, tmp_path):
    h = host_harness
    client, transport, context = configured(h, tmp_path)
    transport.tamper = True
    h.host.material_preparer = client.prepare
    context = context.model_copy(
        update={
            "ref": context.ref.model_copy(update={"capabilitiesDigest": h.host.capabilities_digest})
        }
    )
    h.context, h.preparation = context, replace(h.preparation, context=context)
    with pytest.raises(ContextConflict):
        await h.prepare()
    assert await h.host.kernel.get_execution_grant(context.grant) is None
    assert list(client.root.iterdir()) == []


async def test_cache_scope_is_original_command_and_revocation_applies_to_cache(
    host_harness, tmp_path
):
    h = host_harness
    client, transport, context = configured(h, tmp_path)
    result = await client.materialize(context)
    from uuid import uuid4

    command = context.command.model_copy(update={"command_id": uuid4()})
    another = context.model_copy(
        update={
            "command": command,
            "ref": context.ref.model_copy(update={"commandId": str(command.command_id)}),
        }
    )
    second = await client.materialize(another)
    assert second.directory != result.directory
    transport.allowed = False
    with pytest.raises(ContextConflict, match="revoked"):
        await client.prepare(context)


async def test_symlink_hardlink_and_unlisted_files_are_never_accepted(host_harness, tmp_path):
    h = host_harness
    for kind in ("symlink", "hardlink", "extra"):
        client, _, context = configured(h, tmp_path / kind, {"sub/file.txt": b"data"})
        result = await client.materialize(context)
        outside = tmp_path / (kind + "-outside")
        outside.write_bytes(b"data")
        target = result.directory / "sub/file.txt"
        if kind == "extra":
            (result.directory / "unlisted").write_bytes(b"extra")
        else:
            target.unlink()
            if kind == "symlink":
                target.symlink_to(outside)
            else:
                os.link(outside, target)
        with pytest.raises(ContextConflict):
            await client.prepare(context)
        assert outside.read_bytes() == b"data"


async def test_cancelled_download_leaves_no_proof_or_partially_ready_directory(
    host_harness, tmp_path
):
    client, transport, context = configured(host_harness, tmp_path)
    transport.wait = asyncio.Event()
    task = asyncio.create_task(client.prepare(context))
    for _ in range(100):
        if transport.downloads:
            break
        await asyncio.sleep(0.001)
    assert transport.downloads
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(client.root.iterdir()) == []
    transport.wait = None
    assert (await client.prepare(context)).digest == context.material_manifest_digest


async def test_parallel_prepare_and_reauthorization_before_atomic_commit(host_harness, tmp_path):
    client, transport, context = configured(host_harness, tmp_path)
    left, right = await asyncio.gather(client.materialize(context), client.materialize(context))
    assert left == right
    assert len(list(client.root.iterdir())) == 1
    other, transport2, context2 = configured(host_harness, tmp_path / "revoked")
    original = transport2.manifest

    async def revoked_after_download(ctx, ref):
        if transport2.downloads:
            transport2.allowed = False
        return await original(ctx, ref)

    other.fetch_manifest = revoked_after_download
    with pytest.raises(ContextConflict, match="revoked"):
        await other.prepare(context2)
    assert list(other.root.iterdir()) == []


async def test_full_manifest_digest_required_before_downloading(host_harness, tmp_path):
    client, transport, context = configured(host_harness, tmp_path)
    bad = context.model_copy(update={"material_manifest_digest": "sha256:" + "0" * 64})
    with pytest.raises(ContextConflict, match="manifest"):
        await client.prepare(bad)
    assert transport.downloads == 0


def test_material_cache_root_must_be_trusted_absolute_and_not_symlink(tmp_path):
    async def unused(*args):
        raise AssertionError("unused")

    with pytest.raises(ValueError):
        TeamsMaterializer("relative", fetch_manifest=unused, open_blob=unused)
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        TeamsMaterializer(link, fetch_manifest=unused, open_blob=unused)

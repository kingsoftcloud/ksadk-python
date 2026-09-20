"""Verified Teams material downloads into a private, execution-scoped cache.

The transport is injected by the trusted Node/runtime assembly. It receives the
original preparation, never a model-supplied URL, bucket credential or local
destination. No archive extraction, git command or symlink traversal is used.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncContextManager, AsyncIterable, Awaitable, Callable

from pydantic import Field

from ksadk.kernel.teams_execution_context import ContextConflict, PreparedTeamsContext
from ksadk.plugins.teams.cloud_contracts import (
    Digest,
    Identifier,
    MaterialManifest,
    MaterialProof,
    WireModel,
    digest,
)

CHUNK_BYTES = 64 * 1024


class ReadyMaterial(WireModel):
    materialId: Identifier
    manifestDigest: Digest
    state: str
    sizeBytes: int = Field(strict=True, ge=0, le=64 * 1024 * 1024)
    manifest: MaterialManifest


@dataclass(frozen=True)
class PreparedMaterial:
    proof: MaterialProof
    directory: Path


def _open_directory(path, *, dir_fd=None):
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)


def _parent(root_fd, path, *, create=False):
    """Return an owned parent fd; every component is opened without symlinks."""
    parts = path.split("/")
    parent = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=parent)
                    os.fsync(parent)
                except FileExistsError:
                    pass
            child = _open_directory(part, dir_fd=parent)
            os.close(parent)
            parent = child
        return parent, parts[-1]
    except BaseException:
        os.close(parent)
        raise


def _verify_file(root_fd, entry):
    parent, name = _parent(root_fd, entry.path)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size != entry.sizeBytes
            ):
                raise ContextConflict("material cache entry is not the original regular file")
            fingerprint, size = hashlib.sha256(), 0
            while chunk := source.read(min(CHUNK_BYTES, entry.sizeBytes - size + 1)):
                size += len(chunk)
                if size > entry.sizeBytes:
                    raise ContextConflict("material cache file changed while reading")
                fingerprint.update(chunk)
            after = os.fstat(source.fileno())
            if (
                size != entry.sizeBytes
                or "sha256:" + fingerprint.hexdigest() != entry.digest
                or (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            ):
                raise ContextConflict("material cache file checksum changed")
    finally:
        os.close(parent)


def _verify_tree(root_fd, name, manifest):
    fd = _open_directory(name, dir_fd=root_fd)
    try:
        expected_files = {entry.path for entry in manifest.entries}
        expected_dirs = {
            str(parent)
            for path in expected_files
            for parent in Path(path).parents
            if str(parent) != "."
        }

        def walk(directory, prefix=""):
            for child in os.listdir(directory):
                path = prefix + child
                info = os.stat(child, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode) and path in expected_dirs:
                    child_fd = _open_directory(child, dir_fd=directory)
                    try:
                        walk(child_fd, path + "/")
                    finally:
                        os.close(child_fd)
                elif not stat.S_ISREG(info.st_mode) or path not in expected_files:
                    raise ContextConflict("material cache contains an unexpected file or link")

        walk(fd)
        for entry in manifest.entries:
            _verify_file(fd, entry)
        return os.fstat(fd).st_ino
    finally:
        os.close(fd)


async def _verify_async(root_fd, name, manifest):
    # A cancelled asyncio.to_thread does not stop its OS thread. Give that
    # thread an independent fd and shield scheduling so cancellation cannot
    # leak it or close/reuse the caller's descriptor while it is being read.
    owned_fd = os.dup(root_fd)

    def verify():
        try:
            return _verify_tree(owned_fd, name, manifest)
        finally:
            os.close(owned_fd)

    task = asyncio.create_task(asyncio.to_thread(verify))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(lambda result: None if result.cancelled() else result.exception())
        raise


class TeamsMaterializer:
    """Materialize a trusted preparation before native execution admission.

    ``fetch_manifest(context, material_ref)`` returns the Server's ready manifest
    after current command/grant authorization. ``open_blob(context, ref, digest)``
    is an async context manager yielding an async iterator of raw byte chunks.
    Both callbacks must use the configured Server and current short-lived Node
    or runtime authorization; cached files do not skip this manifest check.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        fetch_manifest: Callable[[PreparedTeamsContext, str], Awaitable[dict]],
        open_blob: Callable[
            [PreparedTeamsContext, str, str], AsyncContextManager[AsyncIterable[bytes]]
        ],
    ):
        if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            raise RuntimeError("Teams materialization requires no-follow directory operations")
        path = Path(root)
        if not path.is_absolute():
            raise ValueError("material cache root must be an absolute trusted path")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink():
            raise ValueError("material cache root must not be a symlink")
        self.root = path.resolve(strict=True)
        self.fetch_manifest, self.open_blob = fetch_manifest, open_blob

    async def prepare(self, context: PreparedTeamsContext) -> MaterialProof:
        return (await self.materialize(context)).proof

    async def materialize(self, context: PreparedTeamsContext) -> PreparedMaterial:
        # Revalidation prevents model_copy or a caller's mutable dict bypassing
        # the original command/grant/context validator.
        context = PreparedTeamsContext.model_validate_json(context.model_dump_json())
        ref, fingerprint = context.material_manifest_ref, context.material_manifest_digest
        if ref is None or fingerprint is None:
            raise ContextConflict("preparation has no frozen material manifest")
        response = ReadyMaterial.model_validate(await self.fetch_manifest(context, ref))
        manifest = response.manifest
        if (
            response.state != "ready"
            or response.materialId != ref
            or response.manifestDigest != fingerprint
            or manifest.manifest_digest != fingerprint
            or response.sizeBytes != sum(entry.sizeBytes for entry in manifest.entries)
        ):
            raise ContextConflict("downloaded manifest differs from trusted preparation")
        name = (
            "material_"
            + digest(
                {
                    "authority": context.ref.authorityId,
                    "owner": context.owner_subject,
                    "group": context.ref.groupId,
                    "teamRun": context.ref.teamRunId,
                    "command": context.ref.commandId,
                    "contextRef": context.context_ref,
                    "manifestRef": ref,
                    "manifestDigest": fingerprint,
                }
            )[7:]
        )
        root_fd = _open_directory(self.root)
        stage = None
        try:
            try:
                await _verify_async(root_fd, name, manifest)
            except FileNotFoundError:
                # Only a missing cache *directory* allows download. A damaged
                # existing directory remains evidence, never overwritten.
                try:
                    os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise ContextConflict("existing material cache is incomplete")
            else:
                return PreparedMaterial(
                    MaterialProof(manifestRef=ref, digest=fingerprint), self.root / name
                )
            stage = Path(tempfile.mkdtemp(prefix=".teams-material-", dir=self.root))
            stage_fd = _open_directory(stage.name, dir_fd=root_fd)
            try:
                for entry in sorted(manifest.entries, key=lambda item: item.path):
                    parent, leaf = _parent(stage_fd, entry.path, create=True)
                    try:
                        fd = os.open(
                            leaf,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            mode=0o600,
                            dir_fd=parent,
                        )
                        with os.fdopen(fd, "wb") as target:
                            hasher, size = hashlib.sha256(), 0
                            async with self.open_blob(context, ref, entry.digest) as chunks:
                                async for chunk in chunks:
                                    if not isinstance(chunk, bytes) or len(chunk) > CHUNK_BYTES:
                                        raise ContextConflict(
                                            "material stream must use bounded byte chunks"
                                        )
                                    size += len(chunk)
                                    if size > entry.sizeBytes:
                                        raise ContextConflict(
                                            "material stream exceeded declared size"
                                        )
                                    target.write(chunk)
                                    hasher.update(chunk)
                            if (
                                size != entry.sizeBytes
                                or "sha256:" + hasher.hexdigest() != entry.digest
                            ):
                                raise ContextConflict(
                                    "downloaded bytes differ from frozen material digest"
                                )
                            target.flush()
                            os.fsync(target.fileno())
                            os.fchmod(target.fileno(), 0o400)
                        os.fsync(parent)
                    finally:
                        os.close(parent)
                # Reauthorize after network I/O and before exposing a complete
                # directory. Expired or revoked grants must fail this callback.
                current = ReadyMaterial.model_validate(await self.fetch_manifest(context, ref))
                if current != response:
                    raise ContextConflict(
                        "material authorization or manifest changed during download"
                    )
                os.fsync(stage_fd)
                inode = os.fstat(stage_fd).st_ino
            finally:
                os.close(stage_fd)
            if await _verify_async(root_fd, stage.name, manifest) != inode:
                raise ContextConflict("material staging directory was replaced")
            try:
                os.rename(stage.name, name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            except OSError as exc:
                if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise
                # An independent preparation may have completed the same
                # immutable target. Verify its actual bytes before using it.
                await _verify_async(root_fd, name, manifest)
            else:
                stage = None
                if await _verify_async(root_fd, name, manifest) != inode:
                    raise ContextConflict("material destination changed while committing")
                os.fsync(root_fd)
            return PreparedMaterial(
                MaterialProof(manifestRef=ref, digest=fingerprint), self.root / name
            )
        except OSError as exc:
            raise ContextConflict(
                "material filesystem rejected an unsafe or unavailable path"
            ) from exc
        finally:
            os.close(root_fd)
            if stage is not None:
                # Python's fd-based rmtree avoids following malicious links.
                # Only this request's private unfinished staging dir is removed.
                shutil.rmtree(stage, ignore_errors=True)

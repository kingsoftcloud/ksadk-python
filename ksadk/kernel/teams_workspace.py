"""Private per-execution working files; never a model-selected filesystem root."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ksadk.kernel.teams_execution_context import ContextConflict
from ksadk.kernel.teams_materials import CHUNK_BYTES, _open_directory, _parent
from ksadk.plugins.teams.cloud_contracts import RelativePath, digest


def _regular(fd, *, maximum):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
        raise ContextConflict("workspace requires a bounded regular file")
    return info


def prepare_workspace(root, context, *, prepared_material=None, manifest=None):
    root = Path(root)
    if not root.is_absolute():
        raise ContextConflict("workspace root must be absolute")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    identity = {
        "contextRef": context.context_ref,
        "contextDigest": context.context_digest,
        "manifestDigest": context.material_manifest_digest,
    }
    marker = json.dumps(identity, sort_keys=True).encode()
    name = hashlib.sha256(context.context_ref.encode()).hexdigest()
    root_fd = _open_directory(root)
    stage = None
    try:
        try:
            current = _open_directory(name, dir_fd=root_fd)
        except FileNotFoundError:
            current = None
        if current is not None:
            try:
                fd = os.open(".teams-workspace", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current)
                with os.fdopen(fd, "rb") as stream:
                    _regular(stream.fileno(), maximum=4096)
                    if stream.read() != marker:
                        raise ContextConflict("workspace belongs to another frozen context")
            finally:
                os.close(current)
            return root / name
        if context.material_manifest_ref:
            if (
                prepared_material is None
                or manifest is None
                or prepared_material.proof.manifestRef != context.material_manifest_ref
                or prepared_material.proof.digest != context.material_manifest_digest
                or manifest.manifest_digest != context.material_manifest_digest
            ):
                raise ContextConflict("workspace requires the verified original material")
        stage = Path(tempfile.mkdtemp(prefix=".teams-workspace-", dir=root))
        target = _open_directory(stage)
        try:
            if prepared_material is not None:
                source = _open_directory(prepared_material.directory)
                try:
                    for entry in manifest.entries:
                        if any(part.startswith(".teams-") for part in entry.path.split("/")):
                            raise ContextConflict("material uses reserved workspace control path")
                        parent, leaf = _parent(source, entry.path)
                        output, name_in_target = _parent(target, entry.path, create=True)
                        try:
                            src = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                            dst = os.open(
                                name_in_target,
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                0o600,
                                dir_fd=output,
                            )
                            with os.fdopen(src, "rb") as r, os.fdopen(dst, "wb") as w:
                                before = _regular(r.fileno(), maximum=entry.sizeBytes)
                                fingerprint, size = hashlib.sha256(), 0
                                while chunk := r.read(CHUNK_BYTES):
                                    size += len(chunk)
                                    if size > entry.sizeBytes:
                                        raise ContextConflict(
                                            "material changed during workspace copy"
                                        )
                                    w.write(chunk)
                                    fingerprint.update(chunk)
                                after = os.fstat(r.fileno())
                                if (
                                    size != entry.sizeBytes
                                    or "sha256:" + fingerprint.hexdigest() != entry.digest
                                    or before.st_mtime_ns != after.st_mtime_ns
                                ):
                                    raise ContextConflict("workspace material digest mismatch")
                                w.flush()
                                os.fsync(w.fileno())
                        finally:
                            os.close(parent)
                            os.close(output)
                finally:
                    os.close(source)
            fd = os.open(
                ".teams-workspace", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=target
            )
            with os.fdopen(fd, "wb") as w:
                w.write(marker)
                w.flush()
                os.fsync(w.fileno())
            os.fsync(target)
        finally:
            os.close(target)
        try:
            os.rename(stage.name, name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            stage = None
            os.fsync(root_fd)
        except FileExistsError:
            return prepare_workspace(
                root, context, prepared_material=prepared_material, manifest=manifest
            )
        return root / name
    finally:
        os.close(root_fd)
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)


class FileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: RelativePath


class WriteArgs(FileArgs):
    content: str = Field(max_length=1_000_000)


def workspace_tools(workspace, *, context, authorize):
    from ksadk.harness.tools import HarnessTool

    def path(value):
        if any(part.startswith(".teams-") for part in value.split("/")):
            raise ContextConflict("workspace control files are reserved")
        return value

    async def read(arguments, call_id):
        value = FileArgs.model_validate(arguments)
        await authorize(context, "read_file")
        root = _open_directory(workspace)
        try:
            parent, name = _parent(root, path(value.path))
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                with os.fdopen(fd, "rb") as stream:
                    _regular(stream.fileno(), maximum=1_000_000)
                    data = stream.read(1_000_001)
                    if len(data) > 1_000_000:
                        raise ContextConflict("workspace read too large")
                    return {"path": value.path, "content": data.decode("utf-8")}
            finally:
                os.close(parent)
        finally:
            os.close(root)

    async def write(arguments, call_id):
        value = WriteArgs.model_validate(arguments)
        if not isinstance(call_id, str) or not call_id:
            raise ContextConflict("workspace write requires a stable call identity")
        await authorize(context, "write_file")
        root = _open_directory(workspace)
        parent = None
        temporary = ".teams-write-" + uuid4().hex
        try:
            parent, name = _parent(root, path(value.path), create=True)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
            with os.fdopen(fd, "wb") as stream:
                stream.write(value.content.encode())
                stream.flush()
                os.fsync(stream.fileno())
            await authorize(context, "write_file")
            # replace affects only this trusted directory entry, never follows
            # a pre-existing symlink/hardlink into another working tree.
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
            return {"path": value.path, "digest": digest(value.content)}
        finally:
            if parent is not None:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
                os.close(parent)
            os.close(root)

    async def list_files(arguments, call_id):
        if arguments:
            raise ContextConflict("list_files accepts no path or scope")
        await authorize(context, "list_files")
        result = []

        def scan(fd, prefix="", depth=0):
            if depth > 32:
                raise ContextConflict("workspace nesting too deep")
            for name in sorted(os.listdir(fd)):
                if name.startswith(".teams-"):
                    continue
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise ContextConflict("workspace contains a symbolic link")
                relative = prefix + name
                if stat.S_ISDIR(info.st_mode):
                    child = _open_directory(name, dir_fd=fd)
                    try:
                        scan(child, relative + "/", depth + 1)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    result.append({"path": relative, "sizeBytes": info.st_size})
                    if len(result) > 1000:
                        raise ContextConflict("workspace file listing exceeds limit")
                else:
                    raise ContextConflict("workspace contains a non-regular file")

        root = _open_directory(workspace)
        try:
            scan(root)
        finally:
            os.close(root)
        return {"files": result}

    return {
        "list_files": HarnessTool(
            "list_files",
            "列出当前任务工作区的材料和报告文件",
            {"type": "object", "additionalProperties": False},
            list_files,
            "teams-workspace",
        ),
        "read_file": HarnessTool(
            "read_file",
            "读取当前团队任务工作区的 UTF-8 文本文件",
            FileArgs.model_json_schema(),
            read,
            "teams-workspace",
        ),
        "write_file": HarnessTool(
            "write_file",
            "写入当前团队任务工作区的 UTF-8 报告或文本文件",
            WriteArgs.model_json_schema(),
            write,
            "teams-workspace",
        ),
    }

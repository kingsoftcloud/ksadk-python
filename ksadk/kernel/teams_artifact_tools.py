"""Execution-owned artifact tools over bounded, verified workspace bytes.

Only trusted Host assembly supplies the context, workspace, authorization and
Server transports. Model arguments contain a relative file path or artifact ID.
No model-supplied identity, endpoint or filesystem root is accepted.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ksadk.harness.tools import HarnessTool
from ksadk.kernel.teams_execution_context import ContextConflict, PreparedTeamsContext
from ksadk.kernel.teams_materials import (
    CHUNK_BYTES,
    PreparedMaterial,
    _open_directory,
    _parent,
    _verify_file,
)
from ksadk.plugins.teams.cloud_contracts import (
    MAX_FILE_BYTES,
    Digest,
    Identifier,
    MaterialEntry,
    RelativePath,
    WireModel,
    digest,
)
from ksadk.plugins.teams.tool_service import TeamsToolMethods


class _Publish(WireModel):
    path: RelativePath
    name: RelativePath | None = None


class _Read(WireModel):
    artifactId: Identifier


class _Source(WireModel):
    authorityRef: Identifier
    groupId: Identifier
    memberId: Identifier
    bindingRef: Identifier
    providerRef: Identifier
    sessionId: Identifier
    runId: Identifier


class _Artifact(BaseModel):
    # Preserve Server projection additions, but no extra field grants authority
    # and no returned `uri` is ever used as a download address.
    model_config = ConfigDict(extra="allow", strict=True)
    artifactId: Identifier
    groupId: Identifier
    teamRunId: Identifier
    name: RelativePath
    mediaType: str = Field(min_length=1, max_length=200)
    digest: Digest
    sizeBytes: int = Field(strict=True, ge=0, le=MAX_FILE_BYTES)
    state: str
    source: _Source


def _call_id(value):
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ContextConflict("artifact tool requires a stable call identity")
    return value


def _identity(metadata):
    return metadata.model_dump(
        include={
            "artifactId",
            "groupId",
            "teamRunId",
            "name",
            "mediaType",
            "digest",
            "sizeBytes",
            "state",
            "source",
        }
    )


def _snapshot(root_fd, path):
    parent, leaf = _parent(root_fd, path)
    try:
        fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or not 0 <= before.st_size <= MAX_FILE_BYTES
            ):
                raise ContextConflict("artifact must be a bounded regular file in this workspace")
            copy = tempfile.TemporaryFile()
            try:
                hasher, size = hashlib.sha256(), 0
                while chunk := source.read(min(CHUNK_BYTES, before.st_size - size + 1)):
                    size += len(chunk)
                    if size > before.st_size:
                        raise ContextConflict("artifact changed while being read")
                    copy.write(chunk)
                    hasher.update(chunk)
                after = os.fstat(source.fileno())
                current = os.stat(leaf, dir_fd=parent, follow_symlinks=False)

                def fields(s):
                    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_nlink)

                if (
                    size != before.st_size
                    or fields(before) != fields(after)
                    or fields(current) != fields(after)
                ):
                    raise ContextConflict("artifact changed while being read")
                copy.seek(0)
                return copy, "sha256:" + hasher.hexdigest(), size
            except BaseException:
                copy.close()
                raise
    finally:
        os.close(parent)


class TeamsArtifactTools:
    """Build two HarnessTools for one trusted current execution.

    ``invoke(operation, arguments, call_id)`` calls the existing Server team
    tool endpoint. ``authorize(context, operation)`` checks the current Host
    policy/grant before local I/O. The factory must wrap returned tools with
    its normal effect guard; these callbacks never come from model arguments.
    """

    def __init__(self, context, *, workspace, transport, invoke, authorize, prepared_material=None):
        self.context = PreparedTeamsContext.model_validate_json(context.model_dump_json())
        if not callable(invoke) or not callable(authorize) or transport is None:
            raise ContextConflict("artifact tools require trusted authorization and transport")
        if workspace is None:
            raise ContextConflict("artifact tools require a prepared workspace")
        path = Path(workspace)
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            raise ContextConflict("artifact workspace must be an existing trusted directory")
        if self.context.material_manifest_ref is not None and (
            not isinstance(prepared_material, PreparedMaterial)
            or prepared_material.proof.manifestRef != self.context.material_manifest_ref
            or prepared_material.proof.digest != self.context.material_manifest_digest
        ):
            raise ContextConflict("original frozen materials were not prepared for this workspace")
        self.workspace = path.resolve(strict=True)
        with self._directory(self.workspace) as fd:
            info = os.fstat(fd)
            self._workspace_identity = (info.st_dev, info.st_ino)
        self.transport, self.invoke, self.authorize = transport, invoke, authorize

    @staticmethod
    @contextmanager
    def _directory(path):
        try:
            fd = _open_directory(path)
            try:
                yield fd
            finally:
                os.close(fd)
        except OSError as exc:
            raise ContextConflict("artifact filesystem path is unsafe or unavailable") from exc

    @contextmanager
    def _workspace(self):
        with self._directory(self.workspace) as fd:
            actual = os.fstat(fd)
            if (actual.st_dev, actual.st_ino) != self._workspace_identity:
                raise ContextConflict("original artifact workspace was replaced")
            yield fd

    def _key(self, call_id, phase):
        return (
            "artifact:"
            + digest(
                [
                    self.context.ref.authorityId,
                    self.context.ref.commandId,
                    self.context.context_ref,
                    _call_id(call_id),
                    phase,
                ]
            )[7:]
        )

    def tools(self):
        handlers = {"team_publish_artifact": self.publish, "team_read_artifact": self.read}
        return {
            name: HarnessTool(name, description, parameters, handlers[name], "teams")
            for name, description, parameters in TeamsToolMethods.tool_definitions()
            if name in handlers
        }

    def _metadata(self, value, artifact_id):
        item = _Artifact.model_validate(value)
        ref = self.context.ref
        if (
            item.artifactId != artifact_id
            or item.state != "ready"
            or item.groupId != ref.groupId
            or item.source.groupId != ref.groupId
            or item.source.authorityRef != ref.authorityId
        ):
            raise ContextConflict("artifact metadata does not belong to the authorized group")
        return item

    def _uploaded(self, value, expected, *, ready=False):
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("artifactId"), str)
            or not value["artifactId"].startswith("tar_")
        ):
            raise ContextConflict("Server did not return a durable artifact reference")
        if any(value.get(k) != expected[k] for k in ("name", "mediaType", "digest", "sizeBytes")):
            raise ContextConflict("Server artifact differs from original uploaded bytes")
        producer = value.get("producer")
        if (
            not isinstance(producer, dict)
            or value.get("groupId") != self.context.ref.groupId
            or value.get("teamRunId") != self.context.ref.teamRunId
            or producer.get("contextRef") != self.context.context_ref
            or producer.get("ref") != self.context.ref.model_dump(mode="json")
            or not isinstance(producer.get("nativeRunId"), str)
            or not producer["nativeRunId"]
            or not isinstance(producer.get("storeIncarnation"), str)
            or not producer["storeIncarnation"]
            or value.get("state") not in ({"ready"} if ready else {"pending", "ready"})
        ):
            raise ContextConflict("Server artifact is not from the original execution")
        return value["artifactId"]

    async def publish(self, arguments, call_id):
        request = _Publish.model_validate(arguments)
        _call_id(call_id)
        await self.authorize(self.context, "team_publish_artifact")
        with self._workspace() as root_fd:
            source, fingerprint, size = _snapshot(root_fd, request.path)
        with source:
            body = {
                "name": request.name or PurePosixPath(request.path).name,
                "mediaType": "application/octet-stream",
                "digest": fingerprint,
                "sizeBytes": size,
                "idempotencyKey": self._key(call_id, "create"),
            }
            artifact_id = self._uploaded(
                await self.transport.create_artifact(self.context, body), body
            )

            async def chunks():
                while chunk := source.read(CHUNK_BYTES):
                    yield chunk

            await self.transport.upload_artifact(
                self.context, artifact_id, chunks(), size_bytes=size
            )
            ready = await self.transport.finalize_artifact(
                self.context, artifact_id, idempotency_key=self._key(call_id, "finalize")
            )
            if self._uploaded(ready, body, ready=True) != artifact_id:
                raise ContextConflict("artifact finalize returned another reference")
            await self.authorize(self.context, "team_publish_artifact")
            with self._workspace():
                pass
            value = await self.invoke(
                "team_publish_artifact", {"path": artifact_id, "name": body["name"]}, call_id
            )
            item = self._metadata(value, artifact_id)
            if any(
                getattr(item, key) != body[key]
                for key in ("name", "mediaType", "digest", "sizeBytes")
            ) or (
                item.teamRunId != self.context.ref.teamRunId
                or item.source.memberId != self.context.ref.memberId
                or item.source.bindingRef != self.context.ref.bindingRef
                or item.source.providerRef != self.context.ref.providerRef
                or item.source.sessionId != self.context.ref.sessionId
                or item.source.runId != ready["producer"]["nativeRunId"]
            ):
                raise ContextConflict("published artifact metadata changed")
            return value

    async def read(self, arguments, call_id):
        request = _Read.model_validate(arguments)
        _call_id(call_id)
        await self.authorize(self.context, "team_read_artifact")
        value = await self.invoke("team_read_artifact", request.model_dump(), call_id)
        item = self._metadata(value, request.artifactId)
        filename = "input_" + digest([item.artifactId, item.digest])[7:]
        entry = MaterialEntry(
            path=filename, digest=item.digest, sizeBytes=item.sizeBytes, mediaType=item.mediaType
        )
        with self._workspace() as root_fd:
            try:
                os.mkdir(".team-artifacts", mode=0o700, dir_fd=root_fd)
                os.fsync(root_fd)
            except FileExistsError:
                pass
            directory = _open_directory(".team-artifacts", dir_fd=root_fd)
            temporary = None
            try:
                try:
                    _verify_file(directory, entry)
                except FileNotFoundError:
                    temporary = ".partial-" + uuid4().hex
                    fd = os.open(
                        temporary,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        mode=0o600,
                        dir_fd=directory,
                    )
                    with os.fdopen(fd, "wb") as target:
                        fingerprint, size = hashlib.sha256(), 0
                        async with self.transport.open_artifact(
                            self.context, self.context.ref.groupId, item.artifactId
                        ) as chunks:
                            async for chunk in chunks:
                                if not isinstance(chunk, bytes) or len(chunk) > CHUNK_BYTES:
                                    raise ContextConflict(
                                        "artifact download chunks must be bounded bytes"
                                    )
                                size += len(chunk)
                                if size > item.sizeBytes:
                                    raise ContextConflict("artifact download exceeds declared size")
                                fingerprint.update(chunk)
                                target.write(chunk)
                        if (
                            size != item.sizeBytes
                            or "sha256:" + fingerprint.hexdigest() != item.digest
                        ):
                            raise ContextConflict("artifact download differs from original digest")
                        target.flush()
                        os.fsync(target.fileno())
                        os.fchmod(target.fileno(), 0o400)
                        inode = os.fstat(target.fileno()).st_ino
                    await self.authorize(self.context, "team_read_artifact")
                    current = self._metadata(
                        await self.invoke("team_read_artifact", request.model_dump(), call_id),
                        item.artifactId,
                    )
                    if _identity(current) != _identity(item):
                        raise ContextConflict("artifact metadata changed during download")
                    try:
                        # Atomic no-overwrite publication. A competing reader
                        # may finish first, but changed user files are retained.
                        os.link(
                            temporary,
                            filename,
                            src_dir_fd=directory,
                            dst_dir_fd=directory,
                            follow_symlinks=False,
                        )
                    except FileExistsError:
                        pass
                    else:
                        if (
                            os.stat(filename, dir_fd=directory, follow_symlinks=False).st_ino
                            != inode
                        ):
                            raise ContextConflict("artifact staging file was replaced")
                    os.unlink(temporary, dir_fd=directory)
                    temporary = None
                    os.fsync(directory)
                    _verify_file(directory, entry)
                await self.authorize(self.context, "team_read_artifact")
                with self._workspace():
                    pass
                return {**value, "workspacePath": ".team-artifacts/" + filename}
            finally:
                if temporary is not None:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
                os.close(directory)

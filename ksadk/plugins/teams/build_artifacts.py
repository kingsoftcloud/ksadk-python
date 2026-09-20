"""Bounded Code archive evidence for Teams automatic standby.

This wraps existing CodeBuilder ZIPs; it does not install dependencies or upload
code. Names, version labels and machine paths are never release equivalence.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Mapping

from pydantic import Field, model_validator

from .cloud_contracts import (
    MAX_FILE_BYTES,
    Digest,
    RelativePath,
    WireModel,
    canonical_bytes,
    digest,
)

MANIFEST_PATH = ".teams/build-manifest.json"
DESCRIPTORS = {
    "agentDefinitionDigest": ".teams/agent-definition.json",
    "toolsPolicyDigest": ".teams/tools-policy.json",
    "behaviorConfigDigest": ".teams/behavior-config.json",
    "dependencyLockDigest": ".teams/dependency.lock",
}
MAX_UNPACKED = 64 * 1024 * 1024
MAX_FILES = 4096
CHUNK = 64 * 1024


def byte_digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _no_floats(value):
    if isinstance(value, float):
        raise ValueError("Teams build descriptions forbid floating point values")
    if isinstance(value, dict):
        for child in value.values():
            _no_floats(child)
    elif isinstance(value, list):
        for child in value:
            _no_floats(child)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate build JSON key")
        result[key] = value
    return result


def _json(raw, *, manifest=False):
    try:
        value = json.loads(raw, object_pairs_hook=_unique_pairs)
        if manifest:
            _no_floats(value)
    except RecursionError as exc:
        raise ValueError("build JSON is too deeply nested") from exc
    if canonical_bytes(value) != raw:
        raise ValueError("build descriptions must use canonical JSON bytes")
    return value


class BuildFile(WireModel):
    path: RelativePath
    digest: Digest
    sizeBytes: int = Field(strict=True, ge=0, le=MAX_FILE_BYTES)


class TeamsBuildManifest(WireModel):
    schemaVersion: Literal["teams-build/v1"] = "teams-build/v1"
    entrypoint: RelativePath
    agentDefinitionDigest: Digest
    toolsPolicyDigest: Digest
    dependencyLockDigest: Digest
    materialContractVersion: Literal["materials/v1"] = "materials/v1"
    behaviorConfigDigest: Digest
    files: list[BuildFile] = Field(min_length=1, max_length=MAX_FILES)

    @model_validator(mode="after")
    def complete(self):
        paths = [item.path for item in self.files]
        if paths != sorted(set(paths)) or MANIFEST_PATH in paths:
            raise ValueError("build files must be unique, sorted and exclude the manifest")
        if self.entrypoint not in paths or self.entrypoint.startswith(".teams/"):
            raise ValueError("entrypoint must be an included code file")
        if not set(DESCRIPTORS.values()).issubset(paths):
            raise ValueError("fixed build descriptions and dependency lock are required")
        if sum(item.sizeBytes for item in self.files) > MAX_UNPACKED:
            raise ValueError("unpacked build exceeds 64 MiB")
        files = {item.path: item for item in self.files}
        for field, path in DESCRIPTORS.items():
            if files[path].digest != getattr(self, field):
                raise ValueError("build description digest mismatch")
        for path in paths:
            parts = path.split("/")
            if any("/".join(parts[:i]) in files for i in range(1, len(parts))):
                raise ValueError("build file/directory path conflict")
        return self


class LoadedBuildEvidence(WireModel):
    schemaVersion: Literal["teams-loaded-build/v1"] = "teams-loaded-build/v1"
    codeArtifactDigest: Digest
    buildManifestDigest: Digest
    agentDefinitionDigest: Digest
    toolsPolicyDigest: Digest
    dependencyLockDigest: Digest
    behaviorConfigDigest: Digest
    materialContractVersion: Literal["materials/v1"] = "materials/v1"
    immutableWorkspace: Literal[True]
    externalMutableInputs: Literal[False]


@dataclass(frozen=True)
class VerifiedBuildArchive:
    archive: bytes
    manifest: TeamsBuildManifest

    @property
    def code_digest(self):
        return byte_digest(self.archive)

    @property
    def manifest_digest(self):
        return digest(self.manifest.model_dump(mode="json"))

    def loaded_evidence(self) -> LoadedBuildEvidence:
        """Only called after verifying the actual immutable loaded workspace."""
        return LoadedBuildEvidence(
            codeArtifactDigest=self.code_digest,
            buildManifestDigest=self.manifest_digest,
            immutableWorkspace=True,
            externalMutableInputs=False,
            **{field: getattr(self.manifest, field) for field in DESCRIPTORS},
        )


def _archive_bytes(source) -> bytes:
    if isinstance(source, bytes):
        data = source
    else:
        chunks, total = [], 0
        while chunk := source.read(min(CHUNK, MAX_FILE_BYTES - total + 1)):
            if not isinstance(chunk, bytes):
                raise ValueError("build archive must contain raw bytes")
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise ValueError("Code archive exceeds 20 MiB")
            chunks.append(chunk)
        data = b"".join(chunks)
    if not data or len(data) > MAX_FILE_BYTES:
        raise ValueError("Code archive must be nonempty and at most 20 MiB")
    return data


def _payload(archive: bytes) -> dict[str, bytes]:
    from pydantic import TypeAdapter

    files = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        entries = package.infolist()
        if len(entries) > MAX_FILES + 1:
            raise ValueError("too many archive files")
        for item in entries:
            path = TypeAdapter(RelativePath).validate_python(item.filename)
            if (
                item.is_dir()
                or path in files
                or item.flag_bits & 1
                or stat.S_IFMT(item.external_attr >> 16) not in {0, stat.S_IFREG}
                or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                or item.file_size > MAX_FILE_BYTES
            ):
                raise ValueError("archive contains unsupported, duplicate or oversized files")
            total += item.file_size
            if total > MAX_UNPACKED + 2 * 1024 * 1024:
                raise ValueError("archive unpacked size exceeds limit")
            with package.open(item) as stream:
                raw = stream.read(item.file_size + 1)
            if len(raw) != item.file_size:
                raise ValueError("archive file size mismatch")
            files[path] = raw
    return files


def verify_build_archive(source) -> VerifiedBuildArchive:
    archive = _archive_bytes(source)
    try:
        files = _payload(archive)
    except zipfile.BadZipFile as exc:
        raise ValueError("invalid Code ZIP archive") from exc
    manifest_raw = files.pop(MANIFEST_PATH, None)
    if manifest_raw is None or len(manifest_raw) > 2 * 1024 * 1024:
        raise ValueError("teams-build/v1 manifest missing or oversized")
    manifest = TeamsBuildManifest.model_validate(_json(manifest_raw, manifest=True))
    if set(files) != {item.path for item in manifest.files}:
        raise ValueError("archive files differ from build manifest")
    for item in manifest.files:
        raw = files[item.path]
        if len(raw) != item.sizeBytes or byte_digest(raw) != item.digest:
            raise ValueError("archive file digest or size mismatch")
    for field, path in DESCRIPTORS.items():
        if field != "dependencyLockDigest":
            if not isinstance(_json(files[path]), dict):
                raise ValueError("build descriptions must be JSON objects")
    return VerifiedBuildArchive(archive, manifest)


def build_archive(
    payload: Mapping[str, bytes],
    *,
    entrypoint: str,
    agent_definition: dict,
    tools_policy: dict,
    behavior_config: dict,
    dependency_lock: bytes,
) -> VerifiedBuildArchive:
    """Add canonical evidence to the existing CodeBuilder payload, deterministically."""
    files = dict(payload)
    if MANIFEST_PATH in files or set(files).intersection(DESCRIPTORS.values()):
        raise ValueError("caller payload cannot override build evidence")
    for field, value in (
        ("agentDefinitionDigest", agent_definition),
        ("toolsPolicyDigest", tools_policy),
        ("behaviorConfigDigest", behavior_config),
    ):
        if not isinstance(value, dict):
            raise ValueError("build description must be an object")
        files[DESCRIPTORS[field]] = canonical_bytes(value)
    files[DESCRIPTORS["dependencyLockDigest"]] = dependency_lock
    if any(not isinstance(value, bytes) for value in files.values()):
        raise ValueError("build payload must be raw file bytes")
    manifest = TeamsBuildManifest(
        entrypoint=entrypoint,
        **{field: byte_digest(files[path]) for field, path in DESCRIPTORS.items()},
        files=[
            BuildFile(path=path, digest=byte_digest(raw), sizeBytes=len(raw))
            for path, raw in sorted(files.items())
        ],
    )
    files[MANIFEST_PATH] = canonical_bytes(manifest.model_dump(mode="json"))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for path, raw in sorted(files.items()):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o444) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            package.writestr(info, raw)
    return verify_build_archive(output.getvalue())


def stamp_code_archive(source, **build_inputs) -> VerifiedBuildArchive:
    """Reuse a CodeBuilder ZIP payload, preserving every existing payload byte."""
    return build_archive(_payload(_archive_bytes(source)), **build_inputs)


def verify_loaded_build(
    source,
    workspace: Path,
    *,
    effective_agent_definition: dict,
    effective_tools_policy: dict,
    effective_behavior_config: dict,
    external_mutable_inputs: bool,
) -> LoadedBuildEvidence:
    """Verify actual configured code bytes without following links or extracting.

    The Host must call this on every probe, with its own trusted paths and actual
    effective configuration. Browser-supplied paths/configuration are forbidden.
    A writable development checkout or mutable remote plugin is ineligible.
    """
    if external_mutable_inputs is not False:
        raise ValueError("mutable external build inputs are not eligible")
    verified = verify_build_archive(source)
    for field, value in (
        ("agentDefinitionDigest", effective_agent_definition),
        ("toolsPolicyDigest", effective_tools_policy),
        ("behaviorConfigDigest", effective_behavior_config),
    ):
        if not isinstance(value, dict):
            raise ValueError("effective runtime configuration must be an object")
        if digest(value) != getattr(verified.manifest, field):
            raise ValueError("effective runtime configuration differs from build")
    workspace = Path(workspace)
    if not workspace.is_absolute() or workspace.is_symlink():
        raise ValueError("loaded workspace must be a trusted absolute directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root = os.open(workspace, flags)
    expected = {item.path: (item.digest, item.sizeBytes) for item in verified.manifest.files}
    expected[MANIFEST_PATH] = (
        verified.manifest_digest,
        len(canonical_bytes(verified.manifest.model_dump(mode="json"))),
    )
    found = set()

    def walk(directory, prefix=""):
        if os.fstat(directory).st_mode & 0o222:
            raise ValueError("loaded workspace directories must be read-only")
        for name in os.listdir(directory):
            path = prefix + name
            metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                if not any(item.startswith(path + "/") for item in expected):
                    raise ValueError("loaded workspace contains an extra directory")
                child = os.open(name, flags, dir_fd=directory)
                try:
                    walk(child, path + "/")
                    current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                        raise ValueError("loaded workspace directory changed")
                finally:
                    os.close(child)
                continue
            if (
                path not in expected
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_mode & 0o222
            ):
                raise ValueError("loaded workspace contains extra, linked or writable files")
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                before = os.fstat(fd)
                if (before.st_dev, before.st_ino) != (metadata.st_dev, metadata.st_ino):
                    raise ValueError("loaded workspace changed while reading")
                hasher, size = hashlib.sha256(), 0
                while chunk := os.read(fd, CHUNK):
                    size += len(chunk)
                    if size > expected[path][1]:
                        raise ValueError("loaded file exceeds fixed size")
                    hasher.update(chunk)
                after = os.fstat(fd)
                current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                    raise ValueError("loaded workspace file changed")
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_nlink) != (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                    after.st_nlink,
                ):
                    raise ValueError("loaded workspace changed while reading")
                if ("sha256:" + hasher.hexdigest(), size) != expected[path]:
                    raise ValueError("loaded file differs from archive")
                found.add(path)
            finally:
                os.close(fd)

    try:
        walk(root)
        if found != set(expected):
            raise ValueError("loaded workspace is incomplete")
        if os.stat(workspace, follow_symlinks=False).st_ino != os.fstat(root).st_ino:
            raise ValueError("loaded workspace root changed")
    finally:
        os.close(root)
    return verified.loaded_evidence()


@dataclass(frozen=True)
class EffectiveBuildConfiguration:
    """Current values from the actual adapter, never an HTTP/model payload."""

    agent_definition: dict
    tools_policy: dict
    behavior_config: dict
    external_mutable_inputs: bool


class LoadedBuildProvider:
    """Trusted factory wiring for the actual Code runtime's loaded source.

    The deploy/build owner supplies the retained original ZIP and actual loaded
    code directory. The callback reads current adapter configuration on every
    invocation. Writable development checkouts, mutable external plugins and
    missing archives cannot advertise release equivalence. No path is inferred
    from request data or returned by a remote Host.
    """

    def __init__(
        self,
        *,
        archive_path: Path,
        workspace: Path,
        effective_configuration: Callable[[], EffectiveBuildConfiguration],
    ):
        self.archive_path, self.workspace = Path(archive_path), Path(workspace)
        if not self.archive_path.is_absolute() or not self.workspace.is_absolute():
            raise ValueError("loaded build paths must be trusted absolute paths")
        if not callable(effective_configuration):
            raise ValueError("actual effective configuration reader required")
        self.configuration = effective_configuration

    def __call__(self) -> LoadedBuildEvidence:
        configuration = self.configuration()
        if not isinstance(configuration, EffectiveBuildConfiguration):
            raise ValueError("actual effective build configuration required")
        # Snapshot before opening files; an in-place configuration mutation must
        # not be hidden by returning the same dictionary a second time.
        fixed = canonical_bytes(vars(configuration))
        metadata = os.stat(self.archive_path, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o222
            or not 0 < metadata.st_size <= MAX_FILE_BYTES
        ):
            raise ValueError("original Code archive must be regular, unlinked and read-only")
        fd = os.open(self.archive_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise ValueError("original Code archive changed while opening")
            proof = verify_loaded_build(
                stream,
                self.workspace,
                effective_agent_definition=configuration.agent_definition,
                effective_tools_policy=configuration.tools_policy,
                effective_behavior_config=configuration.behavior_config,
                external_mutable_inputs=configuration.external_mutable_inputs,
            )
            after = os.fstat(stream.fileno())
            current = os.stat(self.archive_path, follow_symlinks=False)

            def identity(item):
                return (
                    item.st_dev,
                    item.st_ino,
                    item.st_size,
                    item.st_mtime_ns,
                    item.st_ctime_ns,
                    item.st_nlink,
                    item.st_mode,
                )

            if identity(before) != identity(after) or identity(after) != identity(current):
                raise ValueError("original Code archive changed during verification")
        current_configuration = self.configuration()
        if not isinstance(current_configuration, EffectiveBuildConfiguration):
            raise ValueError("actual effective build configuration required")
        if canonical_bytes(vars(current_configuration)) != fixed:
            raise ValueError("effective build configuration changed during verification")
        return proof

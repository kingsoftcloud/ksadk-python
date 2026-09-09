"""Bounded, verified artifact delivery before a Skill sandbox is destroyed."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

MAX_FILES = 100
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_BUNDLE_BYTES = MAX_TOTAL_BYTES + 1024 * 1024


class ArtifactDeliveryError(ValueError):
    pass


class ArtifactBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    size: int = Field(ge=0, le=MAX_BUNDLE_BYTES, strict=True)
    file_count: int = Field(ge=0, le=MAX_FILES, strict=True)


def _name(raw: str) -> str:
    path = PurePosixPath(raw)
    if (
        not raw
        or len(raw) > 512
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in raw
        or ":" in raw
        or "\x00" in raw
        or str(path) != raw
        or raw == "."
    ):
        raise ArtifactDeliveryError("Invalid artifact relative path")
    return raw


def export_artifacts(paths: list[str], root: Path, destination: Path) -> ArtifactBundle:
    """Only export regular files under the execution workspace, never symlinks."""
    if len(paths) > MAX_FILES:
        raise ArtifactDeliveryError("Too many artifacts")
    root = root.resolve()
    total = 0
    names = set()
    opened = zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_STORED)
    try:
        with opened as archive:
            for raw in paths:
                path = Path(raw)
                if not path.is_absolute():
                    path = root / path
                try:
                    relative = path.relative_to(root)
                    name = _name(relative.as_posix())
                except ValueError:
                    raise ArtifactDeliveryError(
                        "Artifact is outside the execution workspace"
                    ) from None
                if name in names:
                    raise ArtifactDeliveryError("Duplicate artifact path")
                names.add(name)
                current = root
                for part in relative.parts:
                    current = current / part
                    if current.is_symlink():
                        raise ArtifactDeliveryError("Artifact links are not allowed")
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, "rb") as source:
                    info = os.fstat(source.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                        raise ArtifactDeliveryError("Artifact is not a bounded regular file")
                    content = source.read(MAX_FILE_BYTES + 1)
                total += len(content)
                if len(content) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                    raise ArtifactDeliveryError("Artifact size limit exceeded")
                archive.writestr(name, content)
        os.chmod(destination, 0o600)
        content = destination.read_bytes()
        return ArtifactBundle(
            sha256=hashlib.sha256(content).hexdigest(), size=len(content), file_count=len(names)
        )
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def import_artifacts(
    content: bytes, receipt: ArtifactBundle, *, parent: Path | None = None
) -> list[str]:
    """Verify the full bundle before publishing any consumer-visible paths."""
    if len(content) != receipt.size or hashlib.sha256(content).hexdigest() != receipt.sha256:
        raise ArtifactDeliveryError("Artifact bundle integrity mismatch")
    directory = Path(tempfile.mkdtemp(prefix="ksadk-skill-artifacts-", dir=parent))
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) != receipt.file_count:
                raise ArtifactDeliveryError("Artifact file count mismatch")
            total = 0
            names = set()
            outputs = []
            for entry in entries:
                name = _name(entry.filename)
                mode = entry.external_attr >> 16
                if name in names or entry.is_dir() or stat.S_IFMT(mode) not in {0, stat.S_IFREG}:
                    raise ArtifactDeliveryError("Artifact bundle contains invalid file entries")
                names.add(name)
                total += entry.file_size
                if entry.file_size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                    raise ArtifactDeliveryError("Artifact size limit exceeded")
                with archive.open(entry) as source:
                    data = source.read(MAX_FILE_BYTES + 1)
                if len(data) != entry.file_size or len(data) > MAX_FILE_BYTES:
                    raise ArtifactDeliveryError("Artifact size mismatch")
                target = directory / name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with target.open("xb") as output:
                    os.chmod(target, 0o600)
                    output.write(data)
                outputs.append(str(target))
        if not outputs:
            directory.rmdir()
        return outputs
    except Exception:
        shutil.rmtree(directory)
        raise

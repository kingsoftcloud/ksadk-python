"""Portable plugin bytes; credentials and machine state never belong here.

The caller supplies an already verified, selected plugin tree. Delivery and
credential authorization remain separate from this bounded archive format.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_FILES = 10000
MAX_BYTES = 256 * 1024 * 1024
SCHEMA = "ksadk.plugin-artifact/v1"


class PluginArtifactReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["ksadk.plugin-artifact/v1"] = SCHEMA
    ecosystem: Literal["codex"] = "codex"
    runtime_version: str = Field(min_length=1)
    plugin_lock_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    artifact_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0, le=MAX_BYTES)


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _safe_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or str(path) != value
        or any(p in {".", ".."} for p in path.parts)
    ):
        raise ValueError("Invalid plugin artifact path")
    forbidden = {".credentials.json", "auth.json", "config.toml", ".git", "sessions"}
    if any(part.lower() in forbidden or part.lower().startswith(".env") for part in path.parts):
        raise ValueError("Plugin artifact contains credential or host state paths")
    return value


def _zip_entry(name: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.compress_type = zipfile.ZIP_STORED
    return info


def export_plugin_artifact(
    root: Path,
    output_dir: Path,
    *,
    runtime_version: str,
    plugin_lock_digest: str,
) -> tuple[PluginArtifactReceipt, Path]:
    """Package only the pinned Codex marketplace, independent of local paths."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Plugin artifact root must be a real directory")
    if output_dir.resolve().is_relative_to(root.resolve()):
        raise ValueError("Plugin artifact output must be outside its source tree")
    files = []
    total = 0
    output_dir.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".plugin-artifact-", dir=output_dir)
    os.close(fd)
    temporary_path = Path(temporary)
    try:
        with zipfile.ZipFile(temporary_path, "w") as archive:
            for source in sorted(root.rglob("*")):
                if source.is_symlink():
                    raise ValueError("Plugin artifacts cannot contain symlinks")
                if source.is_dir():
                    _safe_path(source.relative_to(root).as_posix())
                    continue
                if not source.is_file():
                    raise ValueError("Plugin artifacts require regular files")
                name = _safe_path(source.relative_to(root).as_posix())
                size = source.stat().st_size
                total += size
                if total > MAX_BYTES or len(files) >= MAX_FILES:
                    raise ValueError("Plugin artifact exceeds size limits")
                with source.open("rb") as stream:
                    raw = stream.read(size + 1)
                if len(raw) != size:
                    raise ValueError("Plugin artifact source changed during export")
                mode = 0o644 | (source.stat().st_mode & 0o111)
                files.append({"path": name, "digest": _digest(raw), "size": len(raw), "mode": mode})
                archive.writestr(_zip_entry("payload/" + name, mode), raw)
            if not files:
                raise ValueError("Plugin artifact is empty")
            manifest = {
                "schema_version": SCHEMA,
                "ecosystem": "codex",
                "runtime_version": runtime_version,
                "plugin_lock_digest": plugin_lock_digest,
                "files": files,
            }
            archive.writestr(
                _zip_entry("manifest.json", 0o644),
                json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
            )
        if temporary_path.stat().st_size > MAX_BYTES:
            raise ValueError("Plugin artifact exceeds size limits")
        raw = temporary_path.read_bytes()
        receipt = PluginArtifactReceipt(
            runtime_version=runtime_version,
            plugin_lock_digest=plugin_lock_digest,
            artifact_digest=_digest(raw),
            size_bytes=len(raw),
        )
        target = output_dir / (receipt.artifact_digest.removeprefix("sha256:") + ".zip")
        os.replace(temporary_path, target)
        return receipt, target
    finally:
        temporary_path.unlink(missing_ok=True)


def restore_plugin_artifact(
    archive_path: Path, receipt: PluginArtifactReceipt, target: Path
) -> Path:
    """Verify a downloaded archive before atomic installation into a fresh path."""
    if archive_path.stat().st_size != receipt.size_bytes:
        raise ValueError("Plugin artifact size mismatch")
    if _digest(archive_path.read_bytes()) != receipt.artifact_digest:
        raise ValueError("Plugin artifact digest mismatch")
    if target.exists() or target.is_symlink():
        raise ValueError("Plugin artifact target already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".plugin-restore-", dir=target.parent))
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if len(names) != len(set(names)) or len(names) > MAX_FILES + 1:
                raise ValueError("Duplicate or excessive plugin archive entries")
            if sum(entry.file_size for entry in entries) > MAX_BYTES:
                raise ValueError("Plugin archive expands beyond size limit")
            manifest = json.loads(archive.read("manifest.json"))
            for key in ("schema_version", "ecosystem", "runtime_version", "plugin_lock_digest"):
                if manifest.get(key) != getattr(receipt, key):
                    raise ValueError("Plugin artifact manifest mismatch")
            files = manifest.get("files")
            if not isinstance(files, list) or not files:
                raise ValueError("Missing plugin artifact file index")
            expected = ["payload/" + _safe_path(item["path"]) for item in files]
            if len(expected) != len(set(expected)) or set(names) != {"manifest.json", *expected}:
                raise ValueError("Plugin artifact file index mismatch")
            for item, name in zip(files, expected, strict=True):
                info = archive.getinfo(name)
                mode = info.external_attr >> 16
                if not stat.S_ISREG(mode) or (item["mode"] & ~0o111) != 0o644:
                    raise ValueError("Plugin artifact has invalid file type or mode")
                raw = archive.read(name)
                if len(raw) != item["size"] or _digest(raw) != item["digest"]:
                    raise ValueError("Plugin artifact file digest mismatch")
                destination = staging / item["path"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(raw)
                destination.chmod(item["mode"])
        os.replace(staging, target)
        return target
    finally:
        if staging.exists():
            shutil.rmtree(staging)

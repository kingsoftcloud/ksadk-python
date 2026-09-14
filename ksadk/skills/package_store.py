from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import threading
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

try:
    import fcntl
except ImportError:  # Windows legacy callers retain in-process synchronization.
    fcntl = None

from ksadk.skills.models import SkillRef


class SkillPackageError(RuntimeError):
    pass


@dataclass(frozen=True)
class SkillPackage:
    ref: SkillRef
    archive_path: Path
    extract_dir: Path
    root_dir: Path
    cache_hit: bool = False


class PackageStore:
    def __init__(
        self,
        cache_dir: str | Path,
        *,
        namespace: str = "",
        require_hash: bool = False,
        max_archive_bytes: int = 20 * 1024 * 1024,
        max_extracted_bytes: int = 100 * 1024 * 1024,
        max_files: int = 2000,
    ):
        if any(
            type(value) is not int or value < 1
            for value in (max_archive_bytes, max_extracted_bytes, max_files)
        ):
            raise ValueError("Skill package limits must be positive integers")
        if require_hash and (not namespace or fcntl is None):
            raise ValueError("Verified package stores require a namespace and process locking")
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.namespace = namespace
        self.require_hash = require_hash
        self.max_archive_bytes = max_archive_bytes
        self.max_extracted_bytes = max_extracted_bytes
        self.max_files = max_files
        self._thread_lock = threading.RLock()

    def store_archive(self, ref: SkillRef, content: bytes) -> SkillPackage:
        self._verify_hash(ref, content)
        with self._locked(ref):
            cached = self._get_cached(ref)
            if cached is not None:
                return cached
            skill_dir = self._skill_dir(ref)
            stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=self.cache_dir))
            try:
                (stage / "archive.zip").write_bytes(content)
                os.chmod(stage / "archive.zip", 0o600)
                extracted = stage / "extracted"
                extracted.mkdir(mode=0o700)
                self._safe_extract(stage / "archive.zip", extracted)
                self._find_skill_root(extracted)
                # Publish only a complete validated tree, under the per-key lock.
                if skill_dir.is_symlink() or skill_dir.is_file():
                    skill_dir.unlink()
                elif skill_dir.exists():
                    shutil.rmtree(skill_dir)
                os.replace(stage, skill_dir)
            except (OSError, zipfile.BadZipFile, RuntimeError, ValueError) as error:
                raise SkillPackageError("Skill archive could not be safely materialized") from error
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
            return self._package(ref, skill_dir, cache_hit=False)

    def get_cached(self, ref: SkillRef) -> SkillPackage | None:
        with self._locked(ref):
            return self._get_cached(ref)

    def _get_cached(self, ref: SkillRef) -> SkillPackage | None:
        skill_dir = self._skill_dir(ref)
        archive_path = skill_dir / "archive.zip"
        extract_dir = skill_dir / "extracted"
        if (
            skill_dir.is_symlink()
            or archive_path.is_symlink()
            or extract_dir.is_symlink()
            or not archive_path.is_file()
            or not extract_dir.is_dir()
        ):
            return None
        try:
            if archive_path.stat().st_size > self.max_archive_bytes:
                return None
            content = archive_path.read_bytes()
            self._verify_hash(ref, content)
            self._verify_tree(archive_path, extract_dir)
            return self._package(ref, skill_dir, cache_hit=True)
        except (RuntimeError, OSError, zipfile.BadZipFile, ValueError):
            return None

    def _package(self, ref: SkillRef, skill_dir: Path, *, cache_hit: bool) -> SkillPackage:
        return SkillPackage(
            ref=ref,
            archive_path=skill_dir / "archive.zip",
            extract_dir=skill_dir / "extracted",
            root_dir=self._find_skill_root(skill_dir / "extracted"),
            cache_hit=cache_hit,
        )

    def _skill_dir(self, ref: SkillRef) -> Path:
        identity = [
            self.namespace,
            ref.skill_id,
            ref.version_id,
            ref.version,
            ref.name,
            ref.content_hash.render() if ref.content_hash else "",
        ]
        key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
        return self.cache_dir / key

    @contextmanager
    def _locked(self, ref: SkillRef):
        with self._thread_lock:
            if fcntl is None:
                yield
                return
            path = self._skill_dir(ref).with_suffix(".lock")
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "rb") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def _verify_hash(self, ref: SkillRef, content: bytes) -> None:
        if len(content) > self.max_archive_bytes:
            raise SkillPackageError("Skill archive exceeds size limit")
        if not ref.content_hash:
            if self.require_hash:
                raise SkillPackageError("Verified Skill packages require ContentHash")
            return
        if ref.content_hash.algorithm != "sha256":
            raise SkillPackageError(
                f"Unsupported ContentHash algorithm: {ref.content_hash.algorithm}"
            )
        actual = hashlib.sha256(content).hexdigest()
        if actual.lower() != ref.content_hash.value.lower():
            raise SkillPackageError(
                f"ContentHash mismatch for {ref.name}: expected "
                f"{ref.content_hash.render()}, got sha256:{actual}"
            )

    def _safe_extract(self, archive_path: Path, extract_dir: Path) -> None:
        expanded = 0
        with zipfile.ZipFile(archive_path) as archive:
            for member, relative in self._members(archive):
                target = extract_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if member.is_dir():
                    target.mkdir(exist_ok=True, mode=0o700)
                    continue
                with archive.open(member) as source, target.open("xb") as destination:
                    written = 0
                    while chunk := source.read(64 * 1024):
                        written += len(chunk)
                        expanded += len(chunk)
                        if written > member.file_size or expanded > self.max_extracted_bytes:
                            raise SkillPackageError("Expanded Skill package exceeds limits")
                        destination.write(chunk)
                os.chmod(target, 0o700 if (member.external_attr >> 16) & 0o111 else 0o600)

    def _members(self, archive: zipfile.ZipFile):
        members = archive.infolist()
        if (
            len(members) > self.max_files
            or sum(item.file_size for item in members) > self.max_extracted_bytes
        ):
            raise SkillPackageError("Expanded Skill package exceeds limits")
        seen = set()
        result = []
        for member in members:
            relative = PurePosixPath(member.filename)
            kind = stat.S_IFMT(member.external_attr >> 16)
            if (
                not relative.parts
                or relative.is_absolute()
                or ".." in relative.parts
                or "\\" in member.filename
                or ":" in member.filename
                or "\x00" in member.orig_filename
                or kind not in (0, stat.S_IFREG, stat.S_IFDIR)
                or relative in seen
                or member.flag_bits & 1
            ):
                raise SkillPackageError("Unsafe Skill archive member")
            seen.add(relative)
            result.append((member, relative))
        return result

    def _verify_tree(self, archive_path: Path, extract_dir: Path) -> None:
        expected = set()
        with zipfile.ZipFile(archive_path) as archive:
            for member, relative in self._members(archive):
                expected.update(
                    parent for parent in relative.parents if parent != PurePosixPath(".")
                )
                expected.add(relative)
                target = extract_dir / relative
                if any((extract_dir / parent).is_symlink() for parent in relative.parents):
                    raise SkillPackageError("Skill cache contains a linked parent")
                if target.is_symlink():
                    raise SkillPackageError("Skill cache contains a link")
                if member.is_dir():
                    if not target.is_dir():
                        raise SkillPackageError("Skill cache directory missing")
                    continue
                if not target.is_file() or target.stat().st_size != member.file_size:
                    raise SkillPackageError("Skill cache file changed")
                with archive.open(member) as source, target.open("rb") as current:
                    while chunk := source.read(64 * 1024):
                        if chunk != current.read(len(chunk)):
                            raise SkillPackageError("Skill cache content changed")
        actual = set()
        for path in extract_dir.rglob("*"):
            if path.is_symlink():
                raise SkillPackageError("Skill cache contains a link")
            actual.add(PurePosixPath(path.relative_to(extract_dir).as_posix()))
        if actual != expected:
            raise SkillPackageError("Skill cache file set changed")

    def _find_skill_root(self, extract_dir: Path) -> Path:
        if (extract_dir / "SKILL.md").exists():
            return extract_dir
        candidates = sorted(path.parent for path in extract_dir.rglob("SKILL.md"))
        if not candidates:
            raise SkillPackageError(f"SKILL.md not found under {extract_dir}")
        return candidates[0]

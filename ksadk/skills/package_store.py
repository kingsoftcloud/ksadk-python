from __future__ import annotations

import hashlib
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from ksadk.skills.events import SkillEvent, SkillEventSink
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
    def __init__(self, cache_dir: str | Path):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def store_archive(
        self,
        ref: SkillRef,
        content: bytes,
        *,
        event_sink: SkillEventSink | None = None,
        skill_invocation_id: str = "",
    ) -> SkillPackage:
        hash_started_at = time.time()
        try:
            self._verify_hash(ref, content)
        except SkillPackageError:
            self._emit_lifecycle(
                event_sink,
                "skill.package.hash_verified",
                status="failed",
                ref=ref,
                skill_invocation_id=skill_invocation_id,
                started_at=hash_started_at,
                error_category="hash_verification_failed",
            )
            raise
        self._emit_lifecycle(
            event_sink,
            "skill.package.hash_verified",
            status="completed",
            ref=ref,
            skill_invocation_id=skill_invocation_id,
            started_at=hash_started_at,
        )
        skill_dir = self._skill_dir(ref)
        archive_path = skill_dir / "archive.zip"
        extract_dir = skill_dir / "extracted"

        if archive_path.exists() and extract_dir.exists():
            cached = self.get_cached(ref)
            if cached is not None:
                return cached

        if skill_dir.exists():
            shutil.rmtree(skill_dir)
        skill_dir.mkdir(parents=True, exist_ok=True)
        archive_path.write_bytes(content)
        extract_dir.mkdir(parents=True, exist_ok=True)
        extract_started_at = time.time()
        try:
            self._safe_extract(archive_path, extract_dir)
        except SkillPackageError:
            self._emit_lifecycle(
                event_sink,
                "skill.package.extracted",
                status="failed",
                ref=ref,
                skill_invocation_id=skill_invocation_id,
                started_at=extract_started_at,
                error_category="extract_failed",
            )
            raise
        self._emit_lifecycle(
            event_sink,
            "skill.package.extracted",
            status="completed",
            ref=ref,
            skill_invocation_id=skill_invocation_id,
            started_at=extract_started_at,
        )

        return SkillPackage(
            ref=ref,
            archive_path=archive_path,
            extract_dir=extract_dir,
            root_dir=self._find_skill_root(extract_dir),
            cache_hit=False,
        )

    def get_cached(self, ref: SkillRef) -> SkillPackage | None:
        skill_dir = self._skill_dir(ref)
        archive_path = skill_dir / "archive.zip"
        extract_dir = skill_dir / "extracted"
        if not archive_path.exists() or not extract_dir.exists():
            return None
        content = archive_path.read_bytes()
        try:
            self._verify_hash(ref, content)
        except SkillPackageError:
            return None
        return SkillPackage(
            ref=ref,
            archive_path=archive_path,
            extract_dir=extract_dir,
            root_dir=self._find_skill_root(extract_dir),
            cache_hit=True,
        )

    def _skill_dir(self, ref: SkillRef) -> Path:
        return self.cache_dir / (ref.cache_key or ref.name or "skill")

    def _verify_hash(self, ref: SkillRef, content: bytes) -> None:
        if not ref.content_hash:
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
        base = extract_dir.resolve()
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                target = (extract_dir / member.filename).resolve()
                if not str(target).startswith(str(base) + "/") and target != base:
                    raise SkillPackageError(f"unsafe zip member: {member.filename}")
            archive.extractall(extract_dir)

    def _find_skill_root(self, extract_dir: Path) -> Path:
        if (extract_dir / "SKILL.md").exists():
            return extract_dir
        candidates = sorted(path.parent for path in extract_dir.rglob("SKILL.md"))
        if not candidates:
            raise SkillPackageError(f"SKILL.md not found under {extract_dir}")
        return candidates[0]

    @staticmethod
    def _emit_lifecycle(
        event_sink: SkillEventSink | None,
        event_type: str,
        *,
        status: str,
        ref: SkillRef,
        skill_invocation_id: str,
        started_at: float,
        error_category: str = "",
    ) -> None:
        if event_sink is None or not skill_invocation_id:
            return
        event_sink.emit(
            SkillEvent.create(
                event_type,
                status=status,
                skill_ref=ref,
                skill_invocation_id=skill_invocation_id,
                started_at=started_at,
                ended_at=time.time(),
                error_category=error_category,
            )
        )

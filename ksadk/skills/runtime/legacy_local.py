"""Host-driven delivery for sandbox runtimes predating pinned protocol v1."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ksadk.skills.package_store import SkillPackage, SkillPackageError
from ksadk.skills.runtime.artifact_delivery import (
    MAX_BUNDLE_BYTES,
    ArtifactBundle,
    import_artifacts,
)
from ksadk.skills.runtime.base import SkillRuntimeError
from ksadk.skills.runtime.pinned import load_pinned_packages, stage_packages

PINNED_PROTOCOL = "pinned_v1"
LEGACY_LOCAL_PROTOCOL = "legacy_local_082"
SUPPORTED_PROTOCOLS = frozenset({PINNED_PROTOCOL, LEGACY_LOCAL_PROTOCOL})
PROTOCOL_ENV = "KSADK_SKILL_SANDBOX_PROTOCOL"

MAX_DELIVERY_FILES = 2000
MAX_DELIVERY_FILE_BYTES = 20 * 1024 * 1024
MAX_DELIVERY_BYTES = 100 * 1024 * 1024

_REMOTE_ARTIFACT_COLLECTOR = r"""
import hashlib, json, os, stat, sys, zipfile
from pathlib import Path

root = Path(sys.argv[1]).resolve()
manifest = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
destination = Path(sys.argv[3])
recover = sys.argv[4] == "1"
max_files, max_file_bytes, max_total_bytes = 100, 20 * 1024 * 1024, 100 * 1024 * 1024

def within_root(path):
    try:
        return os.path.commonpath((str(root), str(path))) == str(root)
    except ValueError:
        return False

def checked(path):
    if ".." in path.parts:
        raise ValueError("artifact path is not canonical")
    lexical = Path(os.path.abspath(path))
    if not within_root(lexical) or lexical == root:
        raise ValueError("artifact path is outside request workspace")
    current = root
    for part in lexical.relative_to(root).parts:
        current = current / part
        info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("artifact links are not allowed")
    resolved = lexical.resolve()
    if not within_root(resolved):
        raise ValueError("artifact path resolves outside request workspace")
    if not stat.S_ISREG(os.lstat(resolved).st_mode):
        raise ValueError("artifact is not a regular file")
    return resolved

raw_paths = manifest.get("output_files")
if not isinstance(raw_paths, list) or any(not isinstance(item, str) for item in raw_paths):
    raise ValueError("invalid artifact manifest")
source = "workflow_result"
if not raw_paths and recover:
    source = "recovery_scan"
    artifact_root = root / "artifacts"
    raw_paths = []
    if artifact_root.is_dir() and not artifact_root.is_symlink():
        for parent, directories, files in os.walk(artifact_root, followlinks=False):
            directories[:] = [
                name for name in directories if not (Path(parent) / name).is_symlink()
            ]
            raw_paths.extend(str(Path(parent) / name) for name in files)

paths = []
names = set()
for raw in raw_paths:
    candidate = Path(raw)
    path = checked(candidate if candidate.is_absolute() else root / candidate)
    name = path.relative_to(root).as_posix()
    if name in names:
        raise ValueError("duplicate artifact path")
    names.add(name)
    paths.append((name, path))
if len(paths) > max_files:
    raise ValueError("too many artifacts")

total = 0
with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_STORED) as archive:
    for name, path in sorted(paths):
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > max_file_bytes:
                raise ValueError("artifact exceeds file limit")
            content = stream.read(max_file_bytes + 1)
        total += len(content)
        if len(content) > max_file_bytes or total > max_total_bytes:
            raise ValueError("artifact exceeds delivery limits")
        archive.writestr(name, content)
content = destination.read_bytes()
print(json.dumps({
    "sha256": hashlib.sha256(content).hexdigest(),
    "size": len(content),
    "file_count": len(paths),
    "source": source,
}, sort_keys=True))
"""


@dataclass(frozen=True)
class LegacyDelivery:
    root: str
    skills_dir: str
    work_dir: str
    request_path: str
    collector_path: str
    artifact_manifest_path: str
    artifact_bundle_path: str


@dataclass(frozen=True)
class LegacyArtifactResult:
    output_files: list[str]
    source: str


def resolve_protocol(raw: str | None = None) -> str:
    protocol = (raw if raw is not None else os.environ.get(PROTOCOL_ENV, "")).strip()
    protocol = protocol or PINNED_PROTOCOL
    if protocol not in SUPPORTED_PROTOCOLS:
        raise SkillRuntimeError(
            f"Unsupported {PROTOCOL_ENV}: expected {PINNED_PROTOCOL} or {LEGACY_LOCAL_PROTOCOL}"
        )
    return protocol


def legacy_environment(delivery: LegacyDelivery) -> dict[str, str]:
    """Force the old loader to consume only the request-local Skill tree."""
    return {
        "KSADK_LOCAL_SKILLS_DIR": delivery.skills_dir,
        "KSADK_SKILL_WORKDIR": delivery.work_dir,
        "KSADK_SKILL_SPACE_IDS": "",
        "SKILL_SPACE_ID": "",
        "KSADK_PUBLIC_SKILL_SPACE_IDS": "",
        "KSADK_SKILL_SERVICE_URL": "",
        "KSADK_SKILL_SERVICE_ENDPOINT": "",
        "KSADK_SKILL_SERVICE_SCHEME": "",
        "KSADK_SKILL_SERVICE_TOKEN": "",
        "KSADK_SKILL_SERVICE_ACCESS_KEY": "",
        "KSADK_SKILL_SERVICE_SECRET_KEY": "",
        "KSADK_SKILL_SERVICE_ACCOUNT_ID": "",
    }


def prepare_legacy_delivery(
    session: Any,
    packages: list[SkillPackage],
    *,
    delivery: LegacyDelivery,
    timeout: int,
    env: dict[str, str],
) -> None:
    """Revalidate pinned archives and upload their immutable extracted contents."""
    _run_checked(
        session,
        "mkdir -m 700 "
        + " ".join(
            shlex.quote(path)
            for path in (
                delivery.root,
                delivery.skills_dir,
                delivery.work_dir,
                f"{delivery.work_dir}/artifacts",
            )
        ),
        timeout=timeout,
        env=env,
        error="Could not prepare legacy Skill delivery directory",
    )
    total_files = 0
    total_bytes = 0
    executable_paths: list[str] = []
    with tempfile.TemporaryDirectory(prefix="ksadk-legacy-skill-transfer-") as directory:
        stage = Path(directory)
        entries = stage_packages(packages, stage)
        skills = load_pinned_packages(entries, stage)
        for index, (entry, skill) in enumerate(zip(entries, skills, strict=True)):
            component = f"{index:02d}-{hashlib.sha256(entry.skill_id.encode()).hexdigest()[:16]}"
            remote_root = f"{delivery.skills_dir}/{component}"
            files = _regular_tree_files(skill.root_dir)
            total_files += len(files)
            total_bytes += sum(path.stat().st_size for path, _ in files)
            if total_files > MAX_DELIVERY_FILES or total_bytes > MAX_DELIVERY_BYTES:
                raise SkillPackageError("Legacy Skill delivery exceeds aggregate limits")
            directories = {remote_root}
            for _, relative in files:
                parent = PurePosixPath(remote_root, relative).parent
                while str(parent).startswith(remote_root):
                    directories.add(str(parent))
                    if str(parent) == remote_root:
                        break
                    parent = parent.parent
            _mkdir_chunks(session, sorted(directories), timeout=timeout, env=env)
            for source, relative in files:
                target = str(PurePosixPath(remote_root, relative))
                content = _read_regular_file(source)
                session.write_file(target, content)
                if session.read_file_bytes(target, max_bytes=len(content) + 1) != content:
                    raise SkillRuntimeError("Legacy Skill upload verification failed")
                if source.stat().st_mode & 0o111:
                    executable_paths.append(target)
    for start in range(0, len(executable_paths), 100):
        paths = executable_paths[start : start + 100]
        _run_checked(
            session,
            "chmod 700 " + " ".join(shlex.quote(path) for path in paths),
            timeout=timeout,
            env=env,
            error="Could not preserve legacy Skill executable permissions",
        )
    collector = _REMOTE_ARTIFACT_COLLECTOR.encode("utf-8")
    session.write_file(delivery.collector_path, collector)
    if session.read_file_bytes(
        delivery.collector_path, max_bytes=len(collector) + 1
    ) != collector:
        raise SkillRuntimeError("Legacy artifact collector upload verification failed")


def collect_legacy_artifacts(
    session: Any,
    *,
    stdout: str,
    delivery: LegacyDelivery,
    timeout: int,
    env: dict[str, str],
    parent: Path | None,
    recover: bool,
) -> LegacyArtifactResult:
    output_files = _workflow_output_files(stdout, allow_missing=recover)
    session.write_file(
        delivery.artifact_manifest_path,
        json.dumps({"output_files": output_files}, ensure_ascii=False).encode("utf-8"),
    )
    command = " ".join(
        (
            "python -I",
            shlex.quote(delivery.collector_path),
            shlex.quote(delivery.work_dir),
            shlex.quote(delivery.artifact_manifest_path),
            shlex.quote(delivery.artifact_bundle_path),
            "1" if recover else "0",
        )
    )
    result = session.run_command(command, timeout=timeout, env=env)
    if result.exit_code != 0:
        raise SkillRuntimeError("Legacy artifact collector failed")
    try:
        report = json.loads(result.stdout)
        source = report.pop("source")
        if source not in {"workflow_result", "recovery_scan"}:
            raise ValueError
        receipt = ArtifactBundle.model_validate(report)
    except (KeyError, TypeError, ValueError):
        raise SkillRuntimeError("Legacy artifact collector returned an invalid receipt") from None
    content = session.read_file_bytes(
        delivery.artifact_bundle_path,
        max_bytes=min(receipt.size + 1, MAX_BUNDLE_BYTES + 1),
    )
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return LegacyArtifactResult(
        output_files=import_artifacts(content, receipt, parent=parent),
        source=source,
    )


def rewrite_workflow_artifacts(stdout: str, output_files: list[str]) -> str:
    lines = stdout.splitlines()
    indexes = [index for index, line in enumerate(lines) if line.startswith("workflow_result=")]
    if len(indexes) != 1:
        return stdout
    try:
        payload = json.loads(lines[indexes[0]].split("=", 1)[1])
    except json.JSONDecodeError:
        return stdout
    if not isinstance(payload, dict):
        return stdout
    payload["output_files"] = output_files
    payload["artifacts"] = output_files
    lines[indexes[0]] = "workflow_result=" + json.dumps(
        payload, ensure_ascii=False, sort_keys=True
    )
    return "\n".join(lines) + ("\n" if stdout.endswith("\n") else "")


def _workflow_output_files(stdout: str, *, allow_missing: bool) -> list[str]:
    lines = [
        line.split("=", 1)[1]
        for line in stdout.splitlines()
        if line.startswith("workflow_result=")
    ]
    if not lines and allow_missing:
        return []
    if len(lines) != 1:
        raise SkillRuntimeError("Sandbox returned an invalid number of workflow results")
    try:
        payload = json.loads(lines[0])
    except json.JSONDecodeError:
        raise SkillRuntimeError("Sandbox returned an invalid workflow result") from None
    if not isinstance(payload, dict):
        raise SkillRuntimeError("Sandbox returned an invalid workflow result")
    raw = payload.get("output_files", payload.get("artifacts", []))
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise SkillRuntimeError("Sandbox returned an invalid artifact list")
    return raw


def _regular_tree_files(root: Path) -> list[tuple[Path, PurePosixPath]]:
    root = root.resolve()
    files: list[tuple[Path, PurePosixPath]] = []
    for path in sorted(root.rglob("*")):
        relative = PurePosixPath(path.relative_to(root).as_posix())
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not (
            stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
        ):
            raise SkillPackageError("Legacy Skill tree contains a link or special file")
        if stat.S_ISREG(info.st_mode):
            if info.st_size > MAX_DELIVERY_FILE_BYTES:
                raise SkillPackageError("Legacy Skill file exceeds delivery limit")
            files.append((path, relative))
    if not any(relative == PurePosixPath("SKILL.md") for _, relative in files):
        raise SkillPackageError("Legacy Skill tree is missing SKILL.md")
    return files


def _read_regular_file(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_DELIVERY_FILE_BYTES:
            raise SkillPackageError("Legacy Skill file is not a bounded regular file")
        content = stream.read(MAX_DELIVERY_FILE_BYTES + 1)
    if len(content) > MAX_DELIVERY_FILE_BYTES:
        raise SkillPackageError("Legacy Skill file exceeds delivery limit")
    return content


def _mkdir_chunks(session: Any, directories: list[str], *, timeout: int, env: dict[str, str]):
    for start in range(0, len(directories), 100):
        paths = directories[start : start + 100]
        _run_checked(
            session,
            "mkdir -p " + " ".join(shlex.quote(path) for path in paths),
            timeout=timeout,
            env=env,
            error="Could not prepare legacy Skill subdirectories",
        )


def _run_checked(
    session: Any,
    command: str,
    *,
    timeout: int,
    env: dict[str, str],
    error: str,
) -> None:
    result = session.run_command(command, timeout=timeout, env=env)
    if result.exit_code != 0:
        raise SkillRuntimeError(error)

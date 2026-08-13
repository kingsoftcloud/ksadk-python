"""Inspect/confirm workflow for workspace and allowlisted local Skill candidates."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import yaml  # type: ignore[import-untyped]

from ksadk.studio.capabilities import canonical_json, require_exact_version, sha256_digest
from ksadk.studio.errors import StudioError
from ksadk.studio.workspace import Workspace

MAX_SKILL_BYTES = 100 * 1024 * 1024
MAX_SKILL_FILES = 1000
MAX_PREVIEW_BYTES = 512 * 1024
SCRIPT_SUFFIXES = frozenset({".py", ".sh", ".js", ".ts", ".tsx", ".ps1", ".rb"})
TEXT_SUFFIXES = frozenset(
    {".md", ".txt", ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".css", ".html", ".xml"}
)
EXCLUDED_PARTS = frozenset(
    {".git", ".venv", "venv", "node_modules", "dist", "build", "__pycache__"}
)
WORKSPACE_SCAN_PATHS = ("skills", ".agents/skills", ".codex/skills", ".claude/skills")
USER_SKILL_ROOTS = {
    "user:agents": ".agents/skills",
    "user:codex": ".codex/skills",
    "user:claude": ".claude/skills",
}
DEFAULT_SCAN_PATHS = (*WORKSPACE_SCAN_PATHS, *USER_SKILL_ROOTS)


def skill_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not slug:
        raise StudioError(
            "RESOURCE_NAME_INVALID",
            "Skill 名称必须至少包含一个字母或数字",
            status_code=422,
        )
    return slug[:80]


class SkillDiscoveryService:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def discover(self, *, scan_paths: list[str] | None = None) -> dict[str, Any]:
        requested = scan_paths or list(DEFAULT_SCAN_PATHS)
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        seen_real: set[Path] = set()
        for raw in requested:
            root, source, display_root = self._resolve_scan_root(str(raw))
            if not root.exists():
                continue
            if not root.is_dir() or root.is_symlink():
                raise StudioError(
                    "SKILL_DISCOVERY_PATH_INVALID",
                    "Skill 扫描路径必须是工作区内的普通目录",
                    status_code=422,
                    details={"path": str(raw)},
                )
            manifests = self._scan_manifests(root)
            for manifest in sorted(set(manifests), key=lambda item: item.as_posix()):
                directory = manifest.parent
                relative = self._candidate_relative(directory, source=source, root=root)
                identity = f"{source}:{relative}"
                if identity in seen or (
                    source == "workspace" and relative.startswith("capabilities/skills/")
                ):
                    continue
                seen.add(identity)
                real = directory.resolve()
                if real in seen_real:
                    continue
                seen_real.add(real)
                candidates.append(
                    self.inspect_candidate(
                        directory,
                        source=source,
                        root=root,
                        display_root=display_root,
                    )
                )
        candidates.sort(key=lambda item: (item.get("name", ""), item["path"]))
        token = f"skd_{uuid4().hex}"
        record = {
            "format": "agentkit.skill-discovery/v1",
            "inspectionToken": token,
            "scanPaths": [str(item) for item in requested],
            "candidates": candidates,
            "requiresConfirmation": True,
        }
        self.workspace.atomic_write_text(
            self._record_path(token),
            json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        return record

    def commit(
        self,
        inspection_token: str,
        candidate_id: str,
        *,
        overwrite: bool = False,
    ) -> tuple[str, str]:
        record_path = self._record_path(inspection_token, validate=True)
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StudioError(
                "SKILL_DISCOVERY_NOT_FOUND",
                "Skill 发现记录不存在或已失效",
                status_code=404,
            ) from exc
        candidate = next(
            (
                item
                for item in record.get("candidates") or []
                if item.get("candidateId") == candidate_id
            ),
            None,
        )
        if not isinstance(candidate, dict):
            raise StudioError(
                "SKILL_CANDIDATE_NOT_FOUND",
                "Skill 候选不属于该发现记录",
                status_code=404,
            )
        if candidate.get("status") not in {"ready", "conflict"}:
            raise StudioError(
                "SKILL_DISCOVERY_INVALID",
                "无效 Skill 候选不能导入",
                status_code=422,
                details={"candidateId": candidate_id},
            )
        source, source_kind, source_root, display_root = self._resolve_candidate_directory(
            candidate
        )
        current = self.inspect_candidate(
            source,
            source=source_kind,
            root=source_root,
            display_root=display_root,
        )
        if current.get("digest") != candidate.get("digest"):
            raise StudioError(
                "SKILL_CANDIDATE_CHANGED",
                "Skill 候选在确认前发生变化，请重新扫描",
                status_code=409,
            )
        slug = skill_slug(str(candidate["name"]))
        destination = self.workspace.resolve(Path("capabilities/skills") / slug)
        if destination.exists() and not overwrite:
            raise StudioError(
                "SKILL_IMPORT_CONFLICT",
                "同名 Skill 已安装，必须确认覆盖或取消",
                status_code=409,
                details={
                    "candidateId": candidate_id,
                    "destination": self.workspace.relative(destination),
                },
            )
        staging = self.workspace.resolve(
            Path(".agentkit/cache") / f".skill-{slug}-{uuid4().hex}.tmp"
        )
        shutil.copytree(
            source,
            staging,
            symlinks=False,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )
        self.workspace.atomic_write_yaml(
            staging / "skill.yaml",
            {
                "name": slug,
                "displayName": candidate["displayName"],
                "description": candidate["description"],
                "version": candidate["version"],
                "instructionsFile": "SKILL.md",
                "sourceDigest": candidate["digest"],
            },
        )
        try:
            if destination.exists():
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                trash = self.workspace.resolve(
                    Path(".agentkit/trash/skills") / f"{slug}-{timestamp}"
                )
                trash.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(destination), str(trash))
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staging), str(destination))
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        remaining = [
            item
            for item in record.get("candidates") or []
            if item.get("candidateId") != candidate_id
        ]
        if any(item.get("status") in {"ready", "conflict"} for item in remaining):
            record["candidates"] = remaining
            self.workspace.atomic_write_text(
                record_path,
                json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            )
        else:
            record_path.unlink(missing_ok=True)
        return slug, str(candidate["version"])

    def preview_candidate(
        self,
        inspection_token: str,
        candidate_id: str,
        *,
        path: str | None = None,
    ) -> dict[str, Any]:
        candidate = self._candidate_from_record(inspection_token, candidate_id)
        directory, _, _, _ = self._resolve_candidate_directory(candidate)
        return self.preview_directory(directory, path=path)

    def preview_directory(
        self,
        directory: Path,
        *,
        path: str | None = None,
    ) -> dict[str, Any]:
        resolved = directory.resolve(strict=True)
        files, total_bytes = self._safe_directory(resolved, display_path=directory.name)
        entries = [
            {
                "path": relative,
                "size": (resolved / relative).stat().st_size,
                "kind": self._preview_kind(relative),
            }
            for relative in files
        ]
        if path is None:
            return {"files": entries, "fileCount": len(entries), "totalBytes": total_bytes}

        requested = Path(path)
        if requested.is_absolute() or ".." in requested.parts:
            raise StudioError(
                "SKILL_PREVIEW_PATH_INVALID",
                "Skill 预览路径必须是 Skill 内的相对路径",
                status_code=422,
                details={"path": path},
            )
        target = (resolved / requested).resolve(strict=True)
        try:
            target.relative_to(resolved)
        except ValueError as exc:
            raise StudioError(
                "SKILL_PREVIEW_PATH_FORBIDDEN",
                "Skill 预览文件不在候选目录中",
                status_code=403,
                details={"path": path},
            ) from exc
        if not target.is_file() or target.is_symlink():
            raise StudioError(
                "SKILL_PREVIEW_FILE_INVALID",
                "Skill 预览仅支持普通文件",
                status_code=422,
                details={"path": path},
            )
        relative = target.relative_to(resolved).as_posix()
        if relative not in files:
            raise StudioError(
                "SKILL_PREVIEW_FILE_INVALID",
                "Skill 预览文件未通过安全检查",
                status_code=422,
                details={"path": path},
            )
        content = target.read_bytes()
        kind = self._preview_kind(relative)
        if kind == "binary":
            return {
                "path": relative,
                "size": len(content),
                "kind": kind,
                "content": None,
                "truncated": False,
            }
        truncated = len(content) > MAX_PREVIEW_BYTES
        text = content[:MAX_PREVIEW_BYTES].decode("utf-8", errors="replace")
        return {
            "path": relative,
            "size": len(content),
            "kind": kind,
            "content": text,
            "truncated": truncated,
        }

    def _candidate_from_record(
        self,
        inspection_token: str,
        candidate_id: str,
    ) -> dict[str, Any]:
        record_path = self._record_path(inspection_token, validate=True)
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StudioError(
                "SKILL_DISCOVERY_NOT_FOUND",
                "Skill 发现记录不存在或已失效",
                status_code=404,
            ) from exc
        candidate = next(
            (
                item
                for item in record.get("candidates") or []
                if item.get("candidateId") == candidate_id
            ),
            None,
        )
        if not isinstance(candidate, dict):
            raise StudioError(
                "SKILL_CANDIDATE_NOT_FOUND",
                "Skill 候选不属于该发现记录",
                status_code=404,
            )
        return candidate

    def inspect_candidate(
        self,
        directory: Path,
        *,
        source: str = "workspace",
        root: Path | None = None,
        display_root: str | None = None,
    ) -> dict[str, Any]:
        resolved_root = root or self.workspace.root
        relative = self._candidate_relative(directory, source=source, root=resolved_root)
        display_path = self._display_candidate_path(
            relative,
            source=source,
            display_root=display_root,
        )
        candidate_id = (
            "skill_" + hashlib.sha256(f"{source}:{relative}".encode("utf-8")).hexdigest()[:16]
        )
        try:
            files, total_bytes = self._safe_directory(directory, display_path=display_path)
            metadata = self._frontmatter((directory / "SKILL.md").read_text(encoding="utf-8"))
            name = str(metadata["name"])
            slug = skill_slug(name)
            version = str(metadata.get("version") or "1.0.0")
            require_exact_version(version, field="SKILL.md.version")
            digest = self._directory_digest(directory, files)
            destination = self.workspace.resolve(Path("capabilities/skills") / slug)
            scripts = [
                path
                for path in files
                if Path(path).suffix.lower() in {".py", ".sh", ".js", ".ts", ".ps1"}
            ]
            text = (directory / "SKILL.md").read_text(encoding="utf-8").lower()
            network_markers = [
                marker
                for marker in ("http://", "https://", "curl ", "wget ", "network")
                if marker in text
            ]
            return {
                "candidateId": candidate_id,
                "path": display_path,
                "source": source,
                "relativePath": relative,
                "name": slug,
                "displayName": name,
                "description": str(metadata["description"]),
                "version": version,
                "digest": digest,
                "status": "conflict" if destination.exists() else "ready",
                "fileCount": len(files),
                "totalBytes": total_bytes,
                "risk": {
                    "containsScripts": bool(scripts),
                    "scripts": scripts,
                    "networkMarkers": network_markers,
                    "requiresReview": bool(scripts or network_markers),
                },
                "diagnostics": [],
            }
        except (StudioError, OSError, UnicodeDecodeError) as exc:
            return {
                "candidateId": candidate_id,
                "path": display_path,
                "source": source,
                "relativePath": relative,
                "name": directory.name,
                "displayName": directory.name,
                "description": "",
                "version": "",
                "digest": "",
                "status": "invalid",
                "risk": {"requiresReview": True},
                "diagnostics": [
                    {
                        "code": getattr(exc, "code", "SKILL_DISCOVERY_INVALID"),
                        "message": getattr(exc, "message", str(exc)),
                    }
                ],
            }

    def _safe_directory(self, directory: Path, *, display_path: str) -> tuple[list[str], int]:
        files: list[str] = []
        total_bytes = 0
        for path in sorted(directory.rglob("*"), key=lambda item: item.as_posix()):
            relative = path.relative_to(directory)
            if any(part in EXCLUDED_PARTS for part in relative.parts):
                continue
            if path.is_symlink():
                raise StudioError(
                    "SKILL_DISCOVERY_UNSAFE",
                    "Skill 候选包含软链接",
                    status_code=422,
                    details={"path": display_path},
                )
            if not path.is_file():
                continue
            if not stat.S_ISREG(path.stat().st_mode):
                raise StudioError(
                    "SKILL_DISCOVERY_UNSAFE",
                    "Skill 候选包含非常规文件",
                    status_code=422,
                )
            files.append(relative.as_posix())
            total_bytes += path.stat().st_size
            if len(files) > MAX_SKILL_FILES or total_bytes > MAX_SKILL_BYTES:
                raise StudioError(
                    "SKILL_DISCOVERY_TOO_LARGE",
                    "Skill 候选文件数或总大小超限",
                    status_code=422,
                )
        if files.count("SKILL.md") != 1:
            raise StudioError(
                "SKILL_MANIFEST_REQUIRED",
                "Skill 候选根目录必须且只能包含一个 SKILL.md",
                status_code=422,
            )
        return files, total_bytes

    def _resolve_scan_root(self, raw: str) -> tuple[Path, str, str]:
        token = raw.strip()
        user_roots = self._user_skill_roots()
        if token in user_roots:
            root, display = user_roots[token]
            return root, token, display
        if token.startswith("~") or Path(token).is_absolute():
            expanded = Path(token).expanduser().resolve()
            for source, (root, display) in user_roots.items():
                try:
                    expanded.relative_to(root)
                except ValueError:
                    continue
                return expanded, source, display
            raise StudioError(
                "SKILL_DISCOVERY_PATH_FORBIDDEN",
                "仅允许扫描工作区目录或预置的本地 Skill 目录",
                status_code=403,
                details={"path": raw},
            )
        root = self.workspace.resolve(token)
        return root, "workspace", self.workspace.relative(root)

    def _resolve_candidate_directory(
        self,
        candidate: dict[str, Any],
    ) -> tuple[Path, str, Path, str]:
        source = str(candidate.get("source") or "workspace")
        relative = str(candidate.get("relativePath") or candidate.get("path") or "")
        if source == "workspace":
            directory = self.workspace.resolve(relative, must_exist=True)
            return directory, source, self.workspace.root, ""
        configured = self._user_skill_roots().get(source)
        if configured is None:
            raise StudioError(
                "SKILL_DISCOVERY_PATH_FORBIDDEN",
                "Skill 候选来源不在允许的本地目录中",
                status_code=403,
            )
        root, _display_root = configured
        directory = (root / relative).resolve(strict=True)
        for allowed_source, (
            allowed_root,
            allowed_display_root,
        ) in self._user_skill_roots().items():
            try:
                directory.relative_to(allowed_root)
            except ValueError:
                continue
            return directory, allowed_source, allowed_root, allowed_display_root
        raise StudioError(
            "SKILL_DISCOVERY_PATH_FORBIDDEN",
            "Skill 候选路径不在允许的本地目录中",
            status_code=403,
        )

    def _candidate_relative(self, directory: Path, *, source: str, root: Path) -> str:
        if source == "workspace":
            return self.workspace.relative(directory)
        configured = self._user_skill_roots().get(source)
        source_root = configured[0] if configured is not None else root
        try:
            return directory.relative_to(source_root).as_posix()
        except ValueError:
            return directory.resolve().relative_to(source_root).as_posix()

    @staticmethod
    def _scan_manifests(root: Path) -> list[Path]:
        """递归查找 SKILL.md；跟随目录软链（用户技能常通过软链挂载），并防循环。"""
        manifests: list[Path] = []
        if (root / "SKILL.md").is_file():
            manifests.append(root / "SKILL.md")
        visited: set[str] = set()
        for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
            real = os.path.realpath(dirpath)
            if real in visited:
                dirnames[:] = []
                continue
            visited.add(real)
            dirnames[:] = [d for d in dirnames if d not in EXCLUDED_PARTS]
            if dirpath == str(root):
                continue
            if "SKILL.md" in filenames:
                manifests.append(Path(dirpath) / "SKILL.md")
        return manifests

    def _display_candidate_path(
        self,
        relative: str,
        *,
        source: str,
        display_root: str | None,
    ) -> str:
        if source == "workspace":
            return relative
        root = display_root or source
        return root if relative == "." else f"{root}/{relative}"

    @staticmethod
    def _user_skill_roots() -> dict[str, tuple[Path, str]]:
        home = Path.home()
        return {
            source: ((home / relative).resolve(), f"~/{relative}")
            for source, relative in USER_SKILL_ROOTS.items()
        }

    @staticmethod
    def _directory_digest(directory: Path, files: list[str]) -> str:
        entries = []
        for relative in files:
            content = (directory / relative).read_bytes()
            entries.append(
                {
                    "path": relative,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                }
            )
        return sha256_digest(canonical_json(entries))

    @staticmethod
    def _preview_kind(relative: str) -> str:
        path = Path(relative)
        suffix = path.suffix.lower()
        if suffix == ".md":
            return "markdown"
        if suffix in SCRIPT_SUFFIXES:
            return "script"
        if suffix in TEXT_SUFFIXES or not suffix:
            return "text"
        return "binary"

    @staticmethod
    def _frontmatter(content: str) -> dict[str, Any]:
        if not content.startswith("---\n"):
            raise StudioError(
                "SKILL_MANIFEST_INVALID",
                "SKILL.md 必须包含 YAML frontmatter",
                status_code=422,
            )
        parts = content.split("\n---\n", 1)
        if len(parts) != 2:
            raise StudioError(
                "SKILL_MANIFEST_INVALID",
                "SKILL.md frontmatter 未闭合",
                status_code=422,
            )
        try:
            payload = yaml.safe_load(parts[0][4:]) or {}
        except yaml.YAMLError as exc:
            raise StudioError(
                "SKILL_MANIFEST_INVALID",
                "SKILL.md frontmatter 无法解析",
                status_code=422,
            ) from exc
        if (
            not isinstance(payload, dict)
            or not payload.get("name")
            or not payload.get("description")
        ):
            raise StudioError(
                "SKILL_MANIFEST_INVALID",
                "SKILL.md frontmatter 必须包含 name 和 description",
                status_code=422,
            )
        return cast(dict[str, Any], payload)

    def _record_path(self, token: str, *, validate: bool = False) -> Path:
        if validate and not re.fullmatch(r"skd_[0-9a-f]{32}", token):
            raise StudioError(
                "SKILL_DISCOVERY_NOT_FOUND",
                "Skill 发现记录不存在或已失效",
                status_code=404,
            )
        return self.workspace.resolve(Path(".agentkit/skill-discoveries") / f"{token}.json")


__all__ = ["SkillDiscoveryService"]

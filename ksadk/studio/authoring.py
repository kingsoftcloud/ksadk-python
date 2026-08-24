"""Inspect-first Agent authoring workflows used by AgentKit Studio.

Every flow returns a proposal or inspection before it mutates the canonical
Agent registry.  Quick creation is the only one-step flow and still delegates
ID allocation to the server.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import re
import shutil
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from urllib.parse import urlparse
from uuid import uuid4

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ksadk.detection.detector import FrameworkDetector
from ksadk.managed_runtime import installed_runtime_version
from ksadk.studio.capabilities import canonical_json, sha256_digest
from ksadk.studio.codex_manifest import CodexAgentManifest
from ksadk.studio.contracts import (
    AgentDraft,
    AgentSpec,
    NetworkPolicy,
    RuntimeRef,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.workspace import Workspace

_SLUG = re.compile(r"[^a-z0-9]+")
_MAX_IMPORT_BYTES = 100 * 1024 * 1024
_MAX_IMPORT_FILES = 2000
_SUPPORTED_RUNTIMES = frozenset({"codex", "adk", "langgraph"})


class ConversationProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    slug: str = Field(min_length=1, max_length=63)
    runtimeType: Literal["codex", "adk", "langgraph"]
    description: str = Field(default="", max_length=1024)
    spec: AgentSpec

    @model_validator(mode="before")
    @classmethod
    def migrate_prompt_only_proposal(cls, value: Any) -> Any:
        """Accept one release of old model output without persisting its data loss.

        Older authoring prompts asked the model for only ``instructions``.  Turn
        that response into a complete AgentSpec at the boundary so callers only
        ever consume the lossless proposal shape.
        """

        if not isinstance(value, dict) or "spec" in value:
            return value
        payload = dict(value)
        instructions = payload.pop("instructions", None)
        payload["spec"] = {
            "description": str(payload.get("description") or ""),
            "instructions": instructions or {},
        }
        return payload

    @model_validator(mode="after")
    def validate_runtime_type(self) -> "ConversationProposal":
        if self.spec.runtime is not None and self.spec.runtime.type != self.runtimeType:
            raise ValueError("spec.runtime.type 必须与 runtimeType 一致")
        return self


@dataclass(frozen=True)
class ImportInspection:
    token: str
    kind: Literal["agent-draft", "codex-manifest"]
    payload: dict[str, Any]
    runtime_type: str
    display_name: str
    source_digest: str
    files: tuple[str, ...]
    staging_directory: Path
    manifest_directory: PurePosixPath


class AgentAuthoringService:
    """Pure inspection and staging support; StudioService owns final commits."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.root = workspace.resolve(".agentkit/authoring")
        self.import_root = workspace.resolve(".agentkit/authoring/imports")
        self.project_root = workspace.resolve(".agentkit/authoring/projects")
        self.import_root.mkdir(parents=True, exist_ok=True)
        self.project_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def normalize_slug(value: str) -> str:
        normalized = _SLUG.sub("-", str(value or "").strip().lower()).strip("-")
        if not normalized or not normalized[0].isalpha():
            normalized = f"agent-{normalized}" if normalized else "agent"
        return normalized[:48].rstrip("-")

    def allocate_agent_id(self, slug: str) -> str:
        base = self.normalize_slug(slug)
        for _attempt in range(100):
            candidate = f"{base}-{uuid4().hex[:12]}"
            if not (self.workspace.resolve("agents") / candidate).exists():
                return candidate
        raise StudioError(
            "AGENT_ID_ALLOCATION_FAILED",
            "无法分配唯一 Agent ID",
            status_code=500,
        )

    @staticmethod
    def runtime_ref(agent_id: str, runtime_type: str) -> RuntimeRef:
        normalized = str(runtime_type or "").strip().lower()
        if normalized not in _SUPPORTED_RUNTIMES:
            raise StudioError(
                "RUNTIME_NOT_SUPPORTED",
                "创建方式仅支持 Codex、ADK 和 LangGraph",
                status_code=422,
                field="runtimeType",
                details={"runtimeType": normalized},
            )
        if normalized == "codex":
            # A new YAML Agent must lock the CLI actually installed on this
            # Studio host. Cloud admission resolves that explicit version via
            # the Server-owned catalog instead of accepting a client image.
            return RuntimeRef(
                type="codex",
                version=installed_runtime_version("codex") or "0.144.4",
            )
        return RuntimeRef(
            type=cast(Any, normalized),
            project_path=f"agents/{agent_id}/source",
            entry_point="agent.py",
            agent_variable="graph" if normalized == "langgraph" else "root_agent",
        )

    def inspect_import(self, content: bytes, *, filename: str) -> dict[str, Any]:
        if not content:
            raise StudioError(
                "AGENT_IMPORT_EMPTY",
                "导入文件不能为空",
                status_code=422,
            )
        if len(content) > _MAX_IMPORT_BYTES:
            raise StudioError(
                "AGENT_IMPORT_TOO_LARGE",
                "Agent 导入包不能超过 100 MiB",
                status_code=413,
            )
        token = f"imp_{uuid4().hex}"
        staging = self.workspace.resolve(self.import_root / token)
        staging.mkdir(parents=True, exist_ok=False)
        try:
            files, manifest_path = self._stage_import(staging, content, filename=filename)
            payload = self._load_yaml_object(manifest_path)
            kind, runtime_type, display_name = self._classify_import(payload)
            manifest_relative = manifest_path.relative_to(staging)
            record = {
                "format": "agentkit.authoring-import/v1",
                "token": token,
                "kind": kind,
                "runtimeType": runtime_type,
                "displayName": display_name,
                "sourceDigest": self._tree_digest(staging, exclude={"inspection.json"}),
                "files": files,
                "manifestPath": manifest_relative.as_posix(),
                "manifestDirectory": manifest_relative.parent.as_posix(),
                "payload": payload,
            }
            self.workspace.atomic_write_text(
                staging / "inspection.json",
                json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            )
            return self._public_import_record(record)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def load_import(self, token: str) -> ImportInspection:
        staging = self._token_directory(self.import_root, token, prefix="imp_")
        record_path = staging / "inspection.json"
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StudioError(
                "AGENT_IMPORT_INSPECTION_NOT_FOUND",
                "导入检查不存在或已失效",
                status_code=404,
            ) from exc
        expected = str(record.get("sourceDigest") or "")
        actual = self._tree_digest(staging, exclude={"inspection.json"})
        if not expected or expected != actual:
            raise StudioError(
                "AGENT_IMPORT_CHANGED",
                "导入暂存内容在确认前发生变化，请重新检查",
                status_code=409,
            )
        manifest_directory = PurePosixPath(str(record.get("manifestDirectory") or "."))
        return ImportInspection(
            token=token,
            kind=cast(Any, record["kind"]),
            payload=cast(dict[str, Any], record["payload"]),
            runtime_type=str(record["runtimeType"]),
            display_name=str(record["displayName"]),
            source_digest=expected,
            files=tuple(str(item) for item in record.get("files") or []),
            staging_directory=staging,
            manifest_directory=manifest_directory,
        )

    def consume_import(self, token: str) -> None:
        staging = self._token_directory(self.import_root, token, prefix="imp_")
        shutil.rmtree(staging, ignore_errors=True)

    def source_directory(self, inspection: ImportInspection) -> Path | None:
        if inspection.kind != "agent-draft":
            return None
        draft = AgentDraft.model_validate(inspection.payload)
        runtime = draft.spec.runtime
        if runtime is None or not runtime.project_path:
            return None
        candidates = [
            inspection.staging_directory / inspection.manifest_directory / runtime.project_path,
            inspection.staging_directory / runtime.project_path,
            inspection.staging_directory / inspection.manifest_directory / "source",
        ]
        for candidate in candidates:
            resolved = candidate.resolve()
            try:
                resolved.relative_to(inspection.staging_directory.resolve())
            except ValueError:
                continue
            if resolved.is_dir():
                return resolved
        return None

    def inspect_project(self, project_path: str) -> dict[str, Any]:
        project = self.workspace.resolve(project_path, must_exist=True)
        if not project.is_dir():
            raise StudioError(
                "PROJECT_PATH_INVALID",
                "项目识别路径必须是工作区内的目录",
                status_code=422,
            )
        detection = FrameworkDetector(str(project)).detect()
        runtime_type = detection.type.value
        if not detection.is_valid or runtime_type not in _SUPPORTED_RUNTIMES:
            raise StudioError(
                "PROJECT_RUNTIME_UNSUPPORTED",
                "没有识别到可由 Studio 管理的 Codex、ADK 或 LangGraph Runtime",
                status_code=422,
                details={"detected": runtime_type},
            )
        token = f"prj_{uuid4().hex}"
        relative = self.workspace.relative(project)
        record = {
            "format": "agentkit.authoring-project/v1",
            "token": token,
            "projectPath": relative,
            "runtimeType": runtime_type,
            "name": detection.name or project.name,
            "entryPoint": detection.entry_point or None,
            "agentVariable": detection.agent_variable or None,
            "confidence": float(detection.confidence),
            "evidence": {
                "config": dict(detection.raw_config or {}),
                "packagePath": detection.package_path,
            },
            "sourceDigest": self._tree_digest(project),
            "requiresConfirmation": True,
        }
        self.workspace.atomic_write_text(
            self.project_root / f"{token}.json",
            json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        return {**record, "inspectionToken": token}

    def load_project(self, token: str) -> dict[str, Any]:
        if not re.fullmatch(r"prj_[0-9a-f]{32}", token):
            raise StudioError(
                "PROJECT_INSPECTION_NOT_FOUND",
                "项目识别检查不存在或已失效",
                status_code=404,
            )
        path = self.workspace.resolve(self.project_root / f"{token}.json")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StudioError(
                "PROJECT_INSPECTION_NOT_FOUND",
                "项目识别检查不存在或已失效",
                status_code=404,
            ) from exc
        project = self.workspace.resolve(str(record["projectPath"]), must_exist=True)
        if self._tree_digest(project) != record.get("sourceDigest"):
            raise StudioError(
                "PROJECT_CHANGED_AFTER_INSPECTION",
                "项目在确认前发生变化，请重新识别",
                status_code=409,
            )
        return cast(dict[str, Any], record)

    def consume_project(self, token: str) -> None:
        path = self.workspace.resolve(self.project_root / f"{token}.json")
        if path.is_file():
            path.unlink()

    @staticmethod
    def _conversation_json_object(content: str) -> dict[str, Any]:
        """Extract one JSON object without trusting surrounding model prose."""

        text = str(content or "").strip()
        candidates = [text]
        candidates.extend(
            match.group(1).strip()
            for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
        )
        decoder = json.JSONDecoder()
        for candidate in candidates:
            try:
                payload = json.loads(candidate)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                return cast(dict[str, Any], payload)
        for start, character in enumerate(text):
            if character != "{":
                continue
            try:
                payload, _end = decoder.raw_decode(text, start)
            except ValueError:
                continue
            if isinstance(payload, dict):
                return cast(dict[str, Any], payload)
        raise ValueError("model output does not contain a JSON object")

    @staticmethod
    def _merge_conversation_patch(
        base: dict[str, Any], patch: dict[str, Any]
    ) -> dict[str, Any]:
        merged = copy.deepcopy(base)
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = AgentAuthoringService._merge_conversation_patch(
                    cast(dict[str, Any], merged[key]), value
                )
            else:
                merged[key] = copy.deepcopy(value)
        return merged

    @staticmethod
    def parse_conversation_proposal(
        content: str,
        *,
        base: ConversationProposal | dict[str, Any] | None = None,
    ) -> ConversationProposal:
        try:
            payload = AgentAuthoringService._conversation_json_object(content)
            for wrapper in ("proposal", "patch"):
                wrapped = payload.get(wrapper)
                if isinstance(wrapped, dict) and len(payload) == 1:
                    payload = cast(dict[str, Any], wrapped)
                    break
            if base is not None:
                base_payload = (
                    base.model_dump(by_alias=True, mode="json")
                    if isinstance(base, ConversationProposal)
                    else base
                )
                payload = AgentAuthoringService._merge_conversation_patch(
                    base_payload, payload
                )
            proposal = ConversationProposal.model_validate(payload)
        except (ValueError, ValidationError) as exc:
            raise StudioError(
                "AUTHORING_MODEL_OUTPUT_INVALID",
                "对话构建模型没有返回合法的 Agent Draft Patch",
                status_code=502,
                details={"reason": str(exc)},
            ) from exc
        normalized_slug = AgentAuthoringService.normalize_slug(proposal.slug)
        return proposal.model_copy(update={"slug": normalized_slug})

    @staticmethod
    def conversation_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
        if not messages:
            raise StudioError(
                "AUTHORING_CONVERSATION_EMPTY",
                "对话构建至少需要一条消息",
                status_code=422,
            )
        normalized: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "你是 AgentKit Studio 的 Agent 设计助手。根据对话生成一个 JSON Draft Patch，"
                    "不得输出 Markdown。首轮顶层字段必须且只能包含 name、slug、runtimeType、"
                    "description、spec；后续轮次可以只返回需要变更的字段，由 Studio 与上一版"
                    "Patch 合并。runtimeType 只能是 codex、adk、langgraph。spec 是完整"
                    " AgentSpec，可包含 runtime、instructions、model、capabilities、bindings、"
                    "execution、context、memory、security、evaluation；instructions 必须包含"
                    " system 和 task。Tool、MCP、Skill、模型、模型参数和策略一旦在对话中明确，"
                    "必须写入 spec，不能只返回提示词。只提出配置，不写文件、不宣称已经创建。"
                ),
            }
        ]
        for item in messages:
            role = str(item.get("role") or "").strip()
            content = str(item.get("content") or "").strip()
            if role not in {"user", "assistant"} or not content:
                raise StudioError(
                    "AUTHORING_CONVERSATION_INVALID",
                    "对话消息只允许非空 user/assistant 文本",
                    status_code=422,
                )
            normalized.append({"role": role, "content": content})
        return normalized

    @staticmethod
    def authoring_network_policy(endpoint_url: str) -> NetworkPolicy:
        host = (urlparse(endpoint_url).hostname or "").lower().rstrip(".")
        return NetworkPolicy(
            mode="restricted",
            allowed_hosts=[host] if host else [],
            allow_private_network=False,
        )

    def _stage_import(
        self,
        staging: Path,
        content: bytes,
        *,
        filename: str,
    ) -> tuple[list[str], Path]:
        if filename.lower().endswith(".zip"):
            return self._extract_zip(staging, content)
        if not filename.lower().endswith((".yaml", ".yml")):
            raise StudioError(
                "AGENT_IMPORT_FORMAT_UNSUPPORTED",
                "仅支持 Agent YAML 或 ZIP",
                status_code=422,
            )
        path = staging / "agent.yaml"
        path.write_bytes(content)
        return ["agent.yaml"], path

    def _extract_zip(self, staging: Path, content: bytes) -> tuple[list[str], Path]:
        try:
            archive = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile as exc:
            raise StudioError(
                "AGENT_IMPORT_ARCHIVE_INVALID",
                "Agent ZIP 无法解析",
                status_code=422,
            ) from exc
        with archive:
            entries = [item for item in archive.infolist() if not item.is_dir()]
            if not entries or len(entries) > _MAX_IMPORT_FILES:
                raise StudioError(
                    "AGENT_IMPORT_ARCHIVE_INVALID",
                    "Agent ZIP 文件数量不合法",
                    status_code=422,
                )
            expanded = sum(item.file_size for item in entries)
            if expanded > _MAX_IMPORT_BYTES:
                raise StudioError(
                    "AGENT_IMPORT_TOO_LARGE",
                    "Agent ZIP 解压后不能超过 100 MiB",
                    status_code=413,
                )
            files: list[str] = []
            manifests: list[Path] = []
            for item in entries:
                path = PurePosixPath(item.filename)
                mode = (item.external_attr >> 16) & 0o170000
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or mode in {stat.S_IFLNK, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO}
                    or (item.compress_size == 0 and item.file_size > 0)
                    or (
                        item.compress_size > 0
                        and item.file_size / item.compress_size > 200
                    )
                ):
                    raise StudioError(
                        "AGENT_IMPORT_ARCHIVE_UNSAFE",
                        "Agent ZIP 包含不安全路径、链接或压缩条目",
                        status_code=422,
                        details={"entry": item.filename},
                    )
                target = staging / Path(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(item))
                files.append(path.as_posix())
                if path.name in {"agent.yaml", "agentengine.yaml"}:
                    manifests.append(target)
        if len(manifests) != 1:
            raise StudioError(
                "AGENT_IMPORT_MANIFEST_REQUIRED",
                "Agent ZIP 必须且只能包含一个 agent.yaml 或 agentengine.yaml",
                status_code=422,
            )
        return sorted(files), manifests[0]

    @staticmethod
    def _load_yaml_object(path: Path) -> dict[str, Any]:
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise StudioError(
                "AGENT_IMPORT_MANIFEST_INVALID",
                "Agent YAML 无法解析",
                status_code=422,
            ) from exc
        if not isinstance(payload, dict):
            raise StudioError(
                "AGENT_IMPORT_MANIFEST_INVALID",
                "Agent YAML 根节点必须是对象",
                status_code=422,
            )
        return cast(dict[str, Any], payload)

    @staticmethod
    def _classify_import(payload: dict[str, Any]) -> tuple[str, str, str]:
        try:
            if payload.get("kind") == "Agent":
                draft = AgentDraft.model_validate(payload)
                if draft.spec.runtime is None:
                    raise ValueError("Agent Draft 缺少 spec.runtime")
                return "agent-draft", draft.spec.runtime.type, draft.metadata.name
            manifest = CodexAgentManifest.model_validate(payload)
            return "codex-manifest", "codex", manifest.name
        except (ValueError, ValidationError) as exc:
            raise StudioError(
                "AGENT_IMPORT_CONTRACT_INVALID",
                "导入内容不符合 Agent Draft 或 Codex agentengine.yaml 合同",
                status_code=422,
                details={"reason": str(exc)},
            ) from exc

    @staticmethod
    def _tree_digest(root: Path, *, exclude: set[str] | None = None) -> str:
        # 默认排除 .agentkit（Studio 自身状态）与常见忽略目录，避免 inspect 后写 token json
        # 改变 digest 导致 commit 时 PROJECT_CHANGED_AFTER_INSPECTION 误报（方案 §6.1）。
        ignored = set(exclude or [])
        ignored.add(".agentkit")
        ignored.add(".git")
        entries: list[dict[str, Any]] = []
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_symlink():
                raise StudioError(
                    "AUTHORING_SOURCE_UNSAFE",
                    "待检查内容包含软链接",
                    status_code=422,
                    details={"path": str(path)},
                )
            if not path.is_file() or path.name in ignored:
                continue
            # 跳过 .agentkit / .git 目录下的文件
            relative = path.relative_to(root).as_posix()
            if any(relative.startswith(ignored_dir + "/") for ignored_dir in (".agentkit", ".git")):
                continue
            content = path.read_bytes()
            entries.append(
                {
                    "path": relative,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                }
            )
        return sha256_digest(canonical_json(entries))

    def _token_directory(self, root: Path, token: str, *, prefix: str) -> Path:
        if not re.fullmatch(rf"{re.escape(prefix)}[0-9a-f]{{32}}", token):
            raise StudioError(
                "AGENT_IMPORT_INSPECTION_NOT_FOUND",
                "导入检查不存在或已失效",
                status_code=404,
            )
        path = self.workspace.resolve(root / token)
        if not path.is_dir():
            raise StudioError(
                "AGENT_IMPORT_INSPECTION_NOT_FOUND",
                "导入检查不存在或已失效",
                status_code=404,
            )
        return path

    @staticmethod
    def _public_import_record(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "inspectionToken": record["token"],
            "kind": record["kind"],
            "runtimeType": record["runtimeType"],
            "displayName": record["displayName"],
            "sourceDigest": record["sourceDigest"],
            "files": record["files"],
            "requiresConfirmation": True,
        }


__all__ = ["AgentAuthoringService", "ConversationProposal", "ImportInspection"]

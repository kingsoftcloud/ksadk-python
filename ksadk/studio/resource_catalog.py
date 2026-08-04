"""Workspace-backed resource catalog for models, tools, MCP servers and Skills."""

from __future__ import annotations

import builtins
import hashlib
import io
import re
import shutil
import stat
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Literal, cast
from uuid import uuid4

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from ksadk.cli.model_catalog import fetch_provider_model_catalog
from ksadk.conversations.model_context import normalize_model_metadata
from ksadk.studio.capabilities import (
    LocalCapabilityResolver,
    canonical_json,
    require_exact_version,
    sha256_digest,
)
from ksadk.studio.contracts import (
    AgentBindings,
    CapabilityBinding,
    CapabilityRef,
    MCPServerRef,
    ModelSpec,
    ResourceDescriptor,
    ToolContract,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.repository import load_yaml_file
from ksadk.studio.skill_discovery import SkillDiscoveryService
from ksadk.studio.workspace import Workspace
from ksadk.toolsets import describe_agentengine_tools, get_agentengine_tools

_SLUG = re.compile(r"[^a-z0-9]+")
_MAX_SKILL_ARCHIVE_BYTES = 50 * 1024 * 1024
_MAX_SKILL_EXPANDED_BYTES = 100 * 1024 * 1024
_MAX_SKILL_FILES = 1000


def _models_endpoint(api_base: str | None) -> str:
    base = str(api_base or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/models"):
        return base
    if base.endswith("/v1"):
        return f"{base}/models"
    return f"{base}/v1/models"


def _provider_reports_context(raw: dict[str, Any]) -> bool:
    direct = {
        "context_window_tokens",
        "context_length",
        "context_window",
        "input_max_length",
    }
    if direct.intersection(raw):
        return True
    for key in ("metadata", "limits"):
        nested = raw.get(key)
        if isinstance(nested, dict) and direct.intersection(nested):
            return True
    return False


def _provider_reports_modalities(raw: dict[str, Any]) -> bool:
    architecture = raw.get("architecture")
    if isinstance(architecture, dict) and isinstance(architecture.get("input_modalities"), list):
        return True
    capabilities = raw.get("capabilities")
    return isinstance(capabilities, dict) and any(
        key in capabilities
        for key in (
            "multimodal_input_image",
            "multimodal_input_video",
            "multimodal_input_file",
        )
    )


def _tool_side_effect(side_effects: list[str]) -> str:
    if not side_effects:
        return "none"
    lowered = " ".join(side_effects).lower()
    if any(marker in lowered for marker in ("write", "edit", "delete", "command", "code")):
        return "write"
    if any(marker in lowered for marker in ("network", "external", "http")):
        return "external"
    return "read"


def resource_slug(value: str) -> str:
    slug = _SLUG.sub("-", value.strip().lower()).strip("-")
    if not slug:
        raise StudioError(
            "RESOURCE_NAME_INVALID",
            "资源名称必须至少包含一个字母或数字",
            status_code=422,
            field="name",
        )
    return slug[:80]


def resource_id(kind: str, source: str, name: str, version: str) -> str:
    return f"{kind}:{source}:{resource_slug(name)}:{version}"


class LocalResourceCatalog:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.resolver = LocalCapabilityResolver(workspace)
        self.skill_discovery = SkillDiscoveryService(workspace)
        self._provider_models: dict[str, ResourceDescriptor] = {}

    def list(
        self,
        *,
        kind: str | None = None,
        query: str = "",
        source: str | None = None,
        status: str | None = None,
        installed: bool | None = None,
        limit: int = 50,
    ) -> list[ResourceDescriptor]:
        candidates = [
            *self._builtin_models(),
            *self._provider_models.values(),
            *self._builtin_tools(),
            *self._persisted("models"),
            *self._persisted("mcp"),
            *self._persisted("tools"),
            *self._local_skills(),
        ]
        resources = list({item.resource_id: item for item in candidates}.values())
        normalized = query.strip().lower()
        filtered = [
            item
            for item in resources
            if (kind is None or item.kind == kind)
            and (source is None or item.source == source)
            and (status is None or item.status == status)
            and (installed is None or item.installed is installed)
            and (
                not normalized
                or normalized in item.name.lower()
                or normalized in item.display_name.lower()
                or normalized in item.description.lower()
            )
        ]
        filtered.sort(
            key=lambda item: (
                item.kind,
                0 if item.source in {"builtin", "provider"} else 1,
                item.display_name.lower(),
                item.version,
            )
        )
        return filtered[:limit]

    async def discover_provider_models(
        self,
        *,
        api_base: str | None,
        api_key: str | None,
        current_model: str | None,
        timeout: float = 5.0,
    ) -> tuple[builtins.list[ResourceDescriptor], str]:
        """Project the same provider catalog used by ksadk runtimes into Studio.

        The provider response is authoritative only for fields it actually
        returns.  Canonical fallback values remain annotated as ``ksadk-default``
        so the UI never presents a guessed context window or modality as probed.
        """

        catalog = await fetch_provider_model_catalog(
            api_base=api_base,
            api_key=api_key,
            timeout=timeout,
        )
        source = "provider" if catalog else "fallback"
        if not catalog:
            catalog = [normalize_model_metadata({"id": current_model or "glm-5.1"})]

        descriptors: list[ResourceDescriptor] = []
        for item in catalog:
            normalized = dict(item)
            raw = normalized.pop("_provider_raw_model", None)
            raw_mapping = raw if isinstance(raw, dict) else {}
            normalized = normalize_model_metadata(normalized)
            model_id = str(normalized.get("id") or current_model or "unknown-model")
            display_name = str(normalized.get("display_name") or model_id)
            context_source = (
                "provider" if _provider_reports_context(raw_mapping) else "ksadk-default"
            )
            modality_source = (
                "provider" if _provider_reports_modalities(raw_mapping) else "ksadk-default"
            )
            spec = ModelSpec(
                provider="openai-compatible",
                model=model_id,
                base_url=(api_base or "https://kspmas.ksyun.com/v1").rstrip("/"),
                credential_ref="env://OPENAI_API_KEY",
                metadata=normalized,
                discovery={
                    "source": source,
                    "endpoint": _models_endpoint(api_base),
                    "contextWindow": context_source,
                    "inputModalities": modality_source,
                },
            )
            descriptor_source = "provider" if source == "provider" else "builtin"
            descriptor = self._descriptor(
                kind="model",
                source=descriptor_source,
                name=model_id,
                display_name=display_name,
                version=str(
                    normalized.get("version") or ("live" if source == "provider" else "1.0.0")
                ),
                description=(
                    "由模型服务 /v1/models 自动发现"
                    if source == "provider"
                    else "模型服务未返回目录，使用 ksadk 当前模型配置"
                ),
                category="provider-catalog",
                contract=spec.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
                required_secret_refs=[spec.credential_ref],
            )
            descriptors.append(descriptor)

        self._provider_models = {item.resource_id: item for item in descriptors}
        return descriptors, source

    def get(self, resource: str) -> ResourceDescriptor:
        found = next(
            (item for item in self.list(limit=10_000) if item.resource_id == resource),
            None,
        )
        if found is None:
            raise StudioError(
                "RESOURCE_NOT_FOUND",
                "Catalog Resource 不存在",
                status_code=404,
                details={"resourceId": resource},
            )
        return found

    def create_model_profile(
        self,
        *,
        name: str,
        display_name: str,
        version: str,
        description: str,
        spec: ModelSpec,
    ) -> ResourceDescriptor:
        require_exact_version(version, field="version")
        return self._persist_descriptor(
            "models",
            self._descriptor(
                kind="model",
                name=name,
                display_name=display_name,
                version=version,
                description=description,
                category="model",
                contract=spec.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
                required_secret_refs=[spec.credential_ref],
            ),
        )

    def create_mcp_server(
        self,
        *,
        display_name: str,
        description: str,
        server: MCPServerRef,
    ) -> ResourceDescriptor:
        require_exact_version(server.version, field="version")
        resolved = self.resolver.resolve_mcp(server)
        descriptor = self._descriptor(
            kind="mcp",
            name=server.name,
            display_name=display_name,
            version=server.version,
            description=description,
            category="mcp",
            contract=resolved,
            required_secret_refs=sorted(set(server.env_refs.values())),
        )
        return self._persist_descriptor("mcp", descriptor)

    def create_tool(
        self,
        *,
        display_name: str,
        category: str,
        contract: ToolContract,
    ) -> ResourceDescriptor:
        require_exact_version(contract.version, field="version")
        if contract.executor == "python":
            contract = self._snapshot_python_tool(contract)
        resolved = self.resolver.resolve_tool(contract)
        descriptor = self._descriptor(
            kind="tool",
            name=contract.name,
            display_name=display_name,
            version=contract.version,
            description=contract.description,
            category=category,
            contract=resolved.model_dump(
                by_alias=True,
                exclude_none=True,
                mode="json",
            ),
        )
        return self._persist_descriptor("tools", descriptor)

    def _snapshot_python_tool(self, contract: ToolContract) -> ToolContract:
        assert contract.source_path is not None
        source = self.workspace.resolve(contract.source_path, must_exist=True)
        if source.is_symlink() or not source.is_file() or source.suffix != ".py":
            raise StudioError(
                "TOOL_SOURCE_INVALID",
                "Python Tool sourcePath 必须是工作区内的普通 .py 文件",
                status_code=422,
                field="sourcePath",
            )
        content = source.read_bytes()
        if len(content) > 1024 * 1024:
            raise StudioError(
                "TOOL_SOURCE_TOO_LARGE",
                "Python Tool 源码不能超过 1 MiB",
                status_code=422,
                field="sourcePath",
            )
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StudioError(
                "TOOL_SOURCE_INVALID",
                "Python Tool 源码必须使用 UTF-8 编码",
                status_code=422,
                field="sourcePath",
            ) from exc
        target = self.workspace.resolve(
            Path(".agentkit/catalog/tool-sources")
            / f"{resource_slug(contract.name)}-{resource_slug(contract.version)}"
            / "tool.py"
        )
        self.workspace.atomic_write_text(target, text)
        return cast(
            ToolContract,
            contract.model_copy(
                update={
                    "source_path": self.workspace.relative(target),
                    "source_sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
                }
            ),
        )

    def save_probe(
        self,
        resource: str,
        *,
        result: dict[str, Any],
    ) -> ResourceDescriptor:
        descriptor = self.get(resource)
        if descriptor.kind != "mcp" or descriptor.source != "local":
            raise StudioError(
                "RESOURCE_KIND_INVALID",
                "只有本地 MCP Resource 可以保存探测结果",
                status_code=422,
                details={"resourceId": resource},
            )
        updated = descriptor.model_copy(deep=True)
        updated.status = "ready"
        updated.health = {
            "status": "ready",
            "toolCount": len(result.get("tools") or []),
            "serverInfo": result.get("serverInfo") or {},
            "probedAt": datetime.now(timezone.utc).isoformat(),
        }
        updated.contract["discoveredTools"] = result.get("tools") or []
        updated.updated_at = datetime.now(timezone.utc)
        return self._persist_descriptor("mcp", updated, overwrite=True)

    def mark_probe_failed(
        self,
        resource: str,
        *,
        code: str,
    ) -> ResourceDescriptor:
        descriptor = self.get(resource)
        updated = descriptor.model_copy(deep=True)
        updated.status = "unhealthy"
        updated.health = {
            "status": "unhealthy",
            "code": code,
            "probedAt": datetime.now(timezone.utc).isoformat(),
        }
        updated.updated_at = datetime.now(timezone.utc)
        return self._persist_descriptor("mcp", updated, overwrite=True)

    def policy_preview(
        self,
        bindings: AgentBindings,
    ) -> tuple[builtins.list[ToolContract], builtins.list[str]]:
        tools: builtins.list[ToolContract] = []
        permissions: set[str] = set()
        for binding in bindings.tools:
            if not binding.enabled:
                continue
            descriptor = self.get(binding.resource_id)
            if descriptor.kind != "tool":
                raise StudioError(
                    "RESOURCE_KIND_INVALID",
                    "Tool binding 必须引用 Tool Resource",
                    status_code=422,
                    details={"resourceId": binding.resource_id},
                )
            if descriptor.status != "ready":
                raise StudioError(
                    "RESOURCE_NOT_READY",
                    "不可用的 Tool Resource 不能绑定",
                    status_code=409,
                    details={
                        "resourceId": descriptor.resource_id,
                        "status": descriptor.status,
                    },
                )
            tool = ToolContract.model_validate(descriptor.contract)
            approval = self._effective_approval(
                tool,
                bindings.policy_template,
                binding,
            )
            tool = cast(
                ToolContract,
                tool.model_copy(update={"approval": approval}),
            )
            tools.append(tool)
            permissions.update(tool.permissions)
        tools.sort(key=lambda item: (item.name, item.version))
        return tools, sorted(permissions)

    def resolve_model(self, bindings: AgentBindings) -> ModelSpec | None:
        if not bindings.model_profile_id:
            return None
        descriptor = self.get(bindings.model_profile_id)
        if descriptor.kind != "model":
            raise StudioError(
                "RESOURCE_KIND_INVALID",
                "modelProfileId 必须引用 Model Resource",
                status_code=422,
            )
        if descriptor.status not in {"ready", "missing-secret"}:
            raise StudioError(
                "RESOURCE_NOT_READY",
                "模型资源当前不可用",
                status_code=409,
                details={"resourceId": descriptor.resource_id, "status": descriptor.status},
            )
        resolved = cast(ModelSpec, ModelSpec.model_validate(descriptor.contract))
        if bindings.model_parameters is not None:
            resolved.parameters = bindings.model_parameters
        return resolved

    def resolve_models(self, bindings: AgentBindings) -> builtins.list[ModelSpec]:
        resource_ids = builtins.list(bindings.model_profile_ids)
        if not resource_ids and bindings.model_profile_id:
            resource_ids = [bindings.model_profile_id]
        resolved: builtins.list[ModelSpec] = []
        for resource_id in resource_ids:
            selected = bindings.model_copy(
                update={"model_profile_id": resource_id, "model_profile_ids": []}
            )
            model = self.resolve_model(selected)
            if model is not None:
                resolved.append(model)
        return resolved

    def resolve_skills(self, bindings: AgentBindings) -> builtins.list[CapabilityRef]:
        refs: builtins.list[CapabilityRef] = []
        for binding in bindings.skills:
            if not binding.enabled:
                continue
            descriptor = self._ready_binding(binding, expected_kind="skill")
            refs.append(
                CapabilityRef(
                    name=descriptor.name,
                    version=descriptor.version,
                    digest=descriptor.digest,
                )
            )
        return refs

    def resolve_mcp_servers(
        self,
        bindings: AgentBindings,
    ) -> builtins.list[MCPServerRef]:
        refs: builtins.list[MCPServerRef] = []
        for binding in bindings.mcp_servers:
            if not binding.enabled:
                continue
            descriptor = self._ready_binding(binding, expected_kind="mcp")
            payload = {
                key: value for key, value in descriptor.contract.items() if key != "discoveredTools"
            }
            refs.append(MCPServerRef.model_validate(payload))
        return refs

    def resolve_mcp_tools(
        self,
        bindings: AgentBindings,
    ) -> builtins.list[ToolContract]:
        tools: builtins.list[ToolContract] = []
        for binding in bindings.mcp_servers:
            if not binding.enabled:
                continue
            descriptor = self._ready_binding(binding, expected_kind="mcp")
            for payload in descriptor.contract.get("discoveredTools") or []:
                tool = ToolContract.model_validate(payload)
                approval = self._effective_approval(
                    tool,
                    bindings.policy_template,
                    binding,
                )
                tools.append(
                    cast(
                        ToolContract,
                        tool.model_copy(update={"approval": approval}),
                    )
                )
        return tools

    def import_skill_zip(self, content: bytes, *, filename: str) -> ResourceDescriptor:
        if not filename.lower().endswith(".zip"):
            raise StudioError(
                "SKILL_ARCHIVE_INVALID",
                "Skill 包必须是 .zip 文件",
                status_code=422,
            )
        if len(content) > _MAX_SKILL_ARCHIVE_BYTES:
            raise StudioError(
                "SKILL_ARCHIVE_TOO_LARGE",
                "Skill 包不能超过 50MB",
                status_code=413,
            )
        try:
            archive = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile as exc:
            raise StudioError(
                "SKILL_ARCHIVE_INVALID",
                "Skill ZIP 无法解析",
                status_code=422,
            ) from exc
        with archive:
            files = [info for info in archive.infolist() if not info.is_dir()]
            if not files or len(files) > _MAX_SKILL_FILES:
                raise StudioError(
                    "SKILL_ARCHIVE_INVALID",
                    "Skill ZIP 文件数量不合法",
                    status_code=422,
                )
            expanded = sum(info.file_size for info in files)
            if expanded > _MAX_SKILL_EXPANDED_BYTES:
                raise StudioError(
                    "SKILL_ARCHIVE_TOO_LARGE",
                    "Skill ZIP 解压后不能超过 100MB",
                    status_code=413,
                )
            for info in files:
                path = PurePosixPath(info.filename)
                mode = (info.external_attr >> 16) & 0o170000
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or mode == stat.S_IFLNK
                    or (info.compress_size == 0 and info.file_size > 0)
                    or (info.compress_size > 0 and info.file_size / info.compress_size > 200)
                ):
                    raise StudioError(
                        "SKILL_ARCHIVE_UNSAFE",
                        "Skill ZIP 包含不安全路径、链接或压缩条目",
                        status_code=422,
                        details={"entry": info.filename},
                    )
            skill_entry, prefix = self._skill_entry(files)
            skill_text = archive.read(skill_entry).decode("utf-8")
            metadata = self._skill_frontmatter(skill_text)
            name = str(metadata["name"])
            description = str(metadata["description"])
            version = str(metadata.get("version") or "1.0.0")
            require_exact_version(version, field="SKILL.md.version")
            slug = resource_slug(name)
            destination = self.workspace.resolve(Path("capabilities/skills") / slug)
            if destination.exists():
                raise StudioError(
                    "RESOURCE_ALREADY_EXISTS",
                    "同名 Skill 已安装",
                    status_code=409,
                    details={"name": slug},
                )
            staging = self.workspace.resolve(
                Path(".agentkit/cache") / f".skill-{slug}-{uuid4().hex}.tmp"
            )
            staging.mkdir(parents=True, exist_ok=False)
            try:
                for info in files:
                    source_path = PurePosixPath(info.filename)
                    relative = (
                        PurePosixPath(*source_path.parts[len(prefix.parts) :])
                        if prefix.parts
                        else source_path
                    )
                    target = staging / Path(*relative.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(info))
                self.workspace.atomic_write_yaml(
                    staging / "skill.yaml",
                    {
                        "name": slug,
                        "displayName": name,
                        "description": description,
                        "version": version,
                        "instructionsFile": "SKILL.md",
                    },
                )
                shutil.move(str(staging), str(destination))
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        descriptor = next(
            (
                item
                for item in self._local_skills()
                if item.name == slug and item.version == version
            ),
            None,
        )
        if descriptor is None:
            raise StudioError(
                "SKILL_IMPORT_FAILED",
                "Skill 安装后无法解析",
                status_code=500,
            )
        return descriptor

    def discover_skills(
        self,
        *,
        scan_paths: builtins.list[str] | None = None,
    ) -> dict[str, Any]:
        return self.skill_discovery.discover(scan_paths=scan_paths)

    def commit_discovered_skill(
        self,
        inspection_token: str,
        candidate_id: str,
        *,
        overwrite: bool = False,
    ) -> ResourceDescriptor:
        slug, version = self.skill_discovery.commit(
            inspection_token,
            candidate_id,
            overwrite=overwrite,
        )
        descriptor = next(
            (
                item
                for item in self._local_skills()
                if item.name == slug and item.version == version
            ),
            None,
        )
        if descriptor is None:
            raise StudioError(
                "SKILL_IMPORT_FAILED",
                "Skill 导入后无法解析",
                status_code=500,
            )
        return descriptor

    def _ready_binding(
        self,
        binding: CapabilityBinding,
        *,
        expected_kind: str,
    ) -> ResourceDescriptor:
        descriptor = self.get(binding.resource_id)
        if descriptor.kind != expected_kind:
            raise StudioError(
                "RESOURCE_KIND_INVALID",
                f"Binding 必须引用 {expected_kind} Resource",
                status_code=422,
                details={"resourceId": binding.resource_id},
            )
        if descriptor.status != "ready":
            raise StudioError(
                "RESOURCE_NOT_READY",
                "不可用的 Resource 不能绑定",
                status_code=409,
                details={
                    "resourceId": descriptor.resource_id,
                    "status": descriptor.status,
                },
            )
        return descriptor

    @staticmethod
    def _effective_approval(
        tool: ToolContract,
        template: str,
        binding: CapabilityBinding,
    ) -> str:
        if binding.approval:
            return binding.approval
        if template == "loose":
            return "never"
        if template == "strict":
            return (
                "never"
                if tool.side_effect in {"none", "read"}
                and "process:execute" not in tool.permissions
                else "always"
            )
        return tool.approval

    def _builtin_models(self) -> Iterable[ResourceDescriptor]:
        spec = ModelSpec(
            provider="openai-compatible",
            model="glm-5.1",
            endpoint_url="https://kspmas.ksyun.com/v1/chat/completions",
            credential_ref="env://AGENTKIT_MODEL_API_KEY",
        )
        yield self._descriptor(
            kind="model",
            source="builtin",
            name="glm-5.1",
            display_name="GLM-5.1",
            version="1.0.0",
            description="金山云 OpenAI-compatible 模型配置",
            category="general",
            contract=spec.model_dump(
                by_alias=True,
                exclude_none=True,
                mode="json",
            ),
            required_secret_refs=[spec.credential_ref],
        )

    def _builtin_tools(self) -> Iterable[ResourceDescriptor]:
        runtime_tools = {
            str(getattr(tool, "name", None) or getattr(tool, "__name__", "")): tool
            for tool in get_agentengine_tools(profile="coding", mode="direct")
        }
        for descriptor in describe_agentengine_tools(profile="coding", mode="direct"):
            name = str(descriptor["name"])
            group = str(descriptor.get("group") or "general")
            side_effects = [str(item) for item in descriptor.get("side_effects") or []]
            side_effect = _tool_side_effect(side_effects)
            permissions = [str(item) for item in descriptor.get("approval_scopes") or []]
            if group == "workspace":
                workspace_permission = (
                    "workspace:file:write" if side_effect == "write" else "workspace:file:read"
                )
                if workspace_permission not in permissions:
                    permissions.append(workspace_permission)
            runtime_tool = runtime_tools.get(name)
            args_schema = getattr(runtime_tool, "args_schema", None)
            input_schema = (
                args_schema.model_json_schema()
                if args_schema is not None and hasattr(args_schema, "model_json_schema")
                else {"type": "object", "properties": {}}
            )
            tool = ToolContract(
                name=name,
                version="1.0.0",
                description=str(descriptor.get("description") or ""),
                input_schema=input_schema,
                permissions=permissions,
                side_effect=cast(Any, side_effect),
                approval="always" if descriptor.get("requires_approval") else "never",
                executor="builtin",
                group=group,
                risk_level=str(descriptor.get("risk_level") or "low"),
                boundary=str(descriptor.get("boundary") or "ksadk-runtime"),
                backend=str(descriptor.get("backend") or "") or None,
                enabled=bool(descriptor.get("enabled", True)),
            )
            resolved = self.resolver.resolve_tool(tool)
            resource = self._descriptor(
                kind="tool",
                source="builtin",
                name=tool.name,
                display_name=name,
                version=tool.version,
                description=tool.description,
                category=group,
                contract=resolved.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
            )
            if not tool.enabled:
                resource = resource.model_copy(update={"status": "unresolved"})
            yield resource

    def _local_skills(self) -> Iterable[ResourceDescriptor]:
        root = self.workspace.resolve("capabilities/skills")
        for directory in sorted(root.iterdir()):
            if not directory.is_dir() or directory.is_symlink():
                continue
            manifest_path = directory / "skill.yaml"
            manifest = load_yaml_file(manifest_path) if manifest_path.is_file() else {}
            version = str(manifest.get("version") or "unversioned")
            status: Literal["ready", "unresolved"]
            try:
                resolved = self.resolver.resolve_skill(
                    CapabilityRef(name=directory.name, version=version)
                )
                digest = str(resolved["digest"])
                status = "ready"
            except StudioError:
                digest = sha256_digest(directory.name.encode("utf-8"))
                status = "unresolved"
            yield ResourceDescriptor(
                resource_id=resource_id("skill", "local", directory.name, version),
                kind="skill",
                name=directory.name,
                display_name=str(
                    manifest.get("displayName") or manifest.get("name") or directory.name
                ),
                version=version,
                digest=digest,
                source="local",
                status=cast(Any, status),
                description=str(manifest.get("description") or ""),
                category=str(manifest.get("category") or "general"),
                installed=True,
                contract={
                    "name": directory.name,
                    "version": version,
                    "digest": digest,
                },
            )

    def _persisted(self, directory: str) -> Iterable[ResourceDescriptor]:
        root = self.workspace.resolve(Path(".agentkit/catalog") / directory)
        for path in sorted(root.glob("*.yaml")):
            try:
                yield ResourceDescriptor.model_validate(load_yaml_file(path))
            except (StudioError, ValidationError):
                continue

    def _persist_descriptor(
        self,
        directory: str,
        descriptor: ResourceDescriptor,
        *,
        overwrite: bool = False,
    ) -> ResourceDescriptor:
        target = self.workspace.resolve(
            Path(".agentkit/catalog")
            / directory
            / f"{resource_slug(descriptor.name)}-{resource_slug(descriptor.version)}.yaml"
        )
        if target.exists() and not overwrite:
            raise StudioError(
                "RESOURCE_ALREADY_EXISTS",
                "同名同版本资源已存在",
                status_code=409,
                details={"resourceId": descriptor.resource_id},
            )
        self.workspace.atomic_write_yaml(
            target,
            descriptor.model_dump(
                by_alias=True,
                exclude_none=True,
                mode="json",
            ),
        )
        return descriptor

    def _descriptor(
        self,
        *,
        kind: str,
        name: str,
        display_name: str,
        version: str,
        description: str,
        category: str,
        contract: dict[str, Any],
        source: str = "local",
        required_secret_refs: builtins.list[str] | None = None,
    ) -> ResourceDescriptor:
        digest = str(contract.get("digest") or sha256_digest(canonical_json(contract)))
        return ResourceDescriptor(
            resource_id=resource_id(kind, source, name, version),
            kind=cast(Any, kind),
            name=name,
            display_name=display_name,
            version=version,
            digest=digest,
            source=cast(Any, source),
            status="ready",
            description=description,
            category=category,
            required_secret_refs=required_secret_refs or [],
            contract=contract,
        )

    @staticmethod
    def _skill_entry(
        files: builtins.list[zipfile.ZipInfo],
    ) -> tuple[zipfile.ZipInfo, PurePosixPath]:
        candidates = [info for info in files if PurePosixPath(info.filename).name == "SKILL.md"]
        if len(candidates) != 1:
            raise StudioError(
                "SKILL_MANIFEST_REQUIRED",
                "Skill ZIP 必须且只能包含一个 SKILL.md",
                status_code=422,
            )
        path = PurePosixPath(candidates[0].filename)
        prefix = PurePosixPath(*path.parts[:-1])
        if len(prefix.parts) > 1:
            raise StudioError(
                "SKILL_ARCHIVE_INVALID",
                "SKILL.md 必须位于 ZIP 根目录或单一顶层目录",
                status_code=422,
            )
        return candidates[0], prefix

    @staticmethod
    def _skill_frontmatter(content: str) -> dict[str, Any]:
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

"""Capability resolution for local Skills, MCP servers, and Tool contracts."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Protocol, cast

from ksadk.studio.contracts import (
    BundleManifest,
    CapabilityRef,
    MCPServerRef,
    ModelSpec,
    ResolvedModel,
    ToolContract,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.repository import load_yaml_file
from ksadk.studio.workspace import Workspace

_EXACT_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


def canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def compute_bundle_digest(manifest: BundleManifest) -> str:
    """重算 BundleManifest 的 bundle_digest。

    必须与 AgentBundleBuilder 产出时逐字一致：对去掉 bundle_digest 字段后的
    canonical JSON 求 sha256。Runtime 加载 bundle 时据此校验 manifest 自身
    未被篡改，避免 builder 与 runtime 各持一份易漂移的计算逻辑。
    """
    payload = manifest.model_dump(
        by_alias=True,
        exclude={"bundle_digest"},
        exclude_none=True,
        mode="json",
    )
    return sha256_digest(canonical_json(payload))


def require_exact_version(version: str, *, field: str) -> None:
    if not _EXACT_SEMVER.fullmatch(version):
        raise StudioError(
            "CAPABILITY_VERSION_MUTABLE",
            "能力依赖必须使用确定的语义化版本",
            status_code=422,
            field=field,
            details={"version": version},
        )


class CapabilityResolver(Protocol):
    def resolve_model(self, spec: ModelSpec) -> ResolvedModel: ...

    def resolve_skill(self, ref: CapabilityRef) -> dict[str, Any]: ...

    def resolve_mcp(self, ref: MCPServerRef) -> dict[str, Any]: ...

    def resolve_tool(self, contract: ToolContract) -> ToolContract: ...


class LocalCapabilityResolver:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def resolve_model(self, spec: ModelSpec) -> ResolvedModel:
        endpoint = self._normalize_endpoint(spec)
        return ResolvedModel(
            provider=spec.provider,
            model=spec.model,
            endpoint_url=endpoint,
            credential_ref=spec.credential_ref,
            parameters=spec.parameters,
            wire_api=spec.wire_api,
        )

    def resolve_skill(self, ref: CapabilityRef) -> dict[str, Any]:
        require_exact_version(ref.version, field=f"spec.capabilities.skills[{ref.name}].version")
        root = self.workspace.resolve(Path("capabilities/skills") / ref.name)
        if not root.is_dir():
            raise StudioError(
                "CAPABILITY_UNRESOLVED",
                "本地 Skill 不存在",
                status_code=422,
                details={"kind": "skill", "name": ref.name},
            )
        digest = self._directory_digest(root)
        if ref.digest and ref.digest != digest:
            raise StudioError(
                "CAPABILITY_DIGEST_MISMATCH",
                "Skill 内容与声明 digest 不一致",
                status_code=422,
                details={"name": ref.name, "expected": ref.digest, "actual": digest},
            )
        manifest_path = root / "skill.yaml"
        manifest = load_yaml_file(manifest_path) if manifest_path.is_file() else {}
        instructions_file = str(manifest.get("instructionsFile") or "SKILL.md")
        instructions_path = self.workspace.resolve(root / instructions_file)
        instructions = (
            instructions_path.read_text(encoding="utf-8")
            if instructions_path.is_file()
            else ""
        )
        return {
            "name": ref.name,
            "version": ref.version,
            "digest": digest,
            "bundlePath": f"capabilities/skills/{ref.name}",
            "instructions": instructions,
        }

    def resolve_mcp(self, ref: MCPServerRef) -> dict[str, Any]:
        require_exact_version(
            ref.version,
            field=f"spec.capabilities.mcpServers[{ref.name}].version",
        )
        payload = ref.model_dump(by_alias=True, exclude_none=True, mode="json")
        payload.pop("digest", None)
        digest = sha256_digest(canonical_json(payload))
        if ref.digest and ref.digest != digest:
            raise StudioError(
                "CAPABILITY_DIGEST_MISMATCH",
                "MCP 配置与声明 digest 不一致",
                status_code=422,
                details={"name": ref.name, "expected": ref.digest, "actual": digest},
            )
        payload["digest"] = digest
        return cast(dict[str, Any], payload)

    def resolve_tool(self, contract: ToolContract) -> ToolContract:
        require_exact_version(
            contract.version,
            field=f"spec.capabilities.tools[{contract.name}].version",
        )
        payload = contract.model_dump(by_alias=True, exclude_none=True, mode="json")
        payload.pop("digest", None)
        digest = sha256_digest(canonical_json(payload))
        if contract.digest and contract.digest != digest:
            raise StudioError(
                "CAPABILITY_DIGEST_MISMATCH",
                "Tool 合同与声明 digest 不一致",
                status_code=422,
                details={"name": contract.name, "expected": contract.digest, "actual": digest},
            )
        return cast(
            ToolContract,
            contract.model_copy(update={"digest": digest}),
        )

    @staticmethod
    def _normalize_endpoint(spec: ModelSpec) -> str:
        if spec.endpoint_url:
            return spec.endpoint_url.rstrip("/")
        assert spec.base_url
        base = spec.base_url.rstrip("/")
        suffix = "/responses" if spec.wire_api == "responses" else "/chat/completions"
        return f"{base}{suffix}"

    def _directory_digest(self, root: Path) -> str:
        digest = hashlib.sha256()
        files = [path for path in root.rglob("*") if path.is_file() and not path.is_symlink()]
        for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
            relative = path.relative_to(root).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            content = path.read_bytes()
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
        return f"sha256:{digest.hexdigest()}"


def builtin_tool_contracts() -> dict[str, ToolContract]:
    return {
        "builtin.echo": ToolContract(
            name="builtin.echo",
            version="1.0.0",
            description="回显结构化输入",
            input_schema={"type": "object", "additionalProperties": True},
            output_schema={"type": "object", "additionalProperties": True},
            side_effect="none",
        ),
        "builtin.current_time": ToolContract(
            name="builtin.current_time",
            version="1.0.0",
            description="返回指定时区的当前时间",
            input_schema={
                "type": "object",
                "properties": {"timezone": {"type": "string"}},
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "required": ["iso8601", "timezone"],
                "properties": {
                    "iso8601": {"type": "string"},
                    "timezone": {"type": "string"},
                },
            },
            permissions=["system:time:read"],
            side_effect="read",
        ),
        "workspace.read": ToolContract(
            name="workspace.read",
            version="1.0.0",
            description="读取当前 Agent 工作区内的 UTF-8 文本文件",
            input_schema={
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string"},
                    "maxChars": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200000,
                        "default": 50000,
                    },
                },
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "required": ["path", "content", "truncated"],
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "truncated": {"type": "boolean"},
                },
            },
            permissions=["workspace:file:read"],
            side_effect="read",
        ),
        "workspace.write": ToolContract(
            name="workspace.write",
            version="1.0.0",
            description="在当前 Agent 工作区内写入 UTF-8 文本文件",
            input_schema={
                "type": "object",
                "required": ["path", "content"],
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string", "maxLength": 1000000},
                },
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "required": ["path", "bytesWritten"],
                "properties": {
                    "path": {"type": "string"},
                    "bytesWritten": {"type": "integer"},
                },
            },
            permissions=["workspace:file:write"],
            side_effect="write",
            approval="always",
        ),
        "workspace.edit": ToolContract(
            name="workspace.edit",
            version="1.0.0",
            description="在当前 Agent 工作区文本文件中执行精确替换",
            input_schema={
                "type": "object",
                "required": ["path", "oldText", "newText"],
                "properties": {
                    "path": {"type": "string"},
                    "oldText": {"type": "string", "minLength": 1},
                    "newText": {"type": "string"},
                    "replaceAll": {"type": "boolean", "default": False},
                },
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "required": ["path", "replacements"],
                "properties": {
                    "path": {"type": "string"},
                    "replacements": {"type": "integer"},
                },
            },
            permissions=["workspace:file:read", "workspace:file:write"],
            side_effect="write",
            approval="always",
        ),
        "workspace.glob": ToolContract(
            name="workspace.glob",
            version="1.0.0",
            description="按 glob pattern 列出当前 Agent 工作区文件",
            input_schema={
                "type": "object",
                "required": ["pattern"],
                "properties": {
                    "pattern": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1000,
                        "default": 200,
                    },
                },
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "required": ["matches", "truncated"],
                "properties": {
                    "matches": {"type": "array", "items": {"type": "string"}},
                    "truncated": {"type": "boolean"},
                },
            },
            permissions=["workspace:file:list"],
            side_effect="read",
        ),
        "workspace.grep": ToolContract(
            name="workspace.grep",
            version="1.0.0",
            description="在当前 Agent 工作区 UTF-8 文本文件中搜索字符串",
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "filePattern": {"type": "string", "default": "**/*"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1000,
                        "default": 200,
                    },
                },
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "required": ["matches", "truncated"],
                "properties": {
                    "matches": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["path", "line", "text"],
                            "properties": {
                                "path": {"type": "string"},
                                "line": {"type": "integer"},
                                "text": {"type": "string"},
                            },
                        },
                    },
                    "truncated": {"type": "boolean"},
                },
            },
            permissions=["workspace:file:read", "workspace:file:list"],
            side_effect="read",
        ),
    }

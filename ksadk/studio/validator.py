"""Static validation for Agent drafts and release contracts."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlparse

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]

from ksadk.studio.capabilities import require_exact_version
from ksadk.studio.contracts import (
    AgentDraft,
    Diagnostic,
    DiagnosticSeverity,
    ValidationResult,
)
from ksadk.studio.errors import StudioError

_SECRET_REF = re.compile(r"^(env|keychain|secret-manager)://[A-Za-z0-9_.:/-]+$")
_SUSPECT_SECRET = re.compile(r"^(sk-|AKLT|ASIA)[A-Za-z0-9_-]{8,}")


class AgentValidator:
    def validate(
        self,
        draft: AgentDraft,
        *,
        level: Literal["schema", "build", "release"] = "build",
        raise_on_error: bool = False,
    ) -> ValidationResult:
        diagnostics: list[Diagnostic] = []
        spec = draft.spec
        if not spec.instructions.system.strip():
            diagnostics.append(
                self._error(
                    "AGENT_SYSTEM_INSTRUCTION_REQUIRED",
                    "必须配置 system 指令",
                    "spec.instructions.system",
                )
            )
        if spec.model is None and not spec.bindings.model_profile_id:
            diagnostics.append(
                self._error("AGENT_MODEL_REQUIRED", "必须配置模型", "spec.model")
            )
        elif spec.model is not None:
            self._validate_model(draft, diagnostics)
        self._validate_capabilities(draft, diagnostics)
        if level == "release" and not spec.evaluation.suite_refs:
            diagnostics.append(
                self._error(
                    "EVALUATION_SUITE_REQUIRED",
                    "Release 校验要求至少一个评测集",
                    "spec.evaluation.suiteRefs",
                )
            )
        valid = not any(item.severity == DiagnosticSeverity.ERROR for item in diagnostics)
        result = ValidationResult(valid=valid, level=level, diagnostics=diagnostics)
        if raise_on_error and not valid:
            first = next(item for item in diagnostics if item.severity == DiagnosticSeverity.ERROR)
            raise StudioError(
                first.code,
                first.message,
                status_code=422,
                field=first.field,
                details={
                    "diagnostics": [
                        item.model_dump(by_alias=True, exclude_none=True, mode="json")
                        for item in diagnostics
                    ]
                },
            )
        return result

    def _validate_model(
        self,
        draft: AgentDraft,
        diagnostics: list[Diagnostic],
    ) -> None:
        model = draft.spec.model
        assert model is not None
        endpoint = model.endpoint_url or model.base_url or ""
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            diagnostics.append(
                self._error(
                    "MODEL_ENDPOINT_INVALID",
                    "模型地址必须是有效的 HTTP(S) URL",
                    "spec.model.endpointUrl",
                )
            )
        credential = model.credential_ref.strip()
        if _SUSPECT_SECRET.match(credential) or not _SECRET_REF.fullmatch(credential):
            diagnostics.append(
                self._error(
                    "SECRET_REFERENCE_INVALID",
                    "credentialRef 必须使用 env://、keychain:// 或 secret-manager:// 引用",
                    "spec.model.credentialRef",
                )
            )
        network = draft.spec.security.network
        if network.mode == "restricted" and parsed.hostname:
            allowed = {host.lower().rstrip(".") for host in network.allowed_hosts}
            if parsed.hostname.lower().rstrip(".") not in allowed:
                diagnostics.append(
                    self._error(
                        "NETWORK_TARGET_DENIED",
                        "模型 hostname 不在 security.network.allowedHosts 中",
                        "spec.security.network.allowedHosts",
                    )
                )

    def _validate_capabilities(
        self,
        draft: AgentDraft,
        diagnostics: list[Diagnostic],
    ) -> None:
        capabilities = draft.spec.capabilities
        names: set[str] = set()
        for kind, refs in (
            ("skills", capabilities.skills),
            ("mcpServers", capabilities.mcp_servers),
        ):
            for index, ref in enumerate(refs):
                try:
                    require_exact_version(
                        ref.version,
                        field=f"spec.capabilities.{kind}[{index}].version",
                    )
                except StudioError as exc:
                    diagnostics.append(self._error(exc.code, exc.message, exc.field))
                key = f"{kind}:{ref.name}"
                if key in names:
                    diagnostics.append(
                        self._error(
                            "CAPABILITY_DUPLICATE",
                            "能力名称重复",
                            f"spec.capabilities.{kind}[{index}].name",
                        )
                    )
                names.add(key)
                env_refs = getattr(ref, "env_refs", {})
                for env_name, secret_ref in env_refs.items():
                    if not _SECRET_REF.fullmatch(secret_ref):
                        diagnostics.append(
                            self._error(
                                "SECRET_REFERENCE_INVALID",
                                f"MCP 环境变量 {env_name} 必须使用 Secret 引用",
                                f"spec.capabilities.{kind}[{index}].envRefs.{env_name}",
                            )
                        )

        allowed_permissions = set(draft.spec.security.allowed_permissions)
        mcp_names = {ref.name for ref in capabilities.mcp_servers if ref.enabled}
        tool_names: set[str] = set()
        for index, tool in enumerate(capabilities.tools):
            try:
                require_exact_version(
                    tool.version,
                    field=f"spec.capabilities.tools[{index}].version",
                )
            except StudioError as exc:
                diagnostics.append(self._error(exc.code, exc.message, exc.field))
            if tool.name in tool_names:
                diagnostics.append(
                    self._error(
                        "CAPABILITY_DUPLICATE",
                        "Tool 名称重复",
                        f"spec.capabilities.tools[{index}].name",
                    )
                )
            tool_names.add(tool.name)
            for schema_name, schema in (
                ("inputSchema", tool.input_schema),
                ("outputSchema", tool.output_schema),
            ):
                try:
                    Draft202012Validator.check_schema(schema)
                except SchemaError as exc:
                    diagnostics.append(
                        self._error(
                            "TOOL_SCHEMA_INVALID",
                            f"Tool {schema_name} 不是合法 JSON Schema: {exc.message}",
                            f"spec.capabilities.tools[{index}].{schema_name}",
                        )
                    )
            missing = sorted(set(tool.permissions) - allowed_permissions)
            if missing:
                diagnostics.append(
                    self._error(
                        "TOOL_PERMISSION_DENIED",
                        "Tool 请求了 Agent 未允许的权限",
                        f"spec.capabilities.tools[{index}].permissions",
                        hint=f"缺少权限: {', '.join(missing)}",
                    )
                )
            if tool.side_effect in {"write", "external"} and tool.approval == "never":
                diagnostics.append(
                    self._warning(
                        "TOOL_AUTO_APPROVAL_RISK",
                        "有写入或外部副作用的 Tool 已配置为自动允许",
                        f"spec.capabilities.tools[{index}].approval",
                        hint="仅建议在受信任工作区使用宽松策略",
                    )
                )
            if tool.executor == "mcp" and tool.mcp_server not in mcp_names:
                diagnostics.append(
                    self._error(
                        "CAPABILITY_UNRESOLVED",
                        "MCP Tool 引用了未启用的 MCP Server",
                        f"spec.capabilities.tools[{index}].mcpServer",
                    )
                )

    @staticmethod
    def _error(
        code: str,
        message: str,
        field: str | None,
        *,
        hint: str | None = None,
    ) -> Diagnostic:
        return Diagnostic(
            severity=DiagnosticSeverity.ERROR,
            code=code,
            message=message,
            field=field,
            hint=hint,
        )

    @staticmethod
    def _warning(
        code: str,
        message: str,
        field: str | None,
        *,
        hint: str | None = None,
    ) -> Diagnostic:
        return Diagnostic(
            severity=DiagnosticSeverity.WARNING,
            code=code,
            message=message,
            field=field,
            hint=hint,
        )

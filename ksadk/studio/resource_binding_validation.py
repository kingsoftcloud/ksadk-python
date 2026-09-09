"""Static resource checks shared by Studio validation and the actual Build entry."""

from collections.abc import Sequence
from typing import Any

from ksadk.resource_runtime.plugin_config import resource_plugin_config
from ksadk.resource_runtime.snapshots import MemoryRecallPolicy
from ksadk.studio.contracts import Diagnostic, DiagnosticSeverity, MemorySpec
from ksadk.studio.errors import StudioError
from ksadk.studio.resource_connections import ResourceConnectionRepository


def resource_binding_diagnostics(
    bindings: Sequence[Any],
    memory: MemorySpec | None,
    connections: ResourceConnectionRepository,
) -> list[Diagnostic]:
    diagnostics = []
    for index, binding in enumerate(bindings):
        if not binding.enabled:
            continue
        config = resource_plugin_config(
            binding.plugin_ref,
            binding.ecosystem,
            binding.config,
            enabled=True,
        )
        if config is None:
            continue
        field = f"spec.bindings.plugins[{index}].config"
        try:
            connection = connections.get(config.binding.connection_ref)
            # Only confirm referenced secrets exist; no network or identity claims.
            connections.resolve_credentials(connection.target)
        except StudioError as error:
            diagnostics.append(
                Diagnostic(
                    severity=DiagnosticSeverity.ERROR,
                    code=error.code,
                    message=error.message,
                    field=field + ".binding.connectionRef",
                )
            )
        if (
            config.binding.resource.kind == "memory-instance"
            and memory is not None
            and memory.enabled
        ):
            if memory.provider_ref != f"binding://{config.binding.id}":
                diagnostics.append(
                    Diagnostic(
                        severity=DiagnosticSeverity.ERROR,
                        code="MEMORY_BINDING_MISMATCH",
                        message="记忆策略必须引用当前记忆插件绑定",
                        field="spec.memory.providerRef",
                    )
                )
            try:
                MemoryRecallPolicy.model_validate(memory.recall.model_dump(exclude={"enabled"}))
            except ValueError:
                diagnostics.append(
                    Diagnostic(
                        severity=DiagnosticSeverity.ERROR,
                        code="MEMORY_SEARCH_POLICY_UNSUPPORTED",
                        message="记忆后端不支持当前召回阈值或预算，请调整策略",
                        field="spec.memory.recall",
                    )
                )
        diagnostics.append(
            Diagnostic(
                severity=DiagnosticSeverity.WARNING,
                code="RESOURCE_AUTHORITY_UNVERIFIED",
                message="本次仅校验连接声明与凭证引用，尚未验证平台主体和资源权限",
                field=field + ".binding",
            )
        )
    return diagnostics

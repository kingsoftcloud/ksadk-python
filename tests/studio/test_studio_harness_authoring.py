"""Harness Runtime 授权体验：模板预置权限、显式 spec 语义、拒绝指引。"""

from __future__ import annotations

from pathlib import Path

from ksadk.studio.contracts import (
    AgentSpec,
    Instructions,
    ModelSpec,
    RuntimeRef,
    SecuritySpec,
)
from ksadk.studio.plugin_runtime import _permission_denial_details
from ksadk.studio.service import StudioService

HARNESS_PERMISSION = "process:host-user"


def _model() -> ModelSpec:
    return ModelSpec(
        model="fixture-model",
        endpoint_url="https://model.example.test/v1/chat/completions",
        credential_ref="env://MODEL_API_KEY",
    )


def test_template_default_harness_agent_preset_provider_permission(tmp_path: Path) -> None:
    studio = StudioService(tmp_path)
    draft = studio.create_studio_agent(
        agent_id="harness-default-agent",
        name="Harness Default",
        runtime=RuntimeRef(type="harness"),
    )
    assert HARNESS_PERMISSION in draft.spec.security.allowed_permissions


def test_explicit_spec_permissions_are_preserved(tmp_path: Path) -> None:
    studio = StudioService(tmp_path)
    draft = studio.create_studio_agent(
        agent_id="harness-explicit-agent",
        name="Harness Explicit",
        runtime=RuntimeRef(type="harness"),
        spec=AgentSpec(
            runtime=RuntimeRef(type="harness"),
            instructions=Instructions(system="explicit security choice"),
            model=_model(),
            security=SecuritySpec(allowed_permissions=[]),
        ),
    )
    assert draft.spec.security.allowed_permissions == []


def test_non_harness_runtime_does_not_get_preset_permission(tmp_path: Path) -> None:
    studio = StudioService(tmp_path)
    draft = studio.create_studio_agent(
        agent_id="legacy-langgraph-agent",
        name="Legacy LangGraph",
        runtime=RuntimeRef(
            type="langgraph",
            project_path="agents/legacy-langgraph/source",
            entry_point="graph.py",
            agent_variable="graph",
        ),
    )
    assert HARNESS_PERMISSION not in draft.spec.security.allowed_permissions


def test_authored_default_harness_agent_preset_provider_permission(tmp_path: Path) -> None:
    studio = StudioService(tmp_path)
    draft = studio.create_authored_agent(
        name="Authored Harness",
        runtime_type="harness",
    )
    assert HARNESS_PERMISSION in draft.spec.security.allowed_permissions


def test_permission_denial_error_carries_remediation_hint() -> None:
    """权限拒绝错误必须带缺失权限与修复指引（_preflight_bundle details）。"""

    from ksadk.plugins.host import PluginHostError

    error = PluginHostError(
        "plugin_permission_denied",
        "plugin io.ksadk.harness-provider@1.0.0 requests unapproved permissions: "
        "process:host-user",
    )
    details = _permission_denial_details(error)
    assert details["reason"] == "plugin_permission_denied"
    assert "process:host-user" in details["missingPermissions"]
    assert "重新构建" in details["hint"]


def test_harness_validator_rejects_unsupported_executor_before_chat(tmp_path: Path) -> None:
    from ksadk.studio.contracts import ToolContract
    from ksadk.studio.validator import AgentValidator

    studio = StudioService(tmp_path)
    draft = studio.create_studio_agent(
        agent_id="executor-check", name="Executor Check", runtime=RuntimeRef(type="harness")
    )
    draft.spec.capabilities.tools = [
        ToolContract(name="unavailable", executor="deferred", version="1.0.0")
    ]
    result = AgentValidator().validate(draft)
    assert any(item.code == "TOOL_RUNTIME_INCOMPATIBLE" for item in result.diagnostics)
    draft.spec.capabilities.tools = [
        ToolContract(name="edit_workspace_file", executor="builtin", version="1.0.0")
    ]
    result = AgentValidator().validate(draft)
    assert not any(item.code == "TOOL_RUNTIME_INCOMPATIBLE" for item in result.diagnostics)

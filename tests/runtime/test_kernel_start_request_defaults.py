from pathlib import Path
from types import SimpleNamespace

from ksadk.runtime.adapter import StartRequest
from ksadk.runtime.factory import (
    _create_codex,
    apply_runtime_start_request_defaults,
    kernel_start_request_defaults,
)
from ksadk.runtime.launch import RuntimeLaunchContext, RuntimeServices


def test_codex_manifest_ask_profile_becomes_native_manual_approval():
    context = RuntimeLaunchContext(
        runtime_type="codex",
        project_dir=Path("/tmp/managed-agent"),
        detection=SimpleNamespace(name="managed-agent"),
        config={
            "model": "qwen-test",
            "prompt": "system instructions",
            "task_prompt": "task instructions",
            "approval_mode": "ask",
        },
    )

    defaults = kernel_start_request_defaults(context)

    assert defaults["agent_id"] == "managed-agent"
    assert defaults["model"] == "qwen-test"
    # 只配默认 model 不产生 allowed_models:显式 models/allowedModels 才是白名单。
    # 否则单模型部署会把 run 级 model 覆盖锁死在默认模型上(RunAgent Model 透传 bug)。
    assert "allowed_models" not in defaults
    assert defaults["config"] == {
        "sandbox_read_only": False,
        "sandbox": "workspace-write",
        "approval_mode": "manual",
        "cwd": "/tmp/managed-agent",
        "summary": "auto",
        "ephemeral": False,
        "base_instructions": "system instructions\n\ntask instructions",
    }


def test_codex_manifest_without_approval_stays_fail_closed():
    context = RuntimeLaunchContext(
        runtime_type="codex",
        project_dir=Path("/tmp/managed-agent"),
        config={"sandbox": "read_only"},
    )

    defaults = kernel_start_request_defaults(context)

    assert defaults["config"]["sandbox"] == "read-only"
    assert defaults["config"]["approval_mode"] == "deny_all"


def test_manifest_projects_an_additive_model_allow_list():
    context = RuntimeLaunchContext(
        runtime_type="codex",
        project_dir=Path("/tmp/managed-agent"),
        config={
            "model": "default-model",
            "allowedModels": ["qwen3-coder-plus", "default-model", ""],
        },
    )

    defaults = kernel_start_request_defaults(context)

    assert defaults["model"] == "default-model"
    assert defaults["allowed_models"] == ["default-model", "qwen3-coder-plus"]


def test_manifest_models_are_the_canonical_model_allow_list():
    context = RuntimeLaunchContext(
        runtime_type="codex",
        project_dir=Path("/tmp/managed-agent"),
        config={
            "model": "qwen3.7-flash",
            "models": ["qwen3.7-flash", "glm-5.1"],
        },
    )

    defaults = kernel_start_request_defaults(context)

    assert defaults["allowed_models"] == ["glm-5.1", "qwen3.7-flash"]


def test_codex_adapter_uses_the_same_manifest_sandbox_projection():
    class FakeClient:
        def __init__(self, **_kwargs):
            pass

    context = RuntimeLaunchContext(
        runtime_type="codex",
        project_dir=Path("/tmp/managed-agent"),
        config={"approval_mode": "ask"},
        services=RuntimeServices(codex_client_factory=FakeClient),
    )

    adapter = _create_codex(context)

    assert adapter._sandbox_read_only is False


def test_direct_runtime_start_inherits_manifest_owned_instructions_and_model():
    context = RuntimeLaunchContext(
        runtime_type="codex",
        project_dir=Path("/tmp/managed-agent"),
        detection=SimpleNamespace(name="managed-agent"),
        config={
            "model": "deepseek-v4-flash",
            "models": ["deepseek-v4-flash", "glm-5.1"],
            "prompt": "你是销售日报助手。",
            "task_prompt": "数据不足时必须先追问。",
            "approval_mode": "risk",
        },
    )
    request = StartRequest(
        input="你好",
        user_id="user-1",
        session_id="session-1",
        agent_id="managed-agent",
        model="not-in-allow-list",
    )

    projected = apply_runtime_start_request_defaults(context, request)

    assert projected.model == "deepseek-v4-flash"
    assert projected.config["base_instructions"] == (
        "你是销售日报助手。\n\n数据不足时必须先追问。"
    )
    assert projected.config["sandbox"] == "workspace-write"
    assert projected.config["approval_mode"] == "auto_review"


def test_codex_adapter_projects_manifest_mcp_servers_into_native_config():
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    context = RuntimeLaunchContext(
        runtime_type="codex",
        project_dir=Path("/tmp/managed-agent"),
        config={
            "approval_mode": "risk",
            "mcp_servers": [
                {
                    "name": "metaso-inner",
                    "url": "https://mcp.example.test/mcp",
                    "env_key": "METASO_API_KEY",
                }
            ],
        },
        services=RuntimeServices(codex_client_factory=FakeClient),
    )

    _create_codex(context)

    overrides = tuple(captured["config"].config_overrides)
    assert "mcp_servers.metaso-inner.url=https://mcp.example.test/mcp" in overrides
    assert (
        "mcp_servers.metaso-inner.bearer_token_env_var=METASO_API_KEY" in overrides
    )
    assert "sandbox_workspace_write.network_access=true" in overrides

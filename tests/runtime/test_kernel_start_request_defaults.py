from pathlib import Path
from types import SimpleNamespace

from ksadk.runtime.factory import _create_codex, kernel_start_request_defaults
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

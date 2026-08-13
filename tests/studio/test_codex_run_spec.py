from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ksadk.studio.codex_builder import CodexStudioBuilder
from ksadk.studio.codex_manifest import CodexAgentManifest, CodexManifestRepository
from ksadk.studio.codex_run import CodexRunSpecResolver
from ksadk.studio.errors import StudioError
from ksadk.studio.workspace import Workspace


def _manifest(prompt: str = "检查 src/demo.py，只报告确定的问题。\n"):
    return CodexAgentManifest.model_validate(
        {
            "name": "review-helper",
            "version": "1.0.0",
            "runtime": {"name": "codex", "version": "0.144.4"},
            "model": "glm-5.2",
            "models": ["glm-5.2", "kimi-k2-code"],
            "prompt": prompt,
        }
    )


def _inspector(_runtime) -> tuple[str, str, str]:
    return "0.8.0", "0.144.4", "codex-cli 0.144.4"


def test_resolver_builds_canonical_codex_launch_context(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    CodexManifestRepository(workspace).save(_manifest())
    build = CodexStudioBuilder(workspace, runtime_inspector=_inspector).build()

    spec = CodexRunSpecResolver(workspace).resolve(
        build.id,
        model="kimi-k2-code",
    )

    assert spec.launch_context.runtime_type == "codex"
    assert spec.launch_context.project_dir == tmp_path
    assert spec.agent_id == "review-helper"
    assert spec.model == "kimi-k2-code"
    assert spec.request_config == {
        "base_instructions": _manifest().prompt,
        "cwd": str(tmp_path),
        "skills": [],
        "sandbox": "read-only",
        "sandbox_read_only": True,
        "approval_mode": "deny_all",
        "summary": "auto",
        "ephemeral": False,
    }


def test_resolver_injects_workspace_model_credentials_into_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a configured Studio model still launches Codex unauthenticated."""

    workspace = Workspace(tmp_path)
    workspace.initialize()
    workspace.atomic_write_text(
        ".agentkit/secrets.env",
        "AGENTKIT_MODEL_API_KEY=workspace-model-key\n",
    )
    monkeypatch.setenv("OPENAI_BASE_URL", "https://model.example.com/v1")
    CodexManifestRepository(workspace).save(_manifest())
    build = CodexStudioBuilder(workspace, runtime_inspector=_inspector).build()

    spec = CodexRunSpecResolver(workspace).resolve(build.id, model="glm-5.2")

    assert spec.launch_context.config["env"] == {
        "OPENAI_API_KEY": "workspace-model-key",
        "OPENAI_BASE_URL": "https://model.example.com/v1",
        "OPENAI_API_BASE": "https://model.example.com/v1",
        "OPENAI_MODEL_NAME": "glm-5.2",
    }


def test_resolver_keeps_selected_model_profile_endpoint_and_credential(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    CodexManifestRepository(workspace).save(_manifest())

    profiles = {
        "model-profile/glm": {
            "provider": "openai-compatible",
            "model": "glm-5.2",
            "endpointUrl": "https://glm.example.com/v1/responses",
            "wireApi": "responses",
            "credentialRef": "env://GLM_API_KEY",
        },
        "model-profile/kimi": {
            "provider": "openai-compatible",
            "model": "kimi-k2-code",
            "endpointUrl": "https://kimi.example.com/v1/chat/completions",
            "wireApi": "chat",
            "credentialRef": "env://KIMI_API_KEY",
        },
    }

    class Catalog:
        def get(self, resource_id: str):
            return SimpleNamespace(contract=profiles[resource_id])

        def list(self, **_kwargs):
            return []

    draft = SimpleNamespace(
        spec=SimpleNamespace(
            bindings=SimpleNamespace(
                model_profile_id="model-profile/glm",
                model_profile_ids=["model-profile/glm", "model-profile/kimi"],
            )
        )
    )

    class Credentials:
        def resolve(self, reference: str) -> str:
            return {
                "env://GLM_API_KEY": "glm-secret",
                "env://KIMI_API_KEY": "kimi-secret",
                "env://MUTATED_KIMI_API_KEY": "wrong-live-secret",
            }[reference]

    catalog = Catalog()
    drafts = SimpleNamespace(get=lambda _agent_id: draft)
    build = CodexStudioBuilder(
        workspace,
        runtime_inspector=_inspector,
        resource_catalog=catalog,
        draft_repository=drafts,
    ).build()

    assert build.model_profiles is not None
    assert build.model_profiles["glm-5.2"]["endpointUrl"] == (
        "https://glm.example.com/v1/responses"
    )
    assert build.model_profiles["kimi-k2-code"] == {
        "model": "kimi-k2-code",
        "endpointUrl": "https://kimi.example.com/v1/chat/completions",
        "wireApi": "chat",
        "credentialRef": "env://KIMI_API_KEY",
    }

    # A build is immutable: later Catalog edits must not silently reroute an
    # already-created build to a different gateway or credential reference.
    profiles["model-profile/kimi"] = {
        **profiles["model-profile/kimi"],
        "endpointUrl": "https://mutated.example.com/v1/responses",
        "credentialRef": "env://MUTATED_KIMI_API_KEY",
    }
    assert (
        CodexStudioBuilder(
            workspace,
            runtime_inspector=_inspector,
            resource_catalog=catalog,
            draft_repository=drafts,
        ).is_current(build)
        is False
    )
    rebuilt = CodexStudioBuilder(
        workspace,
        runtime_inspector=_inspector,
        resource_catalog=catalog,
        draft_repository=drafts,
    ).build()
    assert rebuilt.id != build.id

    resolver = CodexRunSpecResolver(
        workspace,
        credential_resolver=Credentials(),
        resource_catalog=catalog,
        draft_repository=drafts,
    )

    spec = resolver.resolve(build.id, model="kimi-k2-code")

    assert spec.launch_context.config["env"] == {
        "OPENAI_API_KEY": "kimi-secret",
        "OPENAI_BASE_URL": "https://kimi.example.com/v1",
        "OPENAI_API_BASE": "https://kimi.example.com/v1",
        "OPENAI_MODEL_NAME": "kimi-k2-code",
    }


@pytest.mark.parametrize(
    ("profile", "sandbox", "approval"),
    [
        ("ask", "workspace-write", "manual"),
        ("risk", "workspace-write", "auto_review"),
        ("full", "full-access", "deny_all"),
    ],
)
def test_resolver_maps_three_studio_approval_profiles(
    tmp_path: Path,
    profile: str,
    sandbox: str,
    approval: str,
) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    CodexManifestRepository(workspace).save(_manifest())
    build = CodexStudioBuilder(workspace, runtime_inspector=_inspector).build()

    spec = CodexRunSpecResolver(workspace).resolve(
        build.id,
        approval_mode=profile,
    )

    assert spec.launch_context.config["sandbox"] == sandbox
    assert spec.launch_context.config["approval_mode"] == approval
    assert spec.request_config["tool_approval_mode"] == profile
    if profile == "ask":
        # Manual approval is a per-thread wire setting (on-request + user
        # reviewer), not a process-global app-server override.
        assert "approval_policy=on-request" not in spec.launch_context.config["codex_overrides"]


def test_resolver_rejects_unbound_model_before_runtime_start(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    CodexManifestRepository(workspace).save(_manifest())
    build = CodexStudioBuilder(workspace, runtime_inspector=_inspector).build()

    with pytest.raises(StudioError) as captured:
        CodexRunSpecResolver(workspace).resolve(build.id, model="unbound")

    assert captured.value.code == "MODEL_NOT_BOUND"


def test_resolver_rejects_stale_build_after_manifest_edit(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    manifests = CodexManifestRepository(workspace)
    manifests.save(_manifest())
    build = CodexStudioBuilder(workspace, runtime_inspector=_inspector).build()
    manifests.save(_manifest("新 Prompt，必须重新构建。\n"))

    with pytest.raises(StudioError) as captured:
        CodexRunSpecResolver(workspace).resolve(build.id)

    assert captured.value.code == "CODEX_BUILD_STALE"

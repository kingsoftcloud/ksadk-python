from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml  # type: ignore[import-untyped]

from ksadk.studio.codex_builder import CodexStudioBuilder
from ksadk.studio.codex_manifest import CodexAgentManifest, CodexManifestRepository
from ksadk.studio.codex_run import CodexRunSpecResolver
from ksadk.studio.contracts import AgentSpec, Instructions, RuntimeRef, SoulDocument
from ksadk.studio.errors import StudioError
from ksadk.studio.service import StudioService
from ksadk.studio.soul import render_soul_markdown, soul_digest
from ksadk.studio.workspace import Workspace


def _manifest(
    prompt: str = "检查 src/demo.py，只报告确定的问题。\n",
    *,
    task_prompt: str | None = None,
):
    return CodexAgentManifest.model_validate(
        {
            "name": "review-helper",
            "version": "1.0.0",
            "runtime": {"name": "codex", "version": "0.144.4"},
            "model": "glm-5.2",
            "models": ["glm-5.2", "kimi-k2-code"],
            "prompt": prompt,
            **({"task_prompt": task_prompt} if task_prompt is not None else {}),
        }
    )


def _inspector(_runtime) -> tuple[str, str, str]:
    return "0.8.0", "0.144.4", "codex-cli 0.144.4"


def test_resolve_run_spec_does_not_mask_nested_codex_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    studio = StudioService(tmp_path)
    build_id = "build_0123456789abcdef0123"
    monkeypatch.setattr(studio.codex_builds, "get", lambda _build_id: object())

    def fail_resolution(_build_id: str, **_kwargs):
        raise StudioError(
            "CODEX_PLUGIN_SNAPSHOT_NOT_FOUND",
            "Codex 插件快照不存在",
            status_code=404,
        )

    monkeypatch.setattr(studio.codex_runs, "resolve", fail_resolution)

    with pytest.raises(StudioError) as raised:
        studio.resolve_run_spec(build_id)

    assert raised.value.code == "CODEX_PLUGIN_SNAPSHOT_NOT_FOUND"


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
        "agent_system": _manifest().prompt,
        "agent_task": "",
        "cwd": str(tmp_path),
        "skills": [],
        "sandbox": "workspace-write",
        "sandbox_read_only": False,
        "approval_mode": "auto_review",
        "summary": "auto",
        "ephemeral": False,
        "max_input_tokens": None,
        "reserve_output_tokens": None,
        "context_engine_rollout": None,
        "memory_recall_enabled": None,
        "memory_recall_top_k": None,
        "memory_recall_max_tokens": None,
        "memory_recall_min_score": None,
        "memory_write_rollout": None,
        "memory_enabled": False,
        "memory_write_mode": "candidate",
        "flush_before_compaction": True,
        "provider_ref": "local-default",
    }


def test_resolver_merges_codex_input_but_preserves_prompt_sources(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    manifest = _manifest(
        "你是代码审查助手。",
        task_prompt="只报告有证据的问题。",
    )
    CodexManifestRepository(workspace).save(manifest)
    build = CodexStudioBuilder(workspace, runtime_inspector=_inspector).build()

    spec = CodexRunSpecResolver(workspace).resolve(build.id)

    assert spec.request_config["base_instructions"] == (
        "你是代码审查助手。\n\n只报告有证据的问题。"
    )
    assert spec.request_config["agent_system"] == "你是代码审查助手。"
    assert spec.request_config["agent_task"] == "只报告有证据的问题。"


def test_editor_soul_update_reaches_managed_runtime_build_and_launch(
    tmp_path: Path,
) -> None:
    """Break caught: Studio retained Soul only in Draft, not deployed Codex input."""

    studio = StudioService(tmp_path, codex_runtime_inspector=_inspector)
    # This test validates Soul projection into the retained direct-runtime
    # compatibility artifact. Formal Provider Build coverage lives in
    # test_codex_provider_build.py and installs an explicit provider fixture.
    studio.codex_builder.provider_build = None
    draft = studio.create_studio_agent(
        agent_id="soul-reviewer",
        name="Soul Reviewer",
        spec=AgentSpec(
            runtime=RuntimeRef(type="codex", version="0.144.4"),
            instructions=Instructions(
                system="Review only verified evidence.",
                task="Return one concise finding.",
            ),
        ),
    )
    candidate = draft.spec.model_copy(deep=True)
    candidate.soul = SoulDocument(
        identity="You are the release evidence reviewer.",
        principles=["Cite evidence before conclusions."],
        boundaries=["Never invent a passing result."],
        tone="Clear and concise.",
    )
    updated = studio.update_studio_agent(
        draft.metadata.id,
        candidate,
        expected_revision=draft.metadata.revision,
    )

    build = studio.codex_builder.build(
        draft.metadata.id,
        source_revision=updated.metadata.revision,
    )
    artifact = yaml.safe_load(studio.codex_builds.manifest_text(build))
    run_spec = studio.resolve_run_spec(build.id)
    soul = candidate.soul
    assert soul is not None
    expected_digest = soul_digest(soul)
    expected_system = f"{render_soul_markdown(soul).rstrip()}\n\nReview only verified evidence."

    assert artifact["prompt"] == "Review only verified evidence."
    assert artifact["soul"]["identity"] == "You are the release evidence reviewer."
    assert artifact["soul_source"] == "AgentSpec.soul"
    assert artifact["soul_digest"] == expected_digest
    assert run_spec.request_config["agent_system"] == expected_system
    assert run_spec.request_config["base_instructions"] == (
        f"{expected_system}\n\nReturn one concise finding."
    )
    assert run_spec.request_config["soul_source"] == "AgentSpec.soul"
    assert run_spec.request_config["soul_digest"] == expected_digest


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
        "KSADK_CODEX_USE_PROXY": "1",
    }


def test_resolver_skips_capability_probe_for_legacy_kspmas_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    monkeypatch.delenv("KSADK_CODEX_USE_PROXY", raising=False)
    workspace.atomic_write_text(
        ".agentkit/secrets.env",
        "AGENTKIT_MODEL_API_KEY=workspace-model-key\n",
    )
    monkeypatch.setenv("OPENAI_BASE_URL", "http://kspmas.ksyun.com/v1")
    CodexManifestRepository(workspace).save(_manifest())
    build = CodexStudioBuilder(workspace, runtime_inspector=_inspector).build()

    spec = CodexRunSpecResolver(workspace).resolve(build.id, model="glm-5.2")

    assert spec.launch_context.config["env"]["KSADK_CODEX_USE_PROXY"] == "1"


@pytest.mark.parametrize(
    ("profile", "sandbox", "approval"),
    [
        ("ask", "workspace-write", "manual"),
        ("risk", "workspace-write", "manual"),
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


def test_rebuild_preserves_only_bound_same_agent_mcp_oauth(tmp_path: Path, monkeypatch) -> None:
    import json
    import stat

    monkeypatch.delenv("KSADK_CODEX_HOME", raising=False)
    workspace = Workspace(tmp_path)
    workspace.initialize()
    manifests = CodexManifestRepository(workspace)
    builder = CodexStudioBuilder(workspace, runtime_inspector=_inspector)
    manifests.save(_manifest())
    original = builder.build()
    old_home = tmp_path / ".agentkit/codex-homes" / original.id
    old_home.mkdir(parents=True)
    grant = {
        "server_name": "design",
        "server_url": "https://design.example/mcp",
        "access_token": "fake-token",
    }
    (old_home / ".credentials.json").write_text(
        json.dumps(
            {
                "design|hash": grant,
                "unbound|hash": {**grant, "server_name": "unbound"},
                "changed|hash": {**grant, "server_url": "https://other.example/mcp"},
            }
        )
    )
    (old_home / "auth.json").write_text('{"never_copy": true}')
    updated = _manifest().model_copy(
        update={
            "mcp_servers": [
                {"name": "design", "transport": "http", "url": "https://design.example/mcp"},
            ]
        }
    )
    manifests.save(updated)
    build = builder.build()
    resolver = CodexRunSpecResolver(workspace)
    foreign = original.model_copy(update={"id": "build_abcdef01", "agent_name": "other-agent"})
    foreign_home = old_home.parent / foreign.id
    foreign_home.mkdir()
    (foreign_home / ".credentials.json").write_text(json.dumps({"foreign|hash": grant}))
    monkeypatch.setattr(resolver.builds, "list", lambda: [original, build, foreign])
    resolver.resolve(build.id)
    target = tmp_path / ".agentkit/codex-homes" / build.id / ".credentials.json"
    assert target.exists(), "OAuth grant disappeared when the MCP binding changed the build"
    assert json.loads(target.read_text()) == {"design|hash": grant}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not (target.parent / "auth.json").exists()
    # A refreshed token in the current build must not be overwritten by old state.
    fresh = {**grant, "access_token": "fake-refreshed"}
    target.write_text(json.dumps({"design|hash": fresh}))
    resolver.resolve(build.id)
    assert json.loads(target.read_text()) == {"design|hash": fresh}

    # Logging out in this build must not recover a stale grant on the next run.
    target.unlink()
    resolver.resolve(build.id)
    assert not target.exists()
    # A subsequent build must not search past this logged-out predecessor.
    manifests.save(updated.model_copy(update={"prompt": "Next revision"}))
    next_build = builder.build()
    monkeypatch.setattr(resolver.builds, "list", lambda: [original, build, next_build, foreign])
    resolver.resolve(next_build.id)
    next_target = target.parent.parent / next_build.id / ".credentials.json"
    assert not next_target.exists()

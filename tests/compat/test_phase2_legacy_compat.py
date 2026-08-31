"""Phase 2 compatibility gate for pre-plugin local Agents.

These tests exercise the established Studio/runtime paths.  They deliberately
do not construct a PluginHost or a PostgreSQL service: a historical 0.8.2
Bundle and a local Runtime must remain usable without either dependency.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from ksadk.kernel import ingress
from ksadk.kernel.bootstrap import clear_agent_kernel_runtime
from ksadk.server.composition import configure_runtime_app
from ksadk.server.factory import RuntimeAppConfig, create_runtime_app
from ksadk.sessions.local_service import LocalSessionService
from ksadk.studio.capabilities import compute_bundle_digest
from ksadk.studio.contracts import (
    AgentSpec,
    BundleManifest,
    Instructions,
    ModelSpec,
    NetworkPolicy,
    RuntimeRef,
    SecuritySpec,
)
from ksadk.studio.service import StudioService


def _build_langgraph_bundle(workspace: Path):
    studio = StudioService(workspace)
    draft = studio.create_studio_agent(
        agent_id="legacy-082-agent",
        name="Legacy 0.8.2 Agent",
        spec=AgentSpec(
            runtime=RuntimeRef(
                type="langgraph",
                project_path="agents/legacy-082-agent/source",
                entry_point="agent.py",
                agent_variable="graph",
            ),
            model=ModelSpec(
                model="model-example",
                endpoint_url="https://model.example.com/v1/chat/completions",
                credential_ref="env://MODEL_API_KEY",
            ),
            instructions=Instructions(system="Keep the historical role."),
            security=SecuritySpec(
                network=NetworkPolicy(allowed_hosts=["model.example.com"])
            ),
        ),
    )
    build = studio.builder.build(draft)
    archive = studio.workspace.resolve(build.artifact_path, must_exist=True)
    return studio, build, archive.parent / "agent-bundle"


def _rewrite_as_historical_v1(studio: StudioService, build, bundle_root: Path) -> None:
    # Construct the historical shape from its stable v1 members instead of
    # naming today's v2-only sidecars.  If Phase 2 grows another sidecar later,
    # this fixture cannot accidentally start accepting it as part of v1.
    legacy_files = {
        "agentkit.lock",
        "checksums.txt",
        "manifest.json",
        "resolved-agent-spec.json",
        "runtime-lock.json",
        "sbom.spdx.json",
    }
    legacy_prefixes = ("instructions/", "runtime/")
    for path in sorted(bundle_root.rglob("*"), reverse=True):
        if not path.is_file():
            continue
        relative = path.relative_to(bundle_root).as_posix()
        if relative in legacy_files or relative.startswith(legacy_prefixes):
            continue
        path.unlink()

    checksum_path = bundle_root / "checksums.txt"
    checksums: list[str] = []
    for path in sorted(bundle_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(bundle_root).as_posix()
        if relative in {"manifest.json", "checksums.txt"}:
            continue
        checksums.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}")
    checksum_path.write_text("\n".join(checksums) + "\n", encoding="utf-8")

    files: list[dict[str, object]] = []
    for path in sorted(bundle_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(bundle_root).as_posix()
        if relative == "manifest.json":
            continue
        content = path.read_bytes()
        files.append(
            {
                "path": relative,
                "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
                "size": len(content),
            }
        )

    manifest = BundleManifest(
        bundle_format="agentkit.bundle/v1",
        agent_id=build.agent_id,
        source_revision=build.source_revision,
        resolved_digest=build.resolved_digest,
        files=files,
    )
    manifest.bundle_digest = compute_bundle_digest(manifest)
    wire = manifest.model_dump(by_alias=True, exclude_none=True, mode="json")
    for field in (
        "runtimeType",
        "sourceDigest",
        "runtimeContract",
        "pluginLockDigest",
        "compositionProfileDigest",
        "hostedKernelRequirementDigest",
    ):
        wire.pop(field, None)
    (bundle_root / "manifest.json").write_text(
        json.dumps(wire, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    build.bundle_digest = manifest.bundle_digest
    studio.builds.save(build)


def test_082_bundle_without_plugin_manifest_uses_legacy_studio_path(tmp_path: Path) -> None:
    studio, build, bundle_root = _build_langgraph_bundle(tmp_path)
    _rewrite_as_historical_v1(studio, build, bundle_root)

    assert not (bundle_root / "ksadk-plugin.yaml").exists()
    assert not (bundle_root / "plugin-lock.json").exists()
    assert not any("plugin" in path.name.lower() for path in bundle_root.rglob("*"))

    # Management views must still find the historical Build.
    managed = studio.build_view(build.id)
    assert managed.id == build.id
    assert any(item["id"] == build.id for item in studio.evaluation_catalog()["builds"])

    # Runtime selection must remain on the established framework resolver;
    # PluginHost is only an admission requirement for composed Bundle v2.
    run_spec = studio.resolve_run_spec(build.id)
    assert run_spec.launch_context.runtime_type == "langgraph"
    assert run_spec.request_config["agent_system"] == "Keep the historical role."


def test_local_runtime_starts_and_manages_sessions_without_kernel_or_postgres(
    monkeypatch, tmp_path: Path
) -> None:
    for name in (
        "AGENT_KERNEL_ENABLED",
        "KSADK_AGENT_KERNEL",
        "AGENT_KERNEL_STORE_DSN",
        "KSADK_SESSION_DSN",
        "KSADK_STM_URL",
        "KSADK_STM_DB_URL",
        "AGENTENGINE_SESSION_BACKEND",
        "KSADK_STM_BACKEND",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KSADK_SESSION_BACKEND", "local")
    monkeypatch.setenv("KSADK_SESSION_PATH", str(tmp_path / "sessions.sqlite"))

    clear_agent_kernel_runtime()
    ingress.clear_agent_kernel()
    app = create_runtime_app(RuntimeAppConfig(), configure_runtime_app)
    try:
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            assert app.state.agent_kernel_runtime is None
            assert isinstance(app.state.runtime.resolve_session_service(), LocalSessionService)
            assert app.state.runtime.describe_session_backend()["Backend"] == "local"

            created = client.post(
                "/agentengine/api/v1/CreateSession",
                json={
                    "AgentId": "legacy-082-agent",
                    "UserId": "local-user",
                    "SessionId": "legacy-session",
                },
            )
            assert created.status_code == 200
            listed = client.post(
                "/agentengine/api/v1/ListSessions",
                json={"AgentId": "legacy-082-agent", "UserId": "local-user"},
            )
            assert listed.status_code == 200
            assert listed.json()["Data"]["Total"] == 1
    finally:
        clear_agent_kernel_runtime()
        ingress.clear_agent_kernel()

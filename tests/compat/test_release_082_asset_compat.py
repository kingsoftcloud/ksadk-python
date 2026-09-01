"""Compatibility gate backed by an AgentBundle built by the v0.8.2 release.

Unlike the synthetic v1 downgrade fixture, this asset is byte-for-byte output
from ``AgentBundleBuilder`` at the annotated public ``v0.8.2`` tag.  Its
adjacent provenance file pins the tag commit, historical builder hashes, input
Agent, BuildRecord, archive hash, and generation procedure.

This is a local compatibility proof.  It does not replace deployment or cloud
management E2E against a released image and Server/Operator build.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from importlib.metadata import version
from pathlib import Path

import pytest

from ksadk.kernel import ingress
from ksadk.kernel.bootstrap import clear_agent_kernel_runtime
from ksadk.studio.cloud import DirectAgentEngineCloudDeploymentGateway, InMemoryCloudGateway
from ksadk.studio.contracts import (
    BuildRecord,
    DeploymentRecord,
    DeploymentTarget,
    RunStatus,
)
from ksadk.studio.repository import BuildRepository
from ksadk.studio.service import StudioService
from ksadk.studio.workspace import Workspace
from tests.e2e.codex_responses_stub import DeterministicResponsesStub

_FIXTURE_ROOT = Path(__file__).parent / "fixtures"
_ARCHIVE_B64 = _FIXTURE_ROOT / "v0.8.2-agent-bundle.zip.b64"
_PROVENANCE = _FIXTURE_ROOT / "v0.8.2-agent-bundle.provenance.json"
_MANAGED_RUNTIME = _FIXTURE_ROOT / "v0.8.2-managed-runtime-agentengine.yaml"
_MANAGED_RUNTIME_PROVENANCE = (
    _FIXTURE_ROOT / "v0.8.2-managed-runtime-agentengine.provenance.json"
)


def _fixture() -> tuple[bytes, dict]:
    provenance = json.loads(_PROVENANCE.read_text(encoding="utf-8"))
    archive = base64.b64decode(_ARCHIVE_B64.read_text(encoding="ascii").strip(), validate=True)
    assert hashlib.sha256(archive).hexdigest() == provenance["archiveSha256"]
    assert provenance["sourceTag"] == "v0.8.2"
    assert provenance["sourceCommit"] == "c8c9be629f4cb054ec4d8818cf0596ef42377671"
    return archive, provenance


def _install_release_build(tmp_path: Path) -> tuple[StudioService, BuildRecord, bytes]:
    archive, provenance = _fixture()
    workspace = Workspace(tmp_path)
    workspace.initialize()
    build = BuildRecord.model_validate(provenance["buildRecord"])
    artifact = workspace.resolve(build.artifact_path or "")
    artifact.parent.mkdir(parents=True, exist_ok=False)
    artifact.write_bytes(archive)
    bundle_root = artifact.parent / "agent-bundle"
    bundle_root.mkdir()
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        for member in bundle.infolist():
            path = Path(member.filename)
            assert not path.is_absolute() and ".." not in path.parts
        bundle.extractall(bundle_root)
    BuildRepository(workspace).save(build)
    return StudioService(tmp_path), build, archive


def test_release_082_asset_is_traceable_and_has_no_plugin_manifest() -> None:
    archive, provenance = _fixture()
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        names = set(bundle.namelist())
        manifest = json.loads(bundle.read("manifest.json"))
        plugin_lock = json.loads(bundle.read("plugin-lock.json"))
        source = bundle.read("runtime/agent.py").decode("utf-8")

    # v0.8.2 already emitted Bundle v2.  The retained synthetic v1 fixture
    # therefore covers still older builds; it must not be presented as a
    # byte-for-byte 0.8.2 artifact.
    assert manifest["bundleFormat"] == "agentkit.bundle/v2"
    assert manifest["bundleDigest"] == provenance["buildRecord"]["bundleDigest"]
    assert plugin_lock == {"lockFormat": "agentkit.plugin-lock/v1", "plugins": []}
    assert "ksadk-plugin.yaml" not in names
    assert "plugin-manifest.json" not in names
    assert not any(name.endswith("/_bundle_identity.py") for name in names)
    assert source == provenance["sourceAgent"]["sourceFiles"]["agent.py"]


@pytest.mark.asyncio
async def test_current_runtime_opens_and_runs_release_082_code_bundle_without_kernel_or_pg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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

    try:
        studio, build, _archive = _install_release_build(tmp_path)
        assert studio.build_view(build.id).bundle_digest == build.bundle_digest
        assert any(item["id"] == build.id for item in studio.evaluation_catalog()["builds"])

        run_spec = studio.resolve_run_spec(build.id)
        assert run_spec.launch_context.runtime_type == "langgraph"
        assert run_spec.plugin_bundle_root is None
        assert run_spec.request_config["agent_system"] == "Preserve the 0.8.2 role."

        completed = await studio.run_service.run(
            run_spec,
            "hello",
            session_id="release-082-compat-session",
        )
        assert completed.status == RunStatus.COMPLETED
        assert completed.output == "hello from release 0.8.2"
        assert ingress.get_agent_kernel() is None
    finally:
        clear_agent_kernel_runtime()
        ingress.clear_agent_kernel()


@pytest.mark.asyncio
async def test_legacy_management_routes_remain_open() -> None:
    code = DirectAgentEngineCloudDeploymentGateway._account_agent_view(
        {
            "agent_id": "ar-existing-code",
            "framework": "langgraph",
            "capabilities": {"session_event_chat": {"enabled": True}},
        }
    )
    native = DirectAgentEngineCloudDeploymentGateway._account_agent_view(
        {
            "agent_id": "ar-existing-native",
            "runtime_kind": "openclaw",
        }
    )
    assert code["chatTransport"] == "studio-session-events"
    assert native["chatTransport"] == "official-dashboard"

    # Old receipts omit Phase 2 fields. Their defaults must keep management
    # and the native/ManagedRuntime Dashboard path usable without admission.
    managed = DeploymentRecord(
        id="dep-existing-managed",
        build_id="build-existing-managed",
        bundle_digest="sha256:" + "a" * 64,
        version_id="managed-aaaaaaaaaaaaaaaa",
        status="READY",
        target=DeploymentTarget(region="test-region", environment="test"),
        agent_id="ar-existing-managed",
        artifact_id="managed-runtime",
    )
    assert managed.requires_kernel is False
    access = await InMemoryCloudGateway().get_deployment_dashboard_access(managed)
    assert access["access_url"] == "memory://dashboard/ar-existing-managed"


def _unused_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_json(url: str, process: subprocess.Popen[str]) -> dict:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read().strip() if process.stdout is not None else ""
            pytest.fail(
                "historical ManagedRuntime exited before health check "
                f"(code={process.returncode}): {output or '(no output)'}"
            )
        try:
            with urllib.request.urlopen(url, timeout=1) as response:  # noqa: S310
                payload = json.loads(response.read())
            assert isinstance(payload, dict)
            return payload
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            time.sleep(0.2)
    pytest.fail("historical ManagedRuntime did not become healthy within 30 seconds")


def _post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(  # noqa: S310
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
            result = json.loads(response.read())
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise AssertionError(f"HTTP {error.code} from {url}: {body}") from error
    assert isinstance(result, dict)
    return result


def _response_text(payload: dict) -> str:
    texts: list[str] = []
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        for part in item.get("content", []):
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
    return "".join(texts)


def test_release_082_managed_runtime_runs_real_codex_binary_without_pluginhost(
    tmp_path: Path,
) -> None:
    """Run the frozen pre-Phase-2 YAML through the current native binary."""

    pytest.importorskip(
        "codex_cli_bin",
        reason="install the codex extra to run native ManagedRuntime compatibility",
    )
    provenance = json.loads(_MANAGED_RUNTIME_PROVENANCE.read_text(encoding="utf-8"))
    manifest = _MANAGED_RUNTIME.read_bytes()
    assert hashlib.sha256(manifest).hexdigest() == provenance["fixtureSha256"]
    assert provenance["sourceTag"] == "v0.8.2"
    assert provenance["sourceCommit"] == "c8c9be629f4cb054ec4d8818cf0596ef42377671"
    assert version("openai-codex") == provenance["openaiCodexVersion"]

    (tmp_path / "agentengine.yaml").write_bytes(manifest)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    port = _unused_local_port()
    env = dict(os.environ)
    env.update(
        {
            "AGENT_KERNEL_ENABLED": "0",
            "KSADK_CODEX_HOME": str(codex_home),
            "KSADK_CODEX_ISOLATE_HOME": "1",
            "KSADK_SESSION_BACKEND": "local",
            "KSADK_SESSION_PATH": str(tmp_path / "sessions.sqlite"),
            "PYTHONUTF8": "1",
        }
    )

    with DeterministicResponsesStub() as responses:
        env.update(
            {
                "KSADK_CODEX_USE_PROXY": "0",
                "OPENAI_API_BASE": responses.base_url,
                "OPENAI_API_KEY": "release-082-local-stub",
                "OPENAI_BASE_URL": responses.base_url,
                "OPENAI_MODEL_NAME": "fixture-codex-model",
            }
        )
        (codex_home / "config.toml").write_text(
            f'''model_provider = "release_082_stub"
approval_policy = "never"
sandbox_mode = "read-only"

[model_providers.release_082_stub]
name = "Release 0.8.2 deterministic compatibility"
base_url = "{responses.base_url}"
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
requires_openai_auth = false
''',
            encoding="utf-8",
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "ksadk.cli",
                "web",
                str(tmp_path),
                "--port",
                str(port),
                "--no-open",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        failure: Exception | None = None
        try:
            health = _wait_for_json(f"http://127.0.0.1:{port}/health", process)
            assert health["status"] == "ok"
            assert health["framework"] == "codex"
            first = _post_json(
                f"http://127.0.0.1:{port}/v1/responses",
                {
                    "model": "fixture-codex-model",
                    "input": "first historical native turn",
                    "conversation": "release-082-native-session",
                },
            )
            second = _post_json(
                f"http://127.0.0.1:{port}/v1/responses",
                {
                    "model": "fixture-codex-model",
                    "input": "second historical native turn",
                    "conversation": "release-082-native-session",
                },
            )
            assert _response_text(first) == "bridge skill received"
            assert _response_text(second) == "bridge skill received"
        except Exception as error:
            failure = error
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        output = process.stdout.read().strip() if process.stdout is not None else ""
        if failure is not None:
            raise AssertionError(
                f"historical ManagedRuntime request failed: {failure}\n{output}"
            ) from failure

    requests = responses.requests()
    assert len(requests) == 2
    assert all(
        "Preserve the release 0.8.2 ManagedRuntime role."
        in str(request.payload.get("instructions") or "")
        for request in requests
    )
    assert (
        requests[0].payload["client_metadata"]["thread_id"]
        == requests[1].payload["client_metadata"]["thread_id"]
    )
    assert "bridge skill received" in requests[1].input_texts("assistant")
    assert process.returncode is not None
    assert not any(path.name == "plugin-lock.json" for path in tmp_path.rglob("*"))

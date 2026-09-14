"""Opt-in wheel/clean-environment acceptance, without publishing or real credentials."""
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from tests.resource_runtime.test_worker_process import upstream as upstream
from tests.test_runtime_common_packaging import REPO_ROOT, _build_wheel_in_isolated_source

pytestmark = pytest.mark.skipif(
    os.environ.get("KSADK_TEST_RESOURCE_WHEEL") != "1",
    reason="Set KSADK_TEST_RESOURCE_WHEEL=1 for isolated wheel build and dependency installation",
)


@pytest.fixture(scope="module")
def distribution(tmp_path_factory):
    root = tmp_path_factory.mktemp("resource-wheel")
    wheel = _build_wheel_in_isolated_source(root, include_static=True)
    return root, wheel


def test_wheel_contains_current_runtime_and_official_bundle_bytes(distribution):
    _, wheel = distribution
    sources = list((REPO_ROOT / "ksadk/resource_runtime").glob("*.py"))
    bundle_root = REPO_ROOT / "ksadk/plugins/providers/bundles"
    for name in (
        "dsh-platform-resources", "dsh-knowledge", "dsh-memory", "dsh-skill-center",
        "ksadk-dsh-capability-host",
    ):
        sources.extend(path for path in (bundle_root / name).rglob("*") if path.is_file())
    sources.extend(REPO_ROOT / path for path in (
        "ksadk/studio/resource_build_materializer.py",
        "ksadk/studio/resource_connections.py",
        "ksadk/studio/api_resource_connections.py",
        "ksadk/skills/runtime/artifact_delivery.py",
        "ksadk/sandbox/e2b_connection.py",
    ))
    with zipfile.ZipFile(wheel) as package:
        names = package.namelist()
        assert "ksadk/server/static/index.html" in names
        assert "ksadk/studio/static/index.html" in names
        assert not any("/node_modules/" in name or name.startswith("tests/") for name in names)
        for source in sources:
            relative = source.relative_to(REPO_ROOT).as_posix()
            assert package.read(relative) == source.read_bytes(), relative


def test_clean_wheel_starts_resource_worker_and_plugin_cli(distribution, upstream):
    root, wheel = distribution
    endpoint, calls = upstream
    constraints = root / "constraints.txt"
    subprocess.run([
        "uv", "export", "--locked", "--no-dev", "--extra", "langgraph",
        "--no-emit-project", "--no-hashes", "--output-file", str(constraints),
    ], cwd=REPO_ROOT, check=True, capture_output=True, text=True)
    environment = root / "venv"
    subprocess.run([
        "uv", "venv", "--python", sys.executable, str(environment),
    ], cwd=root, check=True, capture_output=True, text=True)
    python = environment / "bin/python"
    installed = subprocess.run([
        "uv", "pip", "install", "--python", str(python),
        "--constraint", str(constraints), f"{wheel}[langgraph]",
    ], cwd=root, capture_output=True, text=True, timeout=180)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    # No user credential, PYTHONPATH, plugin directory or source checkout is
    # inherited by the installed CLI or its worker subprocess.
    clean_env = {
        name: os.environ[name] for name in ("PATH", "LANG", "LC_ALL") if name in os.environ
    }
    script = root / "smoke.py"
    script.write_text('''
import asyncio
import json
import sys
import time
from pathlib import Path
import ksadk
from ksadk.resource_runtime.ipc import ResourceRequest
from ksadk.resource_runtime.langgraph import create_bound_resource_tools
from ksadk.resource_runtime.process import ResourceWorkerProcess
from ksadk.resource_runtime.worker import WorkerInitialization
from ksadk.resource_runtime.snapshots import ResourceSnapshot
from ksadk.resource_runtime.leases import ResourceScope

config = {"binding": {"id": "kb", "connectionRef": "connection",
    "resource": {"kind": "knowledge-base", "id": "fixture-kb", "region": "fixture-region"}}}
snapshot = ResourceSnapshot.model_validate({"pluginLockDigest": "sha256:" + "a" * 64,
    "bindings": [{"config": config, "connection": {"connectionRef": "connection",
        "tenantRef": "tenant", "principalRef": "account",
        "endpoint": sys.argv[1], "authMode": "signed"}}]})
scope = ResourceScope.model_validate({"profileDigest": "sha256:" + "b" * 64,
    "generationId": "generation", "activationId": "activation", "buildDigest": "sha256:" + "c" * 64,
    "bindingSnapshotDigest": snapshot.digest, "bindingId": "kb",
    "allowedOperations": ["search_knowledge_base"], "identity": {
        "tenantRef": "tenant", "resourcePrincipalRef": "account", "actorRef": "actor",
        "memorySubjectRef": "user", "agentId": "agent", "sessionRef": "session"}})
init = WorkerInitialization.model_validate({"resourceSnapshot": snapshot.model_dump(by_alias=True),
    "scopes": [scope.model_dump(by_alias=True)], "bindings": [{"config": config,
        "endpoint": sys.argv[1],
        "accessKey": "fake-access", "secretKey": "fake-secret"}]})
async def main():
    worker = await ResourceWorkerProcess.start(init)
    try:
        assert worker.is_running
        request = ResourceRequest.model_validate({
            "v": 1, "requestId": "wheel-query", "deadline": int(time.time() * 1000) + 15000,
            "operation": "search_knowledge_base", "handle": "f" * 32,
            "arguments": {"query": "wheel-query"},
        })
        result = await worker.invoke(request, scope)
        assert result["status"] == "ok", result
        assert result["items"][0]["content"] == "worker-result", result
        assert result["items"][0]["documentId"] == "document-a", result
    finally:
        await worker.aclose()
    assert not worker.is_running
    print(json.dumps({
        "installedPackage": str(Path(ksadk.__file__).resolve()),
        "worker": "started-and-closed",
        "knowledgeHttp": "fixture-query-and-source-passed",
    }))
asyncio.run(main())
''')
    result = subprocess.run([
        str(python), "-I", str(script), endpoint,
    ], cwd=root, env=clean_env, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    assert len(calls) == 1
    assert calls[0][1]["Query"] == "wheel-query"
    assert Path(evidence["installedPackage"]).is_relative_to(environment)
    cli = subprocess.run([
        str(python), "-I", "-m", "ksadk", "plugin", "--help",
    ], cwd=root, env=clean_env, capture_output=True, text=True, timeout=30)
    assert cli.returncode == 0, cli.stdout + cli.stderr
    assert "plugin" in cli.stdout.lower()
    (root / "evidence.json").write_text(json.dumps({
        "wheel": wheel.name,
        "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        **evidence, "pluginCli": "help-passed",
    }, indent=2))

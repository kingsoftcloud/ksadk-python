from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from ksadk.studio.api import create_studio_app
from ksadk.studio.cloud import InMemoryCloudGateway
from ksadk.studio.service import StudioService

_REQUIRED_ENV = (
    "AGENTKIT_E2E_API_KEY",
    "AGENTKIT_E2E_BASE_URL",
    "AGENTKIT_E2E_MODEL",
)


def _wait(client: TestClient, operation_id: str, *, timeout: float = 180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = client.get(f"/api/v1/operations/{operation_id}").json()
        if payload["status"] in {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"operation {operation_id} did not finish")


@pytest.mark.skipif(
    not all(os.environ.get(name) for name in _REQUIRED_ENV),
    reason="real model E2E credentials are not configured",
)
def test_real_model_create_build_run_and_cloud_contract(tmp_path: Path):
    endpoint = os.environ["AGENTKIT_E2E_BASE_URL"].rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint = f"{endpoint}/chat/completions"
    model = os.environ["AGENTKIT_E2E_MODEL"]
    host = urlparse(endpoint).hostname
    assert host

    cloud = InMemoryCloudGateway()
    service = StudioService(tmp_path, cloud_gateway=cloud)
    app = create_studio_app(tmp_path, service=service, security_enabled=False)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents",
            json={
                "id": "e2e-agent",
                "name": "AgentKit E2E Agent",
                "description": "Real glm-5.1 E2E",
                "template": "blank",
            },
        )
        assert created.status_code == 201
        updated = client.put(
            "/api/v1/agents/e2e-agent",
            headers={"If-Match": '"1"'},
            json={
                "description": "Real glm-5.1 E2E",
                "instructions": {
                    "system": (
                        "Your entire response must be exactly AGENTKIT_E2E_OK. "
                        "Do not add punctuation, explanations, or markdown."
                    ),
                    "task": "Return the required fixed E2E marker.",
                },
                "model": {
                    "provider": "openai-compatible",
                    "model": model,
                    "endpointUrl": endpoint,
                    "credentialRef": "env://AGENTKIT_E2E_API_KEY",
                    "parameters": {
                        "temperature": 0,
                        "maxTokens": 1024,
                    },
                },
                "capabilities": {
                    "skills": [],
                    "mcpServers": [],
                    "tools": [],
                },
                "execution": {
                    "strategy": "direct",
                    "maxSteps": 4,
                    "timeoutSeconds": 120,
                    "retry": {
                        "maxAttempts": 2,
                        "backoffSeconds": 1,
                    },
                },
                "context": {
                    "maxInputTokens": 8192,
                    "reserveOutputTokens": 512,
                    "compaction": {
                        "enabled": True,
                        "thresholdRatio": 0.8,
                    },
                },
                "security": {
                    "toolPolicy": "deny-by-default",
                    "allowedPermissions": [],
                    "network": {
                        "mode": "restricted",
                        "allowedHosts": [host],
                        "allowPrivateNetwork": False,
                    },
                },
                "evaluation": {
                    "suiteRefs": [],
                    "minimumPassRate": 1,
                },
            },
        )
        assert updated.status_code == 200, updated.text

        validation = client.post(
            "/api/v1/agents/e2e-agent/validations",
            json={"revision": 2, "level": "build"},
        )
        assert validation.json()["valid"] is True, validation.text

        first_operation = client.post(
            "/api/v1/agents/e2e-agent/builds",
            headers={"Idempotency-Key": "e2e-agent-r2-first"},
            json={"revision": 2},
        ).json()
        first_completed = _wait(client, first_operation["id"])
        assert first_completed["status"] == "SUCCEEDED", first_completed
        first_build = client.get(
            f"/api/v1/builds/{first_completed['resourceId']}"
        ).json()

        second_operation = client.post(
            "/api/v1/agents/e2e-agent/builds",
            headers={"Idempotency-Key": "e2e-agent-r2-second"},
            json={"revision": 2},
        ).json()
        second_completed = _wait(client, second_operation["id"])
        assert second_completed["status"] == "SUCCEEDED", second_completed
        second_build = client.get(
            f"/api/v1/builds/{second_completed['resourceId']}"
        ).json()
        assert first_build["resolvedDigest"] == second_build["resolvedDigest"]
        assert first_build["bundleDigest"] == second_build["bundleDigest"]

        run_operation = client.post(
            f"/api/v1/builds/{first_build['id']}/runs",
            headers={"Idempotency-Key": "e2e-agent-real-run"},
            json={
                "input": {
                    "role": "user",
                    "content": "Reply with the required E2E marker now.",
                },
                "environment": "local",
                "stream": True,
            },
        ).json()
        run_completed = _wait(client, run_operation["id"])
        assert run_completed["status"] == "SUCCEEDED", run_completed
        run = client.get(f"/api/v1/runs/{run_completed['resourceId']}").json()
        assert run["status"] == "COMPLETED", run
        assert "AGENTKIT_E2E_OK" in run["output"]
        assert run["usage"]["totalTokens"] > 0
        event_text = client.get(f"/api/v1/runs/{run['id']}/events").text
        assert "model.completed" in event_text
        assert "run.completed" in event_text
        trace = client.get(f"/api/v1/traces/{run['traceId']}").json()
        assert trace["runId"] == run["id"]

        deployment_operation = client.post(
            f"/api/v1/builds/{first_build['id']}/deployments",
            headers={"Idempotency-Key": "e2e-agent-cloud-contract"},
            json={
                "target": {
                    "region": "cn-beijing-6",
                    "environment": "development",
                },
                "binding": {
                    "model": {"primary": f"cloud-model://{model}"},
                    "secrets": {
                        "model-api-key": "secret-manager://agentkit/e2e-model"
                    },
                },
                "releasePolicy": {
                    "strategy": "rolling",
                    "approval": "none",
                },
            },
        ).json()
        deployment_completed = _wait(client, deployment_operation["id"])
        assert deployment_completed["status"] == "SUCCEEDED"
        assert cloud.uploads[0]["bundle_digest"] == first_build["bundleDigest"]
        assert cloud.versions[0]["bundle_digest"] == first_build["bundleDigest"]

    secret = os.environ["AGENTKIT_E2E_API_KEY"].encode()
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert secret not in path.read_bytes(), f"API key leaked into {path}"

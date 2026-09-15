from __future__ import annotations

import json
import subprocess

from scripts.collect_phase1_live_deployment import collect_live_deployment_evidence


def _deployment(*, name: str, image: str, available: int = 1, generation: int = 4, observed: int = 4):
    return {
        "metadata": {
            "name": name,
            "generation": generation,
            "annotations": {"agentengine.ksyun.com/source-commit": "a" * 40},
        },
        "spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": "app", "image": image}]}}},
        "status": {"availableReplicas": available, "observedGeneration": observed},
    }


def _runner(payloads: dict[str, dict]):
    def run(command):
        name = command[command.index("deployment") + 1]
        if name not in payloads:
            raise subprocess.CalledProcessError(1, command)
        return json.dumps(payloads[name])

    return run


def test_live_evidence_passes_only_for_ready_digest_pinned_deployments():
    evidence = collect_live_deployment_evidence(
        runner=_runner({"server": _deployment(name="server", image="registry/server@sha256:" + "a" * 64)}),
        kubeconfig="/tmp/kubeconfig",
        namespace="isolated",
        deployments=("server",),
        environment="pre",
    )

    check = evidence["checks"]["cross_repo_versions"]
    assert check["status"] == "pass"
    assert check["detail"]["deployments"][0]["source_commit"] == "a" * 40
    assert check["detail"]["deployments"][0]["images"] == ["registry/server@sha256:" + "a" * 64]


def test_live_evidence_fails_for_missing_or_tag_only_deployments():
    evidence = collect_live_deployment_evidence(
        runner=_runner({"gateway": _deployment(name="gateway", image="registry/gateway:latest")}),
        kubeconfig=None,
        namespace="isolated",
        deployments=("gateway", "operator"),
        environment="pre",
    )

    check = evidence["checks"]["cross_repo_versions"]
    assert check["status"] == "fail"
    details = {item["deployment"]: item for item in check["detail"]["deployments"]}
    assert details["gateway"]["reason"] == "deployment_image_not_digest_pinned"
    assert details["operator"]["reason"] == "deployment_not_found_or_unreadable"


def test_live_evidence_fails_for_a_stale_or_unavailable_deployment():
    evidence = collect_live_deployment_evidence(
        runner=_runner({
            "runtime": _deployment(
                name="runtime",
                image="registry/runtime@sha256:" + "b" * 64,
                available=0,
                generation=5,
                observed=4,
            )
        }),
        kubeconfig=None,
        namespace="isolated",
        deployments=("runtime",),
        environment="pre",
    )

    detail = evidence["checks"]["cross_repo_versions"]["detail"]["deployments"][0]
    assert detail["reason"] == "deployment_generation_not_observed"

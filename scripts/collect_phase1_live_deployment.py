#!/usr/bin/env python3
"""Collect non-secret, live Phase 1 deployment provenance from Kubernetes.

This collector intentionally proves only what Kubernetes can prove now: a
named deployment is current, available, digest-pinned, and has an immutable
running image ID.  It does *not* manufacture behavioral checks such as FIFO,
permit rejection, or recovery.  Those must still be supplied by correlated
scenario evidence to :mod:`phase1_preprod_gate`.

The generated JSON has one ``cross_repo_versions`` check and can be passed to
the gate alongside behaviour evidence.  A missing deployment, a stale
generation, an unavailable replica, or a tag-only image is emitted as a
truthful ``fail`` rather than omitted.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_DEPLOYMENTS = (
    "agentengine-kernel",
    "agentengine-gateway-kernel-api",
    "agentengine-gateway-kernel-router",
    "agent-runtime-codex-kernel",
    "agentengine-hosted-ui-kernel",
    "agent-platform-operator-kernel",
)


@dataclass(frozen=True)
class DeploymentSnapshot:
    name: str
    status: str
    detail: dict[str, Any]


Runner = Callable[[Sequence[str]], str]


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _kubectl_runner(command: Sequence[str]) -> str:
    return subprocess.check_output(command, text=True, stderr=subprocess.PIPE)


def _json_command(runner: Runner, command: Sequence[str]) -> dict[str, Any]:
    value = json.loads(runner(command))
    if not isinstance(value, dict):
        raise ValueError("kubectl response must be a JSON object")
    return value


def _is_digest_pinned(image: str) -> bool:
    return "@sha256:" in image


def _collect_one(
    *,
    runner: Runner,
    kubectl_prefix: Sequence[str],
    namespace: str,
    name: str,
) -> DeploymentSnapshot:
    command = [*kubectl_prefix, "-n", namespace, "get", "deployment", name, "-o", "json"]
    try:
        deployment = _json_command(runner, command)
    except subprocess.CalledProcessError as exc:
        return DeploymentSnapshot(
            name=name,
            status="fail",
            detail={
                "deployment": name,
                "reason": "deployment_not_found_or_unreadable",
                "returncode": exc.returncode,
            },
        )
    except (json.JSONDecodeError, ValueError) as exc:
        return DeploymentSnapshot(
            name=name,
            status="fail",
            detail={
                "deployment": name,
                "reason": "deployment_response_invalid",
                "error": str(exc),
            },
        )

    metadata = deployment.get("metadata") if isinstance(deployment.get("metadata"), dict) else {}
    spec = deployment.get("spec") if isinstance(deployment.get("spec"), dict) else {}
    status = deployment.get("status") if isinstance(deployment.get("status"), dict) else {}
    template = spec.get("template") if isinstance(spec.get("template"), dict) else {}
    template_spec = template.get("spec") if isinstance(template.get("spec"), dict) else {}
    containers = template_spec.get("containers") if isinstance(template_spec.get("containers"), list) else []
    images = [str(item.get("image", "")) for item in containers if isinstance(item, dict)]
    desired = int(spec.get("replicas", 1) or 0)
    observed = int(status.get("observedGeneration", 0) or 0)
    generation = int(metadata.get("generation", 0) or 0)
    available = int(status.get("availableReplicas", 0) or 0)
    annotations = metadata.get("annotations") if isinstance(metadata.get("annotations"), dict) else {}

    detail: dict[str, Any] = {
        "deployment": name,
        "generation": generation,
        "observed_generation": observed,
        "desired_replicas": desired,
        "available_replicas": available,
        "images": images,
        "source_commit": annotations.get("agentengine.ksyun.com/source-commit"),
    }
    if observed < generation:
        detail["reason"] = "deployment_generation_not_observed"
        return DeploymentSnapshot(name=name, status="fail", detail=detail)
    if available < desired:
        detail["reason"] = "deployment_not_available"
        return DeploymentSnapshot(name=name, status="fail", detail=detail)
    if not images or not all(_is_digest_pinned(image) for image in images):
        detail["reason"] = "deployment_image_not_digest_pinned"
        return DeploymentSnapshot(name=name, status="fail", detail=detail)
    return DeploymentSnapshot(name=name, status="pass", detail=detail)


def collect_live_deployment_evidence(
    *,
    runner: Runner,
    kubeconfig: str | None,
    namespace: str,
    deployments: Sequence[str],
    environment: str,
) -> dict[str, Any]:
    """Return gate-compatible, non-secret evidence for current deployments."""

    prefix = ["kubectl"]
    if kubeconfig:
        prefix.append(f"--kubeconfig={kubeconfig}")
    snapshots = [
        _collect_one(
            runner=runner,
            kubectl_prefix=prefix,
            namespace=namespace,
            name=name,
        )
        for name in deployments
    ]
    all_ready = all(snapshot.status == "pass" for snapshot in snapshots)
    details = {
        "collected_at": _now_iso(),
        "namespace": namespace,
        "deployments": [snapshot.detail for snapshot in snapshots],
    }
    return {
        "schema_version": 1,
        "environment": environment,
        "checks": {
            "cross_repo_versions": {
                "status": "pass" if all_ready else "fail",
                "detail": details,
            }
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect current, digest-pinned Phase 1 deployment evidence."
    )
    parser.add_argument("--kubeconfig", default=None, help="explicit kubeconfig path")
    parser.add_argument("--namespace", required=True, help="isolated verification namespace")
    parser.add_argument("--environment", default="pre", help="environment label")
    parser.add_argument(
        "--deployment",
        action="append",
        default=None,
        help="deployment to require; repeat to override the default full chain",
    )
    parser.add_argument("--output", required=True, help="evidence JSON output path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    deployments = tuple(args.deployment or DEFAULT_DEPLOYMENTS)
    evidence = collect_live_deployment_evidence(
        runner=_kubectl_runner,
        kubeconfig=args.kubeconfig,
        namespace=args.namespace,
        deployments=deployments,
        environment=args.environment,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2, sort_keys=True))
    status = evidence["checks"]["cross_repo_versions"]["status"]
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

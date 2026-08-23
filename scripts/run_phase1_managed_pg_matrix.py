#!/usr/bin/env python3
"""Run the Phase 1 durability matrix against a managed PostgreSQL canary.

The script never receives or persists a DSN.  ``make phase1-canary-deploy``
places the externally managed PostgreSQL DSN in a temporary Kubernetes Secret;
this runner talks only to the isolated runtime through ``kubectl port-forward``.
It produces raw, gate-compatible behavioral evidence and removes its database
rows through the instance-scoped cleanup hook.  Namespace cleanup remains the
Makefile caller's unconditional trap.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import httpx


class MatrixFailure(RuntimeError):
    """A behavioral invariant failed; the release evidence must remain red."""


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _session(label: str) -> str:
    return f"phase1-{label}-{uuid.uuid4().hex[:12]}"


def _command_id() -> str:
    return str(uuid.uuid4())


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise MatrixFailure(message)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class PortForward:
    kubectl_prefix: list[str]
    namespace: str
    service: str
    process: subprocess.Popen[str] | None = None
    port: int | None = None

    def start(self, *, timeout: float = 60.0) -> str:
        self.close()
        self.port = _free_port()
        command = [
            *self.kubectl_prefix,
            "-n",
            self.namespace,
            "port-forward",
            f"service/{self.service}",
            f"{self.port}:8080",
        ]
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        base_url = f"http://127.0.0.1:{self.port}"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                output = self.process.stdout.read() if self.process.stdout else ""
                raise MatrixFailure(f"kubectl port-forward exited early: {output[-500:]}")
            try:
                response = httpx.get(f"{base_url}/healthz", timeout=1.0)
                if response.status_code == 200:
                    return base_url
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        raise MatrixFailure("canary health did not become reachable through port-forward")

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def __enter__(self) -> "PortForward":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class KubeDriver:
    def __init__(self, *, kubeconfig: str, namespace: str, deployment: str) -> None:
        self.prefix = ["kubectl", f"--kubeconfig={kubeconfig}"]
        self.namespace = namespace
        self.deployment = deployment

    def _run(self, *args: str, timeout: float = 180.0) -> str:
        completed = subprocess.run(
            [*self.prefix, *args],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if completed.returncode != 0:
            raise MatrixFailure(f"kubectl {' '.join(args[:4])} failed: {completed.stderr[-500:]}")
        return completed.stdout

    def deployment_image(self) -> str:
        return self._run(
            "-n",
            self.namespace,
            "get",
            "deployment",
            self.deployment,
            "-o",
            "jsonpath={.spec.template.spec.containers[?(@.name=='runtime')].image}",
        ).strip()

    def current_pod(self) -> tuple[str, str]:
        payload = json.loads(
            self._run(
                "-n",
                self.namespace,
                "get",
                "pods",
                "-l",
                "app=agent-kernel-canary",
                "-o",
                "json",
            )
        )
        candidates = [
            item
            for item in payload.get("items", [])
            if item.get("metadata", {}).get("deletionTimestamp") is None
        ]
        ready = [
            item
            for item in candidates
            if any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in item.get("status", {}).get("conditions", [])
            )
        ]
        selected = ready or candidates
        if not selected:
            raise MatrixFailure("no canary pod found")
        item = sorted(
            selected,
            key=lambda row: row.get("metadata", {}).get("creationTimestamp", ""),
        )[-1]
        return str(item["metadata"]["name"]), str(item["metadata"]["uid"])

    def delete_pod_and_wait_replacement(
        self, old_name: str, old_uid: str, *, timeout: float
    ) -> tuple[str, str, float]:
        started = time.monotonic()
        self._run("-n", self.namespace, "delete", "pod", old_name, "--wait=false")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                name, uid = self.current_pod()
            except MatrixFailure:
                time.sleep(0.5)
                continue
            if uid != old_uid:
                return name, uid, time.monotonic() - started
            time.sleep(0.5)
        raise MatrixFailure("replacement canary pod did not become ready")

    def set_image(self, image: str, *, timeout: float = 240.0) -> float:
        _require("@sha256:" in image, f"rollback image is not digest pinned: {image}")
        started = time.monotonic()
        self._run(
            "-n",
            self.namespace,
            "set",
            "image",
            f"deployment/{self.deployment}",
            f"runtime={image}",
        )
        self._run(
            "-n",
            self.namespace,
            "rollout",
            "status",
            f"deployment/{self.deployment}",
            f"--timeout={int(timeout)}s",
            timeout=timeout + 10,
        )
        return time.monotonic() - started


class CanaryClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.client = httpx.Client(base_url=base_url, timeout=30.0)

    def close(self) -> None:
        self.client.close()

    def contract(self) -> dict[str, Any]:
        response = self.client.get("/v1/meta/contract-digest")
        response.raise_for_status()
        return response.json()

    def worker(self, enabled: bool) -> None:
        response = self.client.post("/test/worker", json={"enabled": enabled})
        response.raise_for_status()

    def submit(
        self,
        session_id: str,
        *,
        command_id: str,
        idempotency_key: str,
        text: str,
    ) -> dict[str, Any]:
        response = self.client.post(
            "/v1/actions/SubmitAgentControl",
            json={
                "session_id": session_id,
                "command_id": command_id,
                "idempotency_key": idempotency_key,
                "command_type": "enqueue",
                "text": text,
            },
        )
        response.raise_for_status()
        return response.json()

    def snapshot(self, session_id: str) -> dict[str, Any]:
        response = self.client.get("/test/snapshot", params={"session_id": session_id})
        response.raise_for_status()
        return response.json()

    def wait_snapshot(
        self,
        session_id: str,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        timeout: float = 180.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.snapshot(session_id)
            if predicate(last):
                return last
            time.sleep(0.25)
        raise MatrixFailure(f"session {session_id} did not reach expected durable state: {last}")

    def stale_fence(self, session_id: str) -> dict[str, Any]:
        response = self.client.post("/test/drills/stale-fence", json={"session_id": session_id})
        response.raise_for_status()
        return response.json()

    def read_sse(
        self, session_id: str, *, after_seq: int, stop_seq: int | None = None
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        with self.client.stream(
            "GET",
            "/v1/actions/SubscribeSessionEvents",
            params={"session_id": session_id, "after_seq": after_seq},
            timeout=30.0,
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                event = json.loads(line.removeprefix("data:").strip())
                events.append(event)
                if stop_seq is None or int(event["seq"]) >= stop_seq:
                    break
        return events

    def cleanup(self) -> dict[str, Any]:
        response = self.client.post("/test/cleanup")
        response.raise_for_status()
        return response.json()


def _completed(snapshot: dict[str, Any], expected: int) -> bool:
    inbox = snapshot.get("inbox") or []
    return len(inbox) == expected and all(row.get("status") == "completed" for row in inbox)


def run_http_matrix(
    client: CanaryClient,
    *,
    expected_digest: str,
    checks: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Run deterministic HTTP/store checks that do not restart the Pod."""

    # Mutate the caller-owned result as each scenario closes.  If a later
    # drill fails, the raw report must retain the already verified checks
    # instead of misleadingly publishing ``checks: {}``.
    checks = checks if checks is not None else {}
    contract = client.contract()
    _require(contract.get("digest") == expected_digest, "runtime contract digest mismatch")
    checks["contract_digest"] = {
        "status": "pass",
        "detail": {"digest": expected_digest, "instance_id": contract.get("instance")},
    }

    idem_session = _session("idem")
    idem_command = _command_id()
    idem_key = f"idem-{uuid.uuid4().hex}"
    client.worker(False)
    first = client.submit(
        idem_session,
        command_id=idem_command,
        idempotency_key=idem_key,
        text="synthetic-idempotency",
    )
    duplicate = client.submit(
        idem_session,
        command_id=idem_command,
        idempotency_key=idem_key,
        text="synthetic-idempotency",
    )
    _require(first.get("status") == "accepted", "idempotency first write not accepted")
    _require(duplicate.get("status") == "duplicate", "idempotency retry not duplicate")
    _require(first.get("message_id") == duplicate.get("message_id"), "duplicate changed message")
    checks["idempotency"] = {
        "status": "pass",
        "detail": {
            "command_id": idem_command,
            "message_id": first.get("message_id"),
            "accepted_seq": first.get("accepted_seq"),
        },
    }

    fifo_session = _session("fifo")
    receipts: list[dict[str, Any]] = []
    command_ids: list[str] = []
    for index in range(101):
        command_id = _command_id()
        command_ids.append(command_id)
        receipts.append(
            client.submit(
                fifo_session,
                command_id=command_id,
                idempotency_key=f"fifo-{uuid.uuid4().hex}",
                text=f"synthetic-fifo-{index}",
            )
        )
    accepted = [item for item in receipts if item.get("status") == "accepted"]
    full = [item for item in receipts if item.get("status") == "queue_full"]
    _require(len(accepted) == 100 and len(full) == 1, "queue limit was not exactly 100")
    paused = client.snapshot(fifo_session)
    accepted_rows = paused.get("inbox") or []
    accepted_seqs = [int(row["accepted_seq"]) for row in accepted_rows]
    _require(accepted_seqs == list(range(1, 101)), "accepted FIFO sequence is not contiguous")
    client.worker(True)
    drained = client.wait_snapshot(
        fifo_session, lambda value: _completed(value, 100), timeout=240.0
    )
    inbox_message_ids = [row["message_id"] for row in drained["inbox"]]
    claimed_message_ids = [
        event["payload"].get("message_id")
        for event in drained["events"]
        if event.get("event_type") == "control.message_claimed"
    ]
    completed_message_ids = [
        event["payload"].get("message_id")
        for event in drained["events"]
        if event.get("event_type") == "control.message_completed"
    ]
    _require(claimed_message_ids == inbox_message_ids, "claim order violated accepted FIFO")
    _require(completed_message_ids == inbox_message_ids, "completion order violated FIFO")
    checks["fifo"] = {
        "status": "pass",
        "detail": {
            "event_id": drained["events"][-1]["event_id"],
            "accepted_seq_range": [1, 100],
            "claimed_in_order": True,
            "completed_in_order": True,
        },
    }
    checks["queue_full"] = {
        "status": "pass",
        "detail": {
            "command_id": command_ids[-1],
            "event_id": paused["events"][-1]["event_id"],
            "accepted_count": 100,
            "error_code": full[0].get("error", {}).get("code"),
        },
    }

    reconnect_session = _session("reconnect")
    client.worker(False)
    reconnect_command = _command_id()
    client.submit(
        reconnect_session,
        command_id=reconnect_command,
        idempotency_key=f"reconnect-{uuid.uuid4().hex}",
        text="synthetic-reconnect",
    )
    first_events = client.read_sse(reconnect_session, after_seq=0)
    _require(len(first_events) == 1, "first SSE read did not stop after one event")
    first_seq = int(first_events[0]["seq"])
    client.worker(True)
    final_snapshot = client.wait_snapshot(reconnect_session, lambda value: _completed(value, 1))
    final_seq = int(final_snapshot["events"][-1]["seq"])
    started = time.monotonic()
    resumed = client.read_sse(reconnect_session, after_seq=first_seq, stop_seq=final_seq)
    reconnect_seconds = time.monotonic() - started
    resumed_seqs = [int(event["seq"]) for event in resumed]
    _require(
        resumed_seqs == list(range(first_seq + 1, final_seq + 1)),
        "SSE reconnect contained a gap or duplicate",
    )
    checks["reconnect"] = {
        "status": "pass",
        "detail": {
            "event_id": resumed[-1]["event_id"],
            "disconnect_after_seq": first_seq,
            "resumed_seq_range": [first_seq + 1, final_seq],
            "duration_seconds": round(reconnect_seconds, 3),
        },
    }

    fence = client.stale_fence(_session("fence"))
    _require(fence.get("stale_writer_rejected") is True, "stale writer was accepted")
    _require(
        int(fence["new_fencing_token"]) == int(fence["old_fencing_token"]) + 1,
        "takeover did not increment the fencing token",
    )
    checks["stale_fence"] = {"status": "pass", "detail": fence}
    return checks


def run_cold_recovery(
    *,
    client: CanaryClient,
    forward: PortForward,
    kube: KubeDriver,
    lease_ttl_seconds: float,
) -> tuple[CanaryClient, dict[str, Any]]:
    session_id = _session("cold")
    client.worker(True)
    warm_command = _command_id()
    client.submit(
        session_id,
        command_id=warm_command,
        idempotency_key=f"warm-{uuid.uuid4().hex}",
        text="synthetic-warm",
    )
    warm = client.wait_snapshot(session_id, lambda value: _completed(value, 1))
    old_activation = warm["activation"]
    _require(old_activation is not None, "warm session did not acquire an activation")
    client.worker(False)
    backlog_ids: list[str] = []
    for index in range(5):
        command_id = _command_id()
        backlog_ids.append(command_id)
        receipt = client.submit(
            session_id,
            command_id=command_id,
            idempotency_key=f"cold-{uuid.uuid4().hex}",
            text=f"synthetic-cold-{index}",
        )
        _require(receipt.get("status") == "accepted", "cold-recovery backlog rejected")
    old_name, old_uid = kube.current_pod()
    recovery_started = time.monotonic()
    client.close()
    forward.close()
    new_name, new_uid, _ = kube.delete_pod_and_wait_replacement(
        old_name,
        old_uid,
        timeout=max(180.0, lease_ttl_seconds + 120.0),
    )
    base_url = forward.start(timeout=120.0)
    replacement = CanaryClient(base_url)
    recovered = replacement.wait_snapshot(
        session_id,
        lambda value: _completed(value, 6),
        timeout=max(240.0, lease_ttl_seconds + 180.0),
    )
    new_activation = recovered["activation"]
    _require(new_activation is not None, "replacement did not acquire activation")
    _require(
        new_activation["activation_id"] != old_activation["activation_id"],
        "replacement reused the old Pod activation identity",
    )
    _require(
        int(new_activation["fencing_token"]) == int(old_activation["fencing_token"]) + 1,
        "Pod takeover did not increment fencing token",
    )
    terminals = [
        event
        for event in recovered["events"]
        if event.get("event_type") == "control.message_completed"
    ]
    _require(len(terminals) == 6, "cold recovery produced missing/duplicate terminals")
    recovery_seconds = time.monotonic() - recovery_started
    return replacement, {
        "event_id": terminals[-1]["event_id"],
        "command_id": backlog_ids[-1],
        "old_pod_uid": old_uid,
        "new_pod_uid": new_uid,
        "old_pod": old_name,
        "new_pod": new_name,
        "old_fencing_token": old_activation["fencing_token"],
        "new_fencing_token": new_activation["fencing_token"],
        "terminal_count": len(terminals),
        "duration_seconds": round(recovery_seconds, 3),
    }


def run_rollback(
    *,
    client: CanaryClient,
    forward: PortForward,
    kube: KubeDriver,
    current_image: str,
    rollback_image: str,
) -> tuple[CanaryClient, dict[str, Any]]:
    session_id = _session("rollback")
    client.worker(False)
    command_ids: list[str] = []
    for index in range(5):
        command_id = _command_id()
        command_ids.append(command_id)
        receipt = client.submit(
            session_id,
            command_id=command_id,
            idempotency_key=f"rollback-{uuid.uuid4().hex}",
            text=f"synthetic-rollback-{index}",
        )
        _require(receipt.get("status") == "accepted", "rollback backlog rejected")
    before = client.snapshot(session_id)
    _require(
        len(before.get("inbox") or []) == 5,
        "rollback precondition did not persist five messages",
    )
    client.close()
    forward.close()
    rollback_seconds = kube.set_image(rollback_image)
    rollback_base = forward.start(timeout=120.0)
    rollback_client = CanaryClient(rollback_base)
    rollback_health = rollback_client.client.get("/healthz")
    rollback_health.raise_for_status()
    rollback_client.close()
    forward.close()
    rollforward_seconds = kube.set_image(current_image)
    current_base = forward.start(timeout=120.0)
    current_client = CanaryClient(current_base)
    recovered = current_client.wait_snapshot(
        session_id, lambda value: _completed(value, 5), timeout=240.0
    )
    event_id = recovered["events"][-1]["event_id"]
    return current_client, {
        "event_id": event_id,
        "command_id": command_ids[-1],
        "rollback_image": rollback_image,
        "rollforward_image": current_image,
        "rollback_seconds": round(rollback_seconds, 3),
        "rollforward_seconds": round(rollforward_seconds, 3),
        "backlog_before": 5,
        "backlog_after": 0,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--namespace", default="agent-kernel-phase1")
    parser.add_argument("--deployment", default="agent-kernel-canary")
    parser.add_argument("--service", default="agent-kernel-canary")
    parser.add_argument("--expected-contract-digest", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--rollback-image", required=True)
    parser.add_argument("--lease-ttl-seconds", type=float, default=30.0)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    kube = KubeDriver(
        kubeconfig=args.kubeconfig,
        namespace=args.namespace,
        deployment=args.deployment,
    )
    forward = PortForward(
        kubectl_prefix=kube.prefix,
        namespace=args.namespace,
        service=args.service,
    )
    client: CanaryClient | None = None
    checks: dict[str, dict[str, Any]] = {}
    current_image = kube.deployment_image()
    _require("@sha256:" in current_image, "current canary image is not digest pinned")
    started = time.monotonic()
    failure: str | None = None
    try:
        client = CanaryClient(forward.start())
        run_http_matrix(
            client,
            expected_digest=args.expected_contract_digest,
            checks=checks,
        )
        client, cold = run_cold_recovery(
            client=client,
            forward=forward,
            kube=kube,
            lease_ttl_seconds=args.lease_ttl_seconds,
        )
        checks["cold_recovery"] = {"status": "pass", "detail": cold}
        client, rollback = run_rollback(
            client=client,
            forward=forward,
            kube=kube,
            current_image=current_image,
            rollback_image=args.rollback_image,
        )
        rollback["commit"] = args.source_commit
        checks["rollback"] = {"status": "pass", "detail": rollback}
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            if kube.deployment_image() != current_image:
                forward.close()
                if client is not None:
                    client.close()
                kube.set_image(current_image)
                client = CanaryClient(forward.start(timeout=120.0))
            if client is not None:
                client.cleanup()
        except Exception as cleanup_exc:
            failure = failure or f"cleanup failed: {cleanup_exc}"
        if client is not None:
            client.close()
        forward.close()

    payload: dict[str, Any] = {
        "schema_version": 1,
        "environment": "preproduction",
        "scenario": "managed_postgres_durability_matrix",
        "generated_at": _now_iso(),
        "source_commit": args.source_commit,
        "contract_digest": args.expected_contract_digest,
        "current_image": current_image,
        "duration_seconds": round(time.monotonic() - started, 3),
        "checks": checks,
    }
    if failure is not None:
        payload["status"] = "fail"
        payload["failure"] = failure
    else:
        payload["status"] = "pass"
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if failure is None else 1


if __name__ == "__main__":
    raise SystemExit(main())

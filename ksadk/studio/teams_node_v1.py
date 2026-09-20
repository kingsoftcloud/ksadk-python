"""Frozen Teams node protocol, with independent control and execution lanes.

The node journals orders/results; it never schedules or invents an execution.
Only freshly claimed authorization can attempt an operation after restart.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import TypeAdapter

from ksadk.kernel.contracts import AgentControlCommand, AgentControlPermit
from ksadk.kernel.execution_host_ingress import KernelTeamsExecutionHost, TrustedGrantWindow
from ksadk.kernel.teams_execution_context import ContextConflict, StoreIdentityMismatch
from ksadk.plugins.teams.cloud_contracts import (
    Digest,
    ExecutionResult,
    HostReceipt,
    Identifier,
    MaterialProof,
    NodeCommandBase,
    NodeProbeCommand,
    NodeProbeReport,
    NodeProbeResult,
    NodeReport,
    ReportError,
    canonical_bytes,
    digest,
    parse_node_message,
)
from ksadk.plugins.teams.cloud_permits import PermitError, TeamsExecutionPermit
from ksadk.plugins.teams.errors import TeamsError
from ksadk.plugins.teams.transport import TeamsHTTPClient

_CAPABILITIES = (
    "enqueue",
    "leader",
    "restore",
    "idempotentLookup",
    "terminalEvidence",
    "isolatedSession",
    "executionGrant",
    "toolPolicy",
    "teamsTools",
    "materialization",
    "effectLedger",
)
_TERMINAL_PHASES = {"described", "prepared", "terminal", "rejected", "control_applied"}
_CREDENTIAL_ERRORS = {
    "node_revoked",
    "node_credential_revoked",
    "node_generation_mismatch",
    "node_credential_invalid",
    "credential_recovery_required",
    "credential_reissue_required",
    "credential_rotation_conflict",
}


def _utc(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone-aware timestamp required")
    return result


def normalized_node_bindings(bindings: list[dict]) -> list[dict]:
    """Match Server NodeBindingAdvertisement defaults, without adding readiness.

    This temporary adapter follows app.teams.credentials. It can be replaced
    with the shared DTO once credential/catalog schemas move into the SDK.
    """
    fields = {
        "localBindingRef",
        "agentId",
        "buildId",
        "providerRef",
        "name",
        "bundleDigest",
        "contractDigest",
        "capabilitiesDigest",
        "capabilities",
    }
    if len(bindings) > 256:
        raise ValueError("node binding advertisement limit exceeded")
    result, seen = [], set()
    for source in bindings:
        if set(source) - fields or fields - {"capabilities"} - source.keys():
            raise ValueError("invalid node binding advertisement fields")
        value = dict(source)
        for name in ("localBindingRef", "agentId", "buildId", "providerRef"):
            TypeAdapter(Identifier).validate_python(value[name])
        for name in ("bundleDigest", "contractDigest", "capabilitiesDigest"):
            TypeAdapter(Digest).validate_python(value[name])
        if not isinstance(value["name"], str) or not 1 <= len(value["name"]) <= 200:
            raise ValueError("invalid node binding name")
        caps = value.get("capabilities") or {}
        if set(caps) - set(_CAPABILITIES) or any(type(flag) is not bool for flag in caps.values()):
            raise ValueError("invalid node binding capabilities")
        value["capabilities"] = {name: caps.get(name, False) for name in _CAPABILITIES}
        if value["localBindingRef"] in seen:
            raise ValueError("duplicate local binding advertisement")
        seen.add(value["localBindingRef"])
        result.append(value)
    return result


class TeamsNodeTransport:
    """Node credentials never share the owner's bearer/signing header path."""

    def __init__(self, base_url: str, *, transport=None):
        self.client = TeamsHTTPClient(base_url, transport=transport)

    def set_access_token(self, token: str) -> None:
        self.client.node_token = token

    async def request(self, method, path, body=None):
        return await self.client.request(method, path, body)

    async def refresh(self, node_id, refresh_secret, rotation_id):
        # This separate unauthenticated client sends only the refresh secret.
        # A live node/owner header is explicitly forbidden on Server refresh.
        path = f"/nodes/{node_id}/credentials/refresh"
        body = {"refreshSecret": refresh_secret, "rotationId": rotation_id}
        content = json.dumps(body, separators=(",", ":"))
        import httpx

        try:
            response = await self.client.http.request(
                "POST",
                self.client.base_url + path,
                content=content,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            value = response.json()
        except (httpx.RequestError, ValueError) as exc:
            raise TeamsError(
                "teams_transport_unavailable", "节点凭据更新结果待核查", status=503
            ) from exc
        if response.is_error or response.is_redirect:
            error = value.get("error") or {}
            raise TeamsError(
                str(error.get("code") or "node_refresh_failed"),
                "节点凭据更新未完成",
                status=response.status_code,
            )
        return value

    async def close(self):
        await self.client.close()


class TeamsNodeV1:
    """Durable outbound client. The executor must use the trusted Kernel Host."""

    def __init__(
        self,
        owner_client,
        executor,
        *,
        state_dir: Path,
        name: str,
        bindings: list[dict],
        node_transport=None,
        execution_slots: int = 1,
        control_slots: int = 1,
        heartbeat_seconds: float = 10,
        claim_wait_seconds: int = 25,
    ):
        if (
            type(execution_slots) is not int
            or type(control_slots) is not int
            or not 1 <= execution_slots <= 64
            or not 1 <= control_slots <= 4
        ):
            raise ValueError("invalid node lane capacity")
        if (
            heartbeat_seconds <= 0
            or type(claim_wait_seconds) is not int
            or not 0 <= claim_wait_seconds <= 25
        ):
            raise ValueError("invalid node heartbeat or claim wait")
        self.owner_client, self.executor = owner_client, executor
        self.transport = node_transport or TeamsNodeTransport(owner_client.base_url)
        self.state_dir, self.name = Path(state_dir), name
        self.bindings = normalized_node_bindings(bindings)
        self.capacity = {"executionSlots": execution_slots, "controlSlots": control_slots}
        self.heartbeat_seconds, self.claim_wait_seconds = heartbeat_seconds, claim_wait_seconds
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_dir, 0o700)
        import fcntl

        self._lock_file = (self.state_dir / ".node.lock").open("a+b")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.identity_path = self.state_dir / "identity-v1.json"
            self.identity = (
                json.loads(self.identity_path.read_text())
                if self.identity_path.exists()
                else {
                    "deviceId": "device_" + uuid4().hex,
                }
            )
            self.db = sqlite3.connect(self.state_dir / "node-v1.sqlite", isolation_level=None)
            os.chmod(self.state_dir / "node-v1.sqlite", 0o600)
            self.db.row_factory = sqlite3.Row
            self.db.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS node_v1_identity (
                    singleton INTEGER PRIMARY KEY, incarnation TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS node_v1_operations (
                    id TEXT PRIMARY KEY, digest TEXT NOT NULL, body TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'received', revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS node_v1_outbox (
                    command_id TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL,
                    acked INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(command_id,revision)
                );
                CREATE TABLE IF NOT EXISTS node_v1_executions (
                    command_id TEXT PRIMARY KEY, context_ref TEXT NOT NULL,
                    store_incarnation TEXT NOT NULL, ref TEXT NOT NULL, prepared TEXT NOT NULL,
                    native_status TEXT NOT NULL DEFAULT 'prepared'
                );
            """)
            stored = self.db.execute(
                "SELECT incarnation FROM node_v1_identity WHERE singleton=1"
            ).fetchone()
            expected = self.identity.get("journalIncarnation")
            if expected is not None and (stored is None or stored[0] != expected):
                raise StoreIdentityMismatch("original node journal is unavailable")
            if stored is None:
                if self.identity.get("refreshSecret"):
                    raise StoreIdentityMismatch(
                        "credentials cannot initialize a replacement node journal"
                    )
                incarnation = str(uuid4())
                self.db.execute(
                    "INSERT INTO node_v1_identity(singleton,incarnation) VALUES(1,?)",
                    (incarnation,),
                )
            else:
                incarnation = stored[0]
            self.identity["journalIncarnation"] = incarnation
            if "native_status" not in {
                row[1] for row in self.db.execute("PRAGMA table_info(node_v1_executions)")
            }:
                self.db.execute(
                    "ALTER TABLE node_v1_executions ADD COLUMN native_status "
                    "TEXT NOT NULL DEFAULT 'prepared'"
                )
            self._save_identity()
        except BaseException:
            if getattr(self, "db", None) is not None:
                self.db.close()
            self._lock_file.close()
            raise
        self._closing = False
        self._credential_blocked = False
        self._lease_deadline = 0.0  # Never reconstruct a lease from restart time.
        self._tasks: list[asyncio.Task] = []
        self._operation_locks: dict[str, asyncio.Lock] = {}
        self._refresh_lock, self._report_lock = asyncio.Lock(), asyncio.Lock()
        self.last_error = None

    def _save_identity(self):
        temp = self.identity_path.with_suffix(".tmp")
        fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(self.identity, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(self.identity_path)
        directory = os.open(self.state_dir, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _install_credentials(self, response):
        required = {
            "authorityId",
            "nodeId",
            "nodeGeneration",
            "credentialVersion",
            "accessToken",
            "refreshSecret",
            "accessExpiresAt",
            "refreshExpiresAt",
        }
        if not required <= response.keys():
            raise TeamsError(
                "node_credential_recovery_required", "需要重新签发节点凭据", status=409
            )
        for field in ("authorityId", "nodeId", "nodeGeneration"):
            if field in self.identity and self.identity[field] != response[field]:
                raise TeamsError(
                    "node_generation_mismatch", "节点身份变化，原执行需核查", status=409
                )
        if response["credentialVersion"] < self.identity.get("credentialVersion", 0):
            raise TeamsError("node_credential_stale", "节点凭据版本已过期", status=409)
        self.identity.update({key: response[key] for key in required})
        self.identity.pop("rotation", None)
        self._credential_blocked = False
        self._save_identity()  # Commit before using the new secret on any request.
        self.transport.set_access_token(self.identity["accessToken"])

    async def register(self):
        if "refreshSecret" in self.identity:
            self.transport.set_access_token(self.identity["accessToken"])
            await self.refresh_credentials()
            return
        request = self.identity.get("registration")
        if request is None:
            request = {
                "deviceId": self.identity["deviceId"],
                "name": self.name,
                "protocolVersion": "teams-node/v1",
                "bindings": self.bindings,
                "capacity": self.capacity,
                "idempotencyKey": "register:" + uuid4().hex,
            }
            self.identity["registration"] = request
            self._save_identity()
        response = await self.owner_client.request("POST", "/nodes/register", request)
        self._install_credentials(response)

    async def refresh_credentials(self, *, expected_version=None):
        async with self._refresh_lock:
            if (
                expected_version is not None
                and self.identity.get("credentialVersion") != expected_version
            ):
                return
            rotation = self.identity.get("rotation")
            if rotation is None:
                rotation = {
                    "rotationId": "rotation:" + uuid4().hex,
                    "refreshSecret": self.identity["refreshSecret"],
                }
                self.identity["rotation"] = rotation
                self._save_identity()
            response = await self.transport.refresh(
                self.identity["nodeId"], rotation["refreshSecret"], rotation["rotationId"]
            )
            self._install_credentials(response)

    async def _request(self, method, path, body=None):
        used_version = self.identity.get("credentialVersion")
        try:
            return await self.transport.request(method, path, body)
        except TeamsError as exc:
            if exc.status == 401 and self.identity.get("refreshSecret"):
                await self.refresh_credentials(expected_version=used_version)
                return await self.transport.request(method, path, body)
            raise

    async def heartbeat_once(self):
        started = time.monotonic()
        refs = [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT ref FROM node_v1_executions WHERE native_status IN "
                "('running','waiting','awaiting_approval','waiting_for_node') "
                "ORDER BY command_id LIMIT 64"
            )
        ]
        # runningRefs are observations only. They never grant/renew execution.
        refs = [
            ref
            for ref in refs
            if ref["target"]["nodeGeneration"] == self.identity["nodeGeneration"]
        ]
        response = await self._request(
            "POST",
            f"/nodes/{self.identity['nodeId']}/heartbeat",
            {
                "nodeGeneration": self.identity["nodeGeneration"],
                "capacity": self.capacity,
                "runningRefs": refs,
                "catalogDigest": self.identity.get("catalogDigest", digest(self.bindings)),
            },
        )
        duration = (_utc(response["leaseUntil"]) - _utc(response["serverTime"])).total_seconds()
        if not 0 < duration <= 120:
            raise TeamsError("node_lease_invalid", "节点租约响应无效", status=502)
        self._lease_deadline = started + max(0, duration - 5)
        for action in response.get("actions", []):
            if action.get("kind") == "catalog_sync_required":
                await self.sync_catalog(accepted_revision=int(action["acceptedRevision"]))

    async def sync_catalog(self, *, accepted_revision=0):
        pending = self.identity.get("catalogRequest")
        if pending is None:
            pending = {
                "nodeGeneration": self.identity["nodeGeneration"],
                "revision": max(accepted_revision, self.identity.get("catalogRevision", 0)) + 1,
                "bindings": self.bindings,
                "idempotencyKey": "catalog:" + uuid4().hex,
            }
            self.identity["catalogRequest"] = pending
            self._save_identity()
        result = await self._request("POST", f"/nodes/{self.identity['nodeId']}/catalog", pending)
        if result["catalogDigest"] != digest(pending["bindings"]):
            raise TeamsError("node_catalog_mismatch", "节点目录确认摘要不一致", status=409)
        self.identity.update(
            catalogRevision=result["acceptedRevision"], catalogDigest=result["catalogDigest"]
        )
        self.identity.pop("catalogRequest", None)
        self._save_identity()

    async def start(self):
        await self.register()
        await self.heartbeat_once()
        if not self._tasks:
            self._tasks = [
                asyncio.create_task(self._heartbeat_loop(), name="teams-node-heartbeat-v1"),
                asyncio.create_task(self._lane_loop("execution"), name="teams-node-execution-v1"),
                asyncio.create_task(self._lane_loop("control"), name="teams-node-control-v1"),
                asyncio.create_task(self._outbox_loop(), name="teams-node-outbox-v1"),
            ]
            if callable(getattr(self.executor, "recover_effect_reports", None)):
                self._tasks.append(
                    asyncio.create_task(self._effects_loop(), name="teams-node-effects-v1")
                )

    async def _effects_loop(self):
        while not self._closing:
            try:
                await self.executor.recover_effect_reports()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = getattr(exc, "code", "effect_recovery_pending")
            await asyncio.sleep(5)

    def _failure(self, exc):
        self.last_error = getattr(exc, "code", "node_transport_unavailable")
        if self.last_error in _CREDENTIAL_ERRORS or getattr(exc, "status", None) in {401, 403}:
            self._lease_deadline = 0
            self._credential_blocked = True

    async def _heartbeat_loop(self):
        while not self._closing:
            if self._credential_blocked:
                await asyncio.sleep(self.heartbeat_seconds)
                continue
            try:
                if (
                    _utc(self.identity["accessExpiresAt"]) - datetime.now(timezone.utc)
                ).total_seconds() < 60:
                    await self.refresh_credentials()
                await self.heartbeat_once()
            except Exception as exc:
                self._failure(exc)
            await asyncio.sleep(self.heartbeat_seconds)

    async def _lane_loop(self, lane):
        while not self._closing:
            try:
                await self.poll_once(lane)
            except Exception as exc:
                self._failure(exc)
                await asyncio.sleep(1)

    async def _outbox_loop(self):
        while not self._closing:
            if self._credential_blocked:
                await asyncio.sleep(1)
                continue
            try:
                await self.flush_outbox()
            except Exception as exc:
                self._failure(exc)
            await asyncio.sleep(0.5)

    async def poll_once(self, lane):
        if time.monotonic() >= self._lease_deadline:
            await asyncio.sleep(0.1)
            return
        started = time.monotonic()
        batch = await self._request(
            "POST",
            "/commands/claim",
            {
                "nodeGeneration": self.identity["nodeGeneration"],
                "lane": lane,
                "limit": 1,
                "waitSeconds": self.claim_wait_seconds,
            },
        )
        server_time = _utc(batch["serverTime"])
        for raw in batch["commands"]:
            command = parse_node_message(raw)
            if command.lane != lane:
                raise TeamsError("node_lane_mismatch", "节点命令通道不匹配", status=409)
            await self.process_command(command, server_time=server_time, request_started=started)

    async def process_command(self, command, *, server_time, request_started):
        command = parse_node_message(command)
        probe = isinstance(command, NodeProbeCommand)
        target = command if probe else command.ref.target
        authority_id = command.authorityId if probe else command.ref.authorityId
        report_type = NodeProbeReport if probe else NodeReport
        if time.monotonic() >= self._lease_deadline:
            raise TeamsError("node_lease_expired", "节点租约已过期", status=409)
        if (
            authority_id != self.identity["authorityId"]
            or target.nodeId != self.identity["nodeId"]
            or target.nodeGeneration != self.identity["nodeGeneration"]
        ):
            raise TeamsError("node_generation_mismatch", "原命令不属于当前节点身份", status=409)
        lease_remaining = (_utc(command.claimLeaseUntil) - server_time).total_seconds()
        if time.monotonic() - request_started >= lease_remaining:
            raise TeamsError("node_claim_expired", "命令领取租约已到期", status=409)
        lock = self._operation_locks.setdefault(command.nodeCommandId, asyncio.Lock())
        async with lock:
            row = self.db.execute(
                "SELECT * FROM node_v1_operations WHERE id=?", (command.nodeCommandId,)
            ).fetchone()
            if row and row["digest"] != command.commandDigest:
                raise TeamsError("node_command_changed", "原节点命令内容不能改变", status=409)
            if row and (
                row["state"] in _TERMINAL_PHASES
                or self.db.execute(
                    "SELECT 1 FROM node_v1_outbox WHERE command_id=? AND acked=0 LIMIT 1",
                    (command.nodeCommandId,),
                ).fetchone()
            ):
                # The dedicated sender replays the exact durable report. Report
                # latency must never stall the control lane or duplicate work.
                return
            self.db.execute(
                "INSERT INTO node_v1_operations(id,digest,body,state) VALUES(?,?,?,'invoking') "
                "ON CONFLICT(id) DO UPDATE SET body=excluded.body,state='invoking'",
                (command.nodeCommandId, command.commandDigest, canonical_bytes(command).decode()),
            )
            try:
                outcome = await self.executor.execute(
                    command, journal=self, server_time=server_time, request_started=request_started
                )
                report = report_type(
                    nodeCommandId=command.nodeCommandId,
                    nodeGeneration=target.nodeGeneration,
                    commandDigest=command.commandDigest,
                    resultRevision=(row["revision"] if row else 0) + 1,
                    **outcome,
                )
            except asyncio.CancelledError:
                # Process kill/cancellation leaves 'invoking'; no fabricated rejection.
                raise
            except Exception as exc:
                code = getattr(exc, "code", "node_operation_uncertain")
                if not isinstance(code, str) or not code.replace("_", "").isalnum():
                    code = "node_operation_uncertain"
                report = report_type(
                    nodeCommandId=command.nodeCommandId,
                    nodeGeneration=target.nodeGeneration,
                    commandDigest=command.commandDigest,
                    resultRevision=(row["revision"] if row else 0) + 1,
                    phase="uncertain",
                    error=ReportError(code=code, retryable=True),
                )
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute(
                    "INSERT INTO node_v1_outbox(command_id,revision,body) VALUES(?,?,?)",
                    (
                        command.nodeCommandId,
                        report.resultRevision,
                        canonical_bytes(report).decode(),
                    ),
                )
                self.db.execute(
                    "UPDATE node_v1_operations SET state=?,revision=? WHERE id=?",
                    (report.phase, report.resultRevision, command.nodeCommandId),
                )
                if (
                    not probe
                    and report.receipt is not None
                    and report.receipt.commandId == command.ref.commandId
                ):
                    prepared = self.execution(command.ref.commandId)
                    if prepared and report.receipt.runId is not None:
                        ref = json.loads(prepared["ref"])
                        ref["nativeRunId"] = report.receipt.runId
                        self.db.execute(
                            "UPDATE node_v1_executions SET ref=?,native_status=? "
                            "WHERE command_id=?",
                            (
                                canonical_bytes(ref).decode(),
                                report.receipt.nativeStatus or "queued",
                                command.ref.commandId,
                            ),
                        )
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
            return report

    async def flush_outbox(self):
        async with self._report_lock:
            for row in self.db.execute(
                "SELECT * FROM node_v1_outbox WHERE acked=0 ORDER BY rowid"
            ).fetchall():
                raw = json.loads(row["body"])
                model = NodeProbeReport if raw.get("reportKind") == "probe" else NodeReport
                report = model.model_validate(raw)
                ack = await self._request(
                    "POST", f"/commands/{row['command_id']}/result", report.model_dump(mode="json")
                )
                if ack.get("ackRevision") != row["revision"] or ack.get("disposition") not in {
                    "applied",
                    "quarantined",
                    "ignored",
                }:
                    raise TeamsError("node_report_ack_invalid", "节点回执确认不匹配", status=409)
                self.db.execute(
                    "UPDATE node_v1_outbox SET acked=1 WHERE command_id=? AND revision=?",
                    (row["command_id"], row["revision"]),
                )

    def execution(self, command_id):
        return self.db.execute(
            "SELECT * FROM node_v1_executions WHERE command_id=?", (command_id,)
        ).fetchone()

    def record_prepared(self, command, result):
        value = (
            command.ref.commandId,
            result.contextRef,
            result.snapshot.storeIncarnation,
            canonical_bytes(command.ref).decode(),
            canonical_bytes(result).decode(),
        )
        old = self.execution(command.ref.commandId)
        if old and (old["context_ref"], old["store_incarnation"]) != value[1:3]:
            raise StoreIdentityMismatch("prepared execution moved to another Kernel store")
        self.db.execute(
            "INSERT INTO node_v1_executions(command_id,context_ref,store_incarnation,ref,prepared) "
            "VALUES(?,?,?,?,?) ON CONFLICT(command_id) DO NOTHING",
            value,
        )

    async def close(self):
        self._closing, self._lease_deadline = True, 0
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        close_executor = getattr(self.executor, "close", None)
        if close_executor is not None:
            await close_executor()
        self.db.close()
        self._lock_file.close()
        await self.transport.close()


class KernelTeamsNodeExecutor:
    """NodeCommand adapter; native controls retain a separately signed permit.

    Host construction/context loading and native permit retrieval are trusted
    bootstrap dependencies. Missing integration is an explicit closed gate.
    """

    def __init__(
        self,
        resolve_host: Callable[[NodeCommandBase], Awaitable[KernelTeamsExecutionHost]],
        *,
        native_permit_provider: Callable[[NodeCommandBase], Awaitable[AgentControlPermit]]
        | None = None,
        material_proof_provider: Callable[[NodeCommandBase], Awaitable[MaterialProof]]
        | None = None,
        native_control_provider: Callable[
            [NodeCommandBase], Awaitable[tuple[AgentControlCommand, AgentControlPermit]]
        ]
        | None = None,
    ):
        self.resolve_host = resolve_host
        self.native_permit_provider = native_permit_provider
        self.material_proof_provider = material_proof_provider
        self.native_control_provider = native_control_provider

    @staticmethod
    def _permit(command):
        try:
            permit = TeamsExecutionPermit.model_validate_json(command.authorization.permit)
        except ValueError as exc:
            raise PermitError("invalid serialized Teams permit") from exc
        if permit.permitKind != command.authorization.permitKind:
            raise PermitError("node authorization kind mismatch")
        return permit

    @staticmethod
    def _same_ref(expected, actual):
        # Scheduler handoff and observed native id do not rewrite the original
        # execution identity. Every other target/attempt/bundle field is fixed.
        def stable(ref):
            value = ref.model_dump(mode="json") if hasattr(ref, "model_dump") else dict(ref)
            value.pop("schedulerEpoch", None)
            value.pop("nativeRunId", None)
            return value

        if stable(expected) != stable(actual):
            raise ContextConflict("node command differs from prepared execution reference")

    async def execute(self, command, *, journal: TeamsNodeV1, server_time, request_started):
        host = await self.resolve_host(command)
        if not isinstance(host, KernelTeamsExecutionHost):
            raise TypeError("node execution requires KernelTeamsExecutionHost")
        if isinstance(command, NodeProbeCommand):
            from ksadk.plugins.teams.cloud_permits import BindingProbePermit

            permit = BindingProbePermit.model_validate_json(command.authorization.permit)
            if (
                permit.authorityId != command.authorityId
                or permit.bindingRef != command.bindingRef
                or permit.target.kind != "node"
                or permit.target.nodeId != command.nodeId
                or permit.target.nodeGeneration != command.nodeGeneration
                or permit.expectedDigests != command.expectedDigests
            ):
                raise ContextConflict("probe command differs from signed scope")
            value = await host.describe_binding(permit=permit)
            return {
                "phase": "described",
                "probeResult": NodeProbeResult(
                    bindingRef=command.bindingRef,
                    localBindingRef=command.localBindingRef,
                    agentInstanceId=host.binding.agent_instance_id,
                    **value,
                ),
            }
        permit = self._permit(command)
        prior = journal.execution(command.ref.commandId)
        if command.operation == "prepare":
            # The trusted loader resolves both context and grant authorization;
            # caller payload supplies only the opaque reference and checksum.
            preparation = await host.loader(command.payload.contextRef)
            self._same_ref(preparation.context.ref, command.ref)
            if (
                preparation.context.context_digest != command.payload.contextDigest
                or preparation.context.material_manifest_ref != command.payload.materialManifestRef
            ):
                raise ContextConflict("prepare differs from trusted context/material digest")
            incarnation = prior["store_incarnation"] if prior else await host.registry.incarnation()
            result = await host.prepare_execution(
                context_ref=command.payload.contextRef,
                expected_incarnation=incarnation,
                permit=permit,
            )
            journal.record_prepared(command, result)
            values = {"phase": "prepared", "operationResult": result}
            if command.payload.materialManifestRef is not None:
                if self.material_proof_provider is None:
                    raise ContextConflict("material proof transport is not configured")
                proof = await self.material_proof_provider(command)
                if (
                    proof.manifestRef != preparation.context.material_manifest_ref
                    or proof.digest != preparation.context.material_manifest_digest
                ):
                    raise ContextConflict("material report differs from trusted context")
                values["materialProof"] = proof
            return values
        if prior is None:
            raise StoreIdentityMismatch("original node preparation is unavailable")
        self._same_ref(json.loads(prior["ref"]), command.ref)
        context_ref, incarnation = prior["context_ref"], prior["store_incarnation"]
        context = await host.registry.get(context_ref, expected_incarnation=incarnation)
        if context is None:
            raise StoreIdentityMismatch("original Kernel preparation is unavailable")
        common = {"context_ref": context_ref, "expected_incarnation": incarnation, "permit": permit}
        if command.operation == "submit":
            native = AgentControlCommand.model_validate(
                command.authorization.nativeCommand or command.payload.command
            )
            if command.payload.payloadDigest != context.payload_digest:
                raise ContextConflict("node submit digest differs from original Kernel command")
            receipt = await host.lookup_before_submit(
                native, expected_incarnation=incarnation, permit=permit
            )
            if receipt.status == "missing":
                native_permit = getattr(command.authorization, "nativePermit", None)
                if native_permit is None and self.native_permit_provider is not None:
                    native_permit = await self.native_permit_provider(command)
                if native_permit is None:
                    raise ContextConflict("independent native control permit is unavailable")
                native_receipt = await host.submit_execution(
                    native,
                    expected_incarnation=incarnation,
                    permit=permit,
                    native_permit=AgentControlPermit.model_validate(native_permit),
                )
                if native_receipt.status not in {"accepted", "duplicate"}:
                    # A native persistence error is not proof of rejection.
                    if native_receipt.status == "rejected":
                        return {
                            "phase": "rejected",
                            "receipt": HostReceipt(
                                status="rejected",
                                commandId=context.ref.commandId,
                                idempotencyKey=context.ref.idempotencyKey,
                                payloadDigest=context.payload_digest,
                                storeIncarnation=incarnation,
                            ),
                        }
                    raise ContextConflict("native admission needs original inbox reconciliation")
                receipt = await host.lookup_before_submit(
                    native, expected_incarnation=incarnation, permit=permit
                )
            return {"phase": "submitted", "receipt": receipt}
        if command.operation == "lookup":
            payload = command.payload
            if payload.storeIncarnation != incarnation:
                raise StoreIdentityMismatch("lookup targets a different original store")
            receipt = await host.lookup_execution(
                **common,
                command_id=payload.commandId,
                idempotency_key=payload.idempotencyKey,
                payload_digest=payload.payloadDigest,
            )
            if receipt.status in {"accepted", "duplicate"}:
                # Separate get_result authority is required; an Inbox terminal
                # state alone never fabricates canonical evidence.
                if "get_result" in permit.allowedOperations:
                    result = await host.get_execution_result(**common)
                    if result["status"] != "pending":
                        return {
                            "phase": "terminal",
                            "receipt": result["receipt"],
                            "executionResult": ExecutionResult.from_host_result(result),
                        }
            return {
                "phase": "waiting" if receipt.status != "missing" else "uncertain",
                "receipt": receipt,
            }
        if command.operation in {"set_grant", "get_grant", "set_admission"}:
            payload = command.payload
            if payload.grantId != context.grant.grant_id:
                raise ContextConflict("control targets another grant")
            if (
                getattr(payload, "attemptEpoch", context.ref.attemptEpoch)
                != context.ref.attemptEpoch
            ):
                raise ContextConflict("control targets another attempt epoch")
            if command.operation == "get_grant":
                result = await host.get_execution_grant(**common, renewal_id=payload.renewalId)
            elif command.operation == "set_admission":
                result = await host.set_execution_admission(
                    **common,
                    allowed=payload.admissionAllowed,
                    expected_revision=payload.expectedAdmissionRevision,
                    control_id=payload.controlId,
                )
            elif payload.renewalId is not None:
                result = await host.renew_execution_grant(
                    **common,
                    expected_revision=payload.expectedRevision,
                    renewal_id=payload.renewalId,
                    trusted_window=TrustedGrantWindow(
                        payload.expiresAt, server_time, request_started
                    ),
                )
            else:
                result = await host.set_execution_grant(
                    **common,
                    state=payload.state,
                    expected_revision=payload.expectedRevision,
                    control_id=payload.controlId,
                )
            return {"phase": "control_applied", "operationResult": result}
        if command.operation == "observe":
            batch = await host.observe_execution(
                **common, after_seq=command.payload.afterSeq, limit=command.payload.limit
            )
            return {"phase": "running", "eventBatch": batch}
        if command.operation in {"cancel", "respond_interaction"}:
            native, native_permit = (
                command.authorization.nativeCommand,
                command.authorization.nativePermit,
            )
            if native is None or native_permit is None:
                if self.native_control_provider is None:
                    raise ContextConflict("trusted native control transport is not configured")
                native, native_permit = await self.native_control_provider(command)
            native = AgentControlCommand.model_validate(native)
            payload = command.payload
            if (
                str(native.command_id) != payload.controlCommandId
                or native.idempotency_key != payload.controlIdempotencyKey
                or native.payload.get("run_id") != command.ref.nativeRunId
            ):
                raise ContextConflict("native control identity differs from frozen node command")
            if command.operation == "cancel":
                if (
                    native.command_type != "interrupt"
                    or native.payload.get("reason") != payload.reason
                ):
                    raise ContextConflict("native interrupt differs from frozen node command")
            elif native.command_type != "submit_interaction" or any(
                native.payload.get(key) != expected
                for key, expected in {
                    "interaction_id": payload.interactionId,
                    "expected_revision": payload.expectedRevision,
                    "action": payload.action,
                    "response": payload.response,
                }.items()
            ):
                raise ContextConflict("native interaction differs from frozen node command")
            receipt = await host.lookup_control(**common, command=native)
            if receipt.status == "missing":
                receipt = await host.submit_native_control(
                    native, **common, native_permit=native_permit
                )
            return {
                "phase": "submitted"
                if receipt.status in {"accepted", "duplicate"}
                else "uncertain",
                "receipt": receipt,
            }
        raise ContextConflict(
            "node operation requires an explicitly configured native control adapter"
        )

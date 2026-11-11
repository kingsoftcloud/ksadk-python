"""PostgreSQL-backed Kernel extension; base AgentKernel/v1 remains unchanged."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from .client import BrokerError, MCPToolOutcomeUnknown
from .langgraph import MARKER, prepare_connection, require_wait_checkpoint
from .security import digest, verify

_SCHEMA_MIGRATION_LOCK = 0x4B5341444B4D4350  # "KSADKMCP", distinct from the base Kernel schema lock.

SQL = """
CREATE TABLE IF NOT EXISTS mcp_tool_dispatches (
 run_id TEXT NOT NULL, request_key TEXT NOT NULL, call_digest TEXT NOT NULL, result JSONB,
 PRIMARY KEY(run_id,request_key)
);
CREATE TABLE IF NOT EXISTS mcp_authorization_waits (
 wait_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, session_id TEXT NOT NULL,
 run_id TEXT NOT NULL, interaction_id TEXT NOT NULL UNIQUE,
 native_target JSONB NOT NULL, snapshot JSONB NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', revision BIGINT NOT NULL DEFAULT 0,
 attempt_key TEXT, fence BIGINT
);
CREATE TABLE IF NOT EXISTS mcp_authorization_completions (
 id TEXT PRIMARY KEY, wait_id TEXT NOT NULL, command JSONB NOT NULL,
 done BOOLEAN NOT NULL DEFAULT FALSE
);
"""


def decode(row):
    if row is None:
        return None
    data = dict(row)
    for key in ("native_target", "snapshot", "command"):
        if isinstance(data.get(key), str):
            data[key] = json.loads(data[key])
    return data


def _connecting_for(command, snapshot):
    """Only the connecting transition immediately following this signed ready notice."""
    return (
        command["status"] == "credential_ready"
        and snapshot["status"] == "connecting"
        and snapshot["revision"] == command["revision"] + 1
    )


class MCPAuthorizationExtension:
    def __init__(
        self, store, client, *, public_key=None, issuer="agentengine-mcp-broker", jwks_source=None
    ):
        if not hasattr(store, "_pool") or not hasattr(store, "_assert_fence"):
            raise ValueError("MCP OAuth requires the PostgreSQL Kernel store")
        self.store, self.pool, self.client = store, store._pool, client
        self.public_key, self.issuer = public_key, issuer
        self.jwks_source = jwks_source
        self._next_poll = {}
        self._schema_ready = False
        self._activation_lock = asyncio.Lock()
        self._negotiated = False

    async def _tool_binding(self, run_id, config_id):
        from ksadk.runtime_context import get_current_identity_context

        from .security import identity

        context = get_current_identity_context()
        if not context.is_complete:
            raise BrokerError("MCP_OAUTH_RUNTIME_UNSUPPORTED")
        async with self._activation_lock:
            if not self._negotiated:
                await self.client.capabilities()
                await self.ensure_schema()
                self._negotiated = True
        from .langgraph import current_declaration

        run = await self.store.load_run(run_id)
        if not run or not run.metadata.get("authorization_ref") or not run.metadata.get("command_id"):
            raise BrokerError("TASK_BINDING_MISMATCH")
        binding = {
            "command_id": run.metadata["command_id"],
            "authorization_ref": run.metadata["authorization_ref"],
        }
        declaration = current_declaration(config_id)
        if declaration is not None:
            binding["declaration"] = declaration
        return {**binding, "subject_digest": digest(identity(context.to_payload()))}

    async def ensure_authorization(self, run_id, config_id, request_key):
        require_wait_checkpoint(hosted=True)
        binding = await self._tool_binding(run_id, config_id)
        if hasattr(self.client, "prepare"):
            await self.client.prepare(run_id, config_id, **binding)
        return await self.client.ensure(run_id, config_id, request_key, **binding)

    async def lookup_credential(
        self, run_id, config_id, request_key, *, rejected_grant_version=None
    ):
        binding = await self._tool_binding(run_id, config_id)
        query = dict(task_id=run_id, config_id=config_id, request_key=request_key,
                     rejected_grant_version=rejected_grant_version, **binding)
        status = await self.client.call("LookupMcpCredential", **query)
        if status.get("status") == "refresh_required":
            await self.client.prepare(run_id, config_id, **binding)
            return await self.client.call("LookupMcpCredential", **query)
        return status

    async def claim_dispatch(self, run_id, request_key, call_digest):
        """Only the inserter may send. A crash after sending cannot elect a second sender."""
        async with self.pool.acquire() as conn, conn.transaction():
            claimed = await conn.fetchrow(
                "INSERT INTO mcp_tool_dispatches (run_id, request_key, call_digest) "
                "VALUES ($1,$2,$3) "
                "ON CONFLICT DO NOTHING RETURNING run_id",
                run_id,
                request_key,
                call_digest,
            )
            if claimed:
                return None
            row = await conn.fetchrow(
                "SELECT call_digest,result FROM mcp_tool_dispatches "
                "WHERE run_id=$1 AND request_key=$2",
                run_id,
                request_key,
            )
            if row["call_digest"] != call_digest:
                raise BrokerError("MCP_TOOL_REPLAY_MISMATCH")
            if row["result"] is None:
                raise MCPToolOutcomeUnknown(
                    "MCP_TOOL_OUTCOME_UNKNOWN: the dispatched operation "
                    "cannot be automatically repeated"
                )
            return json.loads(row["result"]) if isinstance(row["result"], str) else row["result"]

    async def release_declined_dispatch(self, run_id, request_key, call_digest):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM mcp_tool_dispatches WHERE run_id=$1 AND request_key=$2 "
                "AND call_digest=$3 AND result IS NULL",
                run_id,
                request_key,
                call_digest,
            )

    async def finish_dispatch(self, run_id, request_key, call_digest, result):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE mcp_tool_dispatches SET result=$4::jsonb "
                "WHERE run_id=$1 AND request_key=$2 AND call_digest=$3 AND result IS NULL",
                run_id,
                request_key,
                call_digest,
                json.dumps(result),
            )

    async def ensure_schema(self):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # IF NOT EXISTS alone races on PostgreSQL catalog creation across Pods.
                await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", _SCHEMA_MIGRATION_LOCK)
                await conn.execute(SQL)
        self._schema_ready = True

    async def row(self, wait_id):
        async with self.pool.acquire() as conn:
            return decode(
                await conn.fetchrow(
                    "SELECT * FROM mcp_authorization_waits WHERE wait_id=$1", wait_id
                )
            )

    async def pending(self, run_id):
        if not self._schema_ready:
            return []
        async with self.pool.acquire() as conn:
            return [
                decode(r)
                for r in await conn.fetch(
                    "SELECT * FROM mcp_authorization_waits WHERE run_id=$1 AND status IN "
                    "('pending','resolving','unknown')",
                    run_id,
                )
            ]

    async def cancellation_targets(self, run_id):
        if not self._schema_ready:
            return []
        async with self.pool.acquire() as conn:
            return [
                decode(r)["native_target"]
                for r in await conn.fetch(
                    "SELECT native_target FROM mcp_authorization_waits WHERE run_id=$1 AND "
                    "status IN ('pending','resolving','unknown','cancelled')",
                    run_id,
                )
            ]

    async def pending_session_ids(self, instance_id):
        if not self._schema_ready:
            return set()
        async with self.pool.acquire() as conn:
            return {
                row["session_id"]
                for row in await conn.fetch(
                    "SELECT DISTINCT w.session_id FROM mcp_authorization_waits w JOIN kernel_runs r "
                    "ON r.run_id=w.run_id AND r.agent_instance_id=w.instance_id AND r.session_id=w.session_id "
                    "WHERE w.instance_id=$1 AND w.status='consumed'",
                    instance_id,
                )
            }

    async def acknowledge_consumed(self, worker, row, guard):
        from ksadk.kernel.state import RunState

        run = await self.store.load_run(row["run_id"])
        execution = worker._executions.get(row["run_id"])
        if execution and execution.stream_task and not execution.stream_task.done():
            return False
        # Consumption precedes the final event/checkpoint handoff. A process
        # dying in that gap must not advertise a successful task continuation
        # or replay a tool whose result may already have been committed.
        if run.state in {RunState.RUNNING, RunState.WAITING}:
            native = (run.metadata.get("handle") or {}).get("native_ref") or {}
            if row["interaction_id"] in native.get("native_waits", {}):
                await self._state(row, "unknown", guard.fencing_token)
                try:
                    snapshot = await self.client.confirm(row["wait_id"], phase="resume_unknown")
                    await self.publish(worker, run, snapshot, guard)
                except BrokerError:
                    pass
                return False
        try:
            snapshot = await self.client.confirm(row["wait_id"], phase="resumed")
        except BrokerError:
            return False
        await self.publish(worker, run, snapshot, guard)
        await self._state(row, "resolved", guard.fencing_token)
        return True

    @staticmethod
    def _authorization_event(run, snapshot, requested):
        from ksadk.kernel.store import control_event

        envelope = control_event(
            session_id=run.session_id,
            run_id=run.run_id,
            payload=snapshot,
            event_type="mcp.authorization.requested" if requested else "mcp.authorization.updated",
        )
        # Local session history can live in a different SQLite database.
        event_id = uuid5(
            NAMESPACE_URL, f"mcp.authorization/{snapshot['wait_id']}/{snapshot['revision']}"
        )
        return envelope.model_copy(update={"event_id": event_id})

    async def publish(self, worker, run, snapshot, guard):
        # A takeover sees both the public fact and its revision, or neither.
        async with self.pool.acquire() as conn, conn.transaction():
            await self.store._assert_fence(
                conn, run.agent_instance_id, run.session_id, guard.fencing_token
            )
            row = decode(
                await conn.fetchrow(
                    "SELECT snapshot FROM mcp_authorization_waits WHERE wait_id=$1 AND run_id=$2 FOR UPDATE",
                    snapshot["wait_id"],
                    run.run_id,
                )
            )
            if row is None:
                raise BrokerError("WAIT_BINDING_MISMATCH")
            previous = row["snapshot"]
            if previous.get("revision", 0) >= snapshot["revision"]:
                return
            await conn.execute(
                "UPDATE mcp_authorization_waits SET snapshot=$2::jsonb WHERE wait_id=$1",
                snapshot["wait_id"],
                json.dumps(snapshot),
            )
            await self.store._events.append_on(
                conn,
                self._authorization_event(run, snapshot, not previous.get("requires_resume")),
                guard,
            )

    async def record(self, worker, execution, run, event, guard):
        from ksadk.kernel.state import RunState
        from ksadk.runtime.executor import handle_digest

        wait_id = event.request.schema_[MARKER]
        handle = execution.handle
        native_waits = handle.native_ref.get("native_waits", {})
        native = native_waits.get(event.interaction_id)
        if (
            not native
            or native != event._native_target
            or not all(native.get(k) for k in ("checkpoint_id", "thread_id", "call_id", "task_id"))
        ):
            raise BrokerError("WAIT_NOT_DURABLE")
        latest = await worker._store.load_run(run.run_id)
        recorded = latest.metadata.get("mcp_authorization")
        bindings = {}
        if isinstance(recorded, dict):
            bindings = {key: value for key, value in recorded.items() if key in native_waits}
        if bindings.get(event.interaction_id, wait_id) != wait_id:
            raise BrokerError("WAIT_BINDING_MISMATCH")
        bindings[event.interaction_id] = wait_id
        # Commit the complete wait binding with WAITING before any remote I/O.
        # The extension index can then be rebuilt using only this fenced run.
        latest = await worker._store.save_run_transition(
            latest.model_copy(
                update={
                    "state": RunState.WAITING,
                    "metadata": {
                        **latest.metadata,
                        "handle": handle.model_dump(mode="json"),
                        "handle_digest": handle_digest(handle),
                        "mcp_authorization": bindings,
                    },
                }
            ),
            expected_fence=guard.fencing_token,
        )
        await self._confirm_recorded(worker, latest, event.interaction_id, wait_id, guard)
        return latest

    async def _confirm_recorded(self, worker, run, interaction_id, wait_id, guard):
        native = run.metadata["handle"]["native_ref"]["native_waits"][interaction_id]
        try:
            snapshot = await self.client.get(wait_id)
            if snapshot["run_id"] != run.run_id or snapshot["wait_id"] != wait_id:
                raise BrokerError("WAIT_BINDING_MISMATCH")
            async with self.pool.acquire() as conn, conn.transaction():
                await self.store._assert_fence(
                    conn, run.agent_instance_id, run.session_id, guard.fencing_token
                )
                await conn.execute(
                    "INSERT INTO mcp_authorization_waits "
                    "(wait_id,instance_id,session_id,run_id,interaction_id,native_target,snapshot) "
                    "VALUES ($1,$2,$3,$4,$5,$6::jsonb,'{}') ON CONFLICT (wait_id) DO NOTHING",
                    wait_id,
                    run.agent_instance_id,
                    run.session_id,
                    run.run_id,
                    interaction_id,
                    json.dumps(native),
                )
                row = decode(
                    await conn.fetchrow(
                        "SELECT * FROM mcp_authorization_waits WHERE wait_id=$1", wait_id
                    )
                )
                if (
                    row["native_target"] != native
                    or row["run_id"] != run.run_id
                    or row["interaction_id"] != interaction_id
                ):
                    raise BrokerError("WAIT_BINDING_MISMATCH")
            snapshot = await self.client.confirm(
                wait_id,
                {
                    "checkpoint_id": native["checkpoint_id"],
                    "thread_id": native["thread_id"],
                    "interrupt_id": native["call_id"],
                },
            )
        except BrokerError as exc:
            if exc.code not in BrokerError.transient_codes:
                raise
            # Keep the original run waiting. A lost response may already have
            # committed remotely. A new owner repeats this exact confirmation.
            return
        await self.publish(worker, run, snapshot, guard)

    async def accept(self, command, permit):
        public_key = self.public_key
        if self.jwks_source is not None:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            from .security import unb64

            try:
                if len(permit) > 16384:
                    raise ValueError()
                header = json.loads(unb64(permit.split(".")[0]))
                keys = await self.jwks_source.fetch_verification_keys()
                public_key = Ed25519PublicKey.from_public_bytes(unb64(keys[header["kid"]]))
            except Exception:
                raise BrokerError("INVALID_COMPLETION_PERMIT") from None
        claims = verify(
            permit, public_key, issuer=self.issuer, audience="mcp-authorization-runtime"
        )
        if (
            command.get("schema") != "mcp.authorization/v1"
            or command.get("operation") != "resolve_external_authorization"
            or claims.get("body_digest") != digest(command)
            or claims.get("sub") != command.get("instance_id")
            or type(command.get("revision")) is not int
        ):
            raise BrokerError("INVALID_COMPLETION_PERMIT")
        row = await self.row(command["wait_id"])
        if row is None or any(
            row[k] != command.get(k) for k in ("run_id", "session_id", "instance_id")
        ):
            raise BrokerError("WAIT_BINDING_MISMATCH")
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO mcp_authorization_completions(id,wait_id,command) "
                "VALUES($1,$2,$3::jsonb) ON CONFLICT DO NOTHING",
                f"{row['wait_id']}:{command['revision']}",
                row["wait_id"],
                json.dumps(command),
            )
        return {"status": "accepted"}

    async def require_completion(self, wait_id, run_id, revision):
        row = await self.row(wait_id)
        if (
            row is None
            or row["run_id"] != run_id
            or row["status"] not in {"resolving", "consumed", "resolved"}
            or row["revision"] != revision
        ):
            raise BrokerError("AUTHORIZATION_COMPLETION_REQUIRED")

    async def _attach(self, worker, run):
        if run.run_id in worker._executions:
            return worker._executions[run.run_id]
        from ksadk.runtime.adapter import RunHandle
        from ksadk.runtime.executor import handle_digest

        handle = RunHandle.model_validate(run.metadata["handle"])
        if handle_digest(handle) != run.metadata["handle_digest"]:
            raise BrokerError("CHECKPOINT_BINDING_MISMATCH")
        adapter = worker._adapter_factory()
        prepare = getattr(adapter, "prepare_attach_record", None)
        if prepare:
            await prepare(handle, run)
        restored = await adapter.durable_restore(handle)
        if restored != handle:
            raise BrokerError("CHECKPOINT_BINDING_MISMATCH")
        await adapter.restore_run_record(handle, run)
        return worker.adopt_execution(
            durable_run_id=run.run_id,
            runtime_run_id=run.metadata.get("runtime_run_id", handle.run_id),
            adapter=adapter,
            handle=handle,
        )

    async def tick(self, worker, activation, session_id):
        from ksadk.kernel.contracts import ActivationWriteGuard
        from ksadk.kernel.state import RunState

        if not self._schema_ready:
            # Another pod may have saved this run after this worker started.
            run = await self.store.find_active_run(activation.agent_instance_id, session_id)
            if not run or not run.metadata.get("mcp_authorization"):
                return False
            await self.ensure_schema()
        now = time.monotonic()
        if self._next_poll.get(session_id, 0) > now:
            return False
        self._next_poll[session_id] = now + 1
        guard = ActivationWriteGuard(
            activation_id=activation.activation_id, fencing_token=activation.fencing_token
        )
        async with self.pool.acquire() as conn:
            consumed = [
                decode(r)
                for r in await conn.fetch(
                    "SELECT * FROM mcp_authorization_waits WHERE instance_id=$1 AND "
                    "session_id=$2 AND status='consumed'",
                    activation.agent_instance_id,
                    session_id,
                )
            ]
        for row in consumed:
            await self.acknowledge_consumed(worker, row, guard)
        run = await self.store.find_active_run(activation.agent_instance_id, session_id)
        if run is None or not run.metadata.get("mcp_authorization"):
            return False
        pending = await self.pending(run.run_id)
        recorded = run.metadata["mcp_authorization"]
        if isinstance(recorded, dict):
            # A crash after WAITING committed may precede index creation or acknowledgement.
            indexed = {row["wait_id"] for row in pending}
            for interaction_id, wait_id in recorded.items():
                if wait_id not in indexed and await self.row(wait_id) is None:
                    await self._confirm_recorded(worker, run, interaction_id, wait_id, guard)
        for row in pending:
            try:
                snapshot = await self.client.get(row["wait_id"])
                if not row["snapshot"].get("requires_resume") or not snapshot["requires_resume"]:
                    native = row["native_target"]
                    snapshot = await self.client.confirm(row["wait_id"], {
                        "checkpoint_id": native["checkpoint_id"], "thread_id": native["thread_id"],
                        "interrupt_id": native["call_id"],
                    })
            except BrokerError:
                # Preserve the wait while allowing unrelated inbox controls.
                continue
            if snapshot != row["snapshot"]:
                await self.publish(worker, run, snapshot, guard)
            if row["status"] == "resolving" and snapshot["status"] == "resumed":
                await self._state(row, "resolved", guard.fencing_token)
                continue
            if row["status"] == "resolving" and row["fence"] != guard.fencing_token:
                # A takeover cannot establish whether an external tool ran.
                # Preserve the native checkpoint and expose uncertainty.
                await self._state(row, "unknown", guard.fencing_token)
                snapshot = await self.client.confirm(row["wait_id"], phase="resume_unknown")
                await self.publish(worker, run, snapshot, guard)
            if row["status"] == "unknown" and snapshot["status"] == "connecting":
                snapshot = await self.client.confirm(row["wait_id"], phase="resume_unknown")
                await self.publish(worker, run, snapshot, guard)
            if row["status"] != "pending" and snapshot["status"] not in {"cancelled", "expired"}:
                continue
            async with self.pool.acquire() as conn:
                entry = decode(
                    await conn.fetchrow(
                        "SELECT * FROM mcp_authorization_completions WHERE wait_id=$1 AND "
                        "done=FALSE ORDER BY id LIMIT 1",
                        row["wait_id"],
                    )
                )
            if not entry:
                continue
            command = entry["command"]
            # A previous worker may have confirmed connecting before it could
            # reserve the native resume. Only a still-pending row can take over.
            confirmed = row["status"] == "pending" and _connecting_for(command, snapshot)
            if not confirmed and (
                command["revision"] != snapshot["revision"]
                or command["status"] != snapshot["status"]
            ):
                await self._done(entry["id"])
                continue
            execution = await self._attach(worker, run)
            if execution.stream_task is not None and not execution.stream_task.done():
                continue
            if snapshot["status"] in {"cancelled", "expired"}:
                if not await worker._cancel_suspended_langgraph(execution, run, activation):
                    raise BrokerError("CHECKPOINT_BINDING_MISMATCH")
                await self._state(row, "cancelled", guard.fencing_token)
                await self._done(entry["id"])
                return True
            if command["status"] != "credential_ready":
                continue
            try:
                await prepare_connection(self.client, row["wait_id"])
            except BrokerError as exc:
                failed = await self.client.confirm(
                    row["wait_id"],
                    phase=(
                        "authorization_failed"
                        if exc.code in {"REAUTHORIZATION_REQUIRED", "INSUFFICIENT_SCOPE"}
                        else "connection_failed"
                    ),
                    error=exc.code,
                    rejected_grant_version=exc.rejected_grant_version,
                )
                if failed["status"] in {"credential_ready", "connecting"}:
                    # A refresh won the race. Retry this same completion before native resume.
                    continue
                await self.publish(worker, run, failed, guard)
                await self._done(entry["id"])
                return True
            snapshot = await self.client.confirm(row["wait_id"], phase="connecting")
            if not _connecting_for(command, snapshot):
                # Cancellation or expiry may have won during the MCP preflight.
                continue
            await self.publish(worker, run, snapshot, guard)
            from ksadk.interaction.contracts import InteractionRecord, InteractionSubmission
            from ksadk.interaction.provider import InteractionResolveContext

            record = InteractionRecord(
                interaction_id=row["interaction_id"],
                tenant_id=str(run.metadata["tenant_id"]),
                agent_instance_id=run.agent_instance_id,
                session_id=run.session_id,
                run_id=run.run_id,
                kind="structured_input",
                request_schema={"type": "object"},
                created_at=datetime.now(timezone.utc).isoformat(),
                provider_id="langgraph",
                native_target=row["native_target"],
            )
            attempt = entry["id"]
            async with self.pool.acquire() as conn, conn.transaction():
                await self.store._assert_fence(
                    conn, run.agent_instance_id, run.session_id, guard.fencing_token
                )
                reserved = await conn.fetchrow(
                    "UPDATE mcp_authorization_waits SET "
                    "status='resolving',revision=$2,attempt_key=$3,fence=$4 WHERE wait_id=$1 "
                    "AND status='pending' RETURNING wait_id",
                    row["wait_id"],
                    command["revision"],
                    attempt,
                    guard.fencing_token,
                )
            if not reserved:
                continue
            submission = InteractionSubmission(
                interaction_id=record.interaction_id,
                expected_revision=1,
                action="submit",
                response={"wait_id": row["wait_id"], "revision": command["revision"]},
                idempotency_key=attempt,
            )
            try:
                execution.adapter.remember_native_interactions(execution.handle, [record])
                resumed = await execution.interaction_provider.resolve(
                    InteractionResolveContext(
                        adapter=execution.adapter,
                        handle=execution.handle,
                        activation_id=activation.activation_id,
                        fencing_token=guard.fencing_token,
                        session_context=(run.metadata.get("start_request") or {})
                        .get("metadata", {})
                        .get("session_context"),
                    ),
                    record,
                    submission,
                )
                execution = worker._replace_handle(execution, resumed)
                running = await worker._transition_run(run, RunState.RUNNING, guard.fencing_token)
                worker._start_stream(execution, running, guard)
            except Exception:
                # A provider can fail after accepting the native resume. Fence this
                # attempt before notifying the Broker; replay could run a tool twice.
                async with self.pool.acquire() as conn, conn.transaction():
                    await self.store._assert_fence(conn, row["instance_id"], row["session_id"], guard.fencing_token)
                    changed = await conn.fetchval(
                        "UPDATE mcp_authorization_waits SET status='unknown' "
                        "WHERE wait_id=$1 AND status='resolving' AND attempt_key=$2 AND fence=$3 RETURNING wait_id",
                        row["wait_id"], attempt, guard.fencing_token,
                    )
                if changed:
                    snapshot = await self.client.confirm(row["wait_id"], phase="resume_unknown")
                    await self.publish(worker, run, snapshot, guard)
                return False
            await self._done(entry["id"])
            return True
        return False

    async def _done(self, key):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE mcp_authorization_completions SET done=TRUE WHERE id=$1", key
            )

    async def _state(self, row, status, fence):
        async with self.pool.acquire() as conn, conn.transaction():
            await self.store._assert_fence(conn, row["instance_id"], row["session_id"], fence)
            await conn.execute(
                "UPDATE mcp_authorization_waits SET status=$2 WHERE wait_id=$1",
                row["wait_id"],
                status,
            )

    async def cancel_run(self, worker, run, guard):
        for row in await self.pending(run.run_id):
            snapshot = await self.client.call("CancelMcpAuthorizationWait", wait_id=row["wait_id"])
            await self.publish(worker, run, snapshot, guard)
            await self._state(row, "cancelled", guard.fencing_token)

    async def consumed(self, worker, run, event, guard):
        if not run.metadata.get("mcp_authorization"):
            return
        for outcome in event._outcomes:
            async with self.pool.acquire() as conn:
                row = decode(
                    await conn.fetchrow(
                        "SELECT * FROM mcp_authorization_waits WHERE interaction_id=$1",
                        outcome.interaction_id,
                    )
                )
            if not row or row["status"] != "resolving" or row["attempt_key"] != outcome.attempt_key:
                continue
            if outcome.status == "consumed":
                await self._state(row, "consumed", guard.fencing_token)
            else:
                await self._state(row, "unknown", guard.fencing_token)
                try:
                    snapshot = await self.client.confirm(row["wait_id"], phase="resume_unknown")
                    await self.publish(worker, run, snapshot, guard)
                except BrokerError:
                    pass  # durable unknown is reconciled by the next tick


def install_routes(app, extension):
    from .http import install_public_routes

    install_public_routes(app, extension)
    from fastapi import Request
    from fastapi.responses import JSONResponse

    async def resolve(request: Request):
        try:
            data = await request.json()
            return await extension.accept(data["command"], data["permit"])
        except (ValueError, KeyError, BrokerError):
            return JSONResponse({"error": "INVALID_AUTHORIZATION_COMPLETION"}, status_code=403)

    # Resolve postponed annotation for FastAPI's module-scoped inspection.
    resolve.__annotations__["request"] = Request
    app.add_api_route(
        "/agent-kernel/extensions/mcp-authorization/v1/resolve", resolve, methods=["POST"]
    )

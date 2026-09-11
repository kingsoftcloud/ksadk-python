"""Authorized plugin execution services over Studio's shared Build registry.

This module owns transport authorization and immutable execution references.
It has no group/task/leader policy. Installed plugins register their own
authorization and policy resolvers; all commands still enter AgentKernel.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from ksadk.conversations.projector import (
    project_conversation_item,
    project_interaction_conversation_item,
)
from ksadk.events.canonical import parse_runtime_event
from ksadk.kernel.contracts import AgentControlCommand
from ksadk.kernel.execution_grants import ExecutionGrantSpec, execution_grant_run_id
from ksadk.kernel.ingress import trusted_context
from ksadk.plugins.execution_host import (
    ExecutionBarrier,
    ExecutionReceipt,
    PluginExecutionScope,
)
from ksadk.studio.kernel_registry import StudioBuildKernelRegistry

Authorize = Callable[[PluginExecutionScope, str], None]
ResolvePolicy = Callable[..., Awaitable[Any]]


class ExecutionHostError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = 403) -> None:
        super().__init__(message)
        self.code, self.status = code, status


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


class StudioExecutionHost:
    def __init__(self, registry: StudioBuildKernelRegistry, *, state_path: Path) -> None:
        self.registry = registry
        state_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(state_path, isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS plugin_session_scopes (
                session_id TEXT PRIMARY KEY, scope TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS execution_policy_refs (
                ref TEXT PRIMARY KEY, scope TEXT NOT NULL, context TEXT NOT NULL,
                command_id TEXT NOT NULL, run_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS plugin_grant_operations (
                operation_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
                expected_revision INTEGER NOT NULL);
        """)
        self._plugins: dict[str, tuple[str, Authorize, ResolvePolicy]] = {}
        self._grant_lock = asyncio.Lock()
        self._dispatch_lock = asyncio.Lock()
        self._admission_open = True

    def register_plugin(
        self, plugin_id: str, digest: str, *, authorize: Authorize, resolve_policy: ResolvePolicy
    ) -> Callable[[], None]:
        if plugin_id in self._plugins:
            raise ExecutionHostError("plugin_already_registered", "插件已注册")
        registration = (digest, authorize, resolve_policy)
        self._plugins[plugin_id] = registration

        def dispose() -> None:
            if self._plugins.get(plugin_id) is registration:
                self._plugins.pop(plugin_id)

        return dispose

    def _authorize(self, scope: PluginExecutionScope, operation: str) -> None:
        registered = self._plugins.get(scope.plugin_id)
        if registered is None or registered[0] != scope.plugin_digest:
            raise ExecutionHostError("plugin_grant_unavailable", "插件执行授权不可用")
        registered[1](scope, operation)
        row = self._db.execute(
            "SELECT scope FROM plugin_session_scopes WHERE session_id=?", (scope.session_id,)
        ).fetchone()
        if row and row[0] != _json(asdict(scope)):
            raise ExecutionHostError("execution_scope_mismatch", "会话绑定与执行授权不一致")

    @staticmethod
    def build_id(binding_ref: str) -> str:
        prefix = "local-build:"
        if not binding_ref.startswith(prefix) or not binding_ref[len(prefix) :]:
            raise ExecutionHostError("binding_unsupported", "当前宿主不支持该执行绑定", status=422)
        return binding_ref[len(prefix) :]

    async def _target(self, scope: PluginExecutionScope, operation: str):
        self._authorize(scope, operation)
        target = await self.registry.ensure_build(self.build_id(scope.binding_ref))
        if target.tenant_id != scope.tenant_id:
            raise ExecutionHostError("execution_tenant_mismatch", "执行绑定不属于当前授权域")
        return target, self.registry.runtime_for_build(target.build_id)

    async def describe(self, scope: PluginExecutionScope) -> dict[str, Any]:
        target, runtime = await self._target(scope, "describe")
        spec = self.registry.spec_for_build(target.build_id)
        matrix = runtime.kernel.capabilities()
        policy_capability = getattr(matrix, "execution_policy", None)
        policy_supported = bool(policy_capability and policy_capability.supported)
        return {
            "bindingRef": scope.binding_ref,
            "providerRef": spec.request_config.get(
                "provider_ref", spec.launch_context.runtime_type
            ),
            "kind": "local_build",
            "agentId": target.agent_id,
            "buildId": target.build_id,
            "authorityRef": scope.authority_ref,
            "tenantId": target.tenant_id,
            "pluginLockDigest": spec.request_config.get(
                "plugin_bundle_digest", spec.manifest_sha256
            ),
            "capabilities": {
                "enqueue": policy_supported,
                "leader": policy_supported,
                "cancel": matrix.cancel.supported,
                "restore": matrix.durable_restore.supported,
                "steer": matrix.steer.supported,
                "interaction": matrix.submit_interaction.supported,
            },
            "capabilitySnapshot": matrix.model_dump(mode="json"),
        }

    async def ensure_session(self, scope: PluginExecutionScope) -> None:
        target, _ = await self._target(scope, "ensure_session")
        await self.registry.ensure_session(target.build_id, scope.session_id, scope.owner_subject)
        self._db.execute(
            "INSERT OR IGNORE INTO plugin_session_scopes VALUES(?,?)",
            (scope.session_id, _json(asdict(scope))),
        )
        self._authorize(scope, "ensure_session")

    def require_unreserved_session(self, session_id: str) -> None:
        if self.is_reserved_session(session_id):
            raise ExecutionHostError(
                "plugin_session_control_required", "该会话由插件管理，请从插件页面操作"
            )

    def is_reserved_session(self, session_id: str) -> bool:
        return bool(
            self._db.execute(
                "SELECT 1 FROM plugin_session_scopes WHERE session_id=?", (session_id,)
            ).fetchone()
        )

    def require_unreserved_run(self, run_id: str) -> None:
        if self._db.execute(
            "SELECT 1 FROM execution_policy_refs WHERE run_id=?", (run_id,)
        ).fetchone():
            raise ExecutionHostError(
                "plugin_run_control_required", "该执行由插件管理，请从插件页面操作"
            )

    @staticmethod
    def _grant(scope: PluginExecutionScope, target: Any, grant_id: str) -> ExecutionGrantSpec:
        return ExecutionGrantSpec(
            grant_id=grant_id,
            tenant_id=scope.tenant_id,
            agent_instance_id=target.agent_instance_id,
            session_id=scope.session_id,
            owner_ref="plugin:"
            + _hash(
                [scope.plugin_id, scope.plugin_digest, scope.authority_ref, scope.owner_subject]
            ),
        )

    def _command(self, scope, target, verb, key, payload, causation=None):
        trusted = trusted_context(
            source_kind="system",
            source_ref=scope.plugin_id,
            tenant_id=scope.tenant_id,
            agent_instance_id=target.agent_instance_id,
            session_id=scope.session_id,
            operations=(verb,),
        )
        command = AgentControlCommand(
            command_id=uuid5(NAMESPACE_URL, _json([scope.plugin_id, scope.session_id, verb, key])),
            idempotency_key=key,
            tenant_id=scope.tenant_id,
            agent_instance_id=target.agent_instance_id,
            session_id=scope.session_id,
            command_type=verb,
            payload=payload,
            source=trusted.source,
            authorization_ref=trusted.permit.permit_id,
            submitted_at=trusted.received_at,
            causation_id=causation,
        )
        return command, trusted.permit

    async def set_admission_open(self, enabled: bool) -> None:
        async with self._dispatch_lock:
            self._admission_open = enabled

    async def submit(self, scope, **arguments):
        async with self._dispatch_lock:
            if not self._admission_open:
                raise ExecutionHostError("host_maintenance", "插件宿主正在维护", status=503)
            return await self._submit_authorized(scope, **arguments)

    async def _submit_authorized(
        self, scope, *, content, idempotency_key, grant_id, causation, policy_context
    ):
        target, runtime = await self._target(scope, "submit")
        grant = self._grant(scope, target, grant_id)
        await runtime.kernel.ensure_execution_grant(grant)
        ref = "policy_" + _hash([asdict(scope), idempotency_key])
        command, permit = self._command(
            scope,
            target,
            "enqueue",
            idempotency_key,
            {
                "content": content,
                "execution_grant_id": grant_id,
                "execution_policy_ref": ref,
            },
            causation,
        )
        expected = (
            _json(asdict(scope)),
            _json(policy_context),
            str(command.command_id),
            execution_grant_run_id(command),
        )
        self._db.execute(
            "INSERT OR IGNORE INTO execution_policy_refs VALUES(?,?,?,?,?)", (ref, *expected)
        )
        stored = self._db.execute(
            "SELECT scope,context,command_id,run_id FROM execution_policy_refs WHERE ref=?", (ref,)
        ).fetchone()
        if stored != expected:
            raise ExecutionHostError(
                "policy_idempotency_conflict", "原执行授权内容已改变", status=409
            )
        receipt = await self.registry.submit_control(command, permit)
        if receipt.status not in {"accepted", "duplicate"}:
            return ExecutionReceipt(
                "uncertain" if receipt.status == "persistence_uncertain" else "rejected",
                command_id=str(command.command_id),
                message_id=str(receipt.message_id) if receipt.message_id else None,
                reason=receipt.error.code if receipt.error else receipt.status,
            )
        return await self.lookup(scope, idempotency_key)

    async def lookup(self, scope, idempotency_key):
        target, runtime = await self._target(scope, "lookup")
        message = await runtime.kernel_store.load_by_idempotency(scope.session_id, idempotency_key)
        if message is None:
            return ExecutionReceipt("missing")
        command = message.command
        if command is None or message.agent_instance_id != target.agent_instance_id:
            raise ExecutionHostError("command_scope_mismatch", "命令作用域不一致")
        run_id = execution_grant_run_id(command)
        run = await runtime.kernel_store.load_run(run_id)
        if run is None:
            return ExecutionReceipt(
                "rejected" if message.status == "discarded" else "accepted",
                command_id=str(command.command_id),
                message_id=message.message_id,
                reason="execution_admission_revoked" if message.status == "discarded" else None,
            )
        status = {
            "completed": "succeeded",
            "paused": "awaiting_approval",
            "waiting": "awaiting_approval",
            "pending": None,
        }.get(run.state.value, run.state.value)
        output, tokens, source_id = "", 0, None
        if run.state in {"completed", "failed", "cancelled", "interrupted"}:
            # Terminal-only scan; streaming observation uses its own cursor.
            # Stable completed item identities avoid duplicate final chunks.
            messages = {}
            cursor = 0
            while True:
                events = await runtime.session_events.read(scope.session_id, cursor, 500)
                if not events:
                    break
                cursor = events[-1].seq
                for envelope in events:
                    if envelope.run_id != run_id or envelope.family != "runtime":
                        continue
                    payload = envelope.payload
                    if envelope.event_type == "usage.reported":
                        tokens += int(
                            payload.get("total_tokens")
                            or int(payload.get("input_tokens") or 0)
                            + int(payload.get("output_tokens") or 0)
                        )
                    source_metadata = (payload.get("source") or {}).get("metadata") or {}
                    if envelope.family_version == 2 and not source_metadata.get("parent_run_id"):
                        item = project_conversation_item(
                            parse_runtime_event(payload), session_id=scope.session_id
                        )
                        if item.kind == "assistant_text" and item.lifecycle == "completed":
                            messages[item.item_id] = str(item.payload.get("text") or "")
                    source_id = str(envelope.event_id)
            output = "\n\n".join(messages.values())
        return ExecutionReceipt(
            "accepted",
            command_id=str(command.command_id),
            message_id=message.message_id,
            run_id=run_id,
            run_status=status,
            reason="provider_restore_required" if status == "interrupted" else None,
            output=output,
            tokens=tokens,
            source_event_id=source_id,
        )

    async def set_grant(self, scope, grant_id, state, idempotency_key):
        target, runtime = await self._target(scope, "set_grant")
        spec = self._grant(scope, target, grant_id)
        async with self._grant_lock:
            record = await runtime.kernel.ensure_execution_grant(spec)
            operation_id = _hash([asdict(scope), idempotency_key])
            operation_digest = _hash([spec.model_dump(), state])
            self._db.execute(
                "INSERT OR IGNORE INTO plugin_grant_operations VALUES(?,?,?)",
                (operation_id, operation_digest, record.revision),
            )
            row = self._db.execute(
                "SELECT digest,expected_revision FROM plugin_grant_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row[0] != operation_digest:
                raise ExecutionHostError(
                    "grant_idempotency_conflict", "原授权变更内容已改变", status=409
                )
            barrier = await runtime.kernel.set_execution_grant_state(
                spec, state, expected_revision=row[1], idempotency_key=operation_id
            )
        return ExecutionBarrier(
            barrier.grant.state,
            barrier.grant.revision,
            in_flight=barrier.in_flight_message_ids,
            queued=barrier.queued_message_ids,
            discarded=barrier.discarded_message_ids,
            details={"commands": [item.model_dump() for item in barrier.commands]},
        )

    async def cancel(self, scope, run_id, idempotency_key):
        target, runtime = await self._target(scope, "cancel")
        await self._require_run(scope, runtime, target, run_id)
        command, permit = self._command(
            scope, target, "interrupt", idempotency_key, {"run_id": run_id}
        )
        receipt = await self.registry.submit_control(command, permit)
        return ExecutionReceipt(
            "accepted" if receipt.status in {"accepted", "duplicate"} else "rejected",
            command_id=str(command.command_id),
            message_id=str(receipt.message_id) if receipt.message_id else None,
            run_id=run_id,
        )

    @staticmethod
    async def _require_run(scope, runtime, target, run_id):
        run = await runtime.kernel_store.load_run(run_id)
        if (
            not run
            or run.session_id != scope.session_id
            or run.agent_instance_id != target.agent_instance_id
        ):
            raise ExecutionHostError("run_scope_mismatch", "执行不属于此会话", status=404)
        return run

    async def interactions(self, scope):
        _, runtime = await self._target(scope, "interactions")
        records = await runtime.kernel_store.list_pending_interactions(
            scope.tenant_id, scope.session_id
        )
        return [{**record.public_request(), "status": record.status} for record in records]

    async def submit_interaction(
        self,
        scope,
        *,
        run_id,
        interaction_id,
        expected_revision,
        action,
        response,
        idempotency_key,
        actor_subject,
    ):
        target, runtime = await self._target(scope, "submit_interaction")
        if actor_subject != scope.owner_subject:
            raise ExecutionHostError("interaction_actor_forbidden", "只有授权人类主体可以回复审批")
        await self._require_run(scope, runtime, target, run_id)
        record = await runtime.kernel_store.get(
            interaction_id,
            tenant_id=scope.tenant_id,
            agent_instance_id=target.agent_instance_id,
            session_id=scope.session_id,
            run_id=run_id,
        )
        if not record:
            raise ExecutionHostError(
                "interaction_not_found", "审批不存在或不属于此执行", status=404
            )
        previous = await runtime.kernel_store.load_by_idempotency(scope.session_id, idempotency_key)
        if previous is None:
            if record.revision != expected_revision:
                raise ExecutionHostError(
                    "interaction_revision_conflict", "审批状态已更新，请刷新后重试", status=409
                )
            if record.status not in {"pending", "resolving"}:
                raise ExecutionHostError("interaction_already_resolved", "审批已经处理", status=409)
            if record.kind != "approval" and action in {"approve", "reject"}:
                raise ExecutionHostError(
                    "interaction_action_invalid", "此请求需要提交表单", status=422
                )
        command, permit = self._command(
            scope,
            target,
            "submit_interaction",
            idempotency_key,
            {
                "run_id": run_id,
                "interaction_id": interaction_id,
                "expected_revision": expected_revision,
                "action": action,
                "response": response,
                "token_ref": "host:" + _hash([asdict(scope), interaction_id]),
                "idempotency_key": idempotency_key,
            },
        )
        receipt = await self.registry.submit_control(command, permit)
        if receipt.status not in {"accepted", "duplicate"}:
            raise ExecutionHostError(
                receipt.error.code if receipt.error else "interaction_" + receipt.status,
                "审批命令尚未获准执行，请刷新状态后使用原幂等标识重试",
                status=503
                if receipt.status == "persistence_uncertain"
                else 422
                if receipt.status == "unsupported"
                else 409,
            )
        return {
            "status": receipt.status,
            "interactionId": interaction_id,
            "revision": record.revision,
            "commandId": str(command.command_id),
        }

    async def get_interaction(self, scope, *, run_id, interaction_id):
        target, runtime = await self._target(scope, "interactions")
        record = await runtime.kernel_store.get(
            interaction_id,
            tenant_id=scope.tenant_id,
            agent_instance_id=target.agent_instance_id,
            session_id=scope.session_id,
            run_id=run_id,
        )
        return {**record.public_request(), "status": record.status} if record else None

    async def conversation_events(self, scope, *, run_id: str, after: int = 0, limit: int = 200):
        target, runtime = await self._target(scope, "observe")
        await self._require_run(scope, runtime, target, run_id)
        envelopes = await runtime.session_events.read(scope.session_id, after, min(limit, 1000))
        items = []
        for envelope in envelopes:
            if envelope.run_id != run_id:
                continue
            item = None
            if envelope.family == "runtime" and envelope.family_version == 2:
                item = project_conversation_item(
                    parse_runtime_event(envelope.payload), session_id=scope.session_id
                )
            elif envelope.family == "interaction":
                item = project_interaction_conversation_item(envelope)
            if item:
                items.append(
                    {"item": item.model_dump(mode="json", by_alias=True), "cursor": envelope.seq}
                )
        return {"items": items, "cursor": envelopes[-1].seq if envelopes else after}

    async def source_events(self, scope, *, after: int = 0, limit: int = 200):
        _, runtime = await self._target(scope, "observe")
        envelopes = await runtime.session_events.read(scope.session_id, after, min(limit, 1000))
        return {
            "items": [item.model_dump(mode="json") for item in envelopes],
            "cursor": envelopes[-1].seq if envelopes else after,
        }

    async def resolve(self, ref: str, *, request: Any):
        row = self._db.execute(
            "SELECT scope,context,command_id,run_id FROM execution_policy_refs WHERE ref=?", (ref,)
        ).fetchone()
        if not row:
            raise ExecutionHostError("execution_policy_unavailable", "执行策略授权不存在")
        scope = PluginExecutionScope(**json.loads(row[0]))
        self._authorize(scope, "resolve_policy")
        if request.session_id != scope.session_id or request.metadata.get("run_id") != row[3]:
            raise ExecutionHostError(
                "execution_policy_scope_mismatch", "执行策略不能用于其他会话或执行"
            )
        return await self._plugins[scope.plugin_id][2](scope, json.loads(row[1]), request=request)

    async def close(self) -> None:
        self._plugins.clear()
        self._db.close()

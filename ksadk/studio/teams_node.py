"""Outbound-only execution node for a server-owned Agent Team.

Commands and results are journalled before network acknowledgement. The node
never invents a delivery after a timeout and does not own team scheduling.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from ksadk.plugins.execution_host import PluginExecutionScope
from ksadk.plugins.teams.artifacts import read_workspace_artifact
from ksadk.plugins.teams.errors import TeamsError
from ksadk.plugins.teams.transport import TeamsHTTPClient
from ksadk.studio.execution_host import ExecutionHostError

OPERATIONS = frozenset(
    {
        "ensure_session",
        "submit",
        "lookup",
        "set_grant",
        "cancel",
        "interactions",
        "get_interaction",
        "submit_interaction",
        "source_events",
        "conversation_events",
        "describe",
    }
)
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted"})


class TeamsExecutionNode:
    def __init__(
        self,
        client: TeamsHTTPClient,
        host: Any,
        *,
        state_dir: Path,
        name: str,
        kind: str = "local",
        bindings: list[dict[str, Any]],
        allowed_roots: tuple[Path, ...] = (),
        list_bindings=None,
    ):
        if kind not in {"local", "cloud"}:
            raise ValueError("node kind must be local or cloud")
        self.client, self.host = client, host
        self.state_dir, self.name, self.kind, self.bindings = state_dir, name, kind, bindings
        self.allowed_roots = allowed_roots
        self.list_bindings = list_bindings
        self._registration_digest = None
        state_dir.mkdir(parents=True, exist_ok=True)
        import fcntl

        self._node_lock = (state_dir / ".node.lock").open("a+b")
        try:
            fcntl.flock(self._node_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self._node_lock.close()
            raise TeamsError(
                "execution_node_in_use", "这个工作区的执行节点已在运行", status=409
            ) from error
        try:
            self._open_journal()
        except BaseException:
            if getattr(self, "db", None) is not None:
                self.db.close()
            self._node_lock.close()
            raise
        self._deadline = 0.0
        self._tasks: list[asyncio.Task] = []
        self._dispose = None
        self.last_error: str | None = None
        self._closing = False

    def _open_journal(self):
        self.identity_path = self.state_dir / "identity.json"
        self.identity = (
            json.loads(self.identity_path.read_text())
            if self.identity_path.exists()
            else {"nodeId": "node_" + uuid4().hex}
        )
        self.db = sqlite3.connect(self.state_dir / "commands.sqlite", isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY,body TEXT NOT NULL,"
            "result TEXT,revision INTEGER NOT NULL DEFAULT 0,complete INTEGER NOT NULL DEFAULT 0,"
            "cursor INTEGER NOT NULL DEFAULT 0,ack_revision INTEGER NOT NULL DEFAULT 0)"
        )
        if "ack_revision" not in {row[1] for row in self.db.execute("PRAGMA table_info(commands)")}:
            self.db.execute(
                "ALTER TABLE commands ADD COLUMN ack_revision INTEGER NOT NULL DEFAULT 0"
            )

    def _save_identity(self):
        temporary = self.identity_path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(self.identity, stream)
        temporary.replace(self.identity_path)

    @staticmethod
    def registration_key(node_id, name, kind, bindings):
        fingerprint = hashlib.sha256(
            json.dumps([name, kind, bindings], sort_keys=True).encode()
        ).hexdigest()
        return "register:" + node_id + ":" + fingerprint

    async def _register(self):
        if self.list_bindings is not None:
            self.bindings = self.list_bindings()
        key = self.registration_key(self.identity["nodeId"], self.name, self.kind, self.bindings)
        response = await self.client.request(
            "POST",
            "/nodes/register",
            {
                "nodeId": self.identity["nodeId"],
                "name": self.name,
                "kind": self.kind,
                "bindings": self.bindings,
                "idempotencyKey": key,
            },
        )
        self.identity.update(response)
        self._save_identity()
        self.client.node_token = self.identity["nodeToken"]
        self._deadline = time.monotonic() + float(response.get("leaseSeconds", 45))
        self._registration_digest = key

    async def start(self):
        await self._register()
        if self._dispose is None:
            self._dispose = self.host.register_plugin(
                "io.ksadk.teams",
                self.identity["pluginDigest"],
                authorize=self._authorize,
                resolve_policy=self._resolve_policy,
            )
        if not self._tasks:
            self._tasks = [
                asyncio.create_task(self._heartbeats(), name="teams-node-heartbeat"),
                asyncio.create_task(self._work(), name="teams-node-work"),
            ]

    def _authorize(self, scope: PluginExecutionScope, operation: str):
        if (
            scope.plugin_id != "io.ksadk.teams"
            or scope.plugin_digest != self.identity.get("pluginDigest")
            or scope.authority_ref != self.identity.get("authorityRef")
            or scope.tenant_id != self.identity.get("tenantId")
            or scope.owner_subject != self.identity.get("ownerSubject")
        ):
            raise TeamsError("node_scope_forbidden", "执行授权不属于此节点", status=403)
        allowed = {
            b.get("localBindingRef") or b["bindingRef"]
            for b in [*self.bindings, *self.identity.get("bindings", [])]
        }
        if scope.binding_ref not in allowed:
            raise TeamsError("node_binding_forbidden", "节点未注册该固定执行版本", status=403)
        if (
            operation in {"submit", "ensure_session", "resolve_policy"}
            and time.monotonic() >= self._deadline
        ):
            raise TeamsError("node_lease_expired", "节点授权已过期，等待服务端重新确认", status=409)

    def _scope(self, command):
        values = dict(command["scope"])
        prefix = f"node:{self.identity['nodeId']}:"
        if values["binding_ref"].startswith(prefix):
            values["binding_ref"] = values["binding_ref"][len(prefix) :]
        scope = PluginExecutionScope(**values)
        self._authorize(scope, "observe")
        return scope

    async def _heartbeats(self):
        interval = float(self.identity.get("heartbeatIntervalSeconds", 10))
        while not self._closing:
            try:
                if self.list_bindings is not None:
                    current = self.list_bindings()
                    if (
                        self.registration_key(
                            self.identity["nodeId"], self.name, self.kind, current
                        )
                        != self._registration_digest
                    ):
                        await self._register()
                await self.client.request("POST", f"/nodes/{self.identity['nodeId']}/heartbeat", {})
                self._deadline = time.monotonic() + float(self.identity.get("leaseSeconds", 45))
                self.last_error = None
            except TeamsError as error:
                self.last_error = error.code
            await asyncio.sleep(interval)

    async def _work(self):
        while not self._closing:
            try:
                # Flush prior results/observe accepted work before claiming more.
                for row in self.db.execute("SELECT body FROM commands WHERE complete=0").fetchall():
                    await self._execute_checked(json.loads(row[0]))
                for row in self.db.execute("SELECT body FROM commands WHERE complete=2").fetchall():
                    await self._observe_fenced(json.loads(row[0]))
                if time.monotonic() < self._deadline:
                    batch = await self.client.request(
                        "POST", "/commands/claim", {"nodeId": self.identity["nodeId"], "limit": 16}
                    )
                    for command in batch.get("commands", []):
                        encoded = json.dumps(command, sort_keys=True)
                        previous = self.db.execute(
                            "SELECT body FROM commands WHERE id=?", (command["commandId"],)
                        ).fetchone()
                        if previous and previous[0] != encoded:
                            raise TeamsError(
                                "node_command_changed",
                                "同一命令内容发生变化，已拒绝执行",
                                status=409,
                            )
                        self.db.execute(
                            "INSERT OR IGNORE INTO commands(id,body) VALUES(?,?)",
                            (command["commandId"], encoded),
                        )
                        await self._execute_checked(command)
            except TeamsError as error:
                self.last_error = error.code
            except Exception:
                self.last_error = "node_execution_failed"
            await asyncio.sleep(1)

    async def _execute_checked(self, command):
        try:
            await self._execute(command)
        except TeamsError as error:
            if error.code == "execution_epoch_expired":
                # Keep the original receipt for diagnostics/reconciliation.
                # Superseded authority is not proof that execution terminated.
                self.db.execute(
                    "UPDATE commands SET complete=2 WHERE id=?", (command["commandId"],)
                )
                self.last_error = error.code
                return
            raise

    async def _observe_fenced(self, command):
        """Observe the original order after takeover; never submit it again."""
        if command["operation"] != "submit":
            return
        scope = self._scope(command)
        arguments = command["arguments"]
        try:
            receipt = await self.host.lookup(scope, arguments["idempotency_key"])
            if receipt.run_id and receipt.run_status not in TERMINAL:
                # Revocation/cancel have stable identities and do not stand in
                # for a confirmed execution terminal or a side-effect receipt.
                await self.host.set_grant(
                    scope, arguments["grant_id"], "revoked", "fenced-grant:" + command["commandId"]
                )
                await self.host.cancel(
                    scope, receipt.run_id, "fenced-cancel:" + command["commandId"]
                )
            row = self.db.execute(
                "SELECT revision FROM commands WHERE id=?", (command["commandId"],)
            ).fetchone()
            report = {
                "commandId": command["commandId"],
                "epoch": command["epoch"],
                "revision": row[0] + 1,
                "result": asdict(receipt),
                "error": None,
            }
            self.db.execute(
                "UPDATE commands SET result=?,revision=? WHERE id=?",
                (json.dumps(report, sort_keys=True), report["revision"], command["commandId"]),
            )
            try:
                await self.client.request(
                    "POST", f"/commands/{command['commandId']}/result", report
                )
            except TeamsError as error:
                if error.code != "execution_epoch_expired":
                    raise
            if receipt.run_status in TERMINAL:
                self.db.execute(
                    "UPDATE commands SET complete=3 WHERE id=?", (command["commandId"],)
                )
        except Exception as error:
            self.last_error = getattr(error, "code", "fenced_execution_unconfirmed")

    async def _execute(self, command):
        command_id, operation = command["commandId"], command["operation"]
        if command.get("nodeId") != self.identity["nodeId"] or operation not in OPERATIONS:
            raise TeamsError("node_command_forbidden", "命令不属于该节点或操作未获支持", status=403)
        row = self.db.execute(
            "SELECT result,revision,cursor,complete,ack_revision FROM commands WHERE id=?",
            (command_id,),
        ).fetchone()
        if row[3]:
            return
        if row[1] > row[4]:
            previous = json.loads(row[0])
            await self.client.request("POST", f"/commands/{command_id}/result", previous)
            old_result = previous.get("result") or {}
            finished = (
                operation != "submit"
                or bool(previous.get("error"))
                or old_result.get("status") == "rejected"
                or old_result.get("run_status") in TERMINAL
            )
            cursor = (
                old_result.get("events", {}).get("cursor", row[2])
                if isinstance(old_result, dict)
                else row[2]
            )
            self.db.execute(
                "UPDATE commands SET ack_revision=?,complete=?,cursor=? WHERE id=?",
                (row[1], int(finished), cursor, command_id),
            )
            if finished:
                return
            row = (row[0], row[1], cursor, 0, row[1])
        scope = self._scope(command)
        arguments = dict(command.get("arguments") or {})
        result, error = None, None
        phase = "lookup"
        try:
            if operation == "submit":
                # A process crash between host admission and local journal
                # commit is recovered from the Kernel's original key.
                result = await self.host.lookup(scope, arguments["idempotency_key"])
                if result.status == "missing":
                    if time.monotonic() >= self._deadline:
                        return
                    phase = "prepare"
                    await self.host.ensure_session(scope)
                    phase = "submit"
                    result = await self.host.submit(scope, **arguments)
            else:
                if time.monotonic() >= self._deadline:
                    return
                result = await getattr(self.host, operation)(scope, **arguments)
            if is_dataclass(result):
                result = asdict(result)
            if operation == "submit" and result.get("run_id"):
                events = await self.host.source_events(scope, after=row[2], limit=500)
                events = {
                    **events,
                    "items": [
                        item
                        for item in events.get("items", [])
                        if item.get("run_id") == result["run_id"]
                    ],
                }
                interactions = [
                    item
                    for item in await self.host.interactions(scope)
                    if (item.get("runId") or item.get("run_id")) == result["run_id"]
                ]
                result = {
                    **result,
                    "events": events,
                    "interactions": interactions,
                }
        except (TeamsError, ExecutionHostError) as failure:
            if operation == "submit" and phase == "prepare":
                result = {"status": "rejected", "reason": failure.code}
            elif operation == "submit":
                # A safe public exception can still occur after admission.
                # Preserve uncertainty unless the original ledger confirms it.
                try:
                    receipt = await self.host.lookup(scope, arguments["idempotency_key"])
                    result = (
                        asdict(receipt)
                        if receipt.status in {"accepted", "duplicate", "rejected"}
                        else {"status": "uncertain", "reason": "node_submit_unconfirmed"}
                    )
                except Exception:
                    result = {"status": "uncertain", "reason": "node_submit_unconfirmed"}
            else:
                error = {"code": failure.code, "message": str(failure)}
        except Exception:
            # An unknown submit exception is never represented as a rejection.
            if operation == "submit" and phase == "prepare":
                result = {"status": "rejected", "reason": "node_session_prepare_failed"}
            elif operation == "submit":
                result = {"status": "uncertain", "reason": "node_submit_unconfirmed"}
            else:
                error = {
                    "code": "node_operation_failed",
                    "message": "节点操作未完成，请检查执行环境",
                }
        report = {
            "commandId": command_id,
            "epoch": command["epoch"],
            "revision": row[1] + 1,
            "result": result,
            "error": error,
        }
        encoded = json.dumps(report, sort_keys=True)
        self.db.execute(
            "UPDATE commands SET result=?,revision=? WHERE id=?",
            (encoded, report["revision"], command_id),
        )
        await self.client.request("POST", f"/commands/{command_id}/result", report)
        finished = (
            operation != "submit"
            or bool(error)
            or result.get("status") == "rejected"
            or result.get("run_status") in TERMINAL
        )
        cursor = (
            (result or {}).get("events", {}).get("cursor", row[2])
            if isinstance(result, dict)
            else row[2]
        )
        self.db.execute(
            "UPDATE commands SET complete=?,cursor=?,ack_revision=? WHERE id=?",
            (int(finished), cursor, report["revision"], command_id),
        )

    async def _resolve_policy(self, scope, context, *, request):
        from ksadk.harness.execution_policy import ExecutionPolicy
        from ksadk.harness.tools import HarnessTool

        command = None
        for row in self.db.execute("SELECT body FROM commands WHERE complete=0").fetchall():
            candidate = json.loads(row[0])
            if candidate["operation"] == "submit" and candidate.get("arguments", {}).get(
                "policy_context", {}
            ).get("deliveryId") == context.get("deliveryId"):
                command = candidate
                break
        if command is None:
            raise TeamsError("node_policy_unavailable", "原始执行授权不存在", status=403)
        command_id, epoch = command["commandId"], command["epoch"]
        run_id = request.metadata["run_id"]
        policy = await self.client.request(
            "POST", f"/commands/{command_id}/policy", {"epoch": epoch, "runId": run_id}
        )
        from ksadk.plugins.teams.workspaces import prepare_workspace

        identity = policy.get("workspace") or context
        workspace = await asyncio.to_thread(
            prepare_workspace,
            self.state_dir / "workspaces",
            group_id=identity["groupId"],
            team_run_id=identity["teamRunId"],
            member_id=identity["memberId"],
            attempt_id=identity.get("attemptId") or command_id,
            plan=policy.get("workspacePlan"),
            allowed_roots=self.allowed_roots,
            require_pinned_git=True,
        )
        tools = {}
        for definition in policy["tools"]:

            async def call(arguments, call_id, *, operation=definition["name"]):
                self._authorize(scope, "resolve_policy")
                if not call_id:
                    raise TeamsError(
                        "call_identity_required", "团队工具需要稳定调用标识", status=403
                    )
                body = {
                    "epoch": epoch,
                    "runId": run_id,
                    "operation": operation,
                    "arguments": arguments,
                    "callId": call_id,
                }
                if operation == "team_publish_artifact":
                    name, content = read_workspace_artifact(workspace, arguments["path"])
                    body["artifact"] = {
                        "name": arguments.get("name") or name,
                        "data": base64.b64encode(content).decode(),
                        "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                    }
                value = await self.client.request("POST", f"/commands/{command_id}/invoke", body)
                if (
                    operation == "team_read_artifact"
                    and isinstance(value, dict)
                    and "artifactBytes" in value
                ):
                    data = base64.b64decode(value.pop("artifactBytes"), validate=True)
                    checksum = hashlib.sha256(data).hexdigest()
                    if value.get("digest") != "sha256:" + checksum or len(data) > 20 * 1024 * 1024:
                        raise TeamsError(
                            "artifact_checksum_mismatch", "交付物摘要不一致", status=422
                        )
                    target = workspace / ("artifact-" + checksum[:16])
                    if target.exists():
                        if target.is_symlink() or target.read_bytes() != data:
                            raise TeamsError(
                                "artifact_copy_changed", "本地交付物副本已修改", status=409
                            )
                    else:
                        with target.open("xb") as stream:
                            stream.write(data)
                    value["workspacePath"] = str(target)
                return value

            tools[definition["name"]] = HarnessTool(
                definition["name"],
                definition["description"],
                definition["parameters"],
                call,
                "teams",
            )
        return ExecutionPolicy(
            system_context=policy["prompt"],
            tools=tools,
            workspace_root=workspace,
            limits=policy.get("limits", {}),
            child_system_context=policy.get("childSystemContext"),
        )

    async def close(self):
        self._closing = True
        self._deadline = 0
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            _, pending = await asyncio.wait(self._tasks, timeout=5)
            if pending:
                self.last_error = "node_close_timeout"
                raise TeamsError(
                    "node_close_timeout", "节点尚有未结束操作，已停止接收新执行", status=503
                )
        self._tasks.clear()
        if self._dispose:
            self._dispose()
            self._dispose = None
        self.db.close()
        self._node_lock.close()


# Explicit versioned entry; the legacy class remains for existing local integrations.
from ksadk.studio.teams_node_v1 import TeamsNodeV1 as TeamsNodeV1  # noqa: E402

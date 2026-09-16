"""Plugin lifecycle, transactional outbox dispatch and authoritative projection.

Owns no Agent Loop: every execution is submitted through the granted Host port.
The companion belongs to the Teams package and is started only by its enabled
profile. It does not initialize a database while disabled.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from typing import Any

from ksadk.plugins.execution_host import ExecutionReceipt, PluginExecutionHost, PluginExecutionScope

from .contracts import API_VERSION, PLUGIN_VERSION, TERMINAL, Actor
from .domain import TeamsDomain
from .errors import TeamsError
from .store import TeamsStore, digest, now

TEAMS_PLUGIN_ID = "io.ksadk.teams"
TEAMS_PLUGIN_DIGEST = digest(
    {"id": TEAMS_PLUGIN_ID, "version": PLUGIN_VERSION, "protocol": API_VERSION}
)


class TeamsRuntime:
    def __init__(
        self,
        *,
        path: Path,
        authority_ref: str,
        host: PluginExecutionHost,
        poll_interval: float = 0.1,
        plugin_digest: str = TEAMS_PLUGIN_DIGEST,
    ) -> None:
        self.path = path
        self.authority_ref = authority_ref
        self.host = host
        self.poll_interval = poll_interval
        self.plugin_digest = plugin_digest
        self.domain: TeamsDomain | None = None
        self._task: asyncio.Task[None] | None = None
        self._authority_file: Any = None
        self._tick_lock = asyncio.Lock()
        self.last_error: str | None = None
        self._closing = False
        self._shutdown_tasks: set[asyncio.Task] = set()

    async def start(self, *, background: bool = True) -> None:
        if self._closing and (self.domain is not None or self._shutdown_tasks):
            raise TeamsError("shutdown_in_progress", "旧执行仍在关闭，请稍后重试", status=503)
        self._closing = False
        if self.domain is not None:
            if background and self._task is None:
                self._task = asyncio.create_task(self._run(), name="teams-plugin-outbox")
            return
        if self.path.exists():
            import sqlite3

            connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='team_objects'"
                ).fetchone()
                transferred = (
                    exists
                    and connection.execute(
                        "SELECT 1 FROM team_objects WHERE kind='installation' "
                        "AND object_id='teams:authority-transfer'"
                    ).fetchone()
                )
                if transferred:
                    raise TeamsError(
                        "authority_transferred",
                        "本地团队历史已转为只读归档，请连接已迁移的团队服务端",
                        status=409,
                    )
            finally:
                connection.close()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._authority_file = self.path.with_suffix(".authority.lock").open("a+b")
        try:
            self._lock_authority()
            store = TeamsStore(self.path)
            try:
                with store.transaction() as tx:
                    installed = tx.get("installation", TEAMS_PLUGIN_ID, required=False)
                    has_groups = bool(tx.list("group"))
                if installed and installed["pluginDigest"] != self.plugin_digest:
                    self._migrate_compatible_artifact(store, installed)
                with store.transaction() as tx:
                    installed = tx.get("installation", TEAMS_PLUGIN_ID, required=False)
                    if installed is None and has_groups:
                        raise TeamsError(
                            "artifact_migration_required",
                            "团队数据绑定的插件制品已改变；请恢复原锁定版本或备份后执行版本迁移",
                            status=409,
                        )
                    if installed is None:
                        tx.put(
                            "installation",
                            TEAMS_PLUGIN_ID,
                            {
                                "pluginId": TEAMS_PLUGIN_ID,
                                "pluginDigest": self.plugin_digest,
                                "pluginVersion": PLUGIN_VERSION,
                                "apiVersion": API_VERSION,
                            },
                        )
                self.domain = TeamsDomain(store, authority_ref=self.authority_ref)
            except BaseException:
                store.close()
                raise
        except BaseException:
            self._authority_file.close()
            self._authority_file = None
            raise
        if background:
            self._task = asyncio.create_task(self._run(), name="teams-plugin-outbox")

    def _migrate_compatible_artifact(
        self, store: TeamsStore, installed: dict[str, Any]
    ) -> None:
        if (
            installed.get("pluginVersion") != PLUGIN_VERSION
            or installed.get("apiVersion") != API_VERSION
        ):
            raise TeamsError(
                "artifact_migration_required",
                "团队插件协议或数据版本不兼容；请使用专用迁移工具",
                status=409,
            )
        transition = hashlib.sha256(
            f"{installed['pluginDigest']}->{self.plugin_digest}".encode()
        ).hexdigest()[:16]
        backup = self.path.with_name(f"{self.path.name}.pre-artifact-{transition}.bak")
        store.backup(backup)
        migrated = {
            **installed,
            "pluginDigest": self.plugin_digest,
            "migratedAt": now(),
            "previousPluginDigest": installed["pluginDigest"],
            "migrationBackup": backup.name,
        }
        with store.transaction() as tx:
            current = tx.get("installation", TEAMS_PLUGIN_ID)
            if current["pluginDigest"] != installed["pluginDigest"]:
                raise TeamsError(
                    "artifact_migration_conflict",
                    "团队插件制品迁移状态已改变，请重试",
                    status=409,
                )
            tx.put("installation", TEAMS_PLUGIN_ID, migrated)

    def _lock_authority(self) -> None:
        try:
            import fcntl
        except ImportError:
            import msvcrt

            self._authority_file.seek(0)
            self._authority_file.write(b"0")
            self._authority_file.flush()
            self._authority_file.seek(0)
            try:
                msvcrt.locking(self._authority_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise TeamsError(
                    "authority_in_use", "该团队存储已有运行中的宿主", status=409
                ) from error
        else:
            try:
                fcntl.flock(self._authority_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise TeamsError(
                    "authority_in_use", "该团队存储已有运行中的宿主", status=409
                ) from error

    async def flush_controls(self, timeout: float = 2.0) -> None:
        async def flush() -> None:
            async with self._tick_lock:
                if self.domain:
                    await self._controls(self.domain)

        task = asyncio.create_task(flush(), name="teams-plugin-shutdown-controls")
        self._shutdown_tasks.add(task)

        def finished(completed: asyncio.Task) -> None:
            self._shutdown_tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()  # Retrieve late host failures after the bounded wait.

        task.add_done_callback(finished)
        _, pending = await asyncio.wait({task}, timeout=timeout)
        if pending:
            task.cancel()
            self.last_error = "teams_shutdown_timeout"
        else:
            await task

    async def close(self, *, timeout: float = 2.0) -> None:
        self._closing = True
        pending = set(self._shutdown_tasks)
        if self._task:
            pending.add(self._task)
        for task in pending:
            task.cancel()
        if pending:
            _, pending = await asyncio.wait(pending, timeout=timeout)
        if pending:
            # Retain the store and authority lock while an uncooperative host
            # call can still write. A replacement must never become authority.
            self.last_error = "teams_shutdown_timeout"
            return
        self._task = None
        try:
            await asyncio.wait_for(self._tick_lock.acquire(), timeout=timeout)
        except TimeoutError:
            self.last_error = "teams_shutdown_timeout"
            return
        try:
            if self.domain:
                self.domain.store.close()
                self.domain = None
            if self._authority_file:
                self._authority_file.close()
                self._authority_file = None
        finally:
            self._tick_lock.release()

    def require_domain(self) -> TeamsDomain:
        if self.domain is None:
            raise TeamsError("teams_disabled", "团队插件未启用", status=503)
        return self.domain

    async def _run(self) -> None:
        while not self._closing:
            try:
                await self.tick()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never persist raw transport exceptions: URLs may contain host
                # credentials. The health endpoint exposes a bounded error code.
                self.last_error = "teams_reconciliation_failed"
            await asyncio.sleep(self.poll_interval)

    def _scope(self, group: dict[str, Any], member: dict[str, Any]) -> PluginExecutionScope:
        return PluginExecutionScope(
            plugin_id=TEAMS_PLUGIN_ID,
            plugin_digest=self.plugin_digest,
            authority_ref=self.authority_ref,
            tenant_id=group["tenantId"],
            owner_subject=group["ownerSubject"],
            binding_ref=member["bindingRef"],
            session_id=member["sessionId"],
        )

    @staticmethod
    def grant_id(run_id: str, member_id: str) -> str:
        return "grant_" + digest([run_id, member_id])[7:]

    async def tick(self) -> None:
        async with self._tick_lock:
            if self._closing:
                return
            domain = self.require_domain()
            # First close start eligibility, then reconcile accepted execution,
            # then submit new work. No network operation holds a DB transaction.
            self._watchdog(domain)
            await self._controls(domain)
            await self._member_controls(domain)
            with domain.store.transaction() as tx:
                deliveries = tx.list("delivery")
            for delivery in deliveries:
                if delivery["status"] in {"accepted", "uncertain"} and not delivery.get(
                    "_terminalState"
                ):
                    await self._reconcile(domain, delivery)
            await self._settle_controls(domain)
            await self._project_interactions(domain)
            from .source_projection import project_sources

            await project_sources(self, domain)
            with domain.store.transaction() as tx:
                candidates = tx.list("delivery")
            for candidate in candidates:
                if (
                    not self._closing
                    and candidate["status"] == "pending"
                    and candidate.get("_nextRetryAt", 0) <= time.time()
                ):
                    await self._submit(domain, candidate["deliveryId"])

    async def _member_controls(self, domain):
        with domain.store.transaction() as tx:
            pending = [item for item in tx.list("member_control") if item["status"] == "pending"]
        for control in pending:
            with domain.store.transaction() as tx:
                delivery = tx.get("delivery", control["deliveryId"])
                group = tx.get("group", control["groupId"])
                member = domain.delivery_member(tx, delivery)
            if delivery.get("_terminalState"):
                status = "settled"
            else:
                try:
                    receipt = await self.host.cancel(
                        self._scope(group, member),
                        run_id=control["runId"],
                        idempotency_key=control["controlId"],
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    continue
                if receipt.status == "uncertain":
                    continue
                status = "accepted" if receipt.status in {"accepted", "duplicate"} else "rejected"
            with domain.store.transaction() as tx:
                control["status"] = status
                domain.publish(tx, "member_control", control["controlId"], control)

    async def _project_interactions(self, domain: TeamsDomain) -> None:
        if not callable(getattr(self.host, "interactions", None)):
            return
        with domain.store.transaction() as tx:
            groups = tx.list("group")
        for group in groups:
            with domain.store.transaction() as tx:
                deliveries = tx.list("delivery", group["groupId"])
                members = list(
                    {d["_sessionId"]: domain.delivery_member(tx, d) for d in deliveries}.values()
                )
                known = tx.list("interaction", group["groupId"])
            for member in members:
                own = [
                    d
                    for d in deliveries
                    if d["memberId"] == member["memberId"]
                    and d["_sessionId"] == member["sessionId"]
                    and d.get("runId")
                ]
                prior = [
                    i
                    for i in known
                    if i["ref"]["memberId"] == member["memberId"]
                    and i["ref"]["sessionId"] == member["sessionId"]
                    and i["status"] in {"pending", "resolving"}
                ]
                if not prior and not any(not d.get("_terminalState") for d in own):
                    continue
                scope = self._scope(group, member)
                try:
                    records = await self.host.interactions(scope)
                    found = {i["interaction_id"] for i in records}
                    for item in prior:
                        if item["ref"]["interactionId"] not in found:
                            record = await self.host.get_interaction(
                                scope,
                                run_id=item["ref"]["runId"],
                                interaction_id=item["ref"]["interactionId"],
                            )
                            if record:
                                records.append(record)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    continue
                for record in records:
                    if not any(d["runId"] == record["run_id"] for d in own):
                        continue
                    ref = {
                        "authorityRef": self.authority_ref,
                        "groupId": group["groupId"],
                        "memberId": member["memberId"],
                        "bindingRef": member["bindingRef"],
                        "providerRef": member["binding"]["providerRef"],
                        "sessionId": member["sessionId"],
                        "runId": record["run_id"],
                        "interactionId": record["interaction_id"],
                    }
                    presentation = record.get("presentation") or {}
                    projected = {
                        "groupId": group["groupId"],
                        "teamRunId": member["teamRunId"],
                        "ref": ref,
                        "revision": record["revision"],
                        "title": presentation.get("title") or "等待你的确认",
                        "message": presentation.get("description")
                        or "请查看此成员的请求并作出决定。",
                        "kind": "approval" if record["kind"] == "approval" else "input",
                        "status": record["status"],
                        "requestSchema": record.get("request_schema"),
                        "createdAt": record["created_at"],
                    }
                    key = "interaction_" + digest(ref)[7:]
                    with domain.store.transaction() as tx:
                        previous = tx.get("interaction", key, required=False)
                        if previous != projected:
                            domain.publish(tx, "interaction", key, projected)

    async def _submit(self, domain: TeamsDomain, delivery_id: str) -> None:
        with domain.store.transaction() as tx:
            delivery = tx.get("delivery", delivery_id)
            run = tx.get("team_run", delivery["teamRunId"])
            group = tx.get("group", delivery["groupId"])
            member = domain.run_member(tx, run["teamRunId"], delivery["memberId"])
            if (
                delivery["status"] != "pending"
                or delivery.get("_fenced")
                or run["dispatchSuspended"]
                or run["status"] in TERMINAL | {"cancel_requested"}
            ):
                return
            if delivery["_dispatchEpoch"] != run["dispatchEpoch"] or member["status"] != "active":
                delivery.update(
                    status="cancelled",
                    reason="authorization_changed",
                    revision=delivery["revision"] + 1,
                )
                domain.publish(tx, "delivery", delivery_id, delivery)
                return
            # One outstanding dispatch per member preserves order and leaves
            # concurrent slots available to other members.
            outstanding = [
                item
                for item in tx.list("delivery", group["groupId"], team_run_id=run["teamRunId"])
                if item["status"] in {"accepted", "uncertain"} and not item.get("_terminalState")
            ]
            admitted = [item for item in outstanding if not item.get("_fenced")]
            if len(admitted) >= run["budget"]["maxConcurrent"] or any(
                item["memberId"] == member["memberId"] for item in admitted
            ):
                return
            available_tokens = (
                run["budget"]["maxTokens"]
                - run["budget"]["tokensUsed"]
                - sum(item.get("_tokenLimit", 0) for item in outstanding)
            )
            if available_tokens <= 0:
                return
            token_limit = min(
                available_tokens,
                max(
                    1,
                    run["budget"]["maxTokens"]
                    // min(run["budget"]["maxConcurrent"], len(run["_roster"])),
                ),
            )
            scope = self._scope(group, member)
            policy_context = {
                "groupId": group["groupId"],
                "teamRunId": run["teamRunId"],
                "memberId": member["memberId"],
                "taskId": delivery.get("_taskId"),
                "attemptId": delivery.get("_attemptId"),
                "deliveryId": delivery_id,
                "groupRevision": run["groupRevision"],
                "dispatchEpoch": run["dispatchEpoch"],
                "tokenLimit": token_limit,
                "workspace": run.get("workspace"),
            }
        try:
            await self.host.ensure_session(scope)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code = str(getattr(error, "code", "execution_preflight_failed"))
            if code in {"execution_node_offline", "node_offline", "node_unavailable"}:
                with domain.store.transaction() as tx:
                    current = tx.get("delivery", delivery_id)
                    if current["status"] == "pending":
                        current.update(
                            reason="waiting_for_node",
                            _nextRetryAt=time.time() + 5,
                            revision=current["revision"] + 1,
                        )
                        domain.publish(tx, "delivery", delivery_id, current)
            else:
                safe_code = (
                    code
                    if code
                    in {
                        "BUILD_UNAVAILABLE",
                        "PROVIDER_UNAVAILABLE",
                        "binding_unavailable",
                        "binding_unsupported",
                        "member_revoked",
                    }
                    else "execution_preflight_failed"
                )
                self._receipt(domain, delivery_id, ExecutionReceipt("rejected", reason=safe_code))
            return
        # Preparation cannot execute. Only the submit boundary creates uncertain
        # admission, after rechecking a stop that may have arrived during preflight.
        with domain.store.transaction() as tx:
            current = tx.get("delivery", delivery_id)
            latest = tx.get("team_run", delivery["teamRunId"])
            if (
                self._closing
                or current["status"] != "pending"
                or latest["dispatchSuspended"]
                or latest["status"] in TERMINAL | {"cancel_requested"}
                or current["_dispatchEpoch"] != latest["dispatchEpoch"]
            ):
                return
            current.update(
                status="uncertain",
                _tokenLimit=token_limit,
                reason=None,
                revision=current["revision"] + 1,
            )
            domain.publish(tx, "delivery", delivery_id, current)
        try:
            receipt = await self.host.submit(
                scope,
                content=delivery["_payload"],
                idempotency_key=delivery_id,
                grant_id=self.grant_id(run["teamRunId"], member["memberId"]),
                causation=delivery["_decisionId"],
                policy_context=policy_context,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Persisted uncertain state is intentional; the next tick looks up
            # the original command before deciding whether a retry is needed.
            return
        self._receipt(domain, delivery_id, receipt)

    def _watchdog(self, domain: TeamsDomain) -> None:
        with domain.store.transaction() as tx:
            runs = tx.list("team_run")
        for run in runs:
            if run["status"] in TERMINAL | {"cancel_requested", "awaiting_acceptance"}:
                continue
            with domain.store.transaction() as tx:
                run = tx.get("team_run", run["teamRunId"])
                domain.update_active_clock(tx, run)
                tx.put("team_run", run["teamRunId"], run)
            elapsed = run.get("activeDurationSeconds", 0)
            reason = (
                "duration_budget_exhausted"
                if elapsed >= run["budget"]["maxDurationSeconds"]
                else "token_budget_exhausted"
                if run["budget"]["tokensUsed"] >= run["budget"]["maxTokens"]
                else None
            )
            if reason:
                with domain.store.transaction() as tx:
                    group = tx.get("group", run["groupId"])
                domain.control(
                    Actor(group["tenantId"], group["ownerSubject"], kind="host"),
                    run["groupId"],
                    run["teamRunId"],
                    "stop",
                    run["revision"],
                    f"budget:{run['teamRunId']}:{reason}",
                )
                with domain.store.transaction() as tx:
                    latest = tx.get("team_run", run["teamRunId"])
                    latest.update(reason=reason, revision=latest["revision"] + 1)
                    domain.publish(tx, "team_run", latest["teamRunId"], latest)

    async def _reconcile(self, domain: TeamsDomain, delivery: dict[str, Any]) -> None:
        with domain.store.transaction() as tx:
            group = tx.get("group", delivery["groupId"])
            member = domain.delivery_member(tx, delivery)
        try:
            receipt = await self.host.lookup(self._scope(group, member), delivery["deliveryId"])
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        if receipt.status == "missing":
            if delivery["status"] == "uncertain" and not delivery.get("_fenced"):
                with domain.store.transaction() as tx:
                    current = tx.get("delivery", delivery["deliveryId"])
                    run = tx.get("team_run", current["teamRunId"])
                    current.update(
                        status="cancelled"
                        if run["status"] in TERMINAL | {"cancel_requested"}
                        else "pending",
                        revision=current["revision"] + 1,
                        _nextRetryAt=time.time() + 0.25,
                    )
                    domain.publish(tx, "delivery", current["deliveryId"], current)
            return
        self._receipt(domain, delivery["deliveryId"], receipt)
        if receipt.run_id and receipt.run_status:
            with domain.store.transaction() as tx:
                current = tx.get("delivery", delivery["deliveryId"])
            if current.get("_runStatus") == receipt.run_status:
                return
            # A run may return to running after offline/approval waits. The
            # same native running event must not suppress this new transition.
            event_id = digest([receipt.run_id, receipt.run_status, current["revision"]])
            domain.project_run(
                delivery_id=delivery["deliveryId"],
                event_id=event_id,
                run_id=receipt.run_id,
                status=receipt.run_status,
                output=receipt.output,
                tokens=receipt.tokens,
            )

    @staticmethod
    def _receipt(domain: TeamsDomain, delivery_id: str, receipt: ExecutionReceipt) -> None:
        with domain.store.transaction() as tx:
            delivery = tx.get("delivery", delivery_id)
            if delivery.get("_terminalState"):
                return
            status = "accepted" if receipt.status in {"accepted", "duplicate"} else receipt.status
            if status not in {"accepted", "rejected", "uncertain"}:
                return
            update = {
                "status": status,
                "commandId": receipt.command_id,
                "runId": receipt.run_id,
                "_kernelMessageId": receipt.message_id,
                "reason": receipt.reason,
            }
            if all(delivery.get(key) == value for key, value in update.items()):
                return
            delivery.update(**update, revision=delivery["revision"] + 1)
            domain.publish(tx, "delivery", delivery_id, delivery)
            if status == "rejected":
                run = tx.get("team_run", delivery["teamRunId"])
                if run["status"] not in TERMINAL | {"cancel_requested"}:
                    run.update(
                        status="needs_attention",
                        reason=receipt.reason or "delivery_rejected",
                        revision=run["revision"] + 1,
                    )
                    domain.publish(tx, "team_run", run["teamRunId"], run)
                if delivery.get("_taskId"):
                    task = tx.get("task", delivery["_taskId"])
                    if task["attempts"] and task["attempts"][-1]["attemptId"] == delivery.get(
                        "_attemptId"
                    ):
                        task.update(
                            status="failed",
                            reason=receipt.reason or "delivery_rejected",
                            revision=task["revision"] + 1,
                        )
                        task["attempts"][-1]["status"] = "failed"
                        domain.publish(tx, "task", task["taskId"], task)
                member = domain.delivery_member(tx, delivery)
                member.update(
                    executionStatus="idle", activeRunId=None, revision=member["revision"] + 1
                )
                current = domain.run_member(tx, delivery["teamRunId"], delivery["memberId"])
                if current["sessionId"] == member["sessionId"]:
                    member["revision"] = current["revision"] + 1
                    domain.publish_run_member(tx, member)
                domain._advance(tx, run)

    async def _controls(self, domain: TeamsDomain) -> None:
        with domain.store.transaction() as tx:
            controls = [control for control in tx.list("control") if control["status"] == "pending"]
        for control in controls:
            with domain.store.transaction() as tx:
                group = tx.get("group", control["groupId"])
                run = tx.get("team_run", control["teamRunId"])
                if control["epoch"] != run["dispatchEpoch"]:
                    control["status"] = "superseded"
                    tx.put("control", control["controlId"], control)
                    continue
                members = run["_roster"]
            state = {
                "stop": "revoked",
                "suspend_dispatch": "suspended",
                "resume_dispatch": "active",
            }[control["action"]]
            for member in members:
                if member["memberId"] in control["barriers"]:
                    continue
                with domain.store.transaction() as tx:
                    latest = tx.get("team_run", control["teamRunId"])
                    if latest["dispatchEpoch"] != control["epoch"]:
                        break
                scope = self._scope(group, member)
                try:
                    barrier = await self.host.set_grant(
                        scope,
                        self.grant_id(run["teamRunId"], member["memberId"]),
                        state,
                        f"{control['controlId']}:{member['memberId']}",
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    continue
                with domain.store.transaction() as tx:
                    current = tx.get("control", control["controlId"])
                    current["barriers"][member["memberId"]] = {
                        "state": barrier.state,
                        "revision": barrier.revision,
                        "inFlight": list(barrier.in_flight),
                    }
                    tx.put("control", current["controlId"], current)
                    control = current
            if len(control["barriers"]) == len(members):
                with domain.store.transaction() as tx:
                    latest = tx.get("team_run", control["teamRunId"])
                    control["status"] = (
                        "barriers_confirmed"
                        if latest["dispatchEpoch"] == control["epoch"]
                        else "superseded"
                    )
                    tx.put("control", control["controlId"], control)

    async def _settle_controls(self, domain: TeamsDomain) -> None:
        with domain.store.transaction() as tx:
            controls = [
                control
                for control in tx.list("control")
                if control["status"] == "barriers_confirmed"
            ]
        for control in controls:
            if control["action"] != "stop":
                with domain.store.transaction() as tx:
                    run = tx.get("team_run", control["teamRunId"])
                    if run["dispatchEpoch"] != control["epoch"]:
                        control["status"] = "superseded"
                    else:
                        control["status"] = "completed"
                        if control["action"] == "resume_dispatch":
                            run.update(
                                dispatchSuspended=False,
                                revision=run["revision"] + 1,
                                updatedAt=now(),
                            )
                            domain.publish(tx, "team_run", run["teamRunId"], run)
                    tx.put("control", control["controlId"], control)
                continue
            with domain.store.transaction() as tx:
                group = tx.get("group", control["groupId"])
                deliveries = tx.list(
                    "delivery", control["groupId"], team_run_id=control["teamRunId"]
                )
                members = {d["deliveryId"]: domain.delivery_member(tx, d) for d in deliveries}
            pending = False
            for delivery in deliveries:
                if delivery["status"] == "uncertain" or (
                    delivery["status"] == "accepted" and not delivery.get("_terminalState")
                ):
                    pending = True
                    if delivery.get("runId"):
                        try:
                            await self.host.cancel(
                                self._scope(group, members[delivery["deliveryId"]]),
                                delivery["runId"],
                                f"{control['controlId']}:{delivery['runId']}",
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            continue
            if pending:
                continue
            with domain.store.transaction() as tx:
                run = tx.get("team_run", control["teamRunId"])
                run.update(status="cancelled", revision=run["revision"] + 1, updatedAt=now())
                domain.publish(tx, "team_run", run["teamRunId"], run)
                for task in tx.list("task", group["groupId"], team_run_id=run["teamRunId"]):
                    if task["status"] not in TERMINAL:
                        task.update(status="cancelled", revision=task["revision"] + 1)
                        if task["attempts"]:
                            task["attempts"][-1]["status"] = "cancelled"
                        domain.publish(tx, "task", task["taskId"], task)
                control["status"] = "completed"
                tx.put("control", control["controlId"], control)

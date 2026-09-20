"""Persistent collaboration decisions. No models, network calls or Kernel loops.

The plugin dispatcher submits this domain's outbox through Host Services.
Provider tools invoke it with a verified Actor. Kernel facts return through
the projector; a model's result candidate never completes a task by itself.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any, Callable

from .contracts import (
    API_VERSION,
    TERMINAL,
    Actor,
    Budget,
    GroupCreateInput,
    MemberInput,
    MessageInput,
    plain_text,
)
from .domain_values import member_key, public, run_member_key
from .errors import TeamsError
from .execution_projection import ExecutionCompletionProjection
from .leader_decisions import LeaderDecisions
from .store import TeamsStore, Transaction, digest, new_id, now
from .task_decisions import TaskDecisions


class TeamsDomain(TaskDecisions, ExecutionCompletionProjection, LeaderDecisions):
    def __init__(self, store: TeamsStore, *, authority_ref: str) -> None:
        self.store = store
        self.authority_ref = authority_ref

    def authorize(
        self,
        tx: Transaction,
        actor: Actor,
        group_id: str,
        *,
        owner: bool = False,
        leader: bool = False,
    ) -> dict[str, Any]:
        group = tx.get("group", group_id)
        if group["tenantId"] != actor.tenant_id:
            raise TeamsError("not_found", "团队不存在或不可访问", status=404)
        if actor.kind == "human":
            if group["ownerSubject"] != actor.subject:
                raise TeamsError("forbidden", "没有访问该团队的权限", status=403)
        elif actor.kind == "member":
            if owner or actor.group_id != group_id or not actor.member_id:
                raise TeamsError("forbidden", "成员无权执行此操作", status=403)
            if not actor.team_run_id:
                raise TeamsError("invocation_required", "缺少受信执行上下文", status=403)
            run = tx.get("team_run", actor.team_run_id)
            if (
                run["groupId"] != group_id
                or run["status"] in TERMINAL
                or run["status"] == "cancel_requested"
            ):
                raise TeamsError("team_run_closed", "本轮已停止，不能继续协作")
            member = self.run_member(tx, actor.team_run_id, actor.member_id)
            if member["status"] != "active" or (leader and member["role"] != "leader"):
                raise TeamsError("forbidden", "成员授权已失效", status=403)
        elif actor.kind != "host":
            raise TeamsError("forbidden", "无效操作主体", status=403)
        return group

    @staticmethod
    def run_member(tx: Transaction, run_id: str, member_id: str) -> dict[str, Any]:
        """Resolve the frozen invocation identity, including pre-RunMember history."""
        key = run_member_key(run_id, member_id)
        stored = tx.get("run_member", key, required=False)
        if stored:
            return stored
        run = tx.get("team_run", run_id)
        frozen = next((m for m in run.get("_roster", []) if m["memberId"] == member_id), None)
        if not frozen or frozen["status"] != "active":
            raise TeamsError("member_unavailable", "成员不属于本轮冻结配置", status=409)
        return {
            **deepcopy(frozen),
            "runMemberId": key,
            "teamRunId": run_id,
            "groupRevision": run["groupRevision"],
        }

    @staticmethod
    def delivery_member(tx: Transaction, delivery: dict[str, Any]) -> dict[str, Any]:
        """Historical execution identity must survive a Leader session takeover."""
        if delivery.get("_memberSnapshot"):
            return deepcopy(delivery["_memberSnapshot"])
        run = tx.get("team_run", delivery["teamRunId"])
        frozen = next(
            (
                m
                for m in run.get("_roster", [])
                if m["memberId"] == delivery["memberId"]
                and m["sessionId"] == delivery["_sessionId"]
            ),
            None,
        )
        if frozen is None:
            raise TeamsError(
                "delivery_scope_unavailable", "原始执行身份不可用，需要核对历史来源", status=409
            )
        return {
            **deepcopy(frozen),
            "runMemberId": run_member_key(run["teamRunId"], frozen["memberId"]),
            "teamRunId": run["teamRunId"],
            "groupRevision": run["groupRevision"],
        }

    @staticmethod
    def run_policy(tx: Transaction, run: dict[str, Any]) -> dict[str, Any]:
        if "_policy" in run:
            return run["_policy"]
        revision = tx.get(
            "group_revision", f"{run['groupId']}:{run['groupRevision']}", required=False
        )
        return (revision or tx.get("group", run["groupId"]))["_policy"]

    def publish_run_member(self, tx: Transaction, member: dict[str, Any]) -> None:
        self.publish(tx, "run_member", member["runMemberId"], member)
        current = tx.get("member", member_key(member["groupId"], member["memberId"]))
        active = [
            m
            for m in tx.list("run_member", member["groupId"])
            if m["memberId"] == member["memberId"] and m["executionStatus"] != "idle"
        ]
        state = next(
            (
                state
                for state in ("running", "waiting", "queued")
                if any(m["executionStatus"] == state for m in active)
            ),
            "idle",
        )
        current.update(
            executionStatus=state,
            activeRunId=active[0].get("activeRunId") if len(active) == 1 else None,
            activeTeamRunIds=[m["teamRunId"] for m in active],
            revision=current["revision"] + 1,
        )
        self.publish(tx, "member", member_key(member["groupId"], member["memberId"]), current)

    def update_active_clock(self, tx: Transaction, run: dict[str, Any]) -> None:
        timestamp = now()
        previous = run.get("activeDurationSeconds", 0)
        if run.get("_activeSince"):
            elapsed = (
                datetime.fromisoformat(timestamp) - datetime.fromisoformat(run["_activeSince"])
            ).total_seconds()
            total = previous + run.get("_activeRemainderSeconds", 0) + max(0, elapsed)
            run["activeDurationSeconds"] = int(total)
            run["_activeRemainderSeconds"] = total - int(total)
        executing = run["status"] not in TERMINAL | {
            "cancel_requested",
            "awaiting_acceptance",
        } and any(
            d.get("_runStatus") == "running"
            and not d.get("_terminalState")
            and not d.get("_fenced")
            for d in tx.list("delivery", run["groupId"], team_run_id=run["teamRunId"])
        )
        run["_activeSince"] = timestamp if executing else None
        if run.get("activeDurationSeconds", 0) != previous:
            run["revision"] += 1
            self.publish(tx, "team_run", run["teamRunId"], run)

    def mutate(
        self,
        actor: Actor,
        group_id: str,
        key: str,
        payload: Any,
        operation: Callable[[Transaction], Any],
        *,
        owner: bool = False,
        leader: bool = False,
    ) -> Any:
        return self.store.mutate(
            f"{actor.tenant_id}:{group_id}:{actor.kind}:{actor.subject}:{actor.member_id or ''}",
            key,
            payload,
            operation,
            authorize=lambda tx: self.authorize(tx, actor, group_id, owner=owner, leader=leader),
        )

    @staticmethod
    def publish(
        tx: Transaction,
        kind: str,
        key: str,
        value: dict[str, Any],
        *,
        created: bool = False,
        **refs: Any,
    ) -> None:
        tx.put(kind, key, value)
        event_type = {
            "team_run": "team_run.updated",
            "message": "message.created" if created else "message.updated",
        }.get(kind, f"{kind}.updated")
        payload_key = {"team_run": "teamRun", "run_member": "runMember"}.get(kind, kind)
        event = tx.event(value["groupId"], event_type, {payload_key: public(value)}, **refs)
        if kind == "message" and created:
            value["_createdSeq"] = event["groupSeq"]
            tx.put(kind, key, value)

    def create_group(
        self, actor: Actor, request: GroupCreateInput, bindings: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        if actor.kind != "human" or not actor.subject or not actor.tenant_id:
            raise TeamsError("human_owner_required", "需要真实用户创建团队", status=403)
        for member in request.members:
            binding = bindings.get(member.bindingRef)
            if not binding or not binding.get("capabilities", {}).get("enqueue"):
                raise TeamsError("binding_unavailable", "所选成员尚不支持团队执行", status=422)
            if member.memberId == request.leaderMemberId and not binding["capabilities"].get(
                "leader"
            ):
                raise TeamsError("leader_unavailable", "所选成员尚不支持协调团队", status=422)

        def create(tx: Transaction) -> dict[str, Any]:
            group_id, timestamp = new_id("grp"), now()
            group = {
                "groupId": group_id,
                "name": request.name,
                "authorityRef": self.authority_ref,
                "tenantId": actor.tenant_id,
                "ownerSubject": actor.subject,
                "leaderMemberId": request.leaderMemberId,
                "revision": 1,
                "status": "active",
                "createdAt": timestamp,
                "updatedAt": timestamp,
                "_policy": {"taskAcceptance": request.taskAcceptance, "peerWake": False},
            }
            if request.leaderStandbyBindingRef:
                group["_leaderStandbyBindingRef"] = request.leaderStandbyBindingRef
            self.publish(tx, "group", group_id, group)
            for selected in request.members:
                member = {
                    "memberId": selected.memberId,
                    "groupId": group_id,
                    "name": selected.name,
                    "role": "leader" if selected.memberId == request.leaderMemberId else "member",
                    "bindingRef": selected.bindingRef,
                    "binding": deepcopy(bindings[selected.bindingRef]),
                    "responsibility": selected.responsibility,
                    "sessionId": new_id("ses"),
                    "revision": 1,
                    "status": "active",
                    "executionStatus": "idle",
                }
                self.publish(tx, "member", member_key(group_id, selected.memberId), member)
            tx.put(
                "group_revision", f"{group_id}:1", {**group, "members": tx.list("member", group_id)}
            )
            return self._snapshot(tx, group_id)

        return self.store.mutate(
            f"{actor.tenant_id}:{actor.subject}:create_group",
            request.idempotencyKey,
            request.model_dump(),
            create,
        )

    def snapshot(
        self, actor: Actor, group_id: str, team_run_id: str | None = None
    ) -> dict[str, Any]:
        with self.store.transaction() as tx:
            self.authorize(tx, actor, group_id)
            if actor.kind == "member" and team_run_id and team_run_id != actor.team_run_id:
                raise TeamsError("forbidden", "不能读取其他轮次", status=403)
            if team_run_id and tx.get("team_run", team_run_id)["groupId"] != group_id:
                raise TeamsError("not_found", "轮次不存在", status=404)
            return self._snapshot(tx, group_id, actor=actor, team_run_id=team_run_id)

    def _snapshot(
        self,
        tx: Transaction,
        group_id: str,
        *,
        actor: Actor | None = None,
        team_run_id: str | None = None,
    ) -> dict[str, Any]:
        snapshot = {
            "apiVersion": API_VERSION,
            "group": tx.get("group", group_id),
            "watermark": tx.watermark(group_id),
        }
        for key, kind in {
            "members": "member",
            "runMembers": "run_member",
            "messages": "message",
            "teamRuns": "team_run",
            "tasks": "task",
            "deliveries": "delivery",
            "interactions": "interaction",
            "artifacts": "artifact",
        }.items():
            values = tx.list(kind, group_id)
            if kind == "run_member":
                indexed = {m["runMemberId"]: m for m in values}
                for run in tx.list("team_run", group_id):
                    for frozen in run.get("_roster", []):
                        if frozen["status"] == "active":
                            identity = run_member_key(run["teamRunId"], frozen["memberId"])
                            indexed.setdefault(
                                identity, self.run_member(tx, run["teamRunId"], frozen["memberId"])
                            )
                values = list(indexed.values())
            selected_run = actor.team_run_id if actor and actor.kind == "member" else team_run_id
            if selected_run and kind != "member":
                values = [v for v in values if v.get("teamRunId") == selected_run]
            if key == "messages":
                values = [value for value in values if value.get("visibility") != "internal"]
            if actor and actor.kind == "member" and key == "interactions":
                values = [value for value in values if value["ref"]["memberId"] == actor.member_id]
            snapshot[key] = values
        return public(snapshot)

    def list_groups(
        self, actor: Actor, *, cursor: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        if actor.kind != "human" or not 1 <= limit <= 100:
            raise TeamsError("forbidden", "无法列出团队", status=403)
        with self.store.transaction() as tx:
            groups = sorted(
                (
                    group
                    for group in tx.list("group")
                    if group["tenantId"] == actor.tenant_id
                    and group["ownerSubject"] == actor.subject
                ),
                key=lambda group: group["groupId"],
            )
            groups = [group for group in groups if not cursor or group["groupId"] > cursor]
            items = []
            for group in groups[:limit]:
                gid = group["groupId"]
                runs = tx.list("team_run", gid)
                active = next(
                    (run for run in reversed(runs) if run["status"] not in TERMINAL), None
                )
                messages = tx.list("message", gid)
                read = tx.get("read_watermark", f"{gid}:{actor.subject}", required=False)
                items.append(
                    {
                        **group,
                        "memberCount": sum(
                            member["status"] == "active" for member in tx.list("member", gid)
                        ),
                        "pendingCount": sum(
                            item["status"] == "pending" for item in tx.list("interaction", gid)
                        )
                        + sum(
                            task["status"] == "awaiting_acceptance" for task in tx.list("task", gid)
                        )
                        + sum(run["status"] == "awaiting_acceptance" for run in runs),
                        "unreadCount": sum(
                            message.get("_createdSeq", 0) > (read or {}).get("watermark", 0)
                            and message.get("senderPrincipal") != actor.subject
                            for message in messages
                        ),
                        "activeTeamRunId": active["teamRunId"] if active else None,
                        "activeTeamRunIds": [
                            r["teamRunId"] for r in runs if r["status"] not in TERMINAL
                        ],
                        "activeStatus": active["status"] if active else None,
                        "lastMessage": plain_text(messages[-1]["parts"])[:160] if messages else "",
                    }
                )
            return {
                "items": public(items),
                "nextCursor": items[-1]["groupId"] if len(groups) > limit else None,
            }

    @staticmethod
    def active_run(tx: Transaction, group_id: str) -> dict[str, Any] | None:
        return next(
            (
                run
                for run in reversed(tx.list("team_run", group_id))
                if run["status"] not in TERMINAL
            ),
            None,
        )

    def _start(
        self,
        tx: Transaction,
        group: dict[str, Any],
        message: dict[str, Any],
        budget: Budget | None = None,
    ) -> dict[str, Any]:
        existing = next(
            (
                run
                for run in tx.list("team_run", group["groupId"])
                if run["goalMessageId"] == message["messageId"]
            ),
            None,
        )
        if existing:
            return existing
        if group.get("legacySource"):
            raise TeamsError(
                "legacy_history_read_only", "导入历史只读，请创建新团队执行任务", status=409
            )
        if group["status"] != "active":
            raise TeamsError("group_archived", "团队已归档", status=409)
        timestamp = now()
        run_id = new_id("tr")
        roster = []
        for current in tx.list("member", group["groupId"]):
            if current["status"] != "active":
                continue
            member = {
                **deepcopy(current),
                "runMemberId": run_member_key(run_id, current["memberId"]),
                "teamRunId": run_id,
                "groupRevision": group["revision"],
                "sessionId": new_id("ses"),
                "revision": 1,
                "executionStatus": "idle",
                "activeRunId": None,
            }
            member.pop("_bindingHistory", None)
            member.pop("activeTeamRunIds", None)
            roster.append(member)
        run = {
            "teamRunId": run_id,
            "groupId": group["groupId"],
            "groupRevision": group["revision"],
            "revision": 1,
            "goalMessageId": message["messageId"],
            "goal": plain_text(message["parts"]),
            "leaderMemberId": group["leaderMemberId"],
            "status": "planning",
            "dispatchSuspended": False,
            "dispatchEpoch": 1,
            "budget": (budget or Budget()).model_dump(),
            "createdAt": timestamp,
            "updatedAt": timestamp,
            "_roster": roster,
            "_leaderStandbyBindingRef": group.get("_leaderStandbyBindingRef"),
            "_leaderStandbyReleaseRef": group.get("_leaderStandbyReleaseRef"),
            "_policy": deepcopy(group["_policy"]),
            "activeDurationSeconds": 0,
            "workspace": deepcopy(message.get("workspace")),
        }
        self.publish(tx, "team_run", run["teamRunId"], run)
        for member in roster:
            self.publish(tx, "run_member", member["runMemberId"], member)
        return run

    def message_run(self, tx: Transaction, group_id: str, request: MessageInput):
        if request.teamRunId:
            run = tx.get("team_run", request.teamRunId)
            if run["groupId"] != group_id:
                raise TeamsError("not_found", "轮次不属于当前团队", status=404)
            if request.intent != "note":
                self._task_run(tx, group_id, request.teamRunId)
            return run
        if request.intent in {"start_goal", "note"}:
            return None
        active = [r for r in tx.list("team_run", group_id) if r["status"] not in TERMINAL]
        if len(active) > 1:
            raise TeamsError(
                "team_run_ambiguous", "团队有多个进行中的任务，请选择本条消息所属任务", status=422
            )
        return active[0] if active else None

    def send(self, actor: Actor, group_id: str, request: MessageInput) -> dict[str, Any]:
        def send(tx: Transaction) -> dict[str, Any]:
            group = self.authorize(tx, actor, group_id)
            if group["status"] != "active":
                raise TeamsError("group_archived", "团队已归档")
            if request.replyTo:
                parent = tx.get("message", request.replyTo)
                if parent["groupId"] != group_id:
                    raise TeamsError("invalid_reply", "不能引用其他团队的消息", status=422)
            if actor.kind != "human" and request.intent == "start_goal":
                raise TeamsError("human_goal_required", "只有群主可以发起新目标", status=403)
            run = None if actor.kind == "member" else self.message_run(tx, group_id, request)
            if actor.kind == "member":
                if request.teamRunId and request.teamRunId != actor.team_run_id:
                    raise TeamsError("forbidden", "不能发送到其他轮次", status=403)
                run = self._task_run(tx, group_id, actor.team_run_id)
            member = (
                self.run_member(tx, actor.team_run_id, actor.member_id) if actor.member_id else None
            )
            message = {
                "messageId": new_id("gm"),
                "groupId": group_id,
                "revision": 1,
                "createdAt": now(),
                "senderPrincipal": actor.subject,
                "senderName": member["name"] if member else "我",
                "groupRole": member["role"] if member else "owner",
                "memberId": actor.member_id,
                "parts": [part.model_dump(exclude_none=True) for part in request.parts],
                "mentions": request.mentions,
                "intent": request.intent,
                "replyTo": request.replyTo,
                "visibility": "public",
                "workspace": request.workspace.model_dump() if request.workspace else None,
            }
            starting = request.intent == "start_goal" or (
                request.intent == "directed" and run is None and actor.kind == "human"
            )
            if starting:
                run = self._start(tx, group, message)
            elif request.intent != "note" and run is None:
                raise TeamsError("team_run_required", "请先发起本轮目标，或选择仅留言", status=422)
            if run:
                message["teamRunId"] = run["teamRunId"]
            if request.replyTo and parent.get("teamRunId") != message.get("teamRunId"):
                raise TeamsError("invalid_reply", "回复必须属于同一轮次", status=422)
            for member_id in request.mentions:
                self.run_member(tx, run["teamRunId"], member_id) if run else self._member(
                    tx, group_id, member_id
                )
            for part in request.parts:
                if part.kind == "attachment":
                    artifact = tx.get("artifact", part.attachmentRef)
                    if artifact["groupId"] != group_id:
                        raise TeamsError("attachment_forbidden", "附件不属于该团队", status=403)
                    # Explicit attachment sharing grants only this destination round access.
                    if run and artifact.get("teamRunId") != run["teamRunId"]:
                        if actor.kind != "human":
                            raise TeamsError(
                                "attachment_forbidden", "跨任务交付物只能由群主显式分享", status=403
                            )
                        grants = run.setdefault("_sharedArtifactIds", [])
                        if part.attachmentRef not in grants:
                            grants.append(part.attachmentRef)
                            tx.put("team_run", run["teamRunId"], run)
            self.publish(tx, "message", message["messageId"], message, created=True)
            if request.intent != "note":
                if actor.kind == "member":
                    # Member text is never a trigger. Explicit tool wake is separate.
                    pass
                elif run["status"] == "cancel_requested":
                    raise TeamsError("stop_in_progress", "本轮正在停止，请等待执行结束")
                else:
                    targets = (
                        [run["leaderMemberId"]]
                        if starting
                        else request.mentions or [run["leaderMemberId"]]
                    )
                    for target in targets:
                        self.dispatch(
                            tx,
                            run,
                            message,
                            target,
                            source=message["messageId"],
                            purpose="goal" if starting else "message",
                        )
            return {
                "status": "accepted",
                "groupId": group_id,
                "messageId": message["messageId"],
                "teamRunId": run["teamRunId"] if run else None,
                "groupSeq": tx.watermark(group_id),
            }

        return self.mutate(
            actor,
            group_id,
            request.idempotencyKey,
            {"operation": "send", **request.model_dump()},
            send,
        )

    def start_goal(self, actor: Actor, group_id: str, message_id: str, key: str) -> dict[str, Any]:
        def start(tx: Transaction) -> dict[str, Any]:
            group = tx.get("group", group_id)
            message = tx.get("message", message_id)
            if message["groupId"] != group_id or message["groupRole"] != "owner":
                raise TeamsError("invalid_goal", "目标必须引用本群的用户消息", status=422)
            run = self._start(tx, group, message)
            self.dispatch(
                tx, run, message, run["leaderMemberId"], source=message_id, purpose="goal"
            )
            return public(run)

        return self.mutate(
            actor, group_id, key, {"operation": "start", "messageId": message_id}, start, owner=True
        )

    @staticmethod
    def _member(tx: Transaction, group_id: str, member_id: str) -> dict[str, Any]:
        member = tx.get("member", member_key(group_id, member_id))
        if member["status"] != "active":
            raise TeamsError("member_unavailable", "目标成员已移除或不可用")
        return member

    def dispatch(
        self,
        tx: Transaction,
        run: dict[str, Any],
        message: dict[str, Any],
        target: str,
        *,
        source: str,
        purpose: str,
        hop: int = 0,
        root: str | None = None,
        task_id: str | None = None,
        attempt_id: str | None = None,
    ) -> dict[str, Any] | None:
        # Always re-read the budget: several targets can be dispatched in one tx.
        run = tx.get("team_run", run["teamRunId"])
        decision_id = "td_" + digest([run["teamRunId"], source, target, purpose])[7:]
        previous = tx.get("trigger", decision_id, required=False)
        if previous:
            return (
                tx.get("delivery", previous["deliveryId"], required=False)
                if previous.get("deliveryId")
                else None
            )
        member = self.run_member(tx, run["teamRunId"], target)
        pending = sum(
            delivery["memberId"] == target and delivery["status"] in {"pending", "uncertain"}
            for delivery in tx.list("delivery", run["groupId"], team_run_id=run["teamRunId"])
        )
        reason = None
        takeover = (
            tx.get("leader_takeover", run["_leaderTakeoverId"])
            if member["role"] == "leader" and run.get("_leaderTakeoverId")
            else None
        )
        if run["status"] in TERMINAL or run["status"] == "cancel_requested":
            reason = "team_run_closed"
        elif takeover and takeover["phase"] != "active" and not (
            purpose == "leader_takeover"
            and source == takeover["takeoverId"]
            and member["sessionId"] == takeover["newSessionId"]
        ):
            # Preserve the public message for the confirmed checkpoint while
            # preventing a new epoch from granting the old Leader a new run.
            reason = "leader_takeover_pending"
        elif member["binding"].get("memberClass") == "task_worker" and not task_id:
            reason = "task_worker_requires_task"
        elif hop > run["budget"]["maxHops"]:
            reason = "hop_limit"
        elif run["budget"]["startsUsed"] >= run["budget"]["maxStarts"]:
            reason = "start_budget_exhausted"
        elif pending >= 64:
            reason = "member_queue_full"
        decision = {
            "decisionId": decision_id,
            "groupId": run["groupId"],
            "teamRunId": run["teamRunId"],
            "sourceEventRef": source,
            "rootTriggerId": root or source,
            "hop": hop,
            "targetMemberId": target,
            "purpose": purpose,
            "blockedReason": reason,
        }
        if reason:
            tx.put("trigger", decision_id, decision)
            if run["status"] not in TERMINAL and run["status"] != "cancel_requested":
                run.update(
                    status="needs_attention",
                    reason=reason,
                    revision=run["revision"] + 1,
                    updatedAt=now(),
                )
                self.publish(tx, "team_run", run["teamRunId"], run)
            tx.event(run["groupId"], "trigger.blocked", {"decision": decision})
            return None
        delivery = {
            "deliveryId": new_id("gd"),
            "groupId": run["groupId"],
            "teamRunId": run["teamRunId"],
            "messageId": message["messageId"],
            "memberId": target,
            "revision": 1,
            "status": "pending",
            "_decisionId": decision_id,
            "_bindingRef": member["bindingRef"],
            "_sessionId": member["sessionId"],
            "_memberSnapshot": deepcopy(member),
            "_dispatchEpoch": run["dispatchEpoch"],
            "_payload": plain_text(message["parts"]),
            "_parts": message["parts"],
            "_taskId": task_id,
            "_attemptId": attempt_id,
            "_candidateMode": (
                "canonical_terminal"
                if member["binding"].get("memberClass") == "task_worker"
                else "team_tool"
            ),
            "_nextRetryAt": 0,
            "_createdAt": now(),
        }
        delivery["_payloadDigest"] = digest(
            {
                "content": delivery["_payload"],
                "parts": delivery["_parts"],
                "taskId": task_id,
                "attemptId": attempt_id,
            }
        )
        decision["deliveryId"] = delivery["deliveryId"]
        tx.put("trigger", decision_id, decision)
        self.publish(tx, "delivery", delivery["deliveryId"], delivery)
        run["budget"]["startsUsed"] += 1
        run.update(revision=run["revision"] + 1, updatedAt=now())
        self.publish(tx, "team_run", run["teamRunId"], run)
        if member["executionStatus"] == "idle":
            member.update(executionStatus="queued", revision=member["revision"] + 1)
            self.publish_run_member(tx, member)
        return delivery

    def control(
        self,
        actor: Actor,
        group_id: str,
        run_id: str,
        action: str,
        expected_revision: int,
        key: str,
    ) -> dict[str, Any]:
        if action not in {"suspend_dispatch", "resume_dispatch", "stop"}:
            raise TeamsError("unsupported_control", "不支持该团队控制", status=422)

        def control(tx: Transaction) -> dict[str, Any]:
            run = tx.get("team_run", run_id)
            if run["groupId"] != group_id:
                raise TeamsError("not_found", "本轮不存在", status=404)
            if run["revision"] != expected_revision:
                raise TeamsError("revision_conflict", "本轮状态已更新，请刷新后重试")
            if run["status"] in TERMINAL:
                raise TeamsError("team_run_closed", "本轮已结束")
            if run["status"] == "cancel_requested" and action != "stop":
                raise TeamsError("stop_in_progress", "已请求停止，不能恢复旧命令")
            run["dispatchEpoch"] += 1
            # Only admission changes here. Running members retain execution and
            # tool authority; resume waits for the independent admission barrier.
            run["dispatchSuspended"] = True
            if action == "stop":
                run["status"] = "cancel_requested"
            run.update(revision=run["revision"] + 1, updatedAt=now())
            self.update_active_clock(tx, run)
            self.publish(tx, "team_run", run_id, run)
            for delivery in tx.list("delivery", group_id, team_run_id=run_id):
                if delivery["status"] == "pending":
                    if action == "stop":
                        delivery["status"] = "cancelled"
                    delivery.update(
                        _dispatchEpoch=run["dispatchEpoch"], revision=delivery["revision"] + 1
                    )
                    self.publish(tx, "delivery", delivery["deliveryId"], delivery)
            # Freeze exact historical scopes before a Leader changes sessions.
            targets = self._control_targets(tx, run, include_terminal=action == "resume_dispatch")
            control_id = new_id("gc")
            tx.put(
                "control",
                control_id,
                {
                    "controlId": control_id,
                    "groupId": group_id,
                    "teamRunId": run_id,
                    "action": action,
                    "epoch": run["dispatchEpoch"],
                    "status": "pending",
                    "barriers": {},
                    "targets": targets,
                },
            )
            return public(run)

        return self.mutate(
            actor,
            group_id,
            key,
            {
                "operation": "control",
                "runId": run_id,
                "action": action,
                "expectedRevision": expected_revision,
            },
            control,
            owner=True,
        )

    def events(
        self, actor: Actor, group_id: str, after: int = 0, limit: int = 200
    ) -> list[dict[str, Any]]:
        with self.store.transaction() as tx:
            self.authorize(tx, actor, group_id)
            if actor.kind == "member":
                raise TeamsError("owner_stream_only", "成员通过受限上下文读取群信息", status=403)
        return self.store.events(group_id, after, limit)

    def project_run(
        self,
        *,
        delivery_id: str,
        event_id: str,
        run_id: str,
        status: str,
        output: str = "",
        tokens: int = 0,
    ) -> dict[str, Any]:
        """Trusted host projection after consulting the canonical Run record.

        The plugin transport does not expose this operation to models/browsers.
        Run state, output and usage must be obtained through Host Services.
        """
        if status not in {
            "running",
            "awaiting_approval",
            "waiting_for_node",
            "succeeded",
            "failed",
            "cancelled",
            "interrupted",
        }:
            raise TeamsError("invalid_run_state", "无法识别执行状态", status=422)

        return self.store.mutate(
            f"projection:{delivery_id}",
            event_id,
            {"runId": run_id, "status": status, "output": output, "tokens": tokens},
            lambda tx: self._project_run(
                tx,
                delivery_id=delivery_id,
                run_id=run_id,
                status=status,
                output=output,
                tokens=tokens,
            ),
        )

    def _project_run(
        self,
        tx: Transaction,
        *,
        delivery_id: str,
        run_id: str,
        status: str,
        output: str = "",
        tokens: int = 0,
        completion_projection: bool = False,
    ) -> dict[str, Any]:
        delivery = tx.get("delivery", delivery_id)
        run = tx.get("team_run", delivery["teamRunId"])
        member = self.delivery_member(tx, delivery)
        if delivery.get("runId") and delivery["runId"] != run_id:
            raise TeamsError("run_identity_conflict", "投递与执行引用不一致")
        if delivery.get("_terminalState"):
            return {"status": delivery["_terminalState"]}
        if (
            delivery.get("_candidateMode") == "canonical_terminal"
            and status
            in {
                "succeeded",
                "failed",
                "cancelled",
                "interrupted",
            }
            and not completion_projection
        ):
            raise TeamsError(
                "completion_projection_required",
                "此成员需要完整的终态结果投影",
                status=409,
            )
        if delivery.get("resultState") == "pending" and not completion_projection:
            return {"status": "result_pending"}
        delivery.update(runId=run_id, status="accepted", revision=delivery["revision"] + 1)
        delivery["_runStatus"] = status
        terminal = status in {"succeeded", "failed", "cancelled", "interrupted"}
        if terminal:
            delivery["_terminalState"] = status
        self.publish(tx, "delivery", delivery_id, delivery)
        self.update_active_clock(tx, run)
        tx.put("team_run", run["teamRunId"], run)
        source = {
            "authorityRef": self.authority_ref,
            "groupId": run["groupId"],
            "memberId": member["memberId"],
            "bindingRef": member["bindingRef"],
            "providerRef": member["binding"]["providerRef"],
            "sessionId": member["sessionId"],
            "runId": run_id,
        }
        member.update(
            activeRunId=None if terminal else run_id,
            executionStatus="idle"
            if terminal
            else "waiting"
            if status in {"awaiting_approval", "waiting_for_node"}
            else "running",
            revision=member["revision"] + 1,
        )
        current_member = self.run_member(tx, run["teamRunId"], delivery["memberId"])
        if current_member["sessionId"] == member["sessionId"]:
            member["revision"] = current_member["revision"] + 1
            self.publish_run_member(tx, member)
        if status == "running" and run["status"] == "planning":
            run.update(status="running", revision=run["revision"] + 1, updatedAt=now())
            self.publish(tx, "team_run", run["teamRunId"], run)
        if delivery.get("_taskId"):
            task = tx.get("task", delivery["_taskId"])
            attempt = task["attempts"][-1]
            if attempt["attemptId"] == delivery.get("_attemptId"):
                attempt["source"] = source
                if run_id not in attempt["_runIds"]:
                    attempt["_runIds"].append(run_id)
                attempt.setdefault("startedAt", now())
                if terminal:
                    attempt["endedAt"] = now()
                    candidate = attempt.get("_candidate")
                    if (
                        status == "succeeded"
                        and candidate
                        and candidate["runId"] == run_id
                        and not delivery.get("_fenced")
                    ):
                        attempt.update(result=candidate["result"], artifacts=candidate["artifacts"])
                        task["status"] = (
                            "succeeded"
                            if task["_acceptancePolicy"] == "result"
                            else "awaiting_acceptance"
                        )
                    else:
                        task["status"] = "cancelled" if status == "cancelled" else "failed"
                        task["reason"] = (
                            "执行结束但未提交可核验结果" if status == "succeeded" else status
                        )
                    attempt["status"] = task["status"]
                else:
                    task["status"] = attempt["status"] = "running"
                    if status == "awaiting_approval":
                        task["reason"] = "等待人工审批"
                    elif status == "waiting_for_node":
                        task["reason"] = "等待设备上线"
                    elif task.get("reason") in {"等待人工审批", "等待设备上线"}:
                        task.pop("reason", None)
                task["revision"] += 1
                self.publish(tx, "task", task["taskId"], task)
        if terminal:
            run = tx.get("team_run", run["teamRunId"])
            run["budget"]["tokensUsed"] += max(0, tokens)
            if (
                member["memberId"] == run["leaderMemberId"]
                and not delivery.get("_fenced")
                and status in {"failed", "cancelled", "interrupted"}
                and run["status"] not in TERMINAL | {"cancel_requested"}
            ):
                run.update(
                    status="needs_attention", dispatchSuspended=True, reason=f"leader_{status}"
                )
            if (
                run["budget"]["tokensUsed"] >= run["budget"]["maxTokens"]
                and run["status"] not in TERMINAL
                and run["status"] != "cancel_requested"
            ):
                run.update(
                    status="needs_attention",
                    dispatchSuspended=True,
                    reason="token_budget_exhausted",
                )
            run.update(revision=run["revision"] + 1, updatedAt=now())
            self.publish(tx, "team_run", run["teamRunId"], run)
            if output.strip():
                message = {
                    "messageId": "result_" + digest([delivery_id, run_id])[7:],
                    "groupId": run["groupId"],
                    "teamRunId": run["teamRunId"],
                    "revision": 1,
                    "createdAt": now(),
                    "senderPrincipal": f"member:{member['memberId']}",
                    "senderName": member["name"],
                    "groupRole": member["role"],
                    "memberId": member["memberId"],
                    "parts": [{"kind": "text", "text": output}],
                    "mentions": [],
                    "intent": "result",
                    "visibility": "public",
                    "sourceRefs": [source],
                }
                self.publish(tx, "message", message["messageId"], message, created=True)
            self._advance(tx, run)
            if not delivery.get("_fenced"):
                self.finalize_if_ready(tx, run["teamRunId"])
        return {"status": status}

    def finalize_if_ready(self, tx, run_id):
        run = tx.get("team_run", run_id)
        candidate = run.get("_finalCandidate")
        if not candidate or run["status"] in TERMINAL | {"cancel_requested", "awaiting_acceptance"}:
            return
        deliveries = tx.list("delivery", run["groupId"], team_run_id=run_id)
        if not any(
            d.get("runId") == candidate["runId"]
            and d.get("_terminalState") == "succeeded"
            and not d.get("_fenced")
            for d in deliveries
        ):
            return
        if any(
            d["status"] in {"pending", "uncertain"}
            or (d["status"] == "accepted" and not d.get("_terminalState"))
            for d in deliveries
        ):
            return
        if any(
            task["status"] != "succeeded"
            for task in tx.list("task", run["groupId"], team_run_id=run_id)
        ):
            return
        run.update(
            status="awaiting_acceptance",
            result=candidate["result"],
            revision=run["revision"] + 1,
            updatedAt=now(),
        )
        self.publish(tx, "team_run", run_id, run)

    def finish_candidate(self, actor: Actor, result: str, key: str) -> dict[str, Any]:
        if (
            actor.kind != "member"
            or not actor.group_id
            or not actor.team_run_id
            or not actor.run_id
        ):
            raise TeamsError("invocation_required", "需要 Leader 的受信执行上下文", status=403)
        if not result.strip() or len(result) > 100_000:
            raise TeamsError("invalid_result", "请提供有效的交付结果", status=422)

        def finish(tx: Transaction) -> dict[str, Any]:
            run = self._task_run(tx, actor.group_id, actor.team_run_id)
            tasks = tx.list("task", actor.group_id, team_run_id=actor.team_run_id)
            if any(task["status"] != "succeeded" for task in tasks):
                raise TeamsError("tasks_unsettled", "仍有任务未通过验收，不能提交最终成果")
            run["_finalCandidate"] = {"runId": actor.run_id, "result": result}
            run["revision"] += 1
            tx.put("team_run", run["teamRunId"], run)
            return {"status": "candidate_received", "teamRunId": run["teamRunId"]}

        return self.mutate(
            actor,
            actor.group_id,
            key,
            {"operation": "finish", "result": result},
            finish,
            leader=True,
        )

    def member_message(
        self,
        actor: Actor,
        *,
        content: str,
        target_member_id: str | None,
        wake: bool,
        source_delivery_id: str,
        key: str,
    ) -> dict[str, Any]:
        """Explicit member communication; prose mentions never trigger work."""
        if actor.kind != "member" or not actor.group_id or not actor.team_run_id:
            raise TeamsError("invocation_required", "需要受信的成员执行上下文", status=403)
        if not content.strip() or len(content) > 100_000:
            raise TeamsError("invalid_message", "消息不能为空或超过大小限制", status=422)

        def send(tx: Transaction) -> dict[str, Any]:
            run = self._task_run(tx, actor.group_id, actor.team_run_id)
            member = self.run_member(tx, actor.team_run_id, actor.member_id)
            source_delivery = tx.get("delivery", source_delivery_id)
            if (
                source_delivery["memberId"] != actor.member_id
                or source_delivery["teamRunId"] != actor.team_run_id
                or source_delivery.get("runId") not in {None, actor.run_id}
            ):
                raise TeamsError("invocation_scope_mismatch", "消息来源执行不一致", status=403)
            if target_member_id:
                self.run_member(tx, actor.team_run_id, target_member_id)
            if wake:
                if not target_member_id:
                    raise TeamsError("member_required", "唤醒需要明确接收成员", status=422)
                if (
                    member["role"] != "leader"
                    and target_member_id != run["leaderMemberId"]
                    and not self.run_policy(tx, run)["peerWake"]
                ):
                    raise TeamsError(
                        "peer_wake_forbidden", "当前策略只允许向 Leader 请求后续处理", status=403
                    )
            message = {
                "messageId": new_id("gm"),
                "groupId": actor.group_id,
                "teamRunId": actor.team_run_id,
                "revision": 1,
                "createdAt": now(),
                "senderPrincipal": f"member:{actor.member_id}",
                "senderName": member["name"],
                "groupRole": member["role"],
                "memberId": actor.member_id,
                "parts": [{"kind": "text", "text": content.strip()}],
                "mentions": [target_member_id] if target_member_id else [],
                "intent": "directed" if wake else "progress",
                "visibility": "public",
            }
            self.publish(tx, "message", message["messageId"], message, created=True)
            delivery = None
            if wake:
                previous = tx.get("trigger", source_delivery["_decisionId"])
                delivery = self.dispatch(
                    tx,
                    run,
                    message,
                    target_member_id,
                    source=f"{actor.run_id}:{key}",
                    purpose="member_request",
                    hop=previous["hop"] + 1,
                    root=previous["rootTriggerId"],
                )
            return {
                "status": "accepted" if not wake or delivery else "blocked",
                "messageId": message["messageId"],
                "groupId": actor.group_id,
                "deliveryId": delivery["deliveryId"] if delivery else None,
            }

        return self.mutate(
            actor,
            actor.group_id,
            key,
            {
                "operation": "member_message",
                "content": content,
                "target": target_member_id,
                "wake": wake,
                "sourceDeliveryId": source_delivery_id,
            },
            send,
        )

    def accept_run(
        self,
        actor: Actor,
        group_id: str,
        run_id: str,
        expected_revision: int,
        key: str,
        *,
        accepted: bool | None = None,
        action: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        chosen = action or ("accept" if accepted else "reject")
        if chosen not in {"accept", "reject", "request_changes"}:
            raise TeamsError("invalid_acceptance_action", "不支持该验收操作", status=422)
        if action is None and accepted is None:
            raise TeamsError("acceptance_action_required", "请选择验收操作", status=422)
        if action and accepted is not None and (action != ("accept" if accepted else "reject")):
            raise TeamsError("acceptance_action_conflict", "验收操作不能互相冲突", status=422)
        if chosen == "request_changes" and not (reason or "").strip():
            raise TeamsError("change_reason_required", "请说明需要修改的内容", status=422)
        if reason and len(reason) > 2000:
            raise TeamsError("invalid_reason", "修改理由不能超过2000字", status=422)

        def accept(tx: Transaction) -> dict[str, Any]:
            run = self._task_run(tx, group_id, run_id)
            if run["revision"] != expected_revision:
                raise TeamsError("revision_conflict", "本轮状态已更新，请刷新后重试")
            if run["status"] != "awaiting_acceptance":
                raise TeamsError("not_awaiting_acceptance", "本轮尚未提交最终成果")
            if any(
                delivery["status"] in {"pending", "uncertain"}
                or (delivery["status"] == "accepted" and not delivery.get("_terminalState"))
                for delivery in tx.list("delivery", group_id, team_run_id=run_id)
            ):
                raise TeamsError("execution_in_flight", "仍有执行未结束，请等待真实终态")
            if chosen == "request_changes":
                run.setdefault("resultHistory", []).append(
                    {
                        "result": run.get("result", ""),
                        "reason": reason.strip(),
                        "revision": run["revision"],
                        "requestedAt": now(),
                    }
                )
                run.pop("_finalCandidate", None)
                run.pop("result", None)
            run.update(
                status="running"
                if chosen == "request_changes"
                else "succeeded"
                if chosen == "accept"
                else "failed",
                revision=run["revision"] + 1,
                updatedAt=now(),
                _acceptedBy=actor.subject,
                _acceptedAt=now(),
            )
            if reason:
                run["reason"] = reason.strip()
            self.publish(tx, "team_run", run_id, run)
            if chosen == "request_changes":
                message = {
                    "messageId": new_id("gm"),
                    "groupId": group_id,
                    "teamRunId": run_id,
                    "revision": 1,
                    "createdAt": now(),
                    "senderPrincipal": actor.subject,
                    "senderName": "我",
                    "groupRole": "owner",
                    "parts": [
                        {
                            "kind": "text",
                            "text": f"最终成果需要修改：{reason.strip()}\n请在本轮继续处理，"
                            "保留已验收的有效结果，必要时创建修订子任务，完成后重新提交最终成果。",
                        }
                    ],
                    "mentions": [run["leaderMemberId"]],
                    "intent": "followup",
                    "visibility": "public",
                }
                self.publish(tx, "message", message["messageId"], message, created=True)
                self.dispatch(
                    tx,
                    run,
                    message,
                    run["leaderMemberId"],
                    source=message["messageId"],
                    purpose="revision",
                )
            return public(tx.get("team_run", run_id))

        return self.mutate(
            actor,
            group_id,
            key,
            {
                "operation": "accept_run",
                "runId": run_id,
                "expectedRevision": expected_revision,
                "accepted": accepted,
                "action": action,
                "reason": reason,
            },
            accept,
            owner=True,
        )

    def update_group(
        self,
        actor: Actor,
        group_id: str,
        expected_revision: int,
        key: str,
        *,
        name: str | None = None,
        leader_member_id: str | None = None,
        remove_member_id: str | None = None,
        task_acceptance: str | None = None,
        peer_wake: bool | None = None,
        archived: bool | None = None,
        add_member: MemberInput | None = None,
        rebind_member: MemberInput | None = None,
        bindings: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if name is not None and (not name.strip() or len(name.strip()) > 120):
            raise TeamsError("invalid_name", "群名不能为空且不能超过120字", status=422)
        if task_acceptance is not None and task_acceptance not in {"leader", "human", "result"}:
            raise TeamsError("invalid_policy", "验收策略无效", status=422)
        payload = {
            "operation": "update_group",
            "expectedRevision": expected_revision,
            "name": name,
            "leaderMemberId": leader_member_id,
            "removeMemberId": remove_member_id,
            "taskAcceptance": task_acceptance,
            "peerWake": peer_wake,
            "archived": archived,
            "addMember": add_member.model_dump() if add_member else None,
            "rebindMember": rebind_member.model_dump() if rebind_member else None,
        }

        def update(tx: Transaction) -> dict[str, Any]:
            group = tx.get("group", group_id)
            if group.get("legacySource"):
                raise TeamsError(
                    "legacy_history_read_only", "导入历史只读，请创建新团队", status=409
                )
            if group["revision"] != expected_revision:
                raise TeamsError("revision_conflict", "团队配置已更新，请刷新后重试")
            if name is not None:
                group["name"] = name.strip()
            for selected, adding in ((add_member, True), (rebind_member, False)):
                if selected is None:
                    continue
                binding = (bindings or {}).get(selected.bindingRef)
                if not binding or not binding.get("capabilities", {}).get("enqueue"):
                    raise TeamsError("binding_unavailable", "所选Build不支持团队执行", status=422)
                if adding:
                    if (
                        len([m for m in tx.list("member", group_id) if m["status"] == "active"])
                        >= 8
                    ):
                        raise TeamsError("member_limit", "一个团队最多8位成员", status=422)
                    if tx.get("member", member_key(group_id, selected.memberId), required=False):
                        raise TeamsError(
                            "member_id_exists", "成员标识已使用，请使用新标识", status=409
                        )
                    member = {
                        "memberId": selected.memberId,
                        "groupId": group_id,
                        "role": "member",
                        "revision": 1,
                        "status": "active",
                        "executionStatus": "idle",
                    }
                else:
                    member = self._member(tx, group_id, selected.memberId)
                    if member["role"] == "leader" and not binding["capabilities"].get("leader"):
                        raise TeamsError(
                            "leader_unavailable", "新Build不支持Leader策略", status=422
                        )
                    member.setdefault("_bindingHistory", []).append(
                        {
                            key: deepcopy(member[key])
                            for key in ("bindingRef", "binding", "sessionId")
                        }
                    )
                    member["revision"] += 1
                member.update(
                    name=selected.name,
                    bindingRef=selected.bindingRef,
                    binding=deepcopy(binding),
                    sessionId=new_id("ses"),
                    responsibility=selected.responsibility,
                )
                self.publish(tx, "member", member_key(group_id, selected.memberId), member)
            if leader_member_id:
                new_leader = self._member(tx, group_id, leader_member_id)
                if not new_leader["binding"]["capabilities"].get("leader"):
                    raise TeamsError("leader_unavailable", "该成员不支持协调团队", status=422)
                for member in tx.list("member", group_id):
                    role = "leader" if member["memberId"] == leader_member_id else "member"
                    if member["role"] != role:
                        member.update(role=role, revision=member["revision"] + 1)
                        self.publish(tx, "member", member_key(group_id, member["memberId"]), member)
                group["leaderMemberId"] = leader_member_id
            if remove_member_id:
                if group["leaderMemberId"] == remove_member_id:
                    raise TeamsError(
                        "leader_required", "移除 Leader 前请指定新的 Leader", status=422
                    )
                member = self._member(tx, group_id, remove_member_id)
                member.update(status="removed", revision=member["revision"] + 1)
                self.publish(tx, "member", member_key(group_id, remove_member_id), member)
            if task_acceptance is not None:
                group["_policy"]["taskAcceptance"] = task_acceptance
            if peer_wake is not None:
                group["_policy"]["peerWake"] = peer_wake
            if archived is not None:
                group["status"] = "archived" if archived else "active"
            group.update(revision=group["revision"] + 1, updatedAt=now())
            self.publish(tx, "group", group_id, group)
            tx.put(
                "group_revision",
                f"{group_id}:{group['revision']}",
                {**group, "members": tx.list("member", group_id)},
            )
            return self._snapshot(tx, group_id)

        return self.mutate(actor, group_id, key, payload, update, owner=True)

    def mark_read(self, actor: Actor, group_id: str, watermark: int) -> dict[str, int]:
        with self.store.transaction() as tx:
            self.authorize(tx, actor, group_id, owner=True)
            if watermark < 0 or watermark > tx.watermark(group_id):
                raise TeamsError("invalid_cursor", "已读水位无效", status=422)
            key = f"{group_id}:{actor.subject}"
            previous = tx.get("read_watermark", key, required=False)
            value = max(watermark, (previous or {}).get("watermark", 0))
            tx.put("read_watermark", key, {"groupId": group_id, "watermark": value})
            return {"watermark": value}

    def execution(self, actor: Actor, group_id: str, run_id: str) -> dict[str, Any]:
        with self.store.transaction() as tx:
            self.authorize(tx, actor, group_id)
            if actor.kind == "member" and run_id != actor.team_run_id:
                raise TeamsError("forbidden", "不能读取其他轮次执行", status=403)
            snapshot = self._snapshot(tx, group_id)
            deliveries = tx.list("delivery", group_id, team_run_id=run_id)
            children = tx.list("child_invocation", group_id, team_run_id=run_id)
            members = {d["deliveryId"]: self.delivery_member(tx, d) for d in deliveries}
            watermark = tx.watermark(group_id)
        if not any(run["teamRunId"] == run_id for run in snapshot["teamRuns"]):
            raise TeamsError("not_found", "执行轮次不存在", status=404)
        nodes, edges = [], []
        for task in snapshot["tasks"]:
            if task["teamRunId"] != run_id:
                continue
            attempt = task["attempts"][-1] if task["attempts"] else None
            node = {
                "nodeId": task["taskId"],
                "kind": "task",
                "title": task["title"],
                "status": task["status"],
                "taskId": task["taskId"],
                "memberId": task["ownerMemberId"],
                "reason": task.get("reason"),
            }
            if attempt:
                node.update(attemptId=attempt["attemptId"], source=attempt.get("source"))
            nodes.append(node)
            edges.extend(
                {"source": dependency, "target": task["taskId"], "kind": "dependency"}
                for dependency in task["dependencies"]
            )
        run_nodes = {}
        for delivery in deliveries:
            if not delivery.get("runId"):
                continue
            member = members[delivery["deliveryId"]]
            binding = member
            node_id = "run_" + digest([delivery["_sessionId"], delivery["runId"]])[7:]
            run_nodes[(delivery["_sessionId"], delivery["runId"])] = node_id
            node = {
                "nodeId": node_id,
                "kind": "run",
                "title": member["name"],
                "status": delivery.get("_terminalState") or delivery.get("_runStatus") or "queued",
                "reason": "execution_uncertain_fenced"
                if delivery.get("_fenced")
                else delivery.get("reason"),
                "memberId": member["memberId"],
                "source": {
                    "authorityRef": self.authority_ref,
                    "groupId": group_id,
                    "memberId": member["memberId"],
                    "bindingRef": binding["bindingRef"],
                    "providerRef": binding["binding"]["providerRef"],
                    "sessionId": binding["sessionId"],
                    "runId": delivery["runId"],
                },
            }
            if delivery.get("_taskId"):
                node.update(
                    taskId=delivery["_taskId"],
                    attemptId=delivery["_attemptId"],
                    parentNodeId=delivery["_taskId"],
                )
                edges.append(
                    {"source": delivery["_taskId"], "target": node_id, "kind": "invocation"}
                )
            nodes.append(node)
        for child in children:
            run_nodes[(child["source"]["sessionId"], child["_nativeRunId"])] = child["nodeId"]
        for child in children:
            node = public(child)
            parent = run_nodes.get((child["source"]["sessionId"], child["_parentRunId"]))
            if parent:
                node["parentNodeId"] = parent
                edges.append({"source": parent, "target": child["nodeId"], "kind": "invocation"})
            nodes.append(node)
        return {
            "groupId": group_id,
            "teamRunId": run_id,
            "watermark": watermark,
            "nodes": nodes,
            "edges": edges,
        }

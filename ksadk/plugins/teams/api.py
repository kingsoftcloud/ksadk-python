"""Plugin-owned Teams HTTP facade, mounted by the generic workspace router."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import Field

from .application import TeamsApplication
from .artifacts import read_workspace_artifact
from .contracts import (
    ControlInput,
    GroupCreateInput,
    InputModel,
    MemberInput,
    MessageInput,
    TaskCreateInput,
)
from .errors import TeamsError


class TeamsRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def wrapped(request):
            try:
                return await handler(request)
            except ValueError as error:
                if hasattr(error, "code") and hasattr(error, "status"):
                    return JSONResponse(
                        {"error": {"code": error.code, "message": "团队请求无法处理"}},
                        status_code=error.status,
                    )
                raise

        return wrapped


class Mutation(InputModel):
    idempotencyKey: str = Field(min_length=1, max_length=200)


class RevisionMutation(Mutation):
    expectedRevision: int = Field(ge=1)


class GroupPatch(RevisionMutation):
    name: str | None = None
    leaderMemberId: str | None = None
    removeMemberId: str | None = None
    taskAcceptance: Literal["human", "result"] | None = None
    peerWake: bool | None = None
    archived: bool | None = None
    addMember: MemberInput | None = None
    rebindMember: MemberInput | None = None


class StartInput(Mutation):
    goalMessageId: str


class AcceptanceInput(RevisionMutation):
    accepted: bool


class TaskActionInput(RevisionMutation):
    action: Literal["assign", "claim", "retry", "accept", "reject"]
    memberId: str | None = None
    reason: str | None = Field(default=None, max_length=2000)


class CreateTaskInput(TaskCreateInput):
    idempotencyKey: str = Field(min_length=1, max_length=200)


class ReadInput(InputModel):
    watermark: int = Field(ge=0)


class MemberRef(InputModel):
    authorityRef: str
    groupId: str
    memberId: str
    bindingRef: str
    providerRef: str
    sessionId: str
    runId: str
    itemId: str | None = None


class InteractionRef(MemberRef):
    interactionId: str


class MemberCancelInput(Mutation):
    ref: MemberRef


class InteractionInput(RevisionMutation):
    ref: InteractionRef
    action: Literal["approve", "reject", "submit", "cancel"]
    response: dict[str, Any] = Field(default_factory=dict)


def _frame(event: str, value: Any, cursor: int | None = None) -> str:
    prefix = f"id: {cursor}\n" if cursor is not None else ""
    return prefix + f"event: {event}\ndata: {json.dumps(value, ensure_ascii=False)}\n\n"


def create_router(application: TeamsApplication) -> APIRouter:
    router = APIRouter(prefix="/groups", route_class=TeamsRoute)

    @router.get("")
    async def groups(cursor: str | None = None, limit: int = Query(default=50, ge=1, le=100)):
        return application.domain.list_groups(application.actor(), cursor=cursor, limit=limit)

    @router.get("/bindings")
    async def bindings():
        return {"items": await application.bindings()}

    @router.post("", status_code=201)
    async def create(payload: GroupCreateInput):
        bindings = {item["bindingRef"]: item for item in await application.bindings()}
        return application.domain.create_group(application.actor(), payload, bindings)

    @router.get("/{group_id}")
    async def snapshot(group_id: str):
        return application.domain.snapshot(application.actor(), group_id)

    @router.patch("/{group_id}")
    async def update(group_id: str, payload: GroupPatch):
        bindings = (
            {item["bindingRef"]: item for item in await application.bindings()}
            if payload.addMember or payload.rebindMember
            else None
        )
        return application.domain.update_group(
            application.actor(),
            group_id,
            payload.expectedRevision,
            payload.idempotencyKey,
            name=payload.name,
            leader_member_id=payload.leaderMemberId,
            remove_member_id=payload.removeMemberId,
            task_acceptance=payload.taskAcceptance,
            peer_wake=payload.peerWake,
            archived=payload.archived,
            add_member=payload.addMember,
            rebind_member=payload.rebindMember,
            bindings=bindings,
        )

    @router.post("/{group_id}/messages", status_code=202)
    async def send(group_id: str, payload: MessageInput):
        for part in payload.parts:
            if part.kind == "attachment":
                with application.domain.store.transaction() as tx:
                    application.domain.authorize(tx, application.actor(), group_id, owner=True)
                    artifact = tx.get("artifact", part.attachmentRef)
                    if artifact["groupId"] != group_id:
                        raise TeamsError("attachment_forbidden", "附件不属于该团队", status=403)
        return application.domain.send(application.actor(), group_id, payload)

    @router.post("/{group_id}/team-runs", status_code=202)
    async def start(group_id: str, payload: StartInput):
        run = application.domain.start_goal(
            application.actor(), group_id, payload.goalMessageId, payload.idempotencyKey
        )
        return {"status": "accepted", "groupId": group_id, "teamRunId": run["teamRunId"]}

    @router.post("/{group_id}/team-runs/{run_id}/control", status_code=202)
    async def control(group_id: str, run_id: str, payload: ControlInput):
        application.domain.control(
            application.actor(),
            group_id,
            run_id,
            payload.action,
            payload.expectedRevision,
            payload.idempotencyKey,
        )
        return {"status": "accepted", "groupId": group_id, "teamRunId": run_id}

    @router.post("/{group_id}/team-runs/{run_id}/acceptance")
    async def accept(group_id: str, run_id: str, payload: AcceptanceInput):
        application.domain.accept_run(
            application.actor(),
            group_id,
            run_id,
            payload.expectedRevision,
            payload.idempotencyKey,
            accepted=payload.accepted,
        )
        return {"status": "accepted", "groupId": group_id, "teamRunId": run_id}

    @router.post("/{group_id}/team-runs/{run_id}/tasks", status_code=201)
    async def create_task(group_id: str, run_id: str, payload: CreateTaskInput):
        return application.domain.create_task(
            application.actor(),
            group_id,
            run_id,
            TaskCreateInput.model_validate(payload.model_dump(exclude={"idempotencyKey"})),
            payload.idempotencyKey,
        )

    @router.post("/{group_id}/tasks/{task_id}/actions")
    async def task_action(group_id: str, task_id: str, payload: TaskActionInput):
        application.domain.task_action(
            application.actor(),
            group_id,
            task_id,
            payload.action,
            payload.expectedRevision,
            payload.idempotencyKey,
            owner_member_id=payload.memberId,
        )
        return {"status": "accepted", "groupId": group_id}

    @router.post("/{group_id}/read")
    async def read(group_id: str, payload: ReadInput):
        return application.domain.mark_read(application.actor(), group_id, payload.watermark)

    @router.get("/{group_id}/execution")
    async def execution(
        group_id: str, team_run_id: str | None = Query(default=None, alias="teamRunId")
    ):
        snapshot = application.domain.snapshot(application.actor(), group_id)
        if team_run_id is None:
            if not snapshot["teamRuns"]:
                return {
                    "groupId": group_id,
                    "teamRunId": "",
                    "watermark": snapshot["watermark"],
                    "nodes": [],
                    "edges": [],
                }
            team_run_id = snapshot["teamRuns"][-1]["teamRunId"]
        return application.domain.execution(application.actor(), group_id, team_run_id)

    @router.get("/{group_id}/events")
    async def events(group_id: str, request: Request, after: int = Query(default=0, ge=0)):
        application.domain.snapshot(application.actor(), group_id)

        async def stream():
            cursor, heartbeat = after, time.monotonic()
            while not await request.is_disconnected():
                try:
                    domain = application.domain
                    with domain.store.transaction() as tx:
                        domain.authorize(tx, application.actor(), group_id)
                    batch = domain.store.events(group_id, after=cursor)
                except TeamsError as error:
                    if error.code == "reset_required":
                        yield _frame("reset_required", {"groupId": group_id})
                    break
                for event in batch:
                    cursor = event["groupSeq"]
                    yield _frame("group.event", event, cursor)
                if time.monotonic() - heartbeat >= 15:
                    yield _frame("heartbeat", {"watermark": cursor})
                    heartbeat = time.monotonic()
                await asyncio.sleep(0.2)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @router.get("/{group_id}/members/{member_id}/conversation")
    async def conversation(
        group_id: str,
        member_id: str,
        session_id: str = Query(alias="sessionId"),
        run_id: str = Query(alias="runId"),
    ):
        return await application.conversation(group_id, member_id, session_id, run_id)

    @router.get("/{group_id}/members/{member_id}/events", include_in_schema=False)
    @router.get("/{group_id}/members/{member_id}/conversation/events")
    async def member_events(
        group_id: str,
        member_id: str,
        request: Request,
        session_id: str = Query(alias="sessionId"),
        run_id: str = Query(alias="runId"),
        after: int = Query(default=0, ge=0),
    ):
        scope, ref = application.member_scope(group_id, member_id, session_id, run_id)

        async def stream():
            cursor, heartbeat = after, time.monotonic()
            while not await request.is_disconnected():
                batch = await application.host.conversation_events(
                    scope, run_id=run_id, after=cursor
                )
                for entry in batch["items"]:
                    yield _frame(
                        "conversation.item",
                        {"ref": {**ref, "itemId": entry["item"]["itemId"]}, **entry},
                        entry["cursor"],
                    )
                cursor = batch["cursor"]
                if time.monotonic() - heartbeat >= 15:
                    yield _frame("heartbeat", {"cursor": cursor})
                    heartbeat = time.monotonic()
                await asyncio.sleep(0.15)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @router.post("/{group_id}/interactions", status_code=202)
    async def interaction(group_id: str, payload: InteractionInput):
        submitted = payload.ref
        if submitted.groupId != group_id:
            raise TeamsError("interaction_scope_mismatch", "审批不属于此团队", status=403)
        scope, ref = application.member_scope(
            group_id, submitted.memberId, submitted.sessionId, submitted.runId
        )
        if any(getattr(submitted, key) != value for key, value in ref.items()):
            raise TeamsError("interaction_scope_mismatch", "审批完整引用不匹配", status=403)
        receipt = await application.host.submit_interaction(
            scope,
            run_id=submitted.runId,
            interaction_id=submitted.interactionId,
            expected_revision=payload.expectedRevision,
            action=payload.action,
            response=payload.response,
            idempotency_key=payload.idempotencyKey,
            actor_subject=application.actor().subject,
        )
        return {**receipt, "groupId": group_id}

    @router.post("/{group_id}/members/{member_id}/cancel", status_code=202)
    async def cancel_member(group_id: str, member_id: str, payload: MemberCancelInput):
        submitted = payload.ref
        if submitted.groupId != group_id or submitted.memberId != member_id:
            raise TeamsError("member_scope_mismatch", "执行不属于此成员", status=403)
        _, ref = application.member_scope(group_id, member_id, submitted.sessionId, submitted.runId)
        if any(getattr(submitted, key) != value for key, value in ref.items()):
            raise TeamsError("member_scope_mismatch", "执行完整引用不匹配", status=403)
        return application.cancel_member(
            group_id,
            member_id,
            submitted.sessionId,
            submitted.runId,
            payload.idempotencyKey,
        )

    @router.get("/{group_id}/artifacts/{artifact_id}/download")
    async def artifact(group_id: str, artifact_id: str):
        with application.domain.store.transaction() as tx:
            application.domain.authorize(tx, application.actor(), group_id, owner=True)
            item = tx.get("artifact", artifact_id)
            if item["groupId"] != group_id:
                raise TeamsError("artifact_forbidden", "文件不属于此团队", status=403)
        artifact_root = (application.runtime.path.parent / "artifacts").resolve()
        _name, content = read_workspace_artifact(artifact_root, item["_path"])
        filename = str(item["name"])
        return Response(
            content=content,
            media_type=item["mediaType"],
            headers={
                "Content-Disposition": (
                    'attachment; filename="artifact"; '
                    f"filename*=UTF-8''{quote(filename, safe='')}"
                ),
                "X-Content-Type-Options": "nosniff",
            },
        )

    return router

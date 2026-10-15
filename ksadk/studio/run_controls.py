"""Host-owned controls for attached Studio runs and child tasks."""

from __future__ import annotations

from ksadk.kernel.contracts import SteerPayload
from ksadk.kernel.errors import UnsupportedControlError
from ksadk.runtime import PauseResult
from ksadk.studio.contracts import RunStatus
from ksadk.studio.errors import StudioError


class StudioRunControlsMixin:
    async def pause_run(self, run_id: str) -> dict[str, str]:
        record = self.event_store.get(run_id)
        if record.status == RunStatus.WAITING_INPUT:
            raise StudioError(
                "RUN_WAITING_INPUT",
                "当前运行正在等待交互输入，请先处理卡片",
                status_code=409,
            )
        if record.status != RunStatus.RUNNING:
            raise StudioError("RUN_NOT_RUNNING", "只有运行中的 Run 可以暂停", status_code=409)
        handle = self._active_handles.get(run_id)
        if handle is None or not self.executor.is_attached(handle):
            raise StudioError("RUN_NOT_ATTACHED", "运行句柄已不在当前进程", status_code=409)
        result = await self.executor.pause(handle)
        if result is PauseResult.NOT_SUPPORTED:
            raise StudioError(
                "RUN_PAUSE_UNSUPPORTED",
                "当前 Runtime 不支持可恢复暂停",
                status_code=409,
            )
        if result is not PauseResult.PAUSED_ACTIVE_TURN:
            raise StudioError("RUN_PAUSE_FAILED", "暂停运行失败", status_code=409)
        return {"runId": run_id, "status": "pausing"}

    async def steer_run(
        self, run_id: str, text: str, *, delivery: str = "replace"
    ) -> dict[str, str]:
        """Route a live correction through the attached RuntimeAdapter owner."""
        if not isinstance(text, str) or not text.strip():
            raise StudioError("RUN_STEER_EMPTY", "插话内容不能为空", status_code=422)
        if not isinstance(delivery, str) or delivery not in {"replace", "guide"}:
            raise StudioError("RUN_STEER_DELIVERY_INVALID", "不支持此输入投递方式", status_code=422)
        record = self.event_store.get(run_id)
        if record.status != RunStatus.RUNNING:
            raise StudioError("RUN_NOT_RUNNING", "只有运行中的 Run 可以插话", status_code=409)
        handle = self._active_handles.get(run_id)
        if handle is None or not self.executor.is_attached(handle):
            raise StudioError("RUN_NOT_ATTACHED", "运行句柄已不在当前进程", status_code=409)
        try:
            method = self.executor.guide if delivery == "guide" else self.executor.steer
            receipt = await method(
                handle, SteerPayload(content=text, run_id=handle.run_id)
            )
        except UnsupportedControlError as exc:
            raise StudioError(
                "RUN_STEER_UNSUPPORTED", "当前 Runtime 不支持运行中插话", status_code=409
            ) from exc
        except ValueError as exc:
            raise StudioError("RUN_STEER_REJECTED", str(exc), status_code=409) from exc
        receipt_status = getattr(receipt, "status", "accepted")
        if receipt_status == "rejected":
            raise StudioError("RUN_STEER_REJECTED", "插话未被运行时接受", status_code=409)
        if receipt_status == "upgraded_to_follow_up":
            raise StudioError(
                "RUN_STEER_LATE", "本轮已结束，请作为新消息发送", status_code=409
            )
        if receipt_status != "accepted":
            raise StudioError("RUN_STEER_REJECTED", "运行时返回未知插话状态", status_code=409)
        event = self.event_store.append(
            run_id,
            "run.steer_requested",
            {"text": text, "runtimeRunId": handle.run_id, "delivery": delivery},
        )
        return {"runId": run_id, "status": "steering", "eventId": str(event.id)}

    def _child_control_handle(self, run_id: str, *, allow_suspended: bool = False):
        record = self.event_store.get(run_id)
        allowed = {RunStatus.RUNNING}
        if allow_suspended:
            allowed.update({RunStatus.PAUSED, RunStatus.WAITING_INPUT})
        if record.status not in allowed:
            raise StudioError("RUN_NOT_RUNNING", "只有运行中的任务可以控制子任务", status_code=409)
        handle = self._active_handles.get(run_id)
        if handle is None or not self.executor.is_attached(handle):
            raise StudioError("RUN_NOT_ATTACHED", "运行句柄已不在当前进程", status_code=409)
        return handle

    async def child_tasks(self, run_id: str) -> dict:
        handle = self._child_control_handle(run_id, allow_suspended=True)
        try:
            tasks = await self.executor.child_tasks(handle)
        except UnsupportedControlError as exc:
            raise StudioError("RUN_CHILD_CONTROL_UNSUPPORTED", "当前 Runtime 不支持子任务控制",
                              status_code=409) from exc
        except ValueError as exc:
            raise StudioError("RUN_CHILD_CONTROL_UNAVAILABLE", "当前任务未装配子任务控制",
                              status_code=409) from exc
        return {"runId": run_id, "tasks": tasks}

    async def execution_children(self, run_id: str, *, child_run_id: str | None = None,
                                 include_details: bool = False) -> list[dict]:
        """Read-only public child views; terminal history survives activation close."""
        from ksadk.studio.run_activity import project_child_execution

        self.event_store.get(run_id)
        handle = self._active_handles.get(run_id)
        if (handle is not None and handle.native_ref.get("engine_kind") == "native"
                and self.executor.is_attached(handle)):
            try:
                tasks = await self.executor.child_tasks(handle)
                children = []
                for task in tasks:
                    identity = task["child_run_id"]
                    if child_run_id and identity != child_run_id:
                        continue
                    if include_details or child_run_id:
                        view = await self.executor.child_execution_view(handle, identity)
                    else:
                        view = {**task, "events": []}
                    children.append(project_child_execution(view, run_id))
                return children
            except (UnsupportedControlError, ValueError):
                pass
        for event in reversed(self.event_store.events(run_id)):
            if event.type == "child.execution_snapshot":
                return [child for child in event.data.get("children", [])
                        if not child_run_id or child.get("callId") == child_run_id]
        return []

    async def cancel_child(self, run_id: str, child_run_id: str, *, command_id: str) -> dict:
        handle = self._child_control_handle(run_id, allow_suspended=True)
        try:
            receipt = await self.executor.cancel_child(handle, child_run_id=child_run_id,
                                                       command_id=command_id)
        except UnsupportedControlError as exc:
            raise StudioError("RUN_CHILD_CONTROL_UNSUPPORTED", "当前 Runtime 不支持子任务取消",
                              status_code=409) from exc
        except ValueError as exc:
            raise StudioError("RUN_CHILD_CANCELLATION_REJECTED", "子任务取消未被接受",
                              status_code=409) from exc
        if getattr(receipt, "status", None) not in {"accepted", "duplicate"}:
            raise StudioError(
                "RUN_CHILD_CANCELLATION_REJECTED", "子任务取消未被接受", status_code=409
            )
        return {"runId": run_id, "childRunId": child_run_id, "commandId": command_id,
                "status": "queued"}

    async def promote_child(self, run_id: str, child_run_id: str, *, command_id: str) -> dict:
        handle = self._child_control_handle(run_id)
        try:
            receipt = await self.executor.promote_child(handle, child_run_id=child_run_id,
                                                         command_id=command_id)
        except UnsupportedControlError as exc:
            raise StudioError("RUN_CHILD_CONTROL_UNSUPPORTED", "当前 Runtime 不支持子任务控制",
                              status_code=409) from exc
        except ValueError as exc:
            raise StudioError("RUN_CHILD_PROMOTION_REJECTED", "子任务控制未被接受",
                              status_code=409) from exc
        if getattr(receipt, "status", None) not in {"accepted", "duplicate"}:
            raise StudioError("RUN_CHILD_PROMOTION_REJECTED", "子任务控制未被接受", status_code=409)
        # Acceptance is an inbox receipt. child.promoted in the runtime ledger
        # is the applied fact; Studio does not invent a second child state.
        return {"runId": run_id, "childRunId": child_run_id, "commandId": command_id,
                "status": "queued"}

    async def send_child_input(self, run_id: str, child_run_id: str, *, text: str,
                               command_id: str, expected_goal_revision: int) -> dict:
        handle = self._child_control_handle(run_id, allow_suspended=True)
        try:
            receipt = await self.executor.send_child_input(
                handle, child_run_id=child_run_id, text=text, command_id=command_id,
                expected_goal_revision=expected_goal_revision,
            )
        except UnsupportedControlError as exc:
            raise StudioError("RUN_CHILD_CONTROL_UNSUPPORTED", "当前 Runtime 不支持子任务输入",
                              status_code=409) from exc
        except ValueError as exc:
            raise StudioError("RUN_CHILD_INPUT_REJECTED", "子任务输入未被接受",
                              status_code=409) from exc
        if getattr(receipt, "status", None) not in {"accepted", "duplicate"}:
            raise StudioError("RUN_CHILD_INPUT_REJECTED", "子任务输入未被接受", status_code=409)
        return {"runId": run_id, "childRunId": child_run_id, "commandId": command_id,
                "status": "queued"}

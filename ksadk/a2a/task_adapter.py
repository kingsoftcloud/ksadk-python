"""A2ARuntimeTaskAdapter — A2A Task 与 Runtime session/run/checkpoint/cancel 的映射 (goal-05 §7.2)。

| A2A | Runtime |
|---|---|
| ``context_id`` | ``session_id`` |
| ``task_id`` | SDK TaskStore 主键;run/checkpoint 引用保存在 Task metadata |
| working | active invocation |
| input-required | pending interaction + checkpoint/resume token |
| canceled | ``RuntimeAdapter.cancel(invocation_id)`` 成功后的终态 |
| artifact/message | RuntimeEvent artifact/text/data |

cancel 一律走 G0.3 冻结的 ``RuntimeAdapter.cancel``(含 pending-cancel 语义),
不在 executor/本模块自造一套。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any, Optional, cast

from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message as ProtobufMessage

from ksadk.events import RuntimeEvent
from ksadk.runtime.adapter import (
    CancelResult,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeAdapter,
    StartRequest,
)

logger = logging.getLogger(__name__)


class A2ARuntimeTaskAdapter:
    """A2A Task ↔ Runtime 的映射器。

    持有一个 G0.3 ``RuntimeAdapter``(由 runtime registry / runner bridge 提供),
    把 A2A 侧的 task/cancel/input-required 映射到 Runtime 六动词。
    """

    def __init__(self, runtime_adapter: RuntimeAdapter, *, runtime_type: str = "local") -> None:
        self._adapter = runtime_adapter
        self._runtime_type = runtime_type
        self._handles_by_task_key: dict[tuple[str, str, str], RunHandle] = {}
        self._accepted_canceled_tasks: set[tuple[str, str, str]] = set()

    @property
    def runtime_adapter(self) -> RuntimeAdapter:
        return self._adapter

    # ---- 映射:task_id/context_id ↔ session/invocation ----

    @staticmethod
    def session_id_from_context(context_id: Optional[str]) -> str:
        """§7.2: A2A context_id ↔ Runtime session_id。"""
        return str(context_id or "")

    def handle_for_task(
        self,
        *,
        task_id: str,
        invocation_id: str,
        session_id: str,
        native_ref: Optional[dict[str, Any]] = None,
    ) -> RunHandle:
        """从 Task metadata 还原 Runtime 句柄(run_id = invocation_id)。"""
        return RunHandle(
            run_id=invocation_id or task_id,
            session_id=session_id,
            runtime_type=self._runtime_type,
            native_ref=native_ref or {},
        )

    # ---- cancel:走 RuntimeAdapter.cancel ----

    async def cancel_task(self, task_id: str, context: Any) -> CancelResult:
        """取消 A2A task → RuntimeAdapter.cancel。

        只使用 ``start`` 返回的进程内真实 handle，或 input-required Task metadata 中
        持久化的 handle；找不到时返回 NOT_RUNNING，不按 task_id 构造假 handle。
        """
        task_key = self._task_key(task_id, context)
        handle = self._handles_by_task_key.get(task_key)
        restored = handle is None
        if handle is None:
            handle = self._restore_handle_from_task(task_id, context)
        if handle is None or not self._handle_matches_context(handle, context):
            return CancelResult.NOT_RUNNING
        try:
            if restored:
                await self._adapter.attach(handle)
            result = await self._adapter.cancel(handle)
        except Exception as exc:  # noqa: BLE001
            logger.error("A2A runtime cancel failed (%s)", type(exc).__name__)
            return CancelResult.FAILED
        if result in {
            CancelResult.INTERRUPTED_ACTIVE_TURN,
            CancelResult.PENDING_CANCEL_RECORDED,
        }:
            self._accepted_canceled_tasks.add(task_key)
        return result

    # ---- input-required → checkpoint/resume token ----

    def build_resume_target(self, *, invocation_id: str) -> ResumeTarget:
        """input-required 的恢复目标(精确到 invocation)。"""
        return ResumeTarget(kind="invocation_id", id=invocation_id)

    def build_resume_payload(self, *, call_id: str | None, answer: Any) -> ResumePayload:
        """input-required 的回包(HITL 回答 / 审批决定)。"""
        return ResumePayload(kind="hitl_answer", call_id=call_id, data=answer)

    async def start_task(
        self,
        *,
        task_id: str,
        context: Any,
        input_data: Any,
    ) -> RunHandle:
        """通过 RuntimeAdapter 启动 A2A task,返回后续共用的真实 handle。"""
        context_metadata = self._as_dict(getattr(context, "metadata", None))
        context_metadata.pop("user_id", None)
        context_metadata.pop("agent_id", None)
        tenant = self._trusted_tenant(context)
        handle = await self._adapter.start(
            StartRequest(
                input=input_data,
                user_id=tenant,
                session_id=self._extract_session_id(context),
                agent_id=None,
                metadata={**context_metadata, "invocation_id": task_id},
            )
        )
        self._handles_by_task_key[self._task_key(task_id, context)] = handle
        return handle

    def build_resume_metadata(
        self,
        *,
        handle: RunHandle,
        checkpoint_id: str | None,
        call_id: str | None,
        payload_kind: str,
    ) -> dict[str, Any]:
        """把 input-required 的真实 handle 与恢复命令序列化到 Task metadata。"""
        if checkpoint_id:
            handle.native_ref["checkpoint_id"] = checkpoint_id
            known_checkpoint_ids = handle.native_ref.setdefault("known_checkpoint_ids", [])
            if checkpoint_id not in known_checkpoint_ids:
                known_checkpoint_ids.append(checkpoint_id)
        if call_id:
            pending_approval_ids = handle.native_ref.setdefault("pending_approval_ids", [])
            if call_id not in pending_approval_ids:
                pending_approval_ids.append(call_id)
        target = ResumeTarget(
            kind="checkpoint_id" if checkpoint_id else "invocation_id",
            id=str(checkpoint_id or handle.run_id),
        )
        payload = ResumePayload.model_validate(
            {"kind": payload_kind, "call_id": call_id, "data": None}
        )
        return {
            "resume_kind": "checkpoint",
            "checkpoint_id": str(checkpoint_id or target.id),
            "run_handle": handle.model_dump(mode="json"),
            "resume_target": target.model_dump(mode="json"),
            "resume_payload": payload.model_dump(mode="json"),
        }

    async def resume_task(
        self,
        task_id: str,
        context: Any,
        *,
        answer: Any,
    ) -> RunHandle:
        """从 input-required Task metadata 恢复同一 runtime handle。"""
        handle, target, payload = self.validate_resume_task(
            task_id,
            context,
            answer=answer,
        )
        task_key = self._task_key(task_id, context)
        local_handle = self._handles_by_task_key.get(task_key)
        if local_handle is None:
            attached_handle = await self._adapter.attach(handle)
            if attached_handle != handle:
                raise ValueError("RuntimeAdapter.attach returned a different run handle")
        elif local_handle != handle:
            raise ValueError("persisted run_handle conflicts with the active task handle")
        resumed_handle = await self._adapter.resume(handle, target, payload)
        if resumed_handle != handle:
            raise ValueError("RuntimeAdapter.resume returned a different run handle")
        self._handles_by_task_key[task_key] = resumed_handle
        return resumed_handle

    def validate_resume_task(
        self,
        task_id: str,
        context: Any,
        *,
        answer: Any,
    ) -> tuple[RunHandle, ResumeTarget, ResumePayload]:
        """Validate an A2A resume command without changing task/runtime state."""
        task = getattr(context, "current_task", None)
        if str(getattr(task, "id", "") or "") != task_id:
            raise ValueError("resume token does not belong to the requested task")
        metadata = self._resume_metadata(context)
        handle = self._parse_handle(metadata)
        target = self._parse_target(metadata)
        payload = self._parse_payload(metadata, answer=answer)
        if not self._handle_matches_context(handle, context):
            raise ValueError("run_handle does not match A2A context or task adapter")
        return handle, target, payload

    def stream_task(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        """恢复后订阅同一个 RuntimeAdapter 的同一 handle。"""
        return self._adapter.stream(handle)

    def was_cancel_accepted(self, task_id: str, context: Any, handle: RunHandle) -> bool:
        """Return whether cancel was accepted for this exact runtime handle."""
        task_key = self._task_key(task_id, context)
        return (
            self._handles_by_task_key.get(task_key) == handle
            and task_key in self._accepted_canceled_tasks
        )

    def forget_task(self, task_id: str, context: Any, handle: RunHandle) -> None:
        """Release process-local tracking after a terminal task state."""
        task_key = self._task_key(task_id, context)
        if self._handles_by_task_key.get(task_key) == handle:
            self._handles_by_task_key.pop(task_key, None)
        self._accepted_canceled_tasks.discard(task_key)

    @staticmethod
    def answer_from_context(context: Any) -> Any:
        """优先读取 A2A ``Part.data``,保留 false/0/空串/null。"""
        message = getattr(context, "message", None)
        for part in getattr(message, "parts", ()) or ():
            data = getattr(part, "data", None)
            which_oneof = getattr(data, "WhichOneof", None)
            if (
                not isinstance(data, ProtobufMessage)
                or not callable(which_oneof)
                or which_oneof("kind") is None
            ):
                continue
            converted = MessageToDict(data, preserving_proto_field_name=True)
            if isinstance(converted, Mapping):
                normalized = dict(converted)
                if set(normalized) == {"value"}:
                    return normalized["value"]
                return normalized
            return converted
        return context.get_user_input()

    # ---- metadata 提取 ----

    @staticmethod
    def _extract_session_id(context: Any) -> str:
        # A follow-up A2A message may carry only task_id. The durable Task is the
        # authority for the original context/session across requests and restarts.
        current_task = getattr(context, "current_task", None)
        task_context_id = getattr(current_task, "context_id", None)
        return str(task_context_id or getattr(context, "context_id", "") or "")

    @staticmethod
    def _trusted_tenant(context: Any) -> str:
        call_context = getattr(context, "call_context", None)
        return str(getattr(call_context, "tenant", "") or "anonymous")

    def _task_key(self, task_id: str, context: Any) -> tuple[str, str, str]:
        return (
            self._trusted_tenant(context),
            self._extract_session_id(context),
            task_id,
        )

    def _restore_handle_from_task(self, task_id: str, context: Any) -> RunHandle | None:
        task = getattr(context, "current_task", None)
        if str(getattr(task, "id", "") or "") != task_id:
            return None
        try:
            return self._parse_handle(self._resume_metadata(context))
        except ValueError:
            return None

    def _handle_matches_context(self, handle: RunHandle, context: Any) -> bool:
        return (
            handle.session_id == self._extract_session_id(context)
            and handle.runtime_type == self._runtime_type
        )

    @classmethod
    def _resume_metadata(cls, context: Any) -> dict[str, Any]:
        task = getattr(context, "current_task", None)
        status = getattr(task, "status", None)
        status_metadata = cls._as_dict(getattr(status, "metadata", None))
        task_metadata = cls._as_dict(getattr(task, "metadata", None))
        metadata = status_metadata or task_metadata
        if not metadata:
            raise ValueError("input-required task is missing resume metadata")
        return metadata

    @classmethod
    def _parse_handle(cls, metadata: Mapping[str, Any]) -> RunHandle:
        raw = cls._as_dict(metadata.get("run_handle"))
        if not raw:
            raise ValueError("resume metadata is missing run_handle")
        try:
            return cast(RunHandle, RunHandle.model_validate(raw))
        except Exception as exc:  # noqa: BLE001
            raise ValueError("invalid run_handle in resume metadata") from exc

    @classmethod
    def _parse_target(cls, metadata: Mapping[str, Any]) -> ResumeTarget:
        raw = cls._as_dict(metadata.get("resume_target"))
        if not raw:
            raise ValueError("resume metadata is missing resume_target")
        try:
            target = cast(ResumeTarget, ResumeTarget.model_validate(raw))
        except Exception as exc:  # noqa: BLE001
            raise ValueError("invalid resume_target in resume metadata") from exc
        if not target.id:
            raise ValueError("invalid resume_target in resume metadata")
        return target

    @classmethod
    def _parse_payload(
        cls,
        metadata: Mapping[str, Any],
        *,
        answer: Any,
    ) -> ResumePayload:
        raw = cls._as_dict(metadata.get("resume_payload"))
        if not raw:
            raise ValueError("resume metadata is missing resume_payload")
        try:
            template = ResumePayload.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("invalid resume_payload in resume metadata") from exc
        data = cls._approval_decision(answer) if template.kind == "approval_decision" else answer
        return cast(ResumePayload, template.model_copy(update={"data": data}))

    @staticmethod
    def _approval_decision(answer: Any) -> dict[str, list[dict[str, Any]]]:
        if isinstance(answer, Mapping):
            if "decisions" in answer:
                decisions = answer.get("decisions")
                if not isinstance(decisions, list) or not decisions:
                    raise ValueError("unknown approval decision")
                normalized = [dict(item) for item in decisions if isinstance(item, Mapping)]
                if len(normalized) != len(decisions):
                    raise ValueError("unknown approval decision")
                if any(
                    str(item.get("type") or "").strip().lower() not in {"approve", "edit", "reject"}
                    for item in normalized
                ):
                    raise ValueError("unknown approval decision")
                return {"decisions": normalized}
            decision = dict(answer)
            decision_type = str(decision.get("type") or "").strip().lower()
            if decision_type not in {"approve", "edit", "reject"}:
                raise ValueError("unknown approval decision")
            decision["type"] = decision_type
            return {"decisions": [decision]}
        token = str(answer).strip().lower() if answer is not None else ""
        if token not in {"approve", "edit", "reject"}:
            raise ValueError(f"unknown approval decision: {answer!r}")
        return {"decisions": [{"type": token}]}

    @staticmethod
    def _as_dict(value: Any) -> dict[str, Any]:
        descriptor = getattr(value, "DESCRIPTOR", None)
        if descriptor is not None:
            converted = MessageToDict(value, preserving_proto_field_name=True)
            return dict(converted) if isinstance(converted, Mapping) else {}
        if isinstance(value, Mapping):
            return dict(value)
        if value is None:
            return {}
        return {}


__all__ = ["A2ARuntimeTaskAdapter"]

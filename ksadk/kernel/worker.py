# -*- coding: utf-8 -*-
"""per-session FIFO worker（Phase 1 Task 6 Step 6）。

- 持有 activation lease（fencing token 通过 Store 的 CAS 校验）才能 claim。
- 按 per-session accepted_seq 保序；active Run 存在时普通 enqueue 保持排队，
  只执行允许作用于该 Run 的控制命令（interrupt/pause/steer/...）。
- 异常分类：retryable kernel error（消息保持 claimed）、typed runtime
  rejection（discarded + control.command_rejected）、terminal failure
  （不 ack，消息保持 claimed 等待 takeover reclaim）。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from ksadk.kernel.contracts import (
    ActivationLease,
    ActivationWriteGuard,
    AgentControlCommand,
)
from ksadk.kernel.contracts import (
    InjectPayload as ContractInjectPayload,
)
from ksadk.kernel.contracts import (
    SteerPayload as ContractSteerPayload,
)
from ksadk.kernel.errors import (
    AgentKernelError,
    InvalidCommandError,
    StaleFenceError,
    UnsupportedControlError,
)
from ksadk.kernel.mapping import COMMAND_HANDLERS, RESUME_TARGET_KINDS
from ksadk.kernel.state import RunState
from ksadk.kernel.store import (
    AgentKernelStore,
    InboxMessage,
    RunRecord,
    control_event,
    new_message_id,
)
from ksadk.runtime.adapter import (
    CancelResult,
    PauseResult,
    RunHandle,
    RuntimeAdapter,
    StartRequest,
)
from ksadk.runtime.adapter import (
    ResumePayload as AdapterResumePayload,
)
from ksadk.runtime.adapter import (
    ResumeTarget as AdapterResumeTarget,
)

WorkOutcome = Literal[
    "idle", "claimed", "completed", "retryable_failure", "terminal_failure"
]


@dataclass(frozen=True)
class WorkResult:
    """进程内调度结果，不是公网协议。idle 时后三项可为空。"""

    outcome: WorkOutcome
    message_id: str | None = None
    run_id: str | None = None
    last_seq: int | None = None


class AgentKernelWorker:
    def __init__(
        self,
        store: AgentKernelStore,
        *,
        adapter_factory: Callable[[], RuntimeAdapter],
        session_events: object | None = None,
    ) -> None:
        self._store = store
        self._adapter_factory = adapter_factory
        # SessionEventStore（typed RuntimeEventStore 的 envelope 写路径）。
        # 缺省时不落 runtime 事件，仅保证 stream 被消费到自然结束。
        self._session_events = session_events
        # 进程内 handle cache：owner 真相在 Store 的 RunRecord，cache miss
        # 不能等价于 Run 不存在（只能说明本进程未 attach）。
        self._handles: dict[str, RunHandle] = {}

    def attach_handle(self, run_id: str, handle: RunHandle) -> None:
        self._handles[run_id] = handle

    async def run_once(
        self, agent_instance_id: str, activation: ActivationLease
    ) -> WorkResult:
        fence = activation.fencing_token
        pending = await self._store.list_pending(
            agent_instance_id, fencing_token=fence
        )
        if not pending:
            return WorkResult(outcome="idle")

        # per-session FIFO：每个 session 只看队头，控制命令可越过被
        # active Run 挡住的 enqueue；全局按 accepted_seq 取最早可执行者。
        # 只处理当前 activation 持有 lease 的 session，避免跨 session 抢占。
        heads: dict[str, InboxMessage] = {}
        for message in pending:
            heads.setdefault(message.session_id, message)
        eligible = None
        for message in sorted(heads.values(), key=lambda m: m.accepted_seq):
            lease = await self._store.current_lease(
                agent_instance_id, message.session_id
            )
            if lease is None or lease.activation_id != activation.activation_id:
                continue  # 该 session 归其它 activation（或无人）持有
            if message.command is None:  # pragma: no cover - defensive
                continue
            if message.command.command_type == "enqueue":
                active = await self._store.find_active_run(
                    agent_instance_id, message.session_id
                )
                if active is not None:
                    continue  # enqueue 保持排队
            eligible = message
            break
        if eligible is None:
            return WorkResult(outcome="idle")

        claimed = await self._store.claim_message(eligible.message_id, fence)
        result = await self._execute_claim(claimed.command, activation)
        return result

    # ------------------------------------------------------------- execution

    async def _execute_claim(
        self, command: AgentControlCommand, activation: ActivationLease
    ) -> WorkResult:
        fence = activation.fencing_token
        message_id = await self._message_id_for(command)
        try:
            run_id = await self._dispatch(command, activation)
        except (UnsupportedControlError, InvalidCommandError) as error:
            # typed rejection：确定性收口，不重试。
            await self._store.append_event(
                control_event(
                    session_id=command.session_id,
                    event_type="control.command_rejected",
                    payload={
                        "command_id": str(command.command_id),
                        "status": "rejected",
                        "reason": getattr(error, "code", "unsupported"),
                    },
                    causation_id=str(command.command_id),
                ),
                expected_fence=fence,
                agent_instance_id=command.agent_instance_id,
            )
            await self._store.discard_claim(message_id, expected_fence=fence)
            return WorkResult(outcome="completed", message_id=message_id)
        except StaleFenceError:
            return WorkResult(outcome="terminal_failure", message_id=message_id)
        except AgentKernelError as error:
            if error.retryable:
                return WorkResult(
                    outcome="retryable_failure", message_id=message_id
                )
            return WorkResult(outcome="terminal_failure", message_id=message_id)
        except Exception:
            # 未知异常绝不 ack 为成功：消息保持 claimed。
            return WorkResult(outcome="terminal_failure", message_id=message_id)

        try:
            await self._store.complete_claim(message_id, expected_fence=fence)
        except StaleFenceError:
            return WorkResult(outcome="terminal_failure", message_id=message_id)
        return WorkResult(outcome="completed", message_id=message_id, run_id=run_id)

    async def _message_id_for(self, command: AgentControlCommand) -> str:
        message = await self._store.load_by_idempotency(
            command.session_id, command.idempotency_key
        )
        assert message is not None  # claim 刚发生
        return message.message_id

    async def _dispatch(
        self, command: AgentControlCommand, activation: ActivationLease
    ) -> str | None:
        handler = COMMAND_HANDLERS[command.command_type]
        if handler == "start":
            return await self._start_run(command, activation)
        return await self._control_active_run(command, activation)

    # enqueue -> adapter.start，仅在没有 active Run 时到达这里。
    async def _start_run(
        self, command: AgentControlCommand, activation: ActivationLease
    ) -> str:
        from ksadk.runtime.executor import handle_digest

        fence = activation.fencing_token
        guard = ActivationWriteGuard(
            activation_id=activation.activation_id, fencing_token=fence
        )
        run_id = new_message_id()
        adapter = self._adapter_factory()
        pending = RunRecord(
            run_id=run_id,
            agent_instance_id=command.agent_instance_id,
            session_id=command.session_id,
            state=RunState.PENDING,
        )
        created = await self._store.save_run_transition(
            pending, expected_fence=fence
        )
        handle = await adapter.start(
            StartRequest(
                input=command.payload.get("content"),
                user_id="agent-kernel",
                session_id=command.session_id,
                # durable run_id 优先传给 adapter；adapter 不认时以
                # runtime_run_id 映射显式记录两个 ID 的对应关系。
                metadata={"command_id": str(command.command_id), "run_id": run_id},
            )
        )
        running_update: dict = {
            "state": RunState.RUNNING,
            "handle": handle.model_dump(mode="json"),
            "handle_digest": handle_digest(handle),
        }
        if handle.run_id != run_id:
            running_update["runtime_run_id"] = handle.run_id
        running = created.model_copy(update=running_update)
        await self._store.save_run_transition(running, expected_fence=fence)
        self._handles[handle.run_id] = handle
        try:
            # 消费整个事件流；只有自然结束才收口 COMPLETED，
            # 异常交给 _execute_claim 做 retryable/terminal/typed 分类。
            await self._consume_stream(adapter, handle, running, guard)
        finally:
            self._handles.pop(handle.run_id, None)
        return run_id

    async def _consume_stream(
        self,
        adapter: RuntimeAdapter,
        handle: RunHandle,
        run: RunRecord,
        guard: ActivationWriteGuard,
    ) -> None:
        """消费 run 的事件流并把每个事实落为 family=runtime/v2 事件。

        事件 run_id 统一改写为 durable run_id（adapter 私有 run_id 通过
        RunRecord.metadata.runtime_run_id 记录映射）。stream 自然结束后
        才把 run 收口为 COMPLETED；任何异常原样上抛。
        """

        runtime_store = None
        if self._session_events is not None:
            # 延迟导入：ksadk.events 反向依赖 kernel.contracts，避免模块环。
            from ksadk.events.canonical_store import RuntimeEventStore

            runtime_store = RuntimeEventStore(
                self._session_events, session_id=run.session_id
            )
        async for event in adapter.stream(handle):
            if runtime_store is None:
                continue
            if event.run_id != run.run_id:
                update: dict = {"run_id": run.run_id}
                if getattr(event, "scope_id", None) == f"run:{handle.run_id}":
                    update["scope_id"] = f"run:{run.run_id}"
                event = event.model_copy(update=update)
            await runtime_store.append(event, guard=guard)
        await self._store.save_run_transition(
            run.model_copy(update={"state": RunState.COMPLETED}),
            expected_fence=guard.fencing_token,
        )

    # 控制命令必须作用于 active Run 且本进程持有 live handle。
    async def _control_active_run(
        self, command: AgentControlCommand, activation: ActivationLease
    ) -> str | None:
        fence = activation.fencing_token
        active = await self._store.find_active_run(
            command.agent_instance_id, command.session_id
        )
        if active is None:
            raise UnsupportedControlError(
                "runtime_no_active_run",
                details={"command_type": command.command_type},
            )
        handle = self._handles.get(active.run_id)
        if handle is None:
            raise UnsupportedControlError(
                "runtime_not_attached",
                details={"run_id": active.run_id},
            )
        adapter = self._adapter_factory()
        verb = COMMAND_HANDLERS[command.command_type]

        if verb == "cancel":
            result = await adapter.cancel(handle)
            if result == CancelResult.INTERRUPTED_ACTIVE_TURN:
                await self._transition_run(active, RunState.CANCELLED, fence)
        elif verb == "pause":
            result = await adapter.pause(handle)
            if result == PauseResult.PAUSED_ACTIVE_TURN:
                await self._transition_run(active, RunState.PAUSED, fence)
        elif verb == "resume":
            target_dict = dict(command.payload.get("target") or {})
            target = AdapterResumeTarget(
                kind=RESUME_TARGET_KINDS[target_dict["kind"]],
                id=str(target_dict["id"]),
            )
            resumed = await adapter.resume(
                handle, target, AdapterResumePayload(kind="free_text")
            )
            self._handles[active.run_id] = resumed
            await self._consume_stream(
                adapter,
                resumed,
                active,
                ActivationWriteGuard(
                    activation_id=activation.activation_id, fencing_token=fence
                ),
            )
        elif verb == "submit":
            await adapter.submit(handle, AdapterResumePayload(kind="hitl_answer"))
            await self._consume_stream(
                adapter,
                handle,
                active,
                ActivationWriteGuard(
                    activation_id=activation.activation_id, fencing_token=fence
                ),
            )
        elif verb == "steer":
            await adapter.steer(
                handle, ContractSteerPayload.model_validate(dict(command.payload))
            )
        elif verb == "inject":
            await adapter.inject(
                handle, ContractInjectPayload.model_validate(dict(command.payload))
            )
        else:  # pragma: no cover - mapping 冻结
            raise UnsupportedControlError(f"unknown handler {verb!r}")
        return active.run_id

    async def _transition_run(
        self, run: RunRecord, state: RunState, fence: int
    ) -> None:
        await self._store.save_run_transition(
            run.model_copy(update={"state": state}), expected_fence=fence
        )


__all__ = ["AgentKernelWorker", "WorkResult", "WorkOutcome"]

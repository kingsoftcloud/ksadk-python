# -*- coding: utf-8 -*-
"""per-session FIFO worker（Kernel Task 6 Step 6）。

- 持有 activation lease（fencing token 通过 Store 的 CAS 校验）才能 claim。
- 按 per-session accepted_seq 保序；active Run 存在时普通 enqueue 保持排队，
  只执行允许作用于该 Run 的控制命令（interrupt/pause/steer/...）。
- 异常分类：retryable kernel error（消息保持 claimed）、typed runtime
  rejection（discarded + control.command_rejected）、terminal failure
  （不 ack，消息保持 claimed 等待 takeover reclaim）。

Task 6：Activation 通过 :class:`ActiveExecution` 拥有 Adapter/RunHandle/
InteractionProvider——控制命令与 Interaction 回包永远作用于同一 live
execution（同一 client 实例）；control lookup 永远按 durable run id。
``submit_interaction`` 不再是静态 ``adapter.submit`` 映射：Worker 载入权威
``InteractionRecord``，调用其绑定 provider 送达回包，provider 接受后才写
``InteractionResolved``，同 fence 恢复 stream 消费。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Literal

from ksadk.interaction.provider import (
    RUNTIME_INTERACTION_UNAVAILABLE,
    InteractionProvider,
    UnavailableInteractionProvider,
)
from ksadk.interaction.providers import default_interaction_providers
from ksadk.kernel.approval_presentation import approval_presentation
from ksadk.kernel.contracts import (
    ActivationLease,
    ActivationWriteGuard,
    AgentControlCommand,
)
from ksadk.kernel.errors import (
    AgentKernelError,
    InvalidCommandError,
    StaleFenceError,
    UnsupportedControlError,
)
from ksadk.kernel.execution_grants import (
    ExecutionGrantBlocked,
    execution_grant_id,
    execution_grant_run_id,
)
from ksadk.kernel.mapping import COMMAND_HANDLERS
from ksadk.kernel.state import RunState
from ksadk.kernel.store import (
    AgentKernelStore,
    RunRecord,
    control_event,
)
from ksadk.kernel.worker_controls import WorkerControlMixin
from ksadk.kernel.worker_identity import prepare_worker_identity
from ksadk.runtime.adapter import (
    CONVERSATION_PREPROCESSING_METADATA_KEY,
    RunHandle,
    RuntimeAdapter,
    StartRequest,
)
from ksadk.runtime_context import TRUSTED_IDENTITY_METADATA_KEY

from ksadk.kernel.worker_interactions import _WorkerInteractionMixin

logger = logging.getLogger(__name__)


WorkOutcome = Literal["idle", "claimed", "completed", "retryable_failure", "terminal_failure"]


@dataclass
class ActiveExecution:
    """一个 activation 拥有的 live execution（Task 6 Step 4）。

    owner 真相仍在 Store 的 RunRecord（durable run id）；本结构只是当前
    进程持有 lease 期间的运行期句柄集合——adapter（含框架 client）、
    live handle 与回包送达 provider 必须同源，否则回包会打到另一个
    client 实例上静默丢失。
    """

    durable_run_id: str
    runtime_run_id: str
    adapter: RuntimeAdapter
    handle: RunHandle
    interaction_provider: InteractionProvider
    stream_task: asyncio.Task[None] | None = None
    stream_guard: ActivationWriteGuard | None = None
    managed_interaction_waiters: dict = field(default_factory=dict)
    managed_a2a_bootstraps: dict = field(default_factory=dict)
    recovering: bool = False


@dataclass(frozen=True)
class WorkResult:
    """进程内调度结果，不是公网协议。idle 时后三项可为空。"""

    outcome: WorkOutcome
    message_id: str | None = None
    run_id: str | None = None
    last_seq: int | None = None
    error: Exception | None = None


class AgentKernelWorker(_WorkerInteractionMixin, WorkerControlMixin):
    def __init__(
        self,
        store: AgentKernelStore,
        *,
        adapter_factory: Callable[[], RuntimeAdapter],
        session_events: object | None = None,
        interaction_providers: Mapping[str, InteractionProvider] | None = None,
        start_request_defaults: Mapping[str, object] | None = None,
        session_service: object | None = None,
    ) -> None:
        self._store = store
        self._adapter_factory = adapter_factory
        # SessionEventStore（typed RuntimeEventStore 的 envelope 写路径）。
        # 缺省时不落 runtime 事件，仅保证 stream 被消费到自然结束。
        self._session_events = session_events
        self._session_service = session_service
        # Deployment-owned defaults (model, prompt and sandbox) come from the
        # admitted immutable manifest. Server may attach a bounded per-turn
        # model/approval selector to the signed command; the worker validates
        # the model allow-list and never lets that selector replace sandbox.
        self._start_request_defaults = dict(start_request_defaults or {})
        # Task 6：activation 拥有 Adapter/RunHandle/Provider 的 live 表。
        # key 永远是 durable run id；cache miss 不能等价于 Run 不存在
        # （只能说明本进程未 attach，takeover 后由 adopt_execution 重建）。
        self._executions: dict[str, ActiveExecution] = {}
        self._providers: dict[str, InteractionProvider] = (
            dict(interaction_providers)
            if interaction_providers is not None
            else default_interaction_providers()
        )
        # A stream may fail after its enqueue has been durably completed.  Keep
        # the exception observable to diagnostics without leaving an unhandled
        # Task warning; the durable run remains open for recovery/takeover.
        self._background_stream_errors: dict[str, tuple[ActivationWriteGuard, Exception]] = {}
        # ``ActivationLease`` is a frozen wire contract and intentionally does
        # not carry ``session_id``.  Production composition roots therefore
        # pass the session scope to ``run_once`` explicitly.  Serialize that
        # scope in-process as well: two scheduler ticks for the same Session
        # must never both list/claim the Inbox head before either claim has
        # completed.  The durable activation/fence remains the cross-process
        # authority; this lock closes the same-owner re-entrancy window.
        self._session_locks: dict[tuple[str, str], asyncio.Lock] = {}

    def attach_handle(
        self,
        run_id: str,
        handle: RunHandle,
        *,
        adapter: RuntimeAdapter | None = None,
        provider: InteractionProvider | None = None,
    ) -> None:
        """把 live handle 注册为当前 activation 的 ActiveExecution。"""

        self.adopt_execution(
            durable_run_id=run_id,
            runtime_run_id=handle.run_id,
            adapter=adapter if adapter is not None else self._adapter_factory(),
            handle=handle,
            provider=provider,
        )

    def adopt_execution(
        self,
        *,
        durable_run_id: str,
        runtime_run_id: str,
        adapter: RuntimeAdapter,
        handle: RunHandle,
        provider: InteractionProvider | None = None,
    ) -> ActiveExecution:
        """takeover 后重建 ActiveExecution（仅在 lease 获取 + attach/resume
        成功后由 RecoveryCoordinator 调用；provider 按 runtime_type 解析）。"""

        if provider is None:
            provider = self._providers.get(handle.runtime_type, UnavailableInteractionProvider())
        execution = ActiveExecution(
            durable_run_id=durable_run_id,
            runtime_run_id=runtime_run_id,
            adapter=adapter,
            handle=handle,
            interaction_provider=provider,
        )
        self._executions[durable_run_id] = execution
        return execution

    def recover_stream(
        self,
        *,
        run: RunRecord,
        adapter: RuntimeAdapter,
        handle: RunHandle,
        guard: ActivationWriteGuard,
    ) -> ActiveExecution:
        """Own native recovery under the same fenced event/interaction pipeline."""
        if handle.session_id != run.session_id or handle.run_id != run.metadata.get(
            "runtime_run_id", run.run_id
        ):
            raise ValueError("recovery handle does not match durable run")
        current = self._executions.get(run.run_id)
        if (
            current is not None
            and current.stream_task is not None
            and not current.stream_task.done()
        ):
            raise ValueError("recovery cannot replace an active stream owner")
        execution = self.adopt_execution(
            durable_run_id=run.run_id,
            runtime_run_id=handle.run_id,
            adapter=adapter,
            handle=handle,
        )
        execution.recovering = True
        return self._start_stream(execution, run, guard)

    def execution_for(self, durable_run_id: str) -> ActiveExecution | None:
        """control lookup 入口：永远按 durable run id 查 live execution。"""

        return self._executions.get(durable_run_id)

    def take_stream_failure(self, run_id: str, activation: ActivationLease) -> Exception | None:
        """Hand a failed stream back only to its original fenced owner."""
        failure = self._background_stream_errors.pop(run_id, None)
        if failure is None:
            return None
        guard, error = failure
        if (guard.activation_id, guard.fencing_token) != (
            activation.activation_id, activation.fencing_token
        ):
            return None
        return error

    def active_session_ids(self) -> set[str]:
        """Sessions whose activation must stay alive after Inbox ack.

        An enqueue is acknowledged once ``adapter.start`` returns, while its
        RuntimeEvent stream may continue for minutes.  The composition root
        uses this set to renew the lease during that interval; relying only on
        accepted/claimed Inbox messages opens a stale-fence window mid-stream.
        """

        return {execution.handle.session_id for execution in self._executions.values()}

    async def run_once(
        self,
        agent_instance_id: str,
        activation: ActivationLease,
        *,
        session_id: str | None = None,
    ) -> WorkResult:
        if session_id is not None:
            key = (agent_instance_id, session_id)
            lock = self._session_locks.setdefault(key, asyncio.Lock())
            async with lock:
                return await self._run_once(agent_instance_id, activation, session_id=session_id)
        # Compatibility for direct/test callers written before the internal
        # scheduler API became session-scoped.  Production callers below all
        # provide ``session_id``; the frozen ActivationLease JSON is unchanged.
        return await self._run_once(agent_instance_id, activation, session_id=None)

    async def _run_once(
        self,
        agent_instance_id: str,
        activation: ActivationLease,
        *,
        session_id: str | None,
    ) -> WorkResult:
        fence = activation.fencing_token
        pending = await self._store.list_pending(
            agent_instance_id, session_id=session_id, fencing_token=fence
        )
        if not pending:
            return WorkResult(outcome="idle")

        # per-session FIFO：通常按 accepted_seq 执行；但 active Run 会挡住
        # enqueue，此时其后的 interrupt/pause/steer 等控制命令必须能越过
        # 该 enqueue 作用于 active Run。只处理当前 activation 持有 lease 的
        # session，避免跨 session 抢占。
        suspended_enqueue = False
        for message in sorted(pending, key=lambda m: m.accepted_seq):
            if session_id is not None and message.session_id != session_id:
                continue
            lease = await self._store.current_lease(agent_instance_id, message.session_id)
            if lease is None or lease.activation_id != activation.activation_id:
                continue
            if message.command is None:
                continue
            if message.command.command_type == "enqueue":
                active = await self._store.find_active_run(agent_instance_id, message.session_id)
                if active is not None or suspended_enqueue:
                    continue
            if await self._awaiting_recovered_waiter(message.command, activation):
                # Recovery installs managed waiters asynchronously. Keep the
                # accepted reply in the durable inbox until its original waiter
                # exists; other sessions and cancellation can still progress.
                continue
            try:
                # Store claim and execution-grant validation share one atomic
                # transaction. A prior list/read is never start authorization.
                claimed = await self._store.claim_message(message.message_id, fence)
            except ExecutionGrantBlocked as error:
                if error.grant_state in {"suspended", "admission_paused", "queued", "busy"}:
                    # Preserve enqueue FIFO while still letting cancel/resume
                    # controls behind the suspended command reach active work.
                    suspended_enqueue = True
                continue
            return await self._execute_claim(claimed.command, activation)
        return WorkResult(outcome="idle")

    async def _awaiting_recovered_waiter(
        self, command: AgentControlCommand, activation: ActivationLease
    ) -> bool:
        if command.command_type != "submit_interaction":
            return False
        interaction_id = command.payload.get("interaction_id")
        if not isinstance(interaction_id, str) or not interaction_id:
            return False
        active = await self._store.find_active_run(command.agent_instance_id, command.session_id)
        execution = self._executions.get(active.run_id) if active is not None else None
        if not (
            execution is not None and execution.recovering
            and execution.stream_task is not None and not execution.stream_task.done()
            and execution.stream_guard is not None
            and execution.stream_guard.activation_id == activation.activation_id
            and execution.stream_guard.fencing_token == activation.fencing_token
            and interaction_id not in execution.managed_interaction_waiters
        ):
            return False
        record = await self._store.get(  # type: ignore[attr-defined]
            interaction_id, tenant_id=command.tenant_id,
            agent_instance_id=command.agent_instance_id, session_id=command.session_id,
            run_id=active.run_id,
        )
        if (record is None or record.provider_id != "managed-a2a"
                or record.status != "pending"
                or record.tenant_id != command.tenant_id
                or record.agent_instance_id != command.agent_instance_id
                or record.session_id != command.session_id
                or command.payload.get("run_id", record.run_id) != record.run_id):
            return False
        return True

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
                ),
                expected_fence=fence,
                agent_instance_id=command.agent_instance_id,
            )
            await self._store.discard_claim(message_id, expected_fence=fence)
            return WorkResult(outcome="completed", message_id=message_id)
        except StaleFenceError as error:
            return WorkResult(outcome="terminal_failure", message_id=message_id, error=error)
        except AgentKernelError as error:
            if error.code == RUNTIME_INTERACTION_UNAVAILABLE:
                # typed rejection：provider 诚实声明无法原生送达回包，
                # Interaction 绝不标 resolved。
                await self._store.append_event(
                    control_event(
                        session_id=command.session_id,
                        event_type="control.command_rejected",
                        payload={
                            "command_id": str(command.command_id),
                            "status": "rejected",
                            "reason": error.code,
                        },
                    ),
                    expected_fence=fence,
                    agent_instance_id=command.agent_instance_id,
                )
                await self._store.discard_claim(message_id, expected_fence=fence)
                return WorkResult(outcome="completed", message_id=message_id)
            if error.retryable:
                return WorkResult(outcome="retryable_failure", message_id=message_id, error=error)
            logger.error(
                "agent kernel command failed permanently: "
                "agent_instance_id=%s session_id=%s command_id=%s "
                "command_type=%s error=%s: %s",
                command.agent_instance_id,
                command.session_id,
                command.command_id,
                command.command_type,
                type(error).__name__,
                error,
            )
            return WorkResult(outcome="terminal_failure", message_id=message_id, error=error)
        except Exception as error:
            # 未知异常绝不 ack 为成功：消息保持 claimed。
            logger.exception(
                "agent kernel command raised an unexpected exception: "
                "agent_instance_id=%s session_id=%s command_id=%s "
                "command_type=%s error=%s: %s",
                command.agent_instance_id,
                command.session_id,
                command.command_id,
                command.command_type,
                type(error).__name__,
                error,
            )
            return WorkResult(outcome="terminal_failure", message_id=message_id, error=error)

        try:
            await self._store.complete_claim(message_id, expected_fence=fence)
        except StaleFenceError as error:
            return WorkResult(outcome="terminal_failure", message_id=message_id, error=error)
        return WorkResult(outcome="completed", message_id=message_id, run_id=run_id)

    async def _message_id_for(self, command: AgentControlCommand) -> str:
        message = await self._store.load_by_idempotency(command.session_id, command.idempotency_key)
        assert message is not None  # claim 刚发生
        return message.message_id

    async def _dispatch(
        self, command: AgentControlCommand, activation: ActivationLease
    ) -> str | None:
        handler = COMMAND_HANDLERS[command.command_type]
        if handler == "start":
            return await self._start_run(command, activation)
        if handler == "submit_interaction":
            return await self._control_active_run(command, activation)
        return await self._control_active_run(command, activation)

    # enqueue -> adapter.start，仅在没有 active Run 时到达这里。
    async def _start_run(self, command: AgentControlCommand, activation: ActivationLease) -> str:
        from ksadk.runtime.executor import handle_digest

        fence = activation.fencing_token
        guard = ActivationWriteGuard(activation_id=activation.activation_id, fencing_token=fence)
        if command.payload.get("legacy_history") is not None:
            if self._session_events is None:
                raise InvalidCommandError("legacy history import requires a SessionEvent store")
            from ksadk.kernel.legacy_history import import_legacy_history

            await import_legacy_history(
                command,
                session_events=self._session_events,
                guard=guard,
            )
        grant_id = execution_grant_id(command)
        # The stable identity also covers ordinary cloud enqueues. Grant-less
        # requests must not repeat a native start whose response was lost.
        run_id = execution_grant_run_id(command)
        previous = await self._store.load_run(run_id)
        if previous is None and not grant_id:
            # Older workers used random Run IDs. Recover their command link
            # instead of silently creating a second run after an upgrade.
            previous = await self._store.find_command_run(
                command.agent_instance_id, command.session_id, str(command.command_id)
            )
        if previous is not None:
            if (previous.agent_instance_id != command.agent_instance_id
                    or previous.session_id != command.session_id
                    or previous.metadata.get("command_id") != str(command.command_id)):
                raise InvalidCommandError("existing run does not match command identity")
            if previous.state == RunState.PENDING:
                await self._store.save_run_transition(
                    previous.model_copy(update={
                        "state": RunState.INTERRUPTED,
                        "reason": "execution_start_uncertain",
                    }),
                    expected_fence=fence,
                )
            return previous.run_id
        adapter = self._adapter_factory()
        pending = RunRecord(
            run_id=run_id,
            agent_instance_id=command.agent_instance_id,
            session_id=command.session_id,
            state=RunState.PENDING,
            # Keep the admitted control identity on the durable run.  This is
            # not user-supplied metadata: it is copied from the trusted
            # command that the worker actually claimed.  Consumers such as the
            # local Scheduler can therefore reconcile a runtime terminal fact
            # back to one occurrence without matching text or wall-clock time.
            metadata={
                "command_id": str(command.command_id),
                # Presentation is durable run metadata, never a model/provider option.
                "presentation_profile": (
                    "agent-block-v1" if command.payload.get("presentation_profile") == "agent-block-v1"
                    else "flat-v1"
                ),
                **({"execution_grant_id": grant_id} if grant_id else {}),
                "source_kind": command.source.kind,
                "source_ref": command.source.ref,
                "correlation_id": command.correlation_id,
            },
        )
        created = await self._store.save_run_transition(pending, expected_fence=fence)
        prepare_continuation = getattr(adapter, "prepare_session_continuation", None)
        continuation_metadata = await self._session_continuation_metadata(
            command.session_id, include_run=callable(prepare_continuation),
        )
        defaults = self._start_request_defaults
        runtime_options = command.payload.get("runtime_options")
        if not isinstance(runtime_options, Mapping):
            runtime_options = {}
        default_model = str(defaults["model"]) if defaults.get("model") is not None else None
        requested_model = str(runtime_options.get("model") or "").strip()
        allowed_models = {
            str(item).strip()
            for item in (defaults.get("allowed_models") or [])
            if str(item).strip()
        }
        # 显式 allowed_models 是收紧边界(不在名单的请求回落默认);
        # 未声明名单 = 未设限制,run 级 model 覆盖直接生效(RunAgent Model 透传)。
        selected_model = (
            requested_model
            if requested_model and (not allowed_models or requested_model in allowed_models)
            else default_model
        )
        request_config = dict(defaults.get("config") or {})
        requested_collaboration = str(
            runtime_options.get("collaborationMode")
            or runtime_options.get("collaboration_mode")
            or ""
        ).strip()
        if requested_collaboration in {"default", "plan"}:
            request_config["collaboration_mode"] = requested_collaboration
        requested_goal = str(
            runtime_options.get("goalObjective")
            or runtime_options.get("goal_objective")
            or ""
        ).strip()
        if requested_goal:
            request_config["goal_objective"] = requested_goal
        approval_mode = str(runtime_options.get("tool_approval_mode") or "").strip().lower()
        approval_overrides = {
            "ask": "manual",
            "risk": "auto_review",
        }
        if approval_mode in approval_overrides:
            request_config["approval_mode"] = approval_overrides[approval_mode]
        effective_user_id, invocation_identity = await prepare_worker_identity(
            command=command,
            defaults=defaults,
            session_service=self._session_service,
        )

        conversation_metadata = {}
        if adapter.runtime.runtime_type == "harness" and self._session_events is not None:
            from ksadk.kernel.worker_history import harness_conversation_messages

            conversation_metadata[CONVERSATION_PREPROCESSING_METADATA_KEY] = {
                "messages": await harness_conversation_messages(
                    command,
                    store=self._store,
                    session_events=self._session_events,
                ),
            }

        start_request = StartRequest(
            input=command.payload.get("content"),
            user_id=effective_user_id,
            session_id=command.session_id,
            agent_id=str(defaults.get("agent_id") or command.agent_instance_id),
            model=selected_model,
            config=request_config,
            # durable run_id 优先传给 adapter；adapter 不认时以
            # runtime_run_id 映射显式记录两个 ID 的对应关系。
            metadata={
                **(
                    {"session_context": command.payload["session_context"]}
                    if "session_context" in command.payload
                    else {}
                ),
                "command_id": str(command.command_id),
                "run_id": run_id,
                # Managed tools and runners consume the canonical tool profile
                # from invocation metadata, alongside the Harness config above.
                **(
                    {"tool_approval_mode": approval_mode}
                    if approval_mode in {"ask", "risk", "full"} else {}
                ),
                **(
                    {TRUSTED_IDENTITY_METADATA_KEY: dict(invocation_identity)}
                    if isinstance(invocation_identity, Mapping)
                    else {}
                ),
                **continuation_metadata,
                **conversation_metadata,
                # Opaque policy reference is supplied only by a trusted
                # ingress. The Provider resolves and reauthorizes it for
                # this exact Run; Kernel owns no plugin policy semantics.
                **(
                    {"execution_policy_ref": command.payload["execution_policy_ref"]}
                    if isinstance(command.payload.get("execution_policy_ref"), str)
                    else {}
                ),
                # An opaque Teams reference carries no permissions by itself.
                # Trusted host policy resolution must match the prepared
                # command/session/run before exposing tools.
                **(
                    {"teams_context_ref": command.payload["teams_context_ref"]}
                    if isinstance(command.payload.get("teams_context_ref"), str)
                    else {}
                ),
            },
        )
        start_snapshot = (
            start_request.model_dump(mode="json")
            if adapter.runtime.runtime_type == "adk"
            or getattr(adapter, "requires_durable_start_request", False)
            else None
        )
        # Some adapters (notably ADK) consume their first native events inside
        # ``start`` in order to discover the invocation id.  Checkpoint/event
        # writes performed there must use the same Kernel-owned session service
        # as the later background stream.
        from ksadk.sessions import bind_session_service

        start_session_scope = (
            bind_session_service(self._session_service)
            if self._session_service is not None
            else nullcontext()
        )
        with start_session_scope:
            if callable(prepare_continuation) and continuation_metadata.get("thread_id"):
                predecessor_id = continuation_metadata.get("continuation_run_id")
                predecessor = await self._store.load_run(predecessor_id) if predecessor_id else None
                await prepare_continuation(
                    predecessor, start_request, agent_instance_id=command.agent_instance_id,
                    tenant_id=command.tenant_id,
                )
            handle = await adapter.start(start_request)
        running_update: dict = {
            "state": RunState.RUNNING,
            "handle": handle.model_dump(mode="json"),
            "handle_digest": handle_digest(handle),
            "tenant_id": command.tenant_id,
        }
        if start_snapshot is not None:
            # Private RunRecord metadata, committed before the stream can
            # invoke tools or contact a model. Never a public control payload.
            running_update["start_request"] = start_snapshot
        if handle.run_id != run_id:
            running_update["runtime_run_id"] = handle.run_id
        running = created.model_copy(update=running_update)
        await self._store.save_run_transition(running, expected_fence=fence)
        # 控制面始终用 durable RunRecord.run_id 查询；adapter 可以拒绝调用方
        # 指定的 run id，因此绝不能以 runtime 私有 id 作为 cache key。
        # Task 6：Activation 拥有 adapter + handle + provider。
        execution = self.adopt_execution(
            durable_run_id=run_id,
            runtime_run_id=handle.run_id,
            adapter=adapter,
            handle=handle,
        )
        execution = self._start_stream(execution, running, guard)
        # Keep the historical synchronous result for immediately exhausted
        # streams (including deterministic test adapters), while a genuinely
        # live stream runs in the background so it cannot block Inbox polling.
        await asyncio.sleep(0)
        if execution.stream_task is not None and execution.stream_task.done():
            execution.stream_task.result()
        return run_id

    async def _session_continuation_metadata(
        self, session_id: str, *, include_run: bool = False,
    ) -> dict[str, str]:
        """Recover the latest native thread identity for a follow-up turn.

        A durable Session owns multiple terminal Runs.  Starting each enqueue
        without the previous ``thread_resume`` continuation silently creates a
        fresh provider conversation, so the UI appears multi-turn while the
        model has no prior context.  The canonical SessionEvent log is the
        authority for this mapping and survives worker/process replacement.
        """

        if self._session_events is None:
            return {}
        from ksadk.events.canonical import ContinuationCreated, ContinuationResumed, RunCompleted
        from ksadk.events.canonical_store import RuntimeEventStore

        events = await RuntimeEventStore(self._session_events).list(session_id, limit=256)
        for event in reversed(events):
            if include_run and isinstance(event, RunCompleted) and event.source.framework == "codex":
                # A long turn can evict its initial continuation fact from this
                # tail window; its completed fact carries the same thread scope.
                completed_thread = str(event.source.metadata.get("thread_id") or "").strip()
                if completed_thread:
                    return {"thread_id": completed_thread, "continuation_run_id": event.run_id}
            if not isinstance(event, (ContinuationCreated, ContinuationResumed)):
                continue
            if event.continuation_kind != "thread_resume":
                continue
            ref = getattr(event, "ref", None)
            thread_id = str(ref.get("thread_id") or "").strip() if isinstance(ref, dict) else ""
            if not thread_id:
                thread_id = str(event.source.metadata.get("thread_id") or "").strip()
            if thread_id:
                predecessor_id = event.run_id
                if include_run:
                    # Native thread continuation facts need not repeat on every
                    # turn. Use its latest completed Run, not the Run that first
                    # announced this thread, or a third turn can lose turn two.
                    for terminal in reversed(events):
                        if (isinstance(terminal, RunCompleted)
                                and terminal.source.framework == "codex"
                                and terminal.source.metadata.get("thread_id") == thread_id):
                            predecessor_id = terminal.run_id
                            break
                return {"thread_id": thread_id, **(
                    {"continuation_run_id": predecessor_id} if include_run else {}
                )}
        return {}

    def _start_stream(
        self,
        execution: ActiveExecution,
        run: RunRecord,
        guard: ActivationWriteGuard,
    ) -> ActiveExecution:
        """Start one non-blocking stream task owned by the current activation."""

        current = self._executions.get(execution.durable_run_id)
        if current is not None and current.stream_task is not None:
            if not current.stream_task.done():
                return current
            execution = replace(current, stream_task=None, stream_guard=None)
        task = asyncio.create_task(self._consume_stream(execution, run, guard))
        updated = replace(execution, stream_task=task, stream_guard=guard)
        self._executions[updated.durable_run_id] = updated
        task.add_done_callback(
            lambda done, run_id=updated.durable_run_id: self._observe_stream_task(run_id, done)
        )
        return updated

    def _observe_stream_task(self, run_id: str, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:  # pragma: no cover - defensive
            return
        if error is not None:
            logger.error(
                "background stream for run %s failed: %s: %s",
                run_id,
                type(error).__name__,
                error,
            )
            # A background stream has already left the Inbox claim path.  Do
            # not turn its failure into an invisible hung UI; keep the full
            # traceback in workload logs while recovery turns the durable run
            # into a terminal fact.
            logger.exception(
                "agent-kernel runtime stream failed for durable run %s (details=%s)",
                run_id,
                getattr(error, "details", {}),
                exc_info=error,
            )
            # The failure happened after the Inbox claim had already been
            # acknowledged, so no foreground owner remains to close this
            # adapter.  Leaving it in the live table leaks provider processes
            # (notably one Codex app-server per failed turn) and makes later
            # sessions stall behind stale transports.  Preserve the durable
            # open Run for recovery, but release this failed process-local
            # attachment immediately.
            execution = self._executions.get(run_id)
            if execution is not None and execution.stream_task is task:
                if execution.stream_guard is not None:
                    self._background_stream_errors[run_id] = (execution.stream_guard, error)
                self._executions.pop(run_id, None)
                cleanup = asyncio.create_task(
                    self._close_failed_execution(execution),
                    name=f"kernel-stream-cleanup:{run_id}",
                )
                cleanup.add_done_callback(self._observe_cleanup_task)

    async def _close_failed_execution(self, execution: ActiveExecution) -> None:
        try:
            await execution.adapter.close(execution.handle)
        except Exception:  # noqa: BLE001
            logger.exception(
                "failed to close adapter after runtime stream error for durable run %s",
                execution.durable_run_id,
            )

    @staticmethod
    def _observe_cleanup_task(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except asyncio.CancelledError:  # pragma: no cover - defensive
            return

    async def _consume_stream(
        self,
        execution: ActiveExecution,
        run: RunRecord,
        guard: ActivationWriteGuard,
    ) -> None:
        if self._session_events is None:
            return await self._consume_owned_stream(execution, run, guard)
        from ksadk.events.run_authority import RunEventAuthority, bind_run_event_authority
        from ksadk.sessions import bind_session_service

        authority = RunEventAuthority(
            self._session_events, session_id=run.session_id, run_id=run.run_id, guard=guard,
            interaction_ledger=self._store, tenant_id=str(run.metadata.get("tenant_id") or ""),
            agent_instance_id=run.agent_instance_id,
            managed_a2a_bootstraps=execution.managed_a2a_bootstraps,
        )
        session_scope = (
            bind_session_service(self._session_service)
            if self._session_service is not None
            else nullcontext()
        )
        with bind_run_event_authority(authority), session_scope:
            return await self._consume_owned_stream(execution, run, guard)

    async def _consume_owned_stream(
        self,
        execution: ActiveExecution,
        run: RunRecord,
        guard: ActivationWriteGuard,
    ) -> None:
        """消费 run 的事件流并把每个事实落为 family=runtime/v2 事件。

        事件 run_id 统一改写为 durable run_id（adapter 私有 run_id 通过
        RunRecord.metadata.runtime_run_id 记录映射）。stream 自然结束后
        才把 run 收口为 COMPLETED；任何异常原样上抛。
        """

        from ksadk.events.canonical import (
            InteractionRequested,
            InteractionResolved,
            RunCompleted,
            RunInterrupted,
            SourceRef,
        )

        runtime_store = None
        recovery_reconciler = None
        if self._session_events is not None:
            # 延迟导入：ksadk.events 反向依赖 kernel.contracts，避免模块环。
            from ksadk.events.canonical_store import RuntimeEventStore

            runtime_store = RuntimeEventStore(self._session_events, session_id=run.session_id)
            if execution.recovering:
                from ksadk.kernel.recovery_projection import RecoveryEventReconciler

                recovery_reconciler = RecoveryEventReconciler(
                    await runtime_store.list(run.session_id, run_id=run.run_id)
                )
        current_run = run
        terminal_state: RunState | None = None
        last_source: SourceRef | None = None
        async for event in execution.adapter.stream(execution.handle):
            if isinstance(event, InteractionRequested):
                current_run = await self._record_interaction_request(
                    execution, current_run, event, guard
                )
                # The ledger's interaction/v1 SessionEvent is the single fact
                # for this request.  Do not append a second runtime/v2 copy.
                continue
            if isinstance(event, InteractionResolved):
                # The submitted command's ledger transition is first-wins and
                # already emitted interaction.resolved; ignore framework echo.
                continue
            if (
                isinstance(event, RunInterrupted)
                and event.interaction_id
                and current_run.state is RunState.WAITING
            ):
                # Framework adapters emit this after InteractionRequested to
                # describe a temporarily blocked native turn.  The durable
                # Kernel state for that condition is WAITING and the
                # Interaction/v1 ledger is its authority.  Treating the
                # companion run.interrupted event as a terminal fact closes the
                # process-local adapter before a human can answer, so the later
                # SubmitInteraction receipt can never resolve.  Do not publish
                # a contradictory terminal runtime event; preserve the live
                # execution until InteractionResolved resumes the same stream.
                continue
            if runtime_store is None:
                continue
            if event.run_id != current_run.run_id:
                update: dict = {"run_id": run.run_id}
                if getattr(event, "scope_id", None) == f"run:{execution.handle.run_id}":
                    update["scope_id"] = f"run:{run.run_id}"
                event = event.model_copy(update=update)
            last_source = event.source
            events_to_append = (
                recovery_reconciler.accept(event) if recovery_reconciler is not None else (event,)
            )
            for candidate in events_to_append:
                await runtime_store.append(candidate, guard=guard)
            remember = getattr(execution.adapter, "remember_managed_checkpoint", None)
            if callable(remember):
                remember(execution.handle, event)
            terminal_state = {
                "run.completed": RunState.COMPLETED,
                "run.failed": RunState.FAILED,
                "run.canceled": RunState.CANCELLED,
                "run.interrupted": RunState.INTERRUPTED,
            }.get(event.event_type)
            if terminal_state is not None:
                # App-server style providers keep their notification channel
                # open across turns.  A canonical terminal RuntimeEvent closes
                # this run even when the transport itself does not produce
                # EOF; waiting for EOF here leaves the durable run RUNNING and
                # every later FIFO command queued forever.
                break

        if recovery_reconciler is not None and runtime_store is not None:
            for candidate in recovery_reconciler.flush():
                await runtime_store.append(candidate, guard=guard)

        # ``submit_interaction`` may resolve a live provider while this task is
        # blocked in the framework stream.  It transitions the durable run
        # WAITING -> RUNNING, but ``current_run`` above is deliberately a
        # local snapshot used to preserve event ordering.  Refresh it before
        # deciding whether natural stream exhaustion can settle the run;
        # otherwise a Codex approval continuation finishes successfully but
        # remains permanently WAITING because this task still sees its stale
        # pre-response snapshot.
        latest_run = await self._store.find_active_run(run.agent_instance_id, run.session_id)
        if latest_run is not None and latest_run.run_id == current_run.run_id:
            current_run = latest_run

        # ``RuntimeAdapter`` is expected to emit a terminal RuntimeEvent, but
        # a number of framework streams naturally exhaust after their last
        # progress/item event.  The Kernel is the lifecycle owner, so it must
        # publish a fenced ``run.completed`` fact before recording COMPLETED.
        # Otherwise foreground callers and Studio SSE wait forever even though
        # the durable RunRecord says completion succeeded.
        if terminal_state is None and current_run.state is not RunState.WAITING:
            terminal_state = RunState.COMPLETED
            if runtime_store is not None:
                source = last_source or SourceRef(
                    framework="ksadk",
                    native_run_id=execution.runtime_run_id,
                )
                await runtime_store.append(
                    RunCompleted(
                        schema_version=2,
                        event_id=f"{current_run.run_id}:kernel-completed",
                        seq=0,
                        timestamp=datetime.now(timezone.utc).timestamp(),
                        run_id=current_run.run_id,
                        scope_id=f"run:{current_run.run_id}",
                        source=source,
                        status="completed",
                        output_refs=(),
                    ),
                    guard=guard,
                )
        if terminal_state is not None:
            # Managed A2A waiters can end on their own deadline without a
            # SubmitInteraction command. A terminal run must not leave their
            # durable approval/input prompts pending in replay. Native provider
            # interactions retain their existing lifecycle.
            now = datetime.now(timezone.utc)
            for record in await self._store.list_pending_interactions(
                str(current_run.metadata.get("tenant_id") or ""), current_run.session_id
            ):
                if (
                    record.run_id != current_run.run_id
                    or record.agent_instance_id != current_run.agent_instance_id
                    or record.provider_id != "managed-a2a"
                ):
                    continue
                expired = record.expires_at is not None and datetime.fromisoformat(record.expires_at) <= now
                settle = self._store.expire if expired else self._store.cancel
                await settle(record.interaction_id, record.revision, guard=guard)
                waiter = execution.managed_interaction_waiters.pop(record.interaction_id, None)
                if waiter is not None:
                    waiter.close()
            current_run = await self._store.save_run_transition(
                current_run.model_copy(update={"state": terminal_state}),
                expected_fence=guard.fencing_token,
            )
        current = self._executions.get(execution.durable_run_id)
        if (
            terminal_state is not None
            and current is not None
            and current.handle == execution.handle
        ):
            self._executions.pop(execution.durable_run_id, None)
        if terminal_state is not None:
            # Each enqueue owns the adapter instance created in ``_start_run``.
            # Once its stream is terminal there is no live interaction left to
            # preserve, so release the provider transport as part of that same
            # lifecycle.  Codex otherwise leaves one app-server child alive per
            # turn; a later process trying to resume the persisted thread can
            # then block behind the stale owner indefinitely.
            try:
                await execution.adapter.close(execution.handle)
            except Exception:  # noqa: BLE001
                # The durable terminal event and RunRecord are already fenced
                # and committed.  A transport cleanup failure is observable but
                # must not rewrite a successful run into a retryable command.
                logger.exception(
                    "failed to close terminal runtime transport for durable run %s",
                    execution.durable_run_id,
                )

    # 控制命令必须作用于 active Run 且本进程持有 live execution。
    def _replace_handle(self, execution: ActiveExecution, handle: RunHandle) -> ActiveExecution:
        if handle is execution.handle or handle == execution.handle:
            return execution
        if execution.stream_task is not None and not execution.stream_task.done():
            execution.stream_task.cancel()
        updated = replace(
            execution,
            handle=handle,
            runtime_run_id=handle.run_id,
            stream_task=None,
            stream_guard=None,
        )
        self._executions[execution.durable_run_id] = updated
        return updated

    async def _transition_run(self, run: RunRecord, state: RunState, fence: int) -> RunRecord:
        return await self._store.save_run_transition(
            run.model_copy(update={"state": state}), expected_fence=fence
        )


__all__ = ["ActiveExecution", "AgentKernelWorker", "WorkResult", "WorkOutcome"]

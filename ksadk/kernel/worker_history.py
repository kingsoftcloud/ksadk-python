"""Recover Harness turn input from the Kernel's durable conversation facts."""

from __future__ import annotations

from ksadk.conversations.context import project_model_messages
from ksadk.events.canonical import RunCompleted
from ksadk.events.canonical_store import RuntimeEventStore, runtime_event_to_session_event
from ksadk.kernel.contracts import AgentControlCommand
from ksadk.kernel.state import RunState
from ksadk.kernel.store import AgentKernelStore
from ksadk.sessions.base import SessionEvent


async def harness_conversation_messages(
    command: AgentControlCommand,
    *,
    store: AgentKernelStore,
    session_events: object,
) -> list[dict[str, str]]:
    """Project successful prior turns; never use an adapter's process cache.

    Harness checkpoints belong to individual Runs. The Kernel retains the
    admitted user input in its Inbox and the reply in canonical RuntimeEvents;
    join those facts by Run.command_id, within this instance, tenant and Session.
    Reuse conversation projection so tool/approval and multiple-message semantics
    stay aligned with the ordinary conversation preparation path.
    """

    inbox = {
        str(message.command.command_id): message.command
        for message in await store.list_messages(command.agent_instance_id, command.session_id)
        if message.command is not None
        and message.command.command_type == "enqueue"
        and message.command.tenant_id == command.tenant_id
        and message.command.session_id == command.session_id
        and message.command.agent_instance_id == command.agent_instance_id
        and message.command.command_id != command.command_id
    }
    events = await RuntimeEventStore(session_events).list(command.session_id)
    by_run: dict[str, list[SessionEvent]] = {}
    projected_runs: set[str] = set()
    history: list[dict[str, str]] = []
    for event in events:
        rows = by_run.setdefault(event.run_id, [])
        rows.append(runtime_event_to_session_event(command.session_id, event))
        if not isinstance(event, RunCompleted) or event.run_id in projected_runs:
            continue
        run = await store.load_run(event.run_id)
        if (
            run is None
            or run.state is not RunState.COMPLETED
            or run.agent_instance_id != command.agent_instance_id
            or run.session_id != command.session_id
        ):
            continue
        previous = inbox.get(str(run.metadata.get("command_id") or ""))
        if previous is None:
            continue
        # This row is an in-memory projection of the already-admitted Inbox
        # command, not another persistent copy of the user's message.
        user = SessionEvent(
            id=str(previous.command_id),
            session_id=command.session_id,
            invocation_id=run.run_id,
            event_type="user_message",
            content={"parts": [{"text": str(previous.payload.get("content") or "")}]},
            seq_id=max(1, rows[0].seq_id - 1),
        )
        history.extend(project_model_messages([user, *rows], assistant_role="assistant"))
        projected_runs.add(event.run_id)
    history.append({"role": "user", "content": str(command.payload.get("content") or "")})
    return history

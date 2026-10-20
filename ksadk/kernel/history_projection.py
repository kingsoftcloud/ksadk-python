"""Read-only user-message projection of admitted Kernel Inbox commands."""

from __future__ import annotations

from typing import Any

from ksadk.conversations.message_projection import project_session_messages


def _input_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_input_text(part) for part in value)
    if isinstance(value, dict):
        for key in ("text", "content", "parts"):
            if key in value:
                return _input_text(value[key])
    return ""


def _accepted_input(event: dict[str, Any], session_id: str) -> dict[str, Any] | None:
    content = event.get("Content")
    envelope = content.get("session_event") if isinstance(content, dict) else None
    payload = envelope.get("payload") if isinstance(envelope, dict) else None
    if (event.get("SessionId") == session_id and isinstance(payload, dict)
            and envelope.get("family") == "control"
            and envelope.get("event_type") == "control.command_accepted"
            and payload.get("command_type") == "enqueue"):
        return payload
    return None


async def project_inbox_inputs(
    events: list[dict[str, Any]], *, runtime: Any, session_id: str,
    tenant_id: str | None = None,
    request_cache: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Use the acceptance fact's identity/cursor; never write another event.

    The caller has already authorized the Session. Inbox lookups are additionally
    bound to the runtime's instance and tenant, and to the admitted command ID.
    Native user items remain the message source when the provider supplies them.
    """
    if runtime is None or not any(_accepted_input(event, session_id) for event in events):
        return events
    existing_users = {
        message.get("InvocationId") for message in project_session_messages(events)
        if message.get("Role") == "user" and message.get("InvocationId")
    }
    cache = request_cache if request_cache is not None else {}
    runs = cache.setdefault("runs", {})
    messages = cache.setdefault("messages", {})
    effective_tenant = tenant_id or runtime.config.tenant_id
    # Kernel table tenant_id is the configured storage partition, not the
    # principal carried by the admitted command. Read only this store's
    # partition; validate the command principal below before exposing input.
    storage_tenant = getattr(runtime.kernel_store, "tenant_id", effective_tenant)
    run_ids = {str(event.get("InvocationId") or "") for event in events} - {""}
    missing_runs = sorted(run_ids - runs.keys())
    batch_runs = getattr(runtime.kernel_store, "load_runs_for_history", None)
    if callable(batch_runs) and missing_runs:
        runs.update(await batch_runs(
            missing_runs, tenant_id=storage_tenant,
            agent_instance_id=runtime.config.agent_instance_id, session_id=session_id,
        ))
    else:
        for run_id in missing_runs:
            runs[run_id] = await runtime.kernel_store.load_run(run_id)
    for run_id in missing_runs:
        runs.setdefault(run_id, None)
    accepted = [payload for event in events
                if (payload := _accepted_input(event, session_id)) is not None]
    message_ids = {str(payload.get("message_id") or "") for payload in accepted} - {""}
    missing_messages = sorted(message_ids - messages.keys())
    batch_messages = getattr(runtime.kernel_store, "load_messages_for_history", None)
    if callable(batch_messages) and missing_messages:
        messages.update(await batch_messages(
            missing_messages, tenant_id=storage_tenant,
            agent_instance_id=runtime.config.agent_instance_id, session_id=session_id,
        ))
    else:
        for message_id in missing_messages:
            messages[message_id] = await runtime.kernel_store.load_message(message_id)
    for message_id in missing_messages:
        messages.setdefault(message_id, None)
    run_by_command: dict[str, str] = {}
    for run_id in run_ids:
        run = runs.get(run_id)
        run_tenant = getattr(run, "tenant_id", None)
        store_tenant = getattr(runtime.kernel_store, "tenant_id", None)
        if run_tenant is not None and run_tenant != effective_tenant:
            continue
        if (not callable(batch_runs) and store_tenant is not None
                and store_tenant != effective_tenant):
            continue
        if (run is not None and run.session_id == session_id
                and run.agent_instance_id == runtime.config.agent_instance_id):
            run_by_command[str(run.metadata.get("command_id") or "")] = run_id

    projected = []
    for event in events:
        payload = _accepted_input(event, session_id)
        if payload is None:
            projected.append(event)
            continue
        message = messages.get(str(payload.get("message_id") or ""))
        command = message.command if message is not None else None
        if (command is None or command.command_type != "enqueue"
                or command.session_id != session_id
                or command.agent_instance_id != runtime.config.agent_instance_id
                or command.tenant_id != effective_tenant
                or str(command.command_id) != str(payload.get("command_id") or "")):
            projected.append(event)
            continue
        run_id = run_by_command.get(str(command.command_id))
        if run_id in existing_users:
            projected.append(event)
            continue
        projected.append({
            **event, "EventType": "user_message", "Author": "user",
            "InvocationId": run_id,
            "Content": {"parts": [{"text": _input_text(command.payload.get("content"))}]},
        })
    return projected


async def complete_history_input_boundary(
    events, *, service, runtime, session_id, budget, after_seq_id=None, request_cache=None
):
    """Complete the first Kernel command by identity, across unassigned control facts.

    Run IDs alone cannot define the input boundary: claim/completion facts have
    no run_id, and acceptance precedes run creation. Do not guess associations
    from neighboring events; use the persisted run's command_id instead.
    """
    if runtime is None or not events:
        return events
    run_id = next((str(e.invocation_id) for e in events if e.invocation_id), "")
    if not run_id:
        return events
    store = runtime.kernel_store
    run_cache = (request_cache if request_cache is not None else {}).setdefault("runs", {})
    batch = getattr(store, "load_runs_for_history", None)
    if run_id in run_cache:
        run = run_cache[run_id]
    elif callable(batch):
        runs = await batch(
            [run_id],
            tenant_id=getattr(store, "tenant_id", runtime.config.tenant_id),
            agent_instance_id=runtime.config.agent_instance_id,
            session_id=session_id,
        )
        run = runs.get(run_id)
    else:
        run = await store.load_run(run_id)
    run_cache[run_id] = run
    if (
        run is None
        or run.session_id != session_id
        or run.agent_instance_id != runtime.config.agent_instance_id
    ):
        return events
    command_id = str(run.metadata.get("command_id") or "")
    if not command_id:
        return events

    def is_acceptance(event):
        content = event.content
        envelope = content.get("session_event") if isinstance(content, dict) else None
        payload = envelope.get("payload") if isinstance(envelope, dict) else None
        return (
            event.session_id == session_id
            and isinstance(payload, dict)
            and envelope.get("family") == "control"
            and envelope.get("event_type") == "control.command_accepted"
            and payload.get("command_type") == "enqueue"
            and str(payload.get("command_id") or "") == command_id
        )

    if any(is_acceptance(e) for e in events):
        return events
    prefix = []
    cursor = events[0].seq_id
    while cursor and cursor > (after_seq_id or 0) + 1:
        page = await service.get_events(
            session_id, limit=64, before_seq_id=cursor, after_seq_id=after_seq_id
        )
        if not page:
            break
        budget.account(page)
        found = next((i for i, e in enumerate(page) if is_acceptance(e)), None)
        prefix[0:0] = page[found:] if found is not None else page
        if found is not None:
            break
        cursor = page[0].seq_id
    return [*prefix, *events]

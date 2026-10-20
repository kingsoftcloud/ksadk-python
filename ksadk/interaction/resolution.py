"""Fully scoped lookup of the committed first-wins interaction fact."""


def matches_resolution(event, record):
    return (
        event.family == "interaction"
        and event.family_version == 1
        and event.event_type == "interaction.resolved"
        and event.run_id == record.run_id
        and all(
            event.payload.get(key) == getattr(record, key)
            for key in (
                "interaction_id",
                "tenant_id",
                "agent_instance_id",
                "session_id",
                "run_id",
                "kind",
                "revision",
            )
        )
    )


async def find_resolution(events, record):
    lookup = getattr(events, "find_interaction_resolution", None)
    if callable(lookup):
        event = await lookup(record)
        if event is not None and not matches_resolution(event, record):
            raise ValueError("interaction resolution lookup returned another scope")
        return event
    cursor = 0
    while True:
        page = await events.read(record.session_id, cursor, 256)
        if not page:
            return None
        for event in page:
            if matches_resolution(event, record):
                return event
        next_cursor = max(event.seq for event in page)
        if next_cursor <= cursor:
            raise RuntimeError("interaction resolution cursor did not advance")
        cursor = next_cursor

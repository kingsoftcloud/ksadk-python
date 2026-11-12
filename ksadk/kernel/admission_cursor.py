"""Rebase legacy Inbox ordinals onto their original committed Session cursor."""

from ksadk.sessions._local_tables import KSADK_EVENTS_TABLE
from ksadk.sessions.local_service import LocalSessionService


async def migrate_sqlite_admission_cursors(connection, path, events):
    service = getattr(events, "session_service", None)
    if not isinstance(service, LocalSessionService) or service.db_path != path:
        return
    # Existing events remain immutable. Only the private Inbox ordering/receipt
    # reference changes; the relative ordering of admitted commands is retained.
    await connection.execute(f"""
        UPDATE kernel_inbox AS i SET accepted_seq=e.seq_id
        FROM {KSADK_EVENTS_TABLE} AS e
        WHERE e.session_id=i.session_id AND e.event_type='control.command_accepted'
          AND json_extract(e.content_json,'$.session_event.payload.message_id')=i.message_id
          AND i.accepted_seq<>e.seq_id
    """)

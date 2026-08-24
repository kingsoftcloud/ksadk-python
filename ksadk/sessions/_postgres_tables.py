"""Postgres session 存储的表/视图常量（纯移动自 postgres_service，行为不变）。"""

KSADK_PG_SESSIONS_TABLE = "ksadk_sessions"
KSADK_PG_EVENTS_TABLE = "ksadk_events"
KSADK_PG_STATES_TABLE = "ksadk_states"
PG_READABLE_EVENTS_VIEW = "ksadk_session_events_readable"
_PG_SCHEMA_ADVISORY_LOCK_KEY = 0x4B5341444B53444B

__all__ = [
    "KSADK_PG_EVENTS_TABLE",
    "KSADK_PG_SESSIONS_TABLE",
    "KSADK_PG_STATES_TABLE",
    "PG_READABLE_EVENTS_VIEW",
    "_PG_SCHEMA_ADVISORY_LOCK_KEY",
]

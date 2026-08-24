"""Local SQLite session 存储的表名常量（纯移动自 local_service，行为不变）。"""

KSADK_SESSIONS_TABLE = "ksadk_sessions"
KSADK_EVENTS_TABLE = "ksadk_events"
KSADK_STATES_TABLE = "ksadk_states"

LEGACY_SESSIONS_TABLE = "sessions"
LEGACY_EVENTS_TABLE = "events"
LEGACY_STATES_TABLE = "states"

DEFAULT_SESSION_DB_NAME = "sessions.sqlite"

__all__ = [
    "DEFAULT_SESSION_DB_NAME",
    "KSADK_EVENTS_TABLE",
    "KSADK_SESSIONS_TABLE",
    "KSADK_STATES_TABLE",
    "LEGACY_EVENTS_TABLE",
    "LEGACY_SESSIONS_TABLE",
    "LEGACY_STATES_TABLE",
]

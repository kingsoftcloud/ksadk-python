"""PostgresSessionService 的 schema 初始化/迁移 DDL（纯移动自 postgres_service，行为不变）。

以 mixin 形式被 :class:`PostgresSessionService` 继承，依赖宿主提供
``_schema_ready`` / ``_schema_lock`` / ``_ensure_pool`` / ``_pool``。
"""

from __future__ import annotations

import logging
from typing import Any

from ksadk.sessions._postgres_tables import (
    _PG_SCHEMA_ADVISORY_LOCK_KEY,
    KSADK_PG_EVENTS_TABLE,
    KSADK_PG_SESSIONS_TABLE,
    KSADK_PG_STATES_TABLE,
    PG_READABLE_EVENTS_VIEW,
)

logger = logging.getLogger(__name__)


class _PostgresSchemaMixin:
    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            await self._ensure_pool()
            async with self._pool.acquire() as connection:
                # A new service instance normally points at an already-current
                # shared schema.  Avoid taking any DDL lock in that hot path;
                # otherwise a cold initializer can deadlock with a ready pod's
                # concurrent event INSERT.
                if await self._core_schema_is_current(connection):
                    self._schema_ready = True
                    return

                migrated = False
                async with connection.transaction():
                    # Instance-local asyncio locks cannot coordinate pods.  The
                    # transaction-scoped database lock serializes true schema
                    # creation/migration, then the second shape check lets a
                    # waiting initializer skip duplicate DDL.
                    await connection.execute(
                        "SELECT pg_advisory_xact_lock($1)",
                        _PG_SCHEMA_ADVISORY_LOCK_KEY,
                    )
                    if not await self._core_schema_is_current(connection):
                        await self._create_core_schema(connection)
                        if not await self._core_schema_is_current(connection):
                            raise RuntimeError(
                                "Postgres session schema migration did not produce "
                                "the required core shape"
                            )
                        migrated = True

                # The readable view is optional.  Create it only for the one
                # initializer that changed core schema, after the core DDL has
                # committed so a view permission error cannot roll it back.
                if migrated:
                    try:
                        await connection.execute(f"""
                        CREATE OR REPLACE VIEW {PG_READABLE_EVENTS_VIEW} AS
                        SELECT
                            event_row.namespace,
                            event_row.tenant_id,
                            event_row.workspace_id,
                            session_row.agent_id,
                            session_row.user_id,
                            session_row.title AS session_title,
                            event_row.session_id,
                            event_row.seq_id,
                            event_row.id AS event_id,
                            event_row.invocation_id,
                            event_row.author,
                            event_row.event_type,
                            CASE
                                WHEN event_row.event_type = 'user_message' THEN 'user'
                                WHEN event_row.event_type IN (
                                    'assistant_message', 'reasoning', 'tool_call'
                                ) THEN 'assistant'
                                WHEN event_row.event_type = 'tool_result' THEN 'tool'
                                ELSE NULL
                            END AS message_role,
                            COALESCE(
                                NULLIF(event_row.content_json #>> '{{parts,0,text}}', ''),
                                NULLIF(event_row.content_json ->> 'text', ''),
                                NULLIF(event_row.metadata_json ->> 'reasoning', ''),
                                NULLIF(event_row.metadata_json ->> 'tool_output', '')
                            ) AS message_text,
                            event_row.metadata_json ->> 'tool_name' AS tool_name,
                            CASE
                                WHEN event_row.event_type = 'run_status' THEN COALESCE(
                                    event_row.content_json ->> 'status',
                                    event_row.metadata_json ->> 'status'
                                )
                                ELSE NULL
                            END AS lifecycle_status,
                            to_timestamp(event_row.timestamp) AS created_at,
                            event_row.content_json,
                            event_row.state_delta_json,
                            event_row.metadata_json
                        FROM {KSADK_PG_EVENTS_TABLE} AS event_row
                        JOIN {KSADK_PG_SESSIONS_TABLE} AS session_row
                          ON session_row.namespace = event_row.namespace
                         AND session_row.id = event_row.session_id;
                            """)
                    except Exception as exc:
                        logger.warning("Postgres readable session view unavailable: %s", exc)
            self._schema_ready = True

    @staticmethod
    async def _core_schema_is_current(connection: Any) -> bool:
        return bool(await connection.fetchval(f"""
                    SELECT
                        to_regclass('{KSADK_PG_SESSIONS_TABLE}') IS NOT NULL
                        AND to_regclass('{KSADK_PG_EVENTS_TABLE}') IS NOT NULL
                        AND to_regclass('{KSADK_PG_STATES_TABLE}') IS NOT NULL
                        AND to_regclass('idx_ksadk_pg_events_session_seq') IS NOT NULL
                        AND to_regclass('idx_ksadk_pg_events_session_invocation_seq') IS NOT NULL
                        AND to_regclass('idx_ksadk_pg_events_session_ts') IS NOT NULL
                        AND to_regclass('idx_ksadk_pg_sessions_agent_updated') IS NOT NULL
                        AND NOT EXISTS (
                            SELECT 1
                            FROM (
                                VALUES
                                    ('{KSADK_PG_SESSIONS_TABLE}', 'namespace'),
                                    ('{KSADK_PG_SESSIONS_TABLE}', 'tenant_id'),
                                    ('{KSADK_PG_SESSIONS_TABLE}', 'workspace_id'),
                                    ('{KSADK_PG_SESSIONS_TABLE}', 'id'),
                                    ('{KSADK_PG_SESSIONS_TABLE}', 'agent_id'),
                                    ('{KSADK_PG_SESSIONS_TABLE}', 'user_id'),
                                    ('{KSADK_PG_SESSIONS_TABLE}', 'title'),
                                    ('{KSADK_PG_SESSIONS_TABLE}', 'title_source'),
                                    ('{KSADK_PG_SESSIONS_TABLE}', 'summary'),
                                    ('{KSADK_PG_SESSIONS_TABLE}', 'first_prompt'),
                                    ('{KSADK_PG_SESSIONS_TABLE}', 'last_prompt'),
                                    ('{KSADK_PG_SESSIONS_TABLE}', 'state_json'),
                                    ('{KSADK_PG_SESSIONS_TABLE}', 'created_at'),
                                    ('{KSADK_PG_SESSIONS_TABLE}', 'updated_at'),
                                    ('{KSADK_PG_SESSIONS_TABLE}', 'version'),
                                    ('{KSADK_PG_EVENTS_TABLE}', 'namespace'),
                                    ('{KSADK_PG_EVENTS_TABLE}', 'tenant_id'),
                                    ('{KSADK_PG_EVENTS_TABLE}', 'workspace_id'),
                                    ('{KSADK_PG_EVENTS_TABLE}', 'id'),
                                    ('{KSADK_PG_EVENTS_TABLE}', 'session_id'),
                                    ('{KSADK_PG_EVENTS_TABLE}', 'author'),
                                    ('{KSADK_PG_EVENTS_TABLE}', 'event_type'),
                                    ('{KSADK_PG_EVENTS_TABLE}', 'content_json'),
                                    ('{KSADK_PG_EVENTS_TABLE}', 'timestamp'),
                                    ('{KSADK_PG_EVENTS_TABLE}', 'state_delta_json'),
                                    ('{KSADK_PG_EVENTS_TABLE}', 'seq_id'),
                                    ('{KSADK_PG_EVENTS_TABLE}', 'invocation_id'),
                                    ('{KSADK_PG_EVENTS_TABLE}', 'metadata_json'),
                                    ('{KSADK_PG_STATES_TABLE}', 'namespace'),
                                    ('{KSADK_PG_STATES_TABLE}', 'tenant_id'),
                                    ('{KSADK_PG_STATES_TABLE}', 'workspace_id'),
                                    ('{KSADK_PG_STATES_TABLE}', 'scope'),
                                    ('{KSADK_PG_STATES_TABLE}', 'agent_id'),
                                    ('{KSADK_PG_STATES_TABLE}', 'user_id'),
                                    ('{KSADK_PG_STATES_TABLE}', 'session_id'),
                                    ('{KSADK_PG_STATES_TABLE}', 'state_json'),
                                    ('{KSADK_PG_STATES_TABLE}', 'version'),
                                    ('{KSADK_PG_STATES_TABLE}', 'updated_at')
                            ) AS required(table_name, column_name)
                            WHERE NOT EXISTS (
                                SELECT 1
                                FROM pg_attribute AS attribute_row
                                WHERE attribute_row.attrelid = to_regclass(required.table_name)
                                  AND attribute_row.attname = required.column_name
                                  AND NOT attribute_row.attisdropped
                            )
                        )
                        AND EXISTS (
                            SELECT 1
                            FROM pg_constraint AS constraint_row
                            WHERE constraint_row.conrelid = to_regclass('{KSADK_PG_SESSIONS_TABLE}')
                              AND constraint_row.contype = 'p'
                              AND pg_get_constraintdef(constraint_row.oid)
                                  = 'PRIMARY KEY (namespace, id)'
                        )
                        AND EXISTS (
                            SELECT 1
                            FROM pg_constraint AS constraint_row
                            WHERE constraint_row.conrelid = to_regclass('{KSADK_PG_EVENTS_TABLE}')
                              AND constraint_row.contype = 'p'
                              AND pg_get_constraintdef(constraint_row.oid)
                                  = 'PRIMARY KEY (namespace, id)'
                        )
                        AND EXISTS (
                            SELECT 1
                            FROM pg_constraint AS constraint_row
                            WHERE constraint_row.conrelid = to_regclass('{KSADK_PG_STATES_TABLE}')
                              AND constraint_row.contype = 'p'
                              AND pg_get_constraintdef(constraint_row.oid)
                                  = 'PRIMARY KEY (namespace, scope, agent_id, user_id, session_id)'
                        )
                        AND EXISTS (
                            SELECT 1
                            FROM pg_constraint AS constraint_row
                            WHERE constraint_row.conrelid = to_regclass('{KSADK_PG_EVENTS_TABLE}')
                              AND constraint_row.contype = 'u'
                              AND pg_get_constraintdef(constraint_row.oid)
                                  = 'UNIQUE (namespace, session_id, seq_id)'
                        )
                        AND EXISTS (
                            SELECT 1
                            FROM pg_constraint AS constraint_row
                            WHERE constraint_row.conrelid = to_regclass('{KSADK_PG_EVENTS_TABLE}')
                              AND constraint_row.contype = 'f'
                              AND pg_get_constraintdef(constraint_row.oid)
                                  = concat(
                                      'FOREIGN KEY (namespace, session_id) REFERENCES ',
                                      '{KSADK_PG_SESSIONS_TABLE}(namespace, id) ON DELETE CASCADE'
                                  )
                        )
                    """))

    @staticmethod
    async def _create_core_schema(connection: Any) -> None:
        await connection.execute(f"""
                CREATE TABLE IF NOT EXISTS {KSADK_PG_SESSIONS_TABLE} (
                    namespace TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    workspace_id TEXT NOT NULL DEFAULT 'default',
                    id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    title_source TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    first_prompt TEXT NOT NULL DEFAULT '',
                    last_prompt TEXT NOT NULL DEFAULT '',
                    state_json JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    created_at DOUBLE PRECISION NOT NULL,
                    updated_at DOUBLE PRECISION NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (namespace, id)
                );

                CREATE TABLE IF NOT EXISTS {KSADK_PG_EVENTS_TABLE} (
                    namespace TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    workspace_id TEXT NOT NULL DEFAULT 'default',
                    id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    author TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    content_json JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    timestamp DOUBLE PRECISION NOT NULL,
                    state_delta_json JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    seq_id INTEGER NOT NULL,
                    invocation_id TEXT,
                    metadata_json JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    PRIMARY KEY (namespace, id),
                    UNIQUE (namespace, session_id, seq_id),
                    FOREIGN KEY (namespace, session_id)
                        REFERENCES {KSADK_PG_SESSIONS_TABLE}(namespace, id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_ksadk_pg_events_session_seq
                ON {KSADK_PG_EVENTS_TABLE} (namespace, session_id, seq_id);

                CREATE INDEX IF NOT EXISTS idx_ksadk_pg_events_session_invocation_seq
                ON {KSADK_PG_EVENTS_TABLE} (namespace, session_id, invocation_id, seq_id);

                CREATE INDEX IF NOT EXISTS idx_ksadk_pg_events_session_ts
                ON {KSADK_PG_EVENTS_TABLE} (namespace, session_id, timestamp, id);

                CREATE INDEX IF NOT EXISTS idx_ksadk_pg_sessions_agent_updated
                ON {KSADK_PG_SESSIONS_TABLE} (namespace, agent_id, updated_at DESC, id);

                CREATE TABLE IF NOT EXISTS {KSADK_PG_STATES_TABLE} (
                    namespace TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    workspace_id TEXT NOT NULL DEFAULT 'default',
                    scope TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    user_id TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    state_json JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    version INTEGER NOT NULL DEFAULT 0,
                    updated_at DOUBLE PRECISION NOT NULL,
                    PRIMARY KEY (namespace, scope, agent_id, user_id, session_id)
                );

                ALTER TABLE {KSADK_PG_SESSIONS_TABLE}
                ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default';
                ALTER TABLE {KSADK_PG_SESSIONS_TABLE}
                ADD COLUMN IF NOT EXISTS workspace_id TEXT NOT NULL DEFAULT 'default';
                ALTER TABLE {KSADK_PG_EVENTS_TABLE}
                ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default';
                ALTER TABLE {KSADK_PG_EVENTS_TABLE}
                ADD COLUMN IF NOT EXISTS workspace_id TEXT NOT NULL DEFAULT 'default';
                ALTER TABLE {KSADK_PG_STATES_TABLE}
                ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default';
                ALTER TABLE {KSADK_PG_STATES_TABLE}
                ADD COLUMN IF NOT EXISTS workspace_id TEXT NOT NULL DEFAULT 'default';
                """)


__all__ = ["_PostgresSchemaMixin"]

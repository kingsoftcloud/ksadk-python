"""Repair legacy Kernel work without serializing data scans behind the DDL lock."""

from ksadk.sessions._postgres_tables import KSADK_PG_EVENTS_TABLE


# Each page follows the existing primary key. Apply the NULL/status filter
# *after* LIMIT so even a table of entirely terminal rows takes bounded steps.
_SOURCES = {
    "kernel_runs": ("run_id", "text", "state IN ('pending','running','paused','waiting')"),
    "kernel_inbox": ("message_id", "uuid", "true"),  # terminal receipts also retain their original cursor
    "kernel_interactions": ("interaction_id", "text", "status IN ('pending','resolving')"),
}
_SEED_PAGE = 4096
_REPAIR_PAGE = 32


async def _seed_legacy_page(conn):
    progress = await conn.fetchrow("""
        SELECT * FROM kernel_legacy_repair_progress ORDER BY work_table LIMIT 1 FOR UPDATE
    """)
    if progress is None:
        return False
    table = progress["work_table"]
    id_column, id_type, eligible = _SOURCES[table]
    if table == "kernel_interactions":
        key, upper, lower = "(tenant_id, interaction_id)", "($1, $2)", "($3, $4)"
        order, reverse = "tenant_id, interaction_id", "tenant_id DESC, interaction_id DESC"
        params = [progress["end_tenant"], progress["end_id"]]
        cursor = [progress["last_tenant"], progress["last_id"]]
    else:
        key, upper, lower = id_column, f"$1::text::{id_type}", f"$2::text::{id_type}"
        order, reverse = id_column, f"{id_column} DESC"
        params = [progress["end_id"]]
        cursor = [progress["last_id"]]
    predicate = f"{key} <= {upper}"
    if progress["last_id"] is not None:
        predicate += f" AND {key} > {lower}"
        params.extend(cursor)
    last = await conn.fetchrow(f"""
        WITH page AS (
            SELECT tenant_id, session_id, agent_instance_id, {id_column}, session_namespace IS NULL AND ({eligible}) AS legacy
            FROM {table} WHERE {predicate} ORDER BY {order} LIMIT {_SEED_PAGE}
        ), queued AS (
            INSERT INTO kernel_legacy_session_repairs(tenant_id, session_id, agent_instance_id)
            SELECT DISTINCT tenant_id, session_id, agent_instance_id FROM page WHERE legacy
            ORDER BY tenant_id, session_id, agent_instance_id ON CONFLICT DO NOTHING
        )
        SELECT tenant_id, {id_column}::text AS id FROM page ORDER BY {reverse} LIMIT 1
    """, *params)
    if last is None or (last["id"] == progress["end_id"] and
                        (table != "kernel_interactions" or last["tenant_id"] == progress["end_tenant"])):
        await conn.execute("DELETE FROM kernel_legacy_repair_progress WHERE work_table=$1", table)
    else:
        await conn.execute("""
            UPDATE kernel_legacy_repair_progress SET last_tenant=$2, last_id=$3 WHERE work_table=$1
        """, table, last["tenant_id"], last["id"])
    return True


async def repair_legacy_sessions(connect, tenant_id, namespace):
    async with connect() as conn:
        if not await conn.fetchval("""
            SELECT EXISTS (SELECT FROM kernel_legacy_repair_progress)
                OR EXISTS (SELECT FROM kernel_legacy_session_repairs)
        """) or not await conn.fetchval("SELECT to_regclass($1)", KSADK_PG_EVENTS_TABLE):
            return
    while True:
        async with connect(write=True) as conn, conn.transaction():
            if not await _seed_legacy_page(conn):
                break
    cursor = (None, None, None)
    while True:
        async with connect(write=True) as conn, conn.transaction():
            batch = await conn.fetch("""
                SELECT q.tenant_id, q.session_id, q.agent_instance_id, q.revision FROM kernel_legacy_session_repairs q
                WHERE (q.tenant_id=$1 OR EXISTS (
                    SELECT FROM ksadk_sessions s WHERE s.namespace=$2 AND s.id=q.session_id
                )) AND ($3::text IS NULL OR (q.tenant_id,q.session_id,q.agent_instance_id)>($3,$4,$5))
                ORDER BY q.tenant_id, q.session_id, q.agent_instance_id LIMIT $6
            """, tenant_id, namespace, *cursor, _REPAIR_PAGE)
            if not batch:
                return
            # Multiple namespaces can own the same public id. Enumerate their
            # parent rows once per batch; look up evidence by namespace AND id.
            scopes = await conn.fetch("""
                WITH RECURSIVE scopes(ns) AS (
                    SELECT min(namespace) FROM ksadk_sessions
                    UNION ALL SELECT (SELECT min(namespace) FROM ksadk_sessions WHERE namespace>scopes.ns)
                    FROM scopes WHERE ns IS NOT NULL
                )
                SELECT id, array_agg(namespace ORDER BY namespace) AS namespaces FROM ksadk_sessions
                WHERE namespace=ANY(ARRAY(SELECT ns FROM scopes WHERE ns IS NOT NULL))
                  AND id=ANY($1::text[]) GROUP BY id
            """, [row["session_id"] for row in batch])
            owners = {row["id"]: row["namespaces"] for row in scopes}
            for row in batch:
                sid = row["session_id"]
                if sid in owners:
                    await conn.execute("SELECT ksadk_repair_legacy_session($1, $2, $3)",
                                       sid, row["agent_instance_id"], owners[sid])
                # Never clear a newer old-writer notification, or forget live
                # work whose original Session evidence is not available yet.
                await conn.execute("""
                    DELETE FROM kernel_legacy_session_repairs WHERE tenant_id=$1 AND session_id=$2 AND agent_instance_id=$3 AND revision=$4
                      AND NOT EXISTS (SELECT FROM kernel_runs WHERE session_id=$2 AND tenant_id=$1 AND agent_instance_id=$3
                                      AND session_namespace IS NULL AND state IN ('pending','running','paused','waiting'))
                      AND NOT EXISTS (SELECT FROM kernel_inbox WHERE tenant_id=$1 AND session_id=$2 AND agent_instance_id=$3
                                      AND session_namespace IS NULL AND status IN ('accepted','claimed'))
                      AND NOT EXISTS (SELECT FROM kernel_interactions WHERE session_id=$2 AND tenant_id=$1 AND agent_instance_id=$3
                                      AND session_namespace IS NULL AND status IN ('pending','resolving'))
                """, row["tenant_id"], sid, row["agent_instance_id"], row["revision"])
            last = batch[-1]
            cursor = (last["tenant_id"], last["session_id"], last["agent_instance_id"])

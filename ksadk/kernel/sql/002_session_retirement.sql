-- NULL identifies legacy work. A Session id is only unique within its namespace.
ALTER TABLE kernel_activations ADD COLUMN IF NOT EXISTS session_namespace text;
ALTER TABLE kernel_runs ADD COLUMN IF NOT EXISTS session_namespace text;
ALTER TABLE kernel_inbox ADD COLUMN IF NOT EXISTS session_namespace text;
ALTER TABLE kernel_interactions ADD COLUMN IF NOT EXISTS session_namespace text;

-- The Session service can run without the Kernel. Installing this function in
-- the Kernel migration lets Session deletion participate only when both exist.
CREATE OR REPLACE FUNCTION ksadk_retire_kernel_session(
    ns text, tenant text, sid text, agent text, reason text
) RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE
    token bigint;
BEGIN
    -- Select the parent and exact work ids from the same MVCC snapshot.
    -- An admission for a newly recreated Session cannot enter this selection.
    WITH scope AS (
        SELECT NOT EXISTS (SELECT FROM ksadk_sessions
            WHERE id=sid AND namespace<>ns AND tenant_id=tenant) AS legacy
        WHERE reason='session_deleted' OR NOT EXISTS (
            SELECT FROM ksadk_sessions WHERE namespace=ns AND id=sid
        )
    ), open_runs AS (
        SELECT run_id, agent_instance_id FROM kernel_runs, scope
        WHERE session_id=sid AND (agent IS NULL OR agent_instance_id=agent)
          AND (session_namespace=ns OR (session_namespace IS NULL AND legacy AND tenant_id=tenant))
          AND state IN ('pending','running','paused','waiting')
    ), commands AS (
        SELECT message_id, agent_instance_id FROM kernel_inbox, scope
        WHERE session_id=sid AND (agent IS NULL OR agent_instance_id=agent)
          AND (session_namespace=ns OR (session_namespace IS NULL AND legacy AND tenant_id=tenant))
          AND status IN ('accepted','claimed')
    ), interactions AS (
        SELECT tenant_id, interaction_id, agent_instance_id FROM kernel_interactions, scope
        WHERE session_id=sid AND (agent IS NULL OR agent_instance_id=agent)
          AND (session_namespace=ns OR (session_namespace IS NULL AND legacy AND tenant_id=tenant))
          AND status IN ('pending','resolving')
    ), owners AS (
        SELECT agent_instance_id FROM open_runs UNION SELECT agent_instance_id FROM commands
        UNION SELECT agent_instance_id FROM interactions
    ), fenced AS (
        UPDATE kernel_activations a SET fencing_token=a.fencing_token+1,
            released=a.released OR reason='session_deleted' FROM scope
        WHERE a.session_id=sid AND (agent IS NULL OR a.agent_instance_id=agent)
          AND (a.session_namespace=ns OR (a.session_namespace IS NULL AND legacy))
          AND (a.agent_instance_id IN (SELECT agent_instance_id FROM owners)
               OR (reason='session_deleted' AND a.session_namespace=ns AND NOT a.released))
        RETURNING a.agent_instance_id, a.fencing_token
    ), retired AS (
        UPDATE kernel_runs r SET state='interrupted', updated_at=now(),
            metadata=r.metadata || jsonb_build_object('interruption_reason',reason),
            activation_fence=COALESCE((SELECT f.fencing_token FROM fenced f
                                      WHERE f.agent_instance_id=r.agent_instance_id), r.activation_fence)
        FROM open_runs o WHERE r.run_id=o.run_id AND r.state IN ('pending','running','paused','waiting')
    ), discarded AS (
        UPDATE kernel_inbox i SET status='discarded' FROM commands c
        WHERE i.message_id=c.message_id AND i.status IN ('accepted','claimed')
    ), cancelled AS (
        UPDATE kernel_interactions i SET status='cancelled', outcome='cancelled',
            revision=i.revision+1, updated_at=now(), event_id=NULL, accepted_seq=NULL
        FROM interactions p WHERE (i.tenant_id,i.interaction_id)=(p.tenant_id,p.interaction_id)
          AND i.status IN ('pending','resolving')
    )
    SELECT max(fencing_token) INTO token FROM fenced;
    -- Only the wait for this Kernel run is obsolete. Broker grants and
    -- credentials have an independent lifetime across sessions.
    IF to_regclass('mcp_authorization_waits') IS NOT NULL THEN
        UPDATE mcp_authorization_waits w SET status='cancelled' FROM kernel_runs r
        WHERE w.run_id=r.run_id AND w.session_id=sid AND r.session_id=sid
          AND w.instance_id=r.agent_instance_id AND (agent IS NULL OR w.instance_id=agent)
          AND w.status IN ('pending','resolving','unknown','consumed')
          AND (r.session_namespace=ns OR (r.session_namespace IS NULL AND r.tenant_id=tenant
               AND NOT EXISTS (SELECT FROM ksadk_sessions WHERE id=sid AND namespace<>ns AND tenant_id=tenant)))
          AND (reason='session_deleted' OR NOT EXISTS (SELECT FROM ksadk_sessions WHERE namespace=ns AND id=sid));
    END IF;
    RETURN token;
END;
$$;

CREATE OR REPLACE FUNCTION ksadk_after_kernel_session_delete()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM ksadk_retire_kernel_session(OLD.namespace, OLD.tenant_id, OLD.id, NULL, 'session_deleted');
    RETURN OLD;
END;
$$;

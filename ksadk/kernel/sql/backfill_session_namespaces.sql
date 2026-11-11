-- Both leading keys of the Session event indexes are known for these lookups.
-- A public session id may have several owners; bind activations only after
-- checking the committed facts in all of its namespaces.
CREATE FUNCTION ksadk_repair_legacy_session(sid text, agent text, namespaces text[])
RETURNS void LANGUAGE plpgsql AS $$
DECLARE ns text;
BEGIN
    FOREACH ns IN ARRAY namespaces LOOP
        UPDATE kernel_runs r SET session_namespace=ns
        WHERE r.session_namespace IS NULL AND r.session_id=sid AND r.agent_instance_id=agent
          AND r.state IN ('pending','running','paused','waiting')
          AND EXISTS (SELECT FROM ksadk_events e WHERE e.namespace=ns AND e.session_id=sid
                      AND e.tenant_id=r.tenant_id AND e.invocation_id=r.run_id
                      AND e.event_type='control.run_transition');

        UPDATE kernel_inbox i SET session_namespace=ns, accepted_seq=e.seq_id
        FROM ksadk_events e
        WHERE i.agent_instance_id=agent AND i.session_id=sid
          AND ((i.session_namespace IS NULL AND e.tenant_id=i.tenant_id)
               OR (i.session_namespace=ns AND i.accepted_seq<>e.seq_id))
          AND e.namespace=ns AND e.session_id=sid
          AND e.event_type='control.command_accepted'
          AND e.content_json #>> '{session_event,payload,message_id}'=i.message_id::text;

        UPDATE kernel_interactions i SET session_namespace=ns FROM ksadk_events e
        WHERE e.namespace=ns AND e.session_id=sid AND e.event_type='interaction.requested'
          AND i.tenant_id=e.content_json #>> '{session_event,payload,tenant_id}'
          AND i.interaction_id=e.content_json #>> '{session_event,payload,interaction_id}'
          AND i.agent_instance_id=agent AND i.session_id=sid AND i.session_namespace IS NULL AND i.run_id=e.invocation_id
          AND i.status IN ('pending','resolving');
    END LOOP;

    IF NOT EXISTS (SELECT FROM kernel_activations WHERE agent_instance_id=agent AND session_id=sid
                   AND session_namespace IS NULL) THEN
        RETURN;
    END IF;
    -- The Interaction ledger is indexed by (tenant_id, session_id, status).
    -- An Agent's ledger tenant can differ from the Session tenant. Step over
    -- distinct tenant keys in the PK rather than walking unrelated history
    -- for each legacy lease (including on PostgreSQL 9.6).
    WITH RECURSIVE ledger_tenants(tenant) AS (
        SELECT min(tenant_id) FROM kernel_interactions
        UNION ALL SELECT (SELECT min(tenant_id) FROM kernel_interactions WHERE tenant_id > t.tenant)
        FROM ledger_tenants t WHERE t.tenant IS NOT NULL
    ), work AS (
        SELECT session_namespace FROM kernel_runs WHERE session_id=sid AND agent_instance_id=agent
          AND state IN ('pending','running','paused','waiting')
        UNION ALL SELECT session_namespace FROM kernel_inbox WHERE agent_instance_id=agent AND session_id=sid
          AND status IN ('accepted','claimed')
        UNION ALL SELECT session_namespace FROM kernel_interactions
        WHERE tenant_id=ANY(ARRAY(SELECT tenant FROM ledger_tenants WHERE tenant IS NOT NULL))
          AND session_id=sid AND agent_instance_id=agent AND status IN ('pending','resolving')
    ), bound AS (
        SELECT min(session_namespace) AS ns FROM work HAVING count(DISTINCT session_namespace)=1
    )
    UPDATE kernel_activations a SET session_namespace=bound.ns FROM bound
    WHERE a.agent_instance_id=agent AND a.session_id=sid AND a.session_namespace IS NULL;
END;
$$;

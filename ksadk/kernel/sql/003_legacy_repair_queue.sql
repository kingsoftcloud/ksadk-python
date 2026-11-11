-- Old binaries omit session_namespace. Remember just the sessions they touch
-- so a restart need not search the entire Kernel history to find those writes.
CREATE TABLE kernel_legacy_session_repairs (
    tenant_id text NOT NULL, session_id text NOT NULL, agent_instance_id text NOT NULL,
    revision bigserial NOT NULL, PRIMARY KEY (tenant_id, session_id, agent_instance_id)
);
-- Capture only the upper primary key of each pre-existing table here.
-- Data scans happen later, in resumable pages after the DDL commits.
CREATE TABLE kernel_legacy_repair_progress (
    work_table text PRIMARY KEY, last_tenant text, last_id text, end_tenant text, end_id text NOT NULL
);
INSERT INTO kernel_legacy_repair_progress(work_table, end_id)
    SELECT 'kernel_runs', run_id FROM kernel_runs ORDER BY run_id DESC LIMIT 1;
INSERT INTO kernel_legacy_repair_progress(work_table, end_id)
    SELECT 'kernel_inbox', message_id::text FROM kernel_inbox ORDER BY kernel_inbox.message_id DESC LIMIT 1;
INSERT INTO kernel_legacy_repair_progress(work_table, end_tenant, end_id)
    SELECT 'kernel_interactions', tenant_id, interaction_id FROM kernel_interactions
    ORDER BY tenant_id DESC, interaction_id DESC LIMIT 1;

CREATE FUNCTION ksadk_queue_legacy_session_repair() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- A scoped Inbox writer seals accepted_seq after appending its event.
    -- A correct receipt is a single unique-index hit and needs no repair.
    IF NEW.session_namespace IS NOT NULL AND to_regclass('ksadk_events') IS NOT NULL THEN
        IF EXISTS (SELECT FROM ksadk_events WHERE namespace=NEW.session_namespace
                   AND session_id=NEW.session_id AND seq_id=NEW.accepted_seq
                   AND event_type='control.command_accepted'
                   AND content_json #>> '{session_event,payload,message_id}'=NEW.message_id::text) THEN
            RETURN NULL;
        END IF;
    END IF;
    INSERT INTO kernel_legacy_session_repairs(tenant_id, session_id, agent_instance_id)
    VALUES (NEW.tenant_id, NEW.session_id, NEW.agent_instance_id)
    ON CONFLICT (tenant_id, session_id, agent_instance_id) DO UPDATE SET revision=excluded.revision;
    RETURN NULL;
END;
$$;
CREATE TRIGGER ksadk_queue_legacy_inbox AFTER INSERT OR UPDATE ON kernel_inbox
    FOR EACH ROW WHEN (NEW.session_namespace IS NULL)
    EXECUTE PROCEDURE ksadk_queue_legacy_session_repair();
CREATE TRIGGER ksadk_queue_legacy_run AFTER INSERT OR UPDATE ON kernel_runs
    FOR EACH ROW WHEN (NEW.session_namespace IS NULL AND NEW.state IN ('pending','running','paused','waiting'))
    EXECUTE PROCEDURE ksadk_queue_legacy_session_repair();
CREATE TRIGGER ksadk_queue_legacy_interaction AFTER INSERT OR UPDATE ON kernel_interactions
    FOR EACH ROW WHEN (NEW.session_namespace IS NULL AND NEW.status IN ('pending','resolving'))
    EXECUTE PROCEDURE ksadk_queue_legacy_session_repair();
CREATE TRIGGER ksadk_queue_changed_receipt AFTER UPDATE OF accepted_seq ON kernel_inbox
    FOR EACH ROW WHEN (NEW.session_namespace IS NOT NULL AND OLD.accepted_seq <> NEW.accepted_seq)
    EXECUTE PROCEDURE ksadk_queue_legacy_session_repair();

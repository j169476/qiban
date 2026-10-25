-- A disposable change counter, not user content and not restored from backups.
CREATE TABLE maintenance_revision (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
    revision BIGINT NOT NULL DEFAULT 0
);
INSERT INTO maintenance_revision VALUES(TRUE, 0);

CREATE FUNCTION companion_data_changed() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
BEGIN
    UPDATE maintenance_revision SET revision=revision+1 WHERE singleton=TRUE;
    RETURN NULL;
END;
$$;

DO $$
DECLARE item TEXT;
BEGIN
    FOREACH item IN ARRAY ARRAY['content_records','content_history','content_audit','core_events',
        'core_actions','actions','events','governance_policies','governance_grants','governance_challenges',
        'governance_sessions','governance_audit','governance_provenance','actor_nodes','relationship_claims']
    LOOP
        EXECUTE format('CREATE TRIGGER maintenance_changed AFTER INSERT OR UPDATE OR DELETE ON %I '
            'FOR EACH STATEMENT EXECUTE FUNCTION companion_data_changed()', item);
    END LOOP;
END;
$$;

CREATE FUNCTION companion_state_changed() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
DECLARE identifier TEXT;
BEGIN
    identifier := CASE WHEN TG_OP='DELETE' THEN OLD.key ELSE NEW.key END;
    -- Heartbeats and maintenance bookkeeping must not cause new backups.
    IF identifier !~ '^(service-heartbeat$|service-backup$|service-inbox$|health:|watchdog:|supervisor:)' THEN
        UPDATE maintenance_revision SET revision=revision+1 WHERE singleton=TRUE;
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER maintenance_state_changed AFTER INSERT OR UPDATE OR DELETE ON state
    FOR EACH ROW EXECUTE FUNCTION companion_state_changed();

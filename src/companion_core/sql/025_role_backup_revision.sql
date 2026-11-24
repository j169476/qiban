-- Count each effective statement once, including a bulk write. Transition tables
-- contain only the rows actually affected by this operation of an upsert.
CREATE FUNCTION role_backup_changed() RETURNS trigger
LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
BEGIN
    IF NOT EXISTS (SELECT FROM changed_rows) THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF NOT EXISTS (SELECT * FROM changed_rows EXCEPT ALL SELECT * FROM previous_rows) THEN
            RETURN NULL;
        END IF;
    END IF;
    UPDATE maintenance_revision SET revision = revision + 1 WHERE singleton;
    RETURN NULL;
END;
$$;

DO $$
DECLARE item text;
BEGIN
    FOREACH item IN ARRAY ARRAY[
        'role_reminders', 'role_matters', 'matter_contacts', 'matter_watches', 'role_activity',
        'privacy_jobs', 'privacy_slices', 'privacy_dependencies', 'privacy_review_sources', 'privacy_corrections'
    ] LOOP
        EXECUTE format('CREATE TRIGGER role_backup_insert AFTER INSERT ON %I '
                       'REFERENCING NEW TABLE AS changed_rows '
                       'FOR EACH STATEMENT EXECUTE FUNCTION role_backup_changed()', item);
        EXECUTE format('CREATE TRIGGER role_backup_update AFTER UPDATE ON %I '
                       'REFERENCING OLD TABLE AS previous_rows NEW TABLE AS changed_rows '
                       'FOR EACH STATEMENT EXECUTE FUNCTION role_backup_changed()', item);
        EXECUTE format('CREATE TRIGGER role_backup_delete AFTER DELETE ON %I '
                       'REFERENCING OLD TABLE AS changed_rows '
                       'FOR EACH STATEMENT EXECUTE FUNCTION role_backup_changed()', item);
    END LOOP;
END;
$$;

-- The last v24 backup may already be stale despite an equal saved revision.
UPDATE maintenance_revision SET revision = revision + 1 WHERE singleton;

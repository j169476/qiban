-- Derived lookup only; original ledger payloads stay encrypted.
CREATE TABLE source_retention (
 owner_kind TEXT NOT NULL CHECK(owner_kind IN ('state','core_action')),
 owner_id TEXT NOT NULL, resource TEXT NOT NULL,
 PRIMARY KEY(owner_kind,owner_id,resource)
);
CREATE INDEX source_retention_resource ON source_retention(resource);
CREATE INDEX governance_audit_resource ON governance_audit(resource);
CREATE FUNCTION remove_source_retention() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_TABLE_NAME = 'state' THEN
  DELETE FROM source_retention WHERE owner_kind='state' AND owner_id=OLD.key;
 ELSE
  DELETE FROM source_retention WHERE owner_kind='core_action' AND owner_id=OLD.id;
 END IF;
 RETURN OLD;
END;
$$;
CREATE TRIGGER state_source_retention_delete AFTER DELETE ON state
 FOR EACH ROW EXECUTE FUNCTION remove_source_retention();
CREATE TRIGGER action_source_retention_delete AFTER DELETE ON core_actions
 FOR EACH ROW EXECUTE FUNCTION remove_source_retention();

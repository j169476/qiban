-- Routing projections contain no message, draft, or profile body. They are
-- committed atomically with their encrypted state receipts before LIMIT.
ALTER TABLE state DROP CONSTRAINT state_value_check;
ALTER TABLE state ADD CONSTRAINT state_encrypted CHECK(left(value,5)='enc1:');
ALTER TABLE events ADD CONSTRAINT events_encrypted CHECK(left(body,5)='enc1:');
ALTER TABLE core_events ADD CONSTRAINT core_events_encrypted CHECK(left(body,5)='enc1:');
ALTER TABLE actions ADD CONSTRAINT actions_payload_encrypted CHECK(left(payload,5)='enc1:');
ALTER TABLE actions ADD CONSTRAINT actions_result_encrypted CHECK(result IS NULL OR left(result,5)='enc1:');
ALTER TABLE core_actions ADD CONSTRAINT core_actions_body_encrypted CHECK(left(body,5)='enc1:');
ALTER TABLE core_actions ADD CONSTRAINT core_actions_result_encrypted CHECK(result IS NULL OR left(result,5)='enc1:');
CREATE TABLE core_event_receipts (
    event_id TEXT PRIMARY KEY,
    version TEXT NOT NULL
);
CREATE TABLE core_delivery_state (
    event_id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    owner TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('completed','deferred')),
    due_at TIMESTAMPTZ,
    config_token TEXT
);
CREATE INDEX core_delivery_due ON core_delivery_state(status,due_at);
